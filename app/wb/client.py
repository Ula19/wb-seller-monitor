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
from app.wb.parser import NormProduct, _delivery, _price, normalize

log = logging.getLogger(__name__)

IMPERSONATE = "chrome"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}

CATALOG_URL = "https://catalog.wb.ru/sellers/v4/catalog"
SUPPLIER_INFO_URL = "https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-id/{}.json"
# slug -> supplier_id: страница /seller/<slug> — SPA, ID отдаёт «конструктор магазинов»
SHOP_BY_SLUG_URL = "https://static-basket-01.wbcontent.net/vol0/constructor-api/shops/v3/{}.json"

# B2B-цены: detail через прокси основного домена (card.wb.ru напрямую даёт 403).
B2B_DETAIL_URL = "https://www.wildberries.ru/__internal/card/cards/v4/detail"
B2B_PARAMS = {
    "appType": 1, "curr": "rub", "dest": -446112, "spp": 30,
    "hide_vflags": 4294967296, "hide_dflags": 131072, "hide_dtype": "11;13;14;15",
    "b2b": "true", "mdg": 3, "mtype": 257, "lang": "ru", "ab_testing": "false",
}


def _parse_cookie(raw: str) -> dict[str, str]:
    """Строку 'k=v; k2=v2' из браузера превращаем в словарь для curl_cffi."""
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _parse_proxies(raw: str) -> list[str]:
    """'socks5h://h:p, http://h2:p2' -> список прокси. Пусто = без прокси (прямое соединение)."""
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


class WBClient:
    def __init__(self) -> None:
        self._cookies = _parse_cookie(settings.wb_cookie) if settings.wb_cookie else None
        if self._cookies:
            log.info("WB-клиент: использую куку аккаунта (%d полей)", len(self._cookies))
        self._proxies = _parse_proxies(settings.wb_proxies)
        self._proxy_idx = 0
        if self._proxies:
            log.info("WB-клиент: прокси %d шт., текущий %s", len(self._proxies), self._current_proxy())
        self._session = self._build_session()
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.b2b_fail_streak = 0  # подряд провалов b2b (0 цен при наличии товаров)
        self.cookie_alerted = False  # уже предупредили владельца о протухшей куке

    def _current_proxy(self) -> str | None:
        return self._proxies[self._proxy_idx] if self._proxies else None

    def _build_session(self) -> AsyncSession:
        proxy = self._current_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return AsyncSession(
            headers=HEADERS, cookies=self._cookies, impersonate=IMPERSONATE,
            timeout=20, proxies=proxies,
        )

    async def _rotate_proxy(self) -> bool:
        """На 403 переключаемся на следующий прокси из списка. False — переключать некуда."""
        if len(self._proxies) < 2:
            return False
        self._proxy_idx = (self._proxy_idx + 1) % len(self._proxies)
        old = self._session
        # ponytail: monitoring почти последователен (2 джоба, max_instances=1) — гонкой
        # за self._session пренебрегаем; если станет проблемой — лок вокруг свопа.
        self._session = self._build_session()
        try:
            await old.close()
        except Exception:
            pass
        log.warning("WB: переключаюсь на прокси %s", self._current_proxy())
        return True

    async def set_cookie(self, raw: str) -> int:
        """Заменяет куку и пересоздаёт сессию без рестарта. Возвращает число полей."""
        self._cookies = _parse_cookie(raw) if raw else None
        old = self._session
        self._session = self._build_session()
        self.b2b_fail_streak = 0
        self.cookie_alerted = False
        try:
            await old.close()
        except Exception:
            pass
        log.info("WB-клиент: кука обновлена (%d полей)", len(self._cookies or {}))
        return len(self._cookies or {})

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

    async def _get(self, url, *, params=None, headers=None, retries=4):
        for attempt in range(retries):
            await self._throttle()
            try:
                r = await self._session.get(url, params=params, headers=headers)
            except Exception as e:
                log.warning("WB сетевая ошибка %s: %s", url, e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # WAF/бан по IP. Если есть запасные прокси — пробуем следующий и ретраим;
                # иначе отступаем сразу (ретраи на том же IP бесполезны).
                if await self._rotate_proxy():
                    log.warning("WB %s -> 403, пробую следующий прокси", url)
                    continue
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

    async def fetch_seller_catalog(
        self, supplier_id: int, b2b: bool = True, subjects: set[int] | None = None
    ) -> list[NormProduct]:
        """Все товары продавца: листаем страницы пока есть данные.

        Цену каталога (обезличенный SPP) заменяем на персональную из detail с кукой:
        b2b=True — бизнес-цена, b2b=False — личная розничная (как на сайте под аккаунтом).
        subjects — непустой набор оставляет только товары этих предметов (категорий WB);
        фильтруем до detail, чтобы не дёргать цены на лишнее.
        """
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
        if subjects:
            products = [p for p in products if p.subject_id in subjects]
        if products:
            await self._apply_detail_prices(products, b2b)
        return products

    async def _apply_detail_prices(self, products: list[NormProduct], b2b: bool) -> None:
        """Заменяет цену каталога на персональную из detail (с кукой), батчи по 100.

        b2b=True — бизнес-цена; b2b=False — личная розница (то же поле product, но без
        флага b2b). Если detail ничего не вернул — оставляем цену каталога (graceful).
        """
        ref = {"Referer": "https://www.wildberries.ru/"}
        base = {**B2B_PARAMS}
        if not b2b:
            base.pop("b2b", None)  # розница: тот же detail, но без бизнес-флага
        prices: dict[int, int] = {}
        deliv: dict[int, tuple] = {}
        nm_ids = [p.nm_id for p in products]
        for i in range(0, len(nm_ids), 100):
            chunk = nm_ids[i:i + 100]
            params = {**base, "nm": ";".join(map(str, chunk))}
            r = await self._get(B2B_DETAIL_URL, params=params, headers=ref)
            if r is None or r.status_code != 200:
                continue
            try:
                items = r.json().get("products") or []
            except Exception:
                continue
            for p in items:
                val = _price(p)
                if val is not None:
                    prices[int(p["id"])] = val
                deliv[int(p["id"])] = _delivery(p)
        for p in products:
            if p.nm_id in prices:
                p.price = prices[p.nm_id]
            if p.nm_id in deliv:
                p.delivery_hours, p.from_seller = deliv[p.nm_id]
        # стрик/алерт о протухшей куке считаем только в бизнес-режиме: для розницы
        # «0 персональных цен» может значить просто нет личной скидки, а не протухшую куку.
        if b2b:
            if products and not prices:
                self.b2b_fail_streak += 1
            else:
                self.b2b_fail_streak = 0
        mode = "b2b" if b2b else "розница"
        log.info("detail цены применены (%s): %d/%d", mode, len(prices), len(products))

    async def resolve_seller_slug(self, slug: str) -> int | None:
        """supplier_id по slug-ссылке /seller/<slug> (для SEO-адресов без числа)."""
        r = await self._get(SHOP_BY_SLUG_URL.format(slug))
        if r and r.status_code == 200:
            try:
                return int(r.json()["supplierID"])
            except Exception:
                return None
        return None

    async def fetch_supplier_info(self, supplier_id: int) -> dict | None:
        r = await self._get(SUPPLIER_INFO_URL.format(supplier_id))
        if r and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None


# единый экземпляр на всё приложение (общий троттлинг для планировщика и хендлеров)
wb_client = WBClient()
