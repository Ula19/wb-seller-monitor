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


class _Slot:
    """Канал к WB: своя сессия (свой IP/прокси) + СВОЙ троттлинг.

    Троттлинг пер-слот, а не общий, — иначе параллельный проход по слотам выродится
    в очередь на едином локе. proxy=None — прямой IP сервера.
    """
    __slots__ = ("session", "proxy", "lock", "last")

    def __init__(self, session: AsyncSession, proxy: str | None) -> None:
        self.session = session
        self.proxy = proxy
        self.lock = asyncio.Lock()
        self.last = 0.0


class WBClient:
    def __init__(self) -> None:
        self._cookies = _parse_cookie(settings.wb_cookie) if settings.wb_cookie else None
        if self._cookies:
            log.info("WB-клиент: использую куку аккаунта (%d полей)", len(self._cookies))
        self._proxies = _parse_proxies(settings.wb_proxies)
        # Пул слотов: прямой IP + по слоту на каждый прокси. Каталог (розница) раскидываем
        # по слотам параллельно; enrich (b2b) ходит через прокси-слоты. Свой троттлинг у
        # каждого → реальный минутный цикл, а 429 на одном IP не валит остальные.
        self._slots = self._make_slots()
        self._enrich_rr = 0  # round-robin по прокси-слотам для b2b enrich
        log.info("WB-клиент: слотов %d (прямой + прокси %d)", len(self._slots), len(self._proxies))
        self.b2b_fail_streak = 0  # подряд провалов b2b (0 цен при наличии товаров)
        self.cookie_alerted = False  # уже предупредили владельца о протухшей куке

    def _make_slots(self) -> list[_Slot]:
        slots = [_Slot(self._make_session(None), None)]  # прямой IP сервера
        slots += [_Slot(self._make_session(px), px) for px in self._proxies]
        return slots

    @property
    def slots(self) -> list[_Slot]:
        return self._slots

    @property
    def _direct_slot(self) -> _Slot:
        return self._slots[0]

    def _proxy_slots(self) -> list[_Slot]:
        return self._slots[1:] or self._slots  # нет прокси → падаем на прямой

    def _make_session(self, proxy: str | None) -> AsyncSession:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return AsyncSession(
            headers=HEADERS, cookies=self._cookies, impersonate=IMPERSONATE,
            timeout=20, proxies=proxies,
        )

    async def set_cookie(self, raw: str) -> int:
        """Заменяет куку и пересоздаёт сессии слотов без рестарта. Возвращает число полей.

        ВАЖНО: мутируем session В СУЩЕСТВУЮЩИХ _Slot, а не пересоздаём слоты —
        активный проход держит снапшот слот-объектов, и подмена списка оставила бы
        его на закрытых сессиях (ретраи по мёртвой сессии на каждый магазин).
        _get перечитывает slot.session на каждой попытке — подхватит новую сразу.
        """
        self._cookies = _parse_cookie(raw) if raw else None
        for sl in self._slots:
            old = sl.session
            sl.session = self._make_session(sl.proxy)
            try:
                await old.close()
            except Exception:
                pass
        self.b2b_fail_streak = 0
        self.cookie_alerted = False
        log.info("WB-клиент: кука обновлена (%d полей)", len(self._cookies or {}))
        return len(self._cookies or {})

    async def close(self) -> None:
        for sl in self._slots:
            try:
                await sl.session.close()
            except Exception:
                pass

    async def _throttle(self, slot: _Slot) -> None:
        """Один запрос раз в request_min_delay + jitter — ПЕР-СЛОТ (свой IP)."""
        async with slot.lock:
            loop = asyncio.get_event_loop()
            wait = (
                settings.request_min_delay
                + random.uniform(0, settings.request_jitter)
                - (loop.time() - slot.last)
            )
            if wait > 0:
                await asyncio.sleep(wait)
            slot.last = loop.time()

    async def _get(self, url, *, params=None, headers=None, retries=4, slot=None):
        slot = slot or self._direct_slot
        who = slot.proxy or "direct"
        for attempt in range(retries):
            await self._throttle(slot)
            try:
                r = await slot.session.get(url, params=params, headers=headers)
            except Exception as e:
                log.warning("WB сетевая ошибка %s (%s): %s", url, who, e)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # ponytail: 403 = этот IP забанен WAF. Кросс-слот failover не делаем —
                # слоты и так бьют разных продавцов параллельно, следующий цикл повторит.
                # Вернуть failover, если b2b снова станет активным и критичным.
                log.warning("WB %s -> 403 (WAF/бан) слот=%s, пропуск без ретраев", url, who)
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
                log.warning("WB %s -> %s слот=%s, пауза %.1fс", url, r.status_code, who, delay)
                await asyncio.sleep(delay)
                continue
            # 404 и прочее — возвращаем как есть (пагинация/перебор хостов разберутся)
            return r
        return None

    async def fetch_seller_catalog(
        self, supplier_id: int, subjects: set[int] | None = None, slot: "_Slot | None" = None
    ) -> list[NormProduct]:
        """Каталог продавца БЕЗ куки: цена и сток витрины (`shelf_price`).

        Фильтр по предмету — на стороне WB (`xsubject`), поэтому у крупных продавцов не
        листаем тысячи лишних товаров (ХОБОТ: 1 страница вместо 57). Для розницы это и
        есть точная цена; b2b-магазинам поверх навешивает бизнес-цену `enrich_prices`.
        subjects — какие предметы оставить (по умолчанию — смартфоны).
        """
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        slot = slot or self._direct_slot
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
            r = await self._get(CATALOG_URL, params=params, slot=slot)
            if r is None or r.status_code != 200:
                # ошибка/бан на ЛЮБОЙ странице → отдаём пусто («магазин пропущен»),
                # а не частичный список: иначе deactivate_missing погасит хвост
                # ассортимента, который просто не долистали. Следующий цикл повторит.
                # None = _get исчерпал ретраи (4×429 подряд или сетевые обрывы).
                st = r.status_code if r is not None else "ретраи исчерпаны (429/сеть)"
                log.warning("каталог %s слот=%s: страница %d → %s, магазин пропущен",
                            supplier_id, slot.proxy or "direct", page, st)
                return []
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
        даже с валидной кукой). IP не проверяет. Зовётся КАЖДЫЙ цикл на все товары
        b2b-магазина (статик-резидентные прокси; забанят/кука протухнет — вернём
        триггерную схему из git).
        """
        base_params = B2B_PARAMS
        pslots = self._proxy_slots()
        slot = pslots[self._enrich_rr % len(pslots)]  # round-robin по прокси-слотам
        self._enrich_rr += 1
        prices: dict[int, int] = {}
        deliv: dict[int, tuple] = {}
        saw_response = False  # был ли хоть один 200-ответ (403/429/обрыв — не про куку)
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
            r = await self._get(B2B_DETAIL_URL, params=params, headers=headers, slot=slot)
            if r is None or r.status_code != 200:
                # обрыв сети или 403/429 (WAF/бан IP) — НЕ признак протухшей куки:
                # streak копим только по 200-ответам без цен
                continue
            saw_response = True
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
                log.warning("b2b: ни одного 200 (сеть/бан IP), счётчик куки не трогаю")
        else:
            self.b2b_fail_streak = 0
        log.info("detail цены применены: %d/%d", len(prices), len(products))
        return set(prices)  # nm с реально полученной ценой — остальным monitor сохранит прежнюю

    async def resolve_seller_slug(self, slug: str) -> int | None:
        """supplier_id по slug-ссылке /seller/<slug> (для SEO-адресов без числа)."""
        r = await self._get(SHOP_BY_SLUG_URL.format(slug), slot=self._direct_slot)
        if r and r.status_code == 200:
            try:
                return int(r.json()["supplierID"])
            except Exception:
                return None
        return None

    async def fetch_supplier_info(self, supplier_id: int) -> dict | None:
        r = await self._get(SUPPLIER_INFO_URL.format(supplier_id), slot=self._direct_slot)
        if r and r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None


# единый экземпляр на всё приложение (пул слотов: прямой IP + прокси, троттлинг пер-слот)
wb_client = WBClient()


