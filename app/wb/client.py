"""HTTP-клиент к внутреннему API WB с троттлингом и ретраями.

ВАЖНО: WB режет запросы по TLS-фингерпринту (WAF "Angie"), поэтому обычные
requests/httpx/aiohttp получают 403 независимо от заголовков. Используем
curl_cffi с имперсонацией Chrome — он подделывает TLS/JA3 настоящего браузера.
"""

import asyncio
import logging
import random

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, basket_host, normalize, photo_url

log = logging.getLogger(__name__)

IMPERSONATE = "chrome"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}

CATALOG_URL = "https://catalog.wb.ru/sellers/v4/catalog"
SUPPLIER_INFO_URL = "https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-id/{}.json"


def _parse_cookie(raw: str) -> dict[str, str]:
    """Строку 'k=v; k2=v2' из браузера превращаем в словарь для curl_cffi."""
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class WBClient:
    def __init__(self) -> None:
        cookies = _parse_cookie(settings.wb_cookie) if settings.wb_cookie else None
        if cookies:
            log.info("WB-клиент: использую куку аккаунта (%d полей)", len(cookies))
        self._session = AsyncSession(
            headers=HEADERS, cookies=cookies, impersonate=IMPERSONATE, timeout=20
        )
        self._lock = asyncio.Lock()
        self._last = 0.0
        # кэш vol -> найденный basket-хост (границы плывут)
        self._basket_cache: dict[int, int] = {}

    async def close(self) -> None:
        await self._session.close()

    async def _throttle(self) -> None:
        """Не чаще одного запроса раз в request_min_delay + jitter секунд."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            wait = (
                settings.request_min_delay
                + random.uniform(0, settings.request_jitter)
                - (loop.time() - self._last)
            )
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()

    async def _get(self, url, *, params=None, retries=4):
        for attempt in range(retries):
            await self._throttle()
            try:
                r = await self._session.get(url, params=params)
            except Exception as e:
                log.warning("WB сетевая ошибка %s: %s", url, e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # WAF/бан по IP: ретраи бесполезны и только продлевают бан.
                # Отступаем сразу — пусть следующий цикл попробует позже.
                log.warning("WB %s -> 403 (WAF/бан), пропуск без ретраев", url)
                return None
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("X-Ratelimit-Retry") or r.headers.get(
                    "Retry-After"
                )
                delay = (
                    float(retry_after)
                    if retry_after and str(retry_after).isdigit()
                    else (2**attempt) + random.uniform(0, 1)
                )
                log.warning("WB %s -> %s, пауза %.1fс", url, r.status_code, delay)
                await asyncio.sleep(delay)
                continue
            # 404 и прочее — возвращаем как есть (пагинация/перебор хостов разберутся)
            return r
        return None

    async def fetch_seller_catalog(self, supplier_id: int) -> list[NormProduct]:
        """Все товары продавца: листаем страницы пока есть данные."""
        products: list[NormProduct] = []
        for page in range(1, settings.max_pages + 1):
            params = {
                "appType": 1,
                "curr": "rub",
                "dest": settings.wb_dest,
                "sort": "popular",
                "spp": settings.wb_spp,
                "supplier": supplier_id,
                "page": page,
            }
            r = await self._get(CATALOG_URL, params=params)
            if r is None or r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            items = data.get("products") or (data.get("data") or {}).get("products") or []
            if not items:
                break
            if settings.debug_raw and page == 1:
                import json
                log.info("RAW первый товар: %s", json.dumps(items[0], ensure_ascii=False))
            products.extend(normalize(p, supplier_id) for p in items)
            if len(items) < 100:
                break
        return products

    async def fetch_supplier_info(self, supplier_id: int) -> dict | None:
        r = await self._get(SUPPLIER_INFO_URL.format(supplier_id))
        if r and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None

    async def download_photo(self, nm_id: int, pics: int = 1) -> bytes | None:
        """Скачивает первое фото. При 404 перебирает соседние basket-хосты."""
        if pics < 1:
            return None
        vol = nm_id // 100000
        primary = self._basket_cache.get(vol, basket_host(nm_id))
        # перебираем хосты по близости к догадке (границы диапазонов плывут)
        candidates = sorted(range(1, 31), key=lambda h: (abs(h - primary), h))
        for h in candidates[:14]:
            r = await self._get(photo_url(nm_id, host=h))
            if r and r.status_code == 200 and r.content:
                self._basket_cache[vol] = h
                return r.content
        return None


# единый экземпляр на всё приложение (общий троттлинг для планировщика и хендлеров)
wb_client = WBClient()
