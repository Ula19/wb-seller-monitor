"""Веб-путь к WB: каталог и цены через www/__internal с куками залогиненного
бизнес-аккаунта (БЕЗ guardium). Куки даёт keeper (web_session), ходим curl_cffi
через выделенный прокси (куки привязаны к IP минтившего их браузера).

Основной путь бота; при сбое client.py откатывается на мобильный api-ios+guardium.
"""
import asyncio
import logging
import random
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, normalize
from app.wb.web_session import cookie_header

log = logging.getLogger(__name__)

CATALOG_URL = "https://www.wildberries.ru/__internal/u-catalog/sellers/v4/catalog"
# параметры сняты с реального трафика сайта (dump_web_urls): appType=1 (веб),
# dest веб-региона, udg/wh_prices — бизнес-контекст (цена аккаунта, не анонима).
BASE_PARAMS = {
    "ab_testing": "false", "appType": 1, "curr": "rub", "dest": settings.wb_web_dest,
    "spp": 30, "hide_vflags": 4294967296, "lang": "ru", "udg": 82, "wh_prices": 1,
}
SMARTPHONE_SUBJECT_ID = 515
IMPERSONATE = "safari18_0"


def _web_proxy() -> str | None:
    if settings.wb_web_proxy.strip():
        return settings.wb_web_proxy.strip()
    first = [x.strip() for x in settings.wb_proxies.split(",") if x.strip()]
    return first[0] if first else None


def _curl_proxy(url: str | None) -> str | None:
    """curl_cffi к socks5-прокси требует socks5h (remote DNS), иначе ошибка 97."""
    if url and url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


class WebClient:
    def __init__(self) -> None:
        self._cookie = ""
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._session: AsyncSession | None = None

    @property
    def ready(self) -> bool:
        return bool(self._cookie)

    def set_session(self, storage_state) -> None:
        """Обновить банку кук (от keeper). Пустая строка — сессии нет."""
        self._cookie = cookie_header(storage_state)
        log.info("веб-клиент: куки %s", "обновлены" if self._cookie else "очищены")

    def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            proxy = _curl_proxy(_web_proxy())
            proxies = {"http": proxy, "https": proxy} if proxy else None
            self._session = AsyncSession(
                impersonate=IMPERSONATE, timeout=20, proxies=proxies,
                headers={"Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9"},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def _throttle(self) -> None:
        """Веб-антибот терпит ~2 req/s; держим паузу, чтобы не ловить бан."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            wait = 0.5 + random.uniform(0, 0.4) - (loop.time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()

    async def fetch_catalog(
        self, supplier_id: int, subjects: set[int] | None = None
    ) -> list[NormProduct] | None:
        """Каталог магазина с ценами залогиненного аккаунта (бизнес-цена).

        None — веб недоступен/ошибка (client.py уйдёт на мобильный fallback).
        Пустой список — магазин без товаров по фильтру (не ошибка).
        """
        if not self._cookie:
            return None
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        session = self._ensure_session()
        headers = {"Cookie": self._cookie}
        products: dict[int, NormProduct] = {}  # nm_id -> товар, дедуп между страницами
        for page in range(1, settings.max_pages + 1):
            params = {**BASE_PARAMS, "supplier": supplier_id, "page": page,
                      "xsubject": ";".join(map(str, sorted(subjects)))}
            await self._throttle()
            try:
                r = await session.get(CATALOG_URL, params=params, headers=headers)
            except Exception as e:
                log.warning("веб-каталог %s: сеть %s", supplier_id, type(e).__name__)
                return None
            if r.status_code != 200:
                log.warning("веб-каталог %s: HTTP %s (401/403 = сессия отклонена)", supplier_id, r.status_code)
                return None
            try:
                data = r.json()
                items = data.get("products") or (data.get("data") or {}).get("products") or []
            except Exception:
                log.warning("веб-каталог %s: неверный JSON", supplier_id)
                return None
            if not items:
                break  # страниц больше нет
            before = len(products)
            for p in items:
                if p.get("subjectId") in subjects:
                    products[int(p["id"])] = normalize(p, supplier_id)
            # защита от зацикливания: WB проигнорил page и вернул те же товары
            if len(products) == before:
                break
        # пусто = либо магазин без смартфонов, либо неверные параметры запроса: не рискуем
        # тихо обнулить каталог — отдаём None, пусть проверит мобильный fallback.
        if not products:
            log.info("веб-каталог %s: пусто → fallback на мобильный", supplier_id)
            return None
        return list(products.values())


web_client = WebClient()
