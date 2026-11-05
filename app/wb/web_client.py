"""Веб-путь к WB (www/__internal) БЕЗ guardium.

Два раздельных запроса, как и в мобильном api-ios:
- розница: каталог продавца АНОНИМНО (только антибот-куки) — список, сток, розничная цена;
- бизнес: detail с куками бизнес-аккаунта + b2b=true — бизнес-цена. Куки передаёт
  вызывающий: у каждого пользователя свой аккаунт, клиент их не хранит.

WB пускает только «браузер»: нужна свежая антибот-кука x_wbaas_token (живёт минуты,
иначе 498) и заголовки настоящего браузера (иначе 403). Их выпускает анонимный
браузер (web_session.mint_antibot) — заранее по возрасту и сразу при 498. Ходим curl_cffi через выделенный прокси:
антибот-кука привязана к IP, с которого её выпустили.
Основной путь бота; при сбое client.py откатывается на мобильный api-ios.
"""
import asyncio
import logging
import random
import time

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.wb.parser import NormProduct, normalize
from app.wb.web_session import SELLER_URL, curl_proxy, mint_antibot, web_proxy

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
# TLS как у Chrome: антибот-кука выпущена Chrome, его UA зашит в неё
IMPERSONATE = "chrome"
# продавец для выпуска антибот-куки, пока бот ещё не ходил ни в один каталог
DEFAULT_MINT_SELLER = 4145836
# после неудачного выпуска не мучаем браузер каждым запросом (~40с на попытку)
MINT_RETRY_SEC = 60
# весь выпуск не дольше этого: 2026-11-03 страница WB зациклила JS, браузер завис
# навсегда и вместе с локом выпуска остановил мониторинг на 15 часов
MINT_HARD_TIMEOUT_SEC = 120


def _items(r) -> list:
    if not r.content:
        return []  # страница за концом списка: WB отдаёт 200 с пустым телом
    data = r.json()
    items = data.get("products") or (data.get("data") or {}).get("products") or []
    if not isinstance(items, list):
        raise ValueError("products должен быть списком")
    return items


