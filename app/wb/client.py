"""HTTP-клиент к внутреннему API WB с троттлингом и ретраями.

Каталог и бизнес-цены — через мобильное API (api-ios), как iOS-приложение WB:
guardium (антибот, ~3 дня) + Bearer аккаунта (без срока) для бизнес-цен.
curl_cffi с имперсонацией остался для статики (static-basket) — мобильному API
TLS-отпечаток не важен.
"""

import asyncio
import base64
import json
import logging
import random

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, _delivery, _price, normalize

log = logging.getLogger(__name__)

IMPERSONATE = "safari18_0"
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}

# Каталог — через мобильное API: там нет WBAAS-куки (x_wbaas_token протухал за часы),
# пускает по x-guardium-token из приложения. Без guardium/с мёртвым — 498.
CATALOG_URL = "https://api-ios.wildberries.ru/__internal/catalog/sellers/v4/catalog"
CATALOG_PAGE_LIMIT = 300
SMARTPHONE_SUBJECT_ID = 515  # WB-предмет «Смартфоны» — по умолчанию мониторим только их
SUPPLIER_INFO_URL = "https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-id/{}.json"
# slug -> supplier_id: страница /seller/<slug> — SPA, ID отдаёт «конструктор магазинов»
SHOP_BY_SLUG_URL = "https://static-basket-01.wbcontent.net/vol0/constructor-api/shops/v3/{}.json"

# Бизнес-цены: тот же detail, что шлёт приложение в бизнес-режиме (снято из HAR
# 2026-10-28): b2b=true + mdg=3 + udg + Bearer аккаунта. Веб-путь (www/__internal
# с кукой) умер: у x_wbaas_token бюджет ~10 запросов, потом 498.
B2B_DETAIL_URL = "https://api-ios.wildberries.ru/__internal/card/cards/v4/detail"
B2B_PARAMS = {
    "appType": 64, "curr": "rub", "spp": 40, "lang": "ru", "locale": "ru",
    "ab_testing": "false", "hide_dflags": 1048576, "hide_dtype": 11,
    "hide_vflags": 35188667056128, "loyalty": 1, "uclusters": 1,
    "b2b": "true", "mdg": 3,
}
# Розница detail НЕ нужна: каталог уже даёт точную розничную цену (СПП у WB
# стандартная, не персональная — аноним == аккаунт, проверено 4/4). enrich только для b2b.


def _parse_proxies(raw: str) -> list[str]:
    """'socks5h://h:p, http://h2:p2' -> список прокси. Пусто = без прокси (прямое соединение)."""
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def _mobile_headers(guardium: str) -> dict[str, str]:
    """Заголовки как у iOS-приложения WB (снято из HAR)."""
    return {
        "Accept": "*/*",
        "User-Agent": settings.wb_ios_ua,
        "deviceid": settings.wb_ios_device_id,
        "x-guardium-token": guardium,
    }


