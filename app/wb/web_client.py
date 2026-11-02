"""Веб-путь к WB (www/__internal) БЕЗ guardium — куки даёт keeper (web_session).

Два раздельных запроса, как и в мобильном api-ios:
- розница: каталог продавца АНОНИМНО (только антибот-куки) — список, сток, розничная цена;
- бизнес: detail с куками БИЗНЕС-аккаунта + b2b=true — бизнес-цена (только b2b-магазины).
Ходим curl_cffi через выделенный прокси (куки привязаны к IP минтившего их браузера).
Основной путь бота; при сбое client.py откатывается на мобильный api-ios.
"""
import asyncio
import logging
import random

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, normalize
from app.wb.web_session import cookie_header, curl_proxy, is_logged, web_proxy

log = logging.getLogger(__name__)

CATALOG_URL = "https://www.wildberries.ru/__internal/u-catalog/sellers/v4/catalog"
DETAIL_URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
# розница: параметры сайта для анонима — без udg/wh_prices (это бизнес-контекст)
CATALOG_PARAMS = {
    "ab_testing": "false", "appType": 1, "curr": "rub", "dest": settings.wb_web_dest,
    "spp": 30, "hide_vflags": 4294967296, "lang": "ru",
}
# бизнес: detail как в мобильном бизнес-режиме (b2b=true + mdg=3 + udg), appType веба
B2B_PARAMS = {
    "ab_testing": "false", "appType": 1, "curr": "rub", "dest": settings.wb_web_dest,
    "spp": 30, "lang": "ru", "b2b": "true", "mdg": 3, "udg": settings.wb_b2b_udg,
}
DETAIL_CHUNK = 100
SMARTPHONE_SUBJECT_ID = 515
IMPERSONATE = "safari18_0"


def _items(r) -> list:
    data = r.json()
    items = data.get("products") or (data.get("data") or {}).get("products") or []
    if not isinstance(items, list):
        raise ValueError("products должен быть списком")
    return items


class WebClient:
    def __init__(self) -> None:
        self._cookie_anon = ""     # только антибот-куки — розничный каталог
        self._cookie_account = ""  # полная банка бизнес-аккаунта — бизнес-detail
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._session: AsyncSession | None = None

    @property
    def ready(self) -> bool:
        """Есть куки для розничного каталога."""
        return bool(self._cookie_anon)

    @property
    def account_ready(self) -> bool:
        """Есть залогиненный бизнес-аккаунт для бизнес-цен."""
        return bool(self._cookie_account)

    def set_session(self, storage_state) -> None:
        """Обновить банку кук (от keeper). Пустая строка — сессии нет."""
        self._cookie_anon = cookie_header(storage_state, anonymous=True)
        self._cookie_account = cookie_header(storage_state) if is_logged(storage_state) else ""
        log.info("веб-клиент: куки %s (аккаунт %s)",
                 "обновлены" if self._cookie_anon else "очищены",
                 "есть" if self._cookie_account else "нет")

    def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            proxy = curl_proxy(web_proxy())
            proxies = {"http": proxy, "https": proxy} if proxy else None
            self._session = AsyncSession(
                impersonate=IMPERSONATE, timeout=20, proxies=proxies,
                headers={"Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9"},
                # не копим куки ответов: иначе куки аккаунта из бизнес-detail утекли бы
                # в анонимный каталог и розница снова получила бы бизнес-цену.
                # Банку кук ведёт keeper (refresh_job).
                discard_cookies=True,
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

    async def _get_items(self, what: str, url: str, params: dict, cookie: str) -> list | None:
        """Один запрос → список products. None — сеть/не 200/битый JSON."""
        await self._throttle()
        try:
            r = await self._ensure_session().get(url, params=params, headers={"Cookie": cookie})
        except Exception as e:
            log.warning("%s: сеть %s", what, type(e).__name__)
            return None
        if r.status_code != 200:
            # 498 = антибот-кука протухла, 401/403 = сессия/доступ отклонены
            log.warning("%s: HTTP %s", what, r.status_code)
            return None
        try:
            return _items(r)
        except Exception:
            log.warning("%s: неверный JSON", what)
            return None

    async def fetch_catalog(
        self, supplier_id: int, subjects: set[int] | None = None
    ) -> list[NormProduct] | None:
        """Розничный каталог магазина (анонимно): список, сток, розничная цена.

        None — веб недоступен/ошибка. [] — WB не вернул товаров по фильтру.
        """
        if not self._cookie_anon:
            return None
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        products: dict[int, NormProduct] = {}  # nm_id -> товар, дедуп между страницами
        for page in range(1, settings.max_pages + 1):
            params = {**CATALOG_PARAMS, "supplier": supplier_id, "page": page,
                      "xsubject": ";".join(map(str, sorted(subjects)))}
            items = await self._get_items(f"веб-каталог {supplier_id} стр.{page}",
                                          CATALOG_URL, params, self._cookie_anon)
            if items is None:
                return None  # ошибка на любой странице → не отдаём недолистанный хвост
            if not items:
                break  # страниц больше нет
            before = len(products)
            for p in items:
                if p.get("subjectId") in subjects:
                    products[int(p["id"])] = normalize(p, supplier_id)
            # защита от зацикливания: WB проигнорил page и вернул те же товары
            if len(products) == before:
                break
        return list(products.values())

    async def fetch_b2b_items(self, nm_ids: list[int]) -> list | None:
        """Бизнес-цены: сырые товары detail под бизнес-аккаунтом (пачками по 100).

        None — аккаунта нет или любой запрос упал (client.py уйдёт на мобильный Bearer).
        """
        if not self._cookie_account:
            return None
        items: list = []
        for i in range(0, len(nm_ids), DETAIL_CHUNK):
            chunk = nm_ids[i:i + DETAIL_CHUNK]
            params = {**B2B_PARAMS, "nm": ";".join(map(str, chunk))}
            got = await self._get_items(f"веб-бизнес-detail ({len(chunk)} шт.)",
                                        DETAIL_URL, params, self._cookie_account)
            if got is None:
                return None
            items.extend(got)
        return items


web_client = WebClient()