class WebClient:
    def __init__(self) -> None:
        self._antibot = ""      # куки антибота из браузера (x_wbaas_token…) — живут минуты
        self._headers: dict[str, str] = {}  # заголовки того же браузера
        self._minted_at = 0.0   # monotonic, когда выпустили антибот
        self._mint_failed_at: float | None = None  # monotonic последнего неудачного выпуска
        self._mint_lock = asyncio.Lock()
        self._mint_seller = DEFAULT_MINT_SELLER
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._session: AsyncSession | None = None

    @property
    def ready(self) -> bool:
        """Розничный веб возможен: антибот выпускаем сами, нужен только прокси
        (прямой IP сервера антибот WB палит)."""
        return bool(web_proxy())

    def _ensure_session(self) -> AsyncSession:
        if self._session is None:
            proxy = curl_proxy(web_proxy())
            self._session = AsyncSession(
                impersonate=IMPERSONATE, timeout=20,
                proxies={"http": proxy, "https": proxy} if proxy else None,
                # не копим куки ответов: иначе куки аккаунта из бизнес-detail утекли бы
                # в анонимный каталог и розница снова получила бы бизнес-цену
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

    def _antibot_age(self) -> float:
        return time.monotonic() - self._minted_at

    async def _ensure_antibot(self, dead: str | None = None) -> bool:
        """Есть живая антибот-кука? Выпускает новую, если её нет, она старше TTL
        или WB только что ответил 498 на куку `dead`. Один браузер за раз (лок)."""
        async with self._mint_lock:
            if dead is not None and self._antibot != dead:
                return True  # пока ждали лок, другой запрос уже перевыпустил
            if dead is None and self._antibot and self._antibot_age() < settings.web_antibot_ttl_sec:
                return True
            started = time.monotonic()
            if self._mint_failed_at is not None and started - self._mint_failed_at < MINT_RETRY_SEC:
                return False  # недавно не вышло — не тратим ещё ~40с браузера
            try:
                got = await asyncio.wait_for(mint_antibot(self._mint_seller), MINT_HARD_TIMEOUT_SEC)
            except Exception as e:
                log.warning("антибот: выпуск упал: %s: %s", type(e).__name__, str(e)[:300])
                got = None
            if not got:
                self._antibot = ""
                self._mint_failed_at = time.monotonic()
                return False
            self._mint_failed_at = None
            self._antibot, self._headers = got["cookie"], got["headers"]
            self._minted_at = time.monotonic()
            log.info("антибот: выпущен за %.0fс", self._minted_at - started)
            return True

    async def _get_items(self, what: str, url: str, params: dict, *,
                         account: str = "", referer: str | None = None) -> list | None:
        """Один запрос → список products. None — антибот/сеть/не 200/битый JSON.

        498 = антибот-кука умерла → перевыпуск и ОДИН повтор.
        """
        for attempt in (1, 2):
            if not await self._ensure_antibot():
                log.warning("%s: нет антибот-куки", what)
                return None
            antibot = self._antibot
            cookie = f"{antibot}; {account}" if account else antibot
            headers = {**self._headers, "cookie": cookie}
            if referer:
                headers["referer"] = referer
            await self._throttle()
            try:
                r = await self._ensure_session().get(url, params=params, headers=headers)
            except Exception as e:
                log.warning("%s: сеть %s", what, type(e).__name__)
                return None
            if r.status_code == 498 and attempt == 1:
                log.info("%s: антибот умер в возрасте %.0fс, перевыпускаю", what, self._antibot_age())
                await self._ensure_antibot(dead=antibot)
                continue
            if r.status_code != 200:
                # 498 после свежей куки = антибот не принят; 401/403 = доступ/заголовки
                log.warning("%s: HTTP %s", what, r.status_code)
                return None
            try:
                return _items(r)
            except Exception:
                log.warning("%s: неверный JSON, тип=%s, байт=%d, начало: %r", what,
                            r.headers.get("content-type"), len(r.content), r.text[:200])
                return None
        return None

    async def fetch_catalog(
        self, supplier_id: int, subjects: set[int] | None = None
    ) -> list[NormProduct] | None:
        """Розничный каталог магазина (анонимно): список, сток, розничная цена.

        None — веб недоступен/ошибка. [] — WB не вернул товаров по фильтру.
        """
        if not self.ready:
            return None
        self._mint_seller = supplier_id  # антибот выпускаем на странице живого продавца
        subjects = subjects or {SMARTPHONE_SUBJECT_ID}
        products: dict[int, NormProduct] = {}  # nm_id -> товар, дедуп между страницами
        for page in range(1, settings.max_pages + 1):
            params = {**CATALOG_PARAMS, "supplier": supplier_id, "page": page,
                      "xsubject": ";".join(map(str, sorted(subjects)))}
            items = await self._get_items(f"веб-каталог {supplier_id} стр.{page}", CATALOG_URL,
                                          params,
                                          referer=SELLER_URL.format(supplier_id))
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

    async def fetch_b2b_items(self, nm_ids: list[int], account: str) -> list | None:
        """Бизнес-цены: сырые товары detail под куками аккаунта `account` (пачками по 100).

        None — куки пусты или любой запрос упал.
        """
        if not account:
            return None
        items: list = []
        for i in range(0, len(nm_ids), DETAIL_CHUNK):
            chunk = nm_ids[i:i + DETAIL_CHUNK]
            params = {**B2B_PARAMS, "nm": ";".join(map(str, chunk))}
            got = await self._get_items(f"веб-бизнес-detail ({len(chunk)} шт.)",
                                        DETAIL_URL, params, account=account)
            if got is None:
                return None
            items.extend(got)
        return items


web_client = WebClient()


if __name__ == "__main__":  # self-check антибота: выпуск, TTL, 498 → перевыпуск, сбой выпуска
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    def resp(code, products=()):
        return SimpleNamespace(status_code=code, content=b"{}", json=lambda: {"products": list(products)})

    def json_err():
        raise ValueError("пустое тело")

    phone = {"id": 1, "subjectId": 515, "sizes": [{"price": {"product": 10000}}]}

    async def demo():
        c = WebClient()
        c._throttle = AsyncMock()
        mints = iter([{"cookie": f"x_wbaas_token=t{i}", "headers": {"user-agent": "UA"}} for i in range(9)])
        mint = AsyncMock(side_effect=lambda sid: next(mints))
        http = SimpleNamespace(get=AsyncMock())
        c._ensure_session = lambda: http
        with patch(f"{__name__}.mint_antibot", mint), patch(f"{__name__}.web_proxy", lambda: "http://p:1"), \
             patch("time.monotonic") as clock:
            clock.return_value = 1000.0
            # 1) первый запрос выпускает антибот; розница идёт БЕЗ кук аккаунта
            http.get.return_value = resp(200, [phone])
            assert [p.nm_id for p in await c.fetch_catalog(7)] == [1]
            assert mint.await_count == 1 and mint.await_args.args == (7,)  # на странице этого продавца
            h = http.get.await_args.kwargs["headers"]
            assert h["cookie"] == "x_wbaas_token=t0" and h["user-agent"] == "UA", h
            # 2) кука моложе TTL — переиспользуем; старше — выпускаем заранее
            await c.fetch_catalog(7)
            assert mint.await_count == 1
            clock.return_value = 1000.0 + settings.web_antibot_ttl_sec
            await c.fetch_catalog(7)
            assert mint.await_count == 2
            # 3) 498 → перевыпуск и ОДИН повтор, который проходит
            # (третий ответ — стр.2 с теми же товарами: пагинация останавливается)
            http.get.side_effect = [resp(498), resp(200, [phone]), resp(200, [phone])]
            assert [p.nm_id for p in await c.fetch_catalog(7)] == [1]
            assert mint.await_count == 3
            # 3б) стр.2 = 200 с пустым телом → конец списка, товары стр.1 не теряем
            empty = SimpleNamespace(status_code=200, content=b"", json=json_err)
            http.get.side_effect = [resp(200, [phone]), empty]
            assert [p.nm_id for p in await c.fetch_catalog(7)] == [1]
            http.get.side_effect = None
            # 4) бизнес-detail: свежий антибот + куки аккаунта
            http.get.return_value = resp(200, [phone])
            assert len(await c.fetch_b2b_items([1], "wbx-validation-key=acc")) == 1
            assert http.get.await_args.kwargs["headers"]["cookie"] == "x_wbaas_token=t2; wbx-validation-key=acc"
            # 5) выпуск не удался → None (client.py уйдёт на api-ios), повтор не раньше паузы
            mint.side_effect = lambda sid: None
            clock.return_value += settings.web_antibot_ttl_sec
            assert await c.fetch_catalog(7) is None
            calls = mint.await_count
            assert await c.fetch_catalog(7) is None and mint.await_count == calls  # браузер не дёргаем
            clock.return_value += MINT_RETRY_SEC
            await c.fetch_catalog(7)
            assert mint.await_count == calls + 1
        # 6) браузер завис → выпуск прерывается по общему таймауту, лок отпущен
        # (вне patch time.monotonic: на нём живут таймеры asyncio)
        async def hang(sid):
            await asyncio.sleep(3600)
        c._antibot, c._mint_failed_at = "", None
        with patch(f"{__name__}.mint_antibot", hang), patch(f"{__name__}.web_proxy", lambda: "http://p:1"), \
             patch(f"{__name__}.MINT_HARD_TIMEOUT_SEC", 0.05):
            assert await c.fetch_catalog(7) is None
        assert not c._mint_lock.locked() and c._mint_failed_at is not None
        print("ok")

    asyncio.run(demo())