def guardium_expires_at(token: str) -> int | None:
    """Когда протухнет guardium (unix-время). Формат: 1.1000.<hex>.<base64>.<подпись>,
    в base64 лежит '0|IP|UA|expiry|reusable|...'. Не разобрали — None."""
    try:
        payload = base64.b64decode(token.split(".")[3]).decode()
        return int(payload.split("|")[3])
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def bearer_user(token: str) -> str | None:
    """ID аккаунта из Bearer (JWT: заголовок.payload.подпись). Не JWT — None."""
    try:
        payload = token.removeprefix("Bearer ").strip().split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data["user"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


class _Slot:
    """Канал к WB: своя сессия (свой IP/прокси) + СВОЙ троттлинг.

    Троттлинг пер-слот, а не общий, — иначе параллельный проход по слотам выродится
    в очередь на едином локе. proxy=None — прямой IP сервера.
    """
    __slots__ = ("session", "proxy", "lock", "last", "err429")

    def __init__(self, session: AsyncSession, proxy: str | None) -> None:
        self.session = session
        self.proxy = proxy
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.err429 = 0  # счётчик 429 за текущий проход (сбрасывает monitoring_job)


class WBClient:
    def __init__(self) -> None:
        self._proxies = _parse_proxies(settings.wb_proxies)
        # Пул равнозначных слотов: прямой IP + по слоту на прокси. Слот магазина
        # обслуживает и каталог, и b2b-detail. Свой троттлинг у каждого → реальный
        # минутный цикл, а 429 на одном IP не валит остальные.
        self._slots = self._make_slots()
        self._slot_rr = 0  # round-robin по слотам для запросов вне минутного прохода
        log.info("WB-клиент: слотов %d (прямой + прокси %d)", len(self._slots), len(self._proxies))
        self.bearer = settings.wb_bearer.strip()  # Bearer аккаунта для бизнес-цен
        self.b2b_fail_streak = 0  # подряд провалов b2b (нет Bearer / 401 / 403)
        self.bearer_alerted = False  # уже предупредили о негодном Bearer
        self.guardium = settings.wb_guardium.strip()
        self.guardium_fail_streak = 0  # подряд 498 от каталога = guardium мёртв
        self.guardium_alerted = False

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

    def _next_slot(self) -> _Slot:
        """Round-robin по ВСЕМ слотам — для запросов вне минутного прохода
        (ручная проверка, сид нового магазина), чтобы не долбить только direct."""
        self._slot_rr += 1
        return self._slots[self._slot_rr % len(self._slots)]

    def _make_session(self, proxy: str | None) -> AsyncSession:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        return AsyncSession(
            headers=HEADERS, impersonate=IMPERSONATE, timeout=20, proxies=proxies,
            discard_cookies=True,  # куки ответов WB нам не нужны, не копим их
        )

    def set_bearer(self, token: str) -> None:
        """Новый Bearer подхватывается следующим же запросом бизнес-цен."""
        token = token.strip()
        self.bearer = token if token.startswith("Bearer ") else f"Bearer {token}"
        self.b2b_fail_streak = 0
        self.bearer_alerted = False
        log.info("WB-клиент: Bearer обновлён")

    def set_guardium(self, token: str) -> None:
        """Новый guardium подхватывается следующим же запросом каталога."""
        self.guardium = token.strip()
        self.guardium_fail_streak = 0
        self.guardium_alerted = False
        log.info("WB-клиент: guardium обновлён, истекает %s", guardium_expires_at(self.guardium))

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
        context = ""
        if params and "supplier" in params:
            context = f" магазин={params['supplier']} страница={params.get('page')}"
        last_failure = "нет попыток"
        for attempt in range(retries):
            await self._throttle(slot)
            try:
                r = await slot.session.get(url, params=params, headers=headers)
            except Exception as e:
                last_failure = f"сеть: {type(e).__name__}"
                log.warning("WB сетевая ошибка %s%s слот=%s попытка=%d/%d: %s",
                            url, context, who, attempt + 1, retries, last_failure)
                await asyncio.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code == 403:
                # 403 означает отказ в доступе, точная причина из статуса не видна.
                # Повтор другим слотом делает monitoring_job после основной очереди.
                log.warning("WB %s%s -> 403 (доступ отклонён) слот=%s, пропуск без ретраев",
                            url, context, who)
                return r  # отдаём 403, а не None: None теперь значит только «сеть легла»
            if r.status_code == 429 or r.status_code >= 500:
                last_failure = f"HTTP {r.status_code}"
                if r.status_code == 429:
                    slot.err429 += 1  # пер-слот статистика для сводки прохода
                delay = self._retry_delay(r, attempt)
                log.warning("WB %s%s -> %s слот=%s попытка=%d/%d, пауза %.1fс",
                            url, context, r.status_code, who, attempt + 1, retries, delay)
                await asyncio.sleep(delay)
                continue
            # 404 и прочее — возвращаем как есть (пагинация/перебор хостов разберутся)
            return r
        log.warning("WB %s%s слот=%s: ретраи исчерпаны, последняя причина: %s",
                    url, context, who, last_failure)
        return None

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        retry_after = response.headers.get("X-Ratelimit-Retry") or response.headers.get("Retry-After")
        if retry_after and str(retry_after).isdigit():
            return float(retry_after)
        return (2**attempt) + random.uniform(0, 1)

    @staticmethod
    def _log_catalog_failure(supplier_id, page, slot, response) -> None:
        if response is None:
            reason = "ретраи исчерпаны; последняя причина указана в строке WB выше"
        else:
            reasons = {
                403: "доступ отклонён",
                498: "guardium протух или не задан; обнови кнопкой «Токены»",
                429: "лимит запросов",
            }
            reason = f"HTTP {response.status_code}: {reasons.get(response.status_code, 'ошибка ответа WB')}"
            reason += (f", server={response.headers.get('server')}, "
                       f"тип={response.headers.get('content-type')}, байт={len(response.content)}")
        log.warning("каталог %s слот=%s: страница %d, магазин пропущен, причина: %s",
                    supplier_id, slot.proxy or "direct", page, reason)

    @staticmethod
    def _catalog_items(response, supplier_id, page, slot) -> list | None:
        try:
            data = response.json()
            items = data.get("products") or (data.get("data") or {}).get("products") or []
            if not isinstance(items, list):
                raise ValueError("products должен быть списком")
        except (ValueError, TypeError, AttributeError) as e:
            log.warning("каталог %s слот=%s: страница %d, магазин пропущен, "
                        "причина: неверный JSON/формат (%s), HTTP=%s, тип=%s, байт=%d",
                        supplier_id, slot.proxy or "direct", page, type(e).__name__,
                        response.status_code, response.headers.get("content-type"), len(response.content))
            return None
        if not items and page == 1:
            log.warning("каталог %s слот=%s: страница 1, причина пустой выдачи: "
                        "HTTP 200, WB не вернул товары для заданного фильтра",
                        supplier_id, slot.proxy or "direct")
        return items

    async def fetch_catalog_page(
        self, supplier_id: int, page: int = 1, *, subjects: set[int] | None = None, slot=None
    ):
        """Одна страница каталога через мобильное API (guardium, без куки)."""
        if supplier_id <= 0 or page <= 0:
            raise ValueError("ID продавца и номер страницы должны быть положительными")
        params = {
            "appType": 64, "curr": "rub", "dest": settings.wb_dest,
            "sort": "popular", "spp": settings.wb_spp, "supplier": supplier_id,
            "page": page, "limit": CATALOG_PAGE_LIMIT,
        }
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        params["xsubject"] = ";".join(map(str, sorted(subjects)))
        # Bearer не передаём: каталог анонимный (это и есть розница).
        return await self._get(CATALOG_URL, params=params,
                               headers=_mobile_headers(self.guardium), slot=slot)

    async def fetch_seller_catalog(
        self, supplier_id: int, subjects: set[int] | None = None, slot: "_Slot | None" = None
    ) -> list[NormProduct]:
        """Анонимный каталог (guardium): цена и сток витрины (`shelf_price`).

        Фильтр по предмету — на стороне WB (`xsubject`), поэтому у крупных продавцов не
        листаем тысячи лишних товаров (ХОБОТ: 1 страница вместо 57). Для розницы это и
        есть точная цена; b2b-магазинам поверх навешивает бизнес-цену `enrich_prices`.
        subjects — какие предметы оставить (по умолчанию — смартфоны).
        """
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        slot = slot or self._next_slot()  # без слота (ручная проверка/сид) — по кругу
        products: list[NormProduct] = []
        # limit=300 (дефолт WB — 100): магазины до 300 позиций влезают в ОДНУ страницу.
        # Меньше запросов → меньше 429; проверено вживую (182 товара одной страницей).
        page_limit = CATALOG_PAGE_LIMIT
        for page in range(1, settings.max_pages + 1):
            r = await self.fetch_catalog_page(supplier_id, page, subjects=subjects, slot=slot)
            if r is not None and r.status_code == 498:
                self.guardium_fail_streak += 1  # guardium мёртв — алерт в monitor
            elif r is not None and r.status_code == 200:
                self.guardium_fail_streak = 0
            if r is None or r.status_code != 200:
                # ошибка/бан на ЛЮБОЙ странице → отдаём пусто («магазин пропущен»),
                # а не частичный список: иначе deactivate_missing погасит хвост
                # ассортимента, который просто не долистали. Следующий цикл повторит.
                # None = _get исчерпал ретраи (4×429 подряд или сетевые обрывы).
                self._log_catalog_failure(supplier_id, page, slot, r)
                return []
            items = self._catalog_items(r, supplier_id, page, slot)
            if items is None:
                return []  # Не сохраняем частичный каталог после ошибки JSON.
            if not items:
                break
            if settings.debug_raw and page == 1:
                import json
                log.info("RAW первый товар: %s", json.dumps(items[0], ensure_ascii=False))
            products.extend(normalize(p, supplier_id) for p in items)
            if len(items) < page_limit:
                break
        if subjects:  # страховка, если WB проигнорит xsubject
            before_filter = len(products)
            products = [p for p in products if p.subject_id in subjects]
            if before_filter and not products:
                log.warning("каталог %s слот=%s: причина пустой выдачи: все %d товаров "
                            "исключены фильтром subject_id=%s",
                            supplier_id, slot.proxy or "direct", before_filter, sorted(subjects))
        return products

    async def enrich_prices(self, products: list[NormProduct], slot: "_Slot | None" = None) -> set[int]:
        """Навешивает БИЗНЕС-цену из мобильного detail (батчи по 100) поверх каталожной.

        Только для b2b-магазинов: бизнес-цена видна лишь с Bearer аккаунта (b2b=true).
        Рознице detail не нужен — каталог уже даёт её цену. Зовётся КАЖДЫЙ цикл на все
        товары b2b-магазина тем же слотом, что и каталог.
        Без Bearer не ходим вовсе: detail без него отдаёт розницу, а её нельзя выдавать
        за бизнес-цену — товары получат «цена не подтверждена».
        """
        nm_ids = [p.nm_id for p in products]
        if not self.bearer:
            if products:
                self.b2b_fail_streak += 1
                log.warning("b2b: Bearer не задан — бизнес-цены пропущены (%d товаров)", len(products))
            return set()
        slot = slot or self._next_slot()
        headers = {**_mobile_headers(self.guardium), "authorization": self.bearer}
        prices: dict[int, int] = {}
        deliv: dict[int, tuple] = {}
        statuses: list[str] = []  # коды ответов по чанкам — для лога
        for i in range(0, len(nm_ids), 100):
            chunk = nm_ids[i:i + 100]
            params = {**B2B_PARAMS, "dest": settings.wb_dest, "udg": settings.wb_b2b_udg,
                      "nm": ";".join(map(str, chunk))}
            r = await self._get(B2B_DETAIL_URL, params=params, headers=headers, slot=slot)
            statuses.append("сеть" if r is None else str(r.status_code))
            if r is None or r.status_code != 200:
                continue  # 498 = guardium (алерт по каталогу), 401/403 считаем ниже
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
        # 401/403 = WB не принял Bearer (сессию отозвали) — копим счётчик для алерта.
        # Сеть/429/498 — не про Bearer, счётчик не трогаем.
        if any(code in ("401", "403") for code in statuses) and not prices:
            self.b2b_fail_streak += 1
        elif prices:
            self.b2b_fail_streak = 0
        log.info("detail цены применены: %d/%d (ответы %s, слот %s)", len(prices), len(products),
                 ",".join(statuses) or "-", slot.proxy or "direct")
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
