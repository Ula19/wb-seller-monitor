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

# b2b-эндпоинт (__internal) пускает только «настоящий» браузерный отпечаток.
# Safari 18 — подтверждённо рабочий (chrome отдаёт 403 на __internal).
IMPERSONATE = "safari18_0"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}

CATALOG_URL = "https://catalog.wb.ru/sellers/v4/catalog"
SMARTPHONE_SUBJECT_ID = 515  # WB-предмет «Смартфоны» — по умолчанию мониторим только их
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
# Розница detail НЕ нужна: каталог без куки уже даёт точную розничную цену (СПП у WB
# стандартная, не персональная — аноним == аккаунт, проверено 4/4). enrich только для b2b.


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
        # проксированная сессия — только для detail (__internal); каталог/CDN ходят напрямую
        # (публичны, работают с любого IP), чтобы флаки-прокси не ронял каждый цикл.
        self._session = self._make_session(self._current_proxy())
        self._direct_session = self._make_session(None)
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.b2b_fail_streak = 0  # подряд провалов b2b (0 цен при наличии товаров)
        self.cookie_alerted = False  # уже предупредили владельца о протухшей куке

    def _current_proxy(self) -> str | None:
        return self._proxies[self._proxy_idx] if self._proxies else None

    def _make_session(self, proxy: str | None) -> AsyncSession:
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
        # ротация касается только проксированной сессии; direct-сессия каталога неизменна.
        self._session = self._make_session(self._current_proxy())
        try:
            await old.close()
        except Exception:
            pass
        log.warning("WB: переключаюсь на прокси %s", self._current_proxy())
        return True

    async def set_cookie(self, raw: str) -> int:
        """Заменяет куку и пересоздаёт сессию без рестарта. Возвращает число полей."""
        self._cookies = _parse_cookie(raw) if raw else None
        old = (self._session, self._direct_session)
        self._session = self._make_session(self._current_proxy())
        self._direct_session = self._make_session(None)
        self.b2b_fail_streak = 0
        self.cookie_alerted = False
        for s in old:
            try:
                await s.close()
            except Exception:
                pass
        log.info("WB-клиент: кука обновлена (%d полей)", len(self._cookies or {}))
        return len(self._cookies or {})

    async def close(self) -> None:
        for s in (self._session, self._direct_session):
            try:
                await s.close()
            except Exception:
                pass

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

    async def _get(self, url, *, params=None, headers=None, retries=4, session=None):
        sess = session or self._session
        proxied = session is None  # ротация прокси имеет смысл только для проксированной сессии
        for attempt in range(retries):
            await self._throttle()
            try:
                r = await sess.get(url, params=params, headers=headers)
            except Exception as e:
                log.warning("WB сетевая ошибка %s: %s", url, e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # WAF/бан по IP. Если есть запасные прокси — пробуем следующий и ретраим;
                # иначе отступаем сразу (ретраи на том же IP бесполезны).
                if proxied and await self._rotate_proxy():
                    sess = self._session  # переключились на новый прокси
                    log.warning("WB %s -> 403, пробую следующий прокси", url)
                    continue
                log.warning("WB %s -> 403 (WAF/бан), пропуск без ретраев", url)
                return r  # отдаём 403, а не None: None теперь значит только «сеть легла»
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
        self, supplier_id: int, subjects: set[int] | None = None
    ) -> list[NormProduct]:
        """Каталог продавца БЕЗ куки: цена = витрина (`shelf_price`), дешёвый триггер.

        Фильтр по предмету — на стороне WB (`xsubject`), поэтому у крупных продавцов не
        листаем тысячи лишних товаров (ХОБОТ: 1 страница вместо 57). Точную «нашу» цену
        навешивает `enrich_prices` по триггеру/sweep'у отдельно. subjects — какие
        предметы оставить (по умолчанию — смартфоны).
        """
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
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
            if subjects:  # WB фильтрует по предмету на сервере — тянем только нужное
                params["xsubject"] = ";".join(map(str, sorted(subjects)))
            r = await self._get(CATALOG_URL, params=params, session=self._direct_session)
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
        if subjects:  # страховка, если WB проигнорит xsubject
            products = [p for p in products if p.subject_id in subjects]
        return products

    async def enrich_prices(self, products: list[NormProduct]) -> set[int]:
        """Навешивает БИЗНЕС-цену из detail (батчи по 100) поверх каталожной.

        Только для b2b-магазинов: бизнес-цена (B2B_PARAMS) видна лишь с кукой и реально
        отличается от розницы. Рознице detail не нужен — каталог уже даёт её цену.
        WBAAS пускает __internal только на браузерный набор заголовков (без них — 403,
        даже с валидной кукой). IP не проверяет. Зовётся по триггеру/в sweep — не каждый
        цикл, чтобы не держать куку на критпути.
        """
        base_params = B2B_PARAMS
        prices: dict[int, int] = {}
        deliv: dict[int, tuple] = {}
        saw_response = False  # получили ли хоть один HTTP-ответ (не сетевой обрыв)
        nm_ids = [p.nm_id for p in products]
        for i in range(0, len(nm_ids), 100):
            chunk = nm_ids[i:i + 100]
            params = {**base_params, "nm": ";".join(map(str, chunk))}
            headers = {
                "Accept": "*/*",
                "Referer": f"https://www.wildberries.ru/catalog/{chunk[0]}/detail.aspx",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "x-requested-with": "XMLHttpRequest",
                "deviceid": settings.wb_device_id,
                "x-spa-version": settings.wb_spa_version,
            }
            r = await self._get(B2B_DETAIL_URL, params=params, headers=headers)
            if r is None:
                continue  # сеть легла (прокси оборвал) — не признак протухшей куки
            saw_response = True
            if r.status_code != 200:
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
        # 0 цен при наличии товаров = кука протухла — НО только если WB реально ответил.
        # Если все чанки легли по сети (прокси оборвал), это не про куку — не копим счётчик,
        # иначе транзиентные обрывы мобильного прокси дают ложный алерт «кука протухла».
        if products and not prices:
            if saw_response:
                self.b2b_fail_streak += 1
            else:
                log.warning("b2b: все запросы легли по сети (прокси), счётчик куки не трогаю")
        else:
            self.b2b_fail_streak = 0
        log.info("detail цены применены: %d/%d", len(prices), len(products))
        return set(prices)  # nm с реально полученной ценой — остальным monitor сохранит прежнюю

    async def resolve_seller_slug(self, slug: str) -> int | None:
        """supplier_id по slug-ссылке /seller/<slug> (для SEO-адресов без числа)."""
        r = await self._get(SHOP_BY_SLUG_URL.format(slug), session=self._direct_session)
        if r and r.status_code == 200:
            try:
                return int(r.json()["supplierID"])
            except Exception:
                return None
        return None

    async def fetch_supplier_info(self, supplier_id: int) -> dict | None:
        r = await self._get(SUPPLIER_INFO_URL.format(supplier_id), session=self._direct_session)
        if r and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None


# единый экземпляр на всё приложение (общий троттлинг для планировщика и хендлеров)
wb_client = WBClient()


