"""HTTP-клиент к внутреннему API WB с троттлингом и ретраями.

Розница и бизнес-цена — РАЗНЫЕ запросы, в обоих режимах:
- розница (все магазины): каталог продавца анонимно — веб (web_client) или api-ios (guardium);
- бизнес (b2b-магазины): detail под бизнес-аккаунтом — веб (куки) или api-ios (Bearer).
Основной путь — веб; у каталога и бизнес-detail свой независимый fallback на api-ios.
curl_cffi с имперсонацией остался для статики (static-basket) — мобильному API
TLS-отпечаток не важен.
"""

import asyncio
import base64
import json
import logging
import random
import time

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, _delivery, _price, normalize
from app.wb.web_client import web_client

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

# Бизнес-цены (fallback веба): тот же detail, что шлёт приложение в бизнес-режиме
# (снято из HAR 2026-10-28): b2b=true + mdg=3 + udg + Bearer аккаунта.
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


class _WebPath:
    """Состояние одного веб-пути (каталог или бизнес-detail).

    Разовый сбой (прокси моргнул) путь не роняет: выключаем после FAILS_TO_DOWN
    сбоев подряд. Выключенный путь раз в RETRY_AFTER сек пробуем снова — удача
    поднимает его сама, без ожидания рефреша keeper'а.
    """
    FAILS_TO_DOWN = 3
    RETRY_AFTER = 15 * 60

    def __init__(self, name: str) -> None:
        self.name = name
        self.fails = 0
        self.down_since: float | None = None  # monotonic, когда выключили
        self.alerted = False  # уже предупредили о падении (сбрасывает ok())

    @property
    def down(self) -> bool:
        return self.down_since is not None

    def usable(self, now: float) -> bool:
        """Можно идти вебом: путь жив или пора пробовать снова."""
        return self.down_since is None or now - self.down_since >= self.RETRY_AFTER

    def ok(self) -> None:
        if self.down:
            log.info("%s: снова работает", self.name)
        self.fails = 0
        self.down_since = None
        self.alerted = False

    def fail(self, now: float) -> None:
        self.fails += 1
        # уже выключен (неудачная повторная проба) — откладываем следующую пробу
        if self.down or self.fails >= self.FAILS_TO_DOWN:
            self.down_since = now
            log.warning("%s: выключен, работаю через api-ios; сбоев подряд %d, повтор через %d мин",
                        self.name, self.fails, self.RETRY_AFTER // 60)
        else:
            log.warning("%s: сбой %d/%d, в этот раз через api-ios",
                        self.name, self.fails, self.FAILS_TO_DOWN)


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
        # Веб-пути (основные): куки от keeper. Каталог (розница) и бизнес-detail
        # падают и поднимаются независимо, у каждого свой fallback на api-ios.
        self.web_catalog = _WebPath("веб-каталог (розница)")
        self.web_b2b = _WebPath("веб-detail (бизнес-цены)")

    def set_web_session(self, storage_state) -> None:
        """Свежая банка кук от keeper — оба веб-пути пробуем заново."""
        web_client.set_session(storage_state)
        self.web_catalog.ok()
        self.web_b2b.ok()

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
        await web_client.close()
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
        """Каталог магазина = розница (анонимно). Основной путь — веб, при сбое — api-ios.

        Пустой веб-ответ веб НЕ роняет (магазин мог остаться без смартфонов): этот
        магазин перепроверяем через api-ios, чтобы неверные веб-параметры не обнулили каталог.
        """
        now = time.monotonic()
        if web_client.ready and self.web_catalog.usable(now):
            products = await web_client.fetch_catalog(supplier_id, subjects)
            if products is None:
                self.web_catalog.fail(now)
            else:
                self.web_catalog.ok()
                if products:
                    return products
                log.info("веб-каталог %s: пусто → перепроверяю через api-ios", supplier_id)
        return await self._fetch_catalog_mobile(supplier_id, subjects, slot)

    async def _fetch_catalog_mobile(
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
        """Бизнес-цена b2b-товаров — отдельным запросом detail под бизнес-аккаунтом.

        Основной путь — веб (куки аккаунта), при сбое — api-ios (Bearer). Каталог
        даёт только розницу, поэтому без detail бизнес-цены нет (monitor пометит stale).
        Возвращает nm с реально полученной ценой.
        """
        if not products:
            return set()
        now = time.monotonic()
        if web_client.account_ready and self.web_b2b.usable(now):
            items = await web_client.fetch_b2b_items([p.nm_id for p in products])
            if items is not None:
                self.web_b2b.ok()
                priced = self._apply_detail(products, items)
                if priced:
                    self.b2b_fail_streak = 0  # бизнес-цены приходят — Bearer-алерт не нужен
                log.info("веб бизнес-цены применены: %d/%d", len(priced), len(products))
                return priced
            self.web_b2b.fail(now)
        return await self._enrich_prices_mobile(products, slot)

    @staticmethod
    def _apply_detail(products: list[NormProduct], items: list) -> set[int]:
        """Цена и доставка из ответа detail поверх товаров. Возвращает nm с ценой."""
        prices: dict[int, int] = {}
        deliv: dict[int, tuple] = {}
        for it in items:
            try:
                nm = int(it["id"])
            except (KeyError, TypeError, ValueError):
                continue
            val = _price(it)
            if val is not None:
                prices[nm] = val
            deliv[nm] = _delivery(it)
        for p in products:
            if p.nm_id in prices:
                p.price = prices[p.nm_id]
            if p.nm_id in deliv:
                p.delivery_hours, p.from_seller = deliv[p.nm_id]
        return set(prices)

    async def _enrich_prices_mobile(self, products: list[NormProduct], slot: "_Slot | None" = None) -> set[int]:
        """Бизнес-цена из мобильного detail (b2b=true + Bearer, батчи по 100) — fallback.

        Без Bearer не ходим вовсе: detail без него отдаёт розницу, а её нельзя выдавать
        за бизнес-цену — товары получат «цена не подтверждена».
        """
        nm_ids = [p.nm_id for p in products]
        if not self.bearer:
            self.b2b_fail_streak += 1
            log.warning("b2b: Bearer не задан — бизнес-цены пропущены (%d товаров)", len(products))
            return set()
        slot = slot or self._next_slot()
        headers = {**_mobile_headers(self.guardium), "authorization": self.bearer}
        items: list = []
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
                items.extend(r.json().get("products") or [])
            except Exception:
                continue
        priced = self._apply_detail(products, items)
        # 401/403 = WB не принял Bearer (сессию отозвали) — копим счётчик для алерта.
        # Сеть/429/498 — не про Bearer, счётчик не трогаем.
        if any(code in ("401", "403") for code in statuses) and not priced:
            self.b2b_fail_streak += 1
        elif priced:
            self.b2b_fail_streak = 0
        log.info("api-ios бизнес-цены применены: %d/%d (ответы %s, слот %s)", len(priced),
                 len(products), ",".join(statuses) or "-", slot.proxy or "direct")
        return priced  # остальным monitor сохранит прежнюю цену

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


if __name__ == "__main__":  # self-check выбора пути: веб/api-ios, пустой магазин, сбои
    from unittest.mock import AsyncMock, patch

    def prod(nm):
        return NormProduct(nm_id=nm, name="x", brand=None, price=100, stock=1, pics=0,
                           supplier_id=1, shelf_price=100)

    async def demo():
        c = WBClient()
        mobile = AsyncMock(return_value=[prod(9)])
        c._fetch_catalog_mobile = mobile
        with patch.object(web_client, "_cookie_anon", "a"), \
             patch.object(web_client, "_cookie_account", "acc"), \
             patch.object(web_client, "fetch_catalog", AsyncMock()) as web_cat, \
             patch.object(web_client, "fetch_b2b_items", AsyncMock()) as web_b2b, \
             patch("time.monotonic") as clock:
            clock.return_value = 1000.0
            # 1) веб отдал товары → api-ios не трогаем
            web_cat.return_value = [prod(1)]
            assert [p.nm_id for p in await c.fetch_seller_catalog(1)] == [1]
            assert mobile.await_count == 0
            # 2) пустой магазин: перепроверка через api-ios, но веб НЕ выключается
            web_cat.return_value = []
            assert [p.nm_id for p in await c.fetch_seller_catalog(1)] == [9]
            assert not c.web_catalog.down and c.web_catalog.fails == 0
            # 3) два сбоя — ещё не выключен, третий — выключен
            web_cat.return_value = None
            await c.fetch_seller_catalog(1); await c.fetch_seller_catalog(1)
            assert not c.web_catalog.down
            await c.fetch_seller_catalog(1)
            assert c.web_catalog.down
            # 4) пока выключен — в веб не ходим; через 15 мин пробуем снова и поднимаемся
            calls = web_cat.await_count
            clock.return_value = 1000.0 + 60
            await c.fetch_seller_catalog(1)
            assert web_cat.await_count == calls
            clock.return_value = 1000.0 + _WebPath.RETRY_AFTER
            web_cat.return_value = [prod(1)]
            await c.fetch_seller_catalog(1)
            assert not c.web_catalog.down
            # 5) бизнес-цена: веб-detail кладёт СВОЮ цену поверх розницы
            web_b2b.return_value = [{"id": 1, "sizes": [{"price": {"product": 9000}}]}]
            ps = [prod(1), prod(2)]
            assert await c.enrich_prices(ps) == {1}
            assert ps[0].price == 90 and ps[1].price == 100  # у nm=2 цены нет → monitor пометит stale
            # 6) веб-detail упал → api-ios с Bearer; каталог при этом жив
            web_b2b.return_value = None
            c.bearer = ""
            assert await c.enrich_prices([prod(1)]) == set()
            assert c.b2b_fail_streak == 1 and c.web_b2b.fails == 1 and not c.web_catalog.down
        print("ok")

    asyncio.run(demo())
