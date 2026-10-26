"""Эксперимент: живой Chrome сам держит сессию WB, а мы проверяем, годятся ли его куки боту.

Chromium запущен обычный (не playwright/selenium — у них флаги автоматизации, WB их ловит).
В страницу код не внедряем: только открываем вкладку и слушаем её сетевые запросы.

Каждые KEEPER_INTERVAL секунд:
  1. открываем свою вкладку с магазином WB (сайт сам обновит x_wbaas_token) и ловим
     заголовки, с которыми сайт сам запросил каталог: x-spa-version, deviceid, sec-ch-ua,
     куки — WB их меняет, поэтому не хардкодим, а копируем у сайта. Прошлую свою вкладку
     закрываем, вкладки человека (где логинился) не трогаем;
  2. сохраняем заголовки → /data/headers.json (потом отсюда их возьмёт бот);
  3. повторяем этими заголовками через curl, как бот: каталог и b2b-detail.
Одна строка лога за цикл (stdout + /data/keeper.log).
"""

import asyncio
import json
import logging
import os
import time
import urllib.request

from curl_cffi.requests import AsyncSession

CDP = "http://127.0.0.1:9222"
PROXY = os.environ["BROWSER_PROXY"]
INTERVAL = int(os.getenv("KEEPER_INTERVAL", "600"))
SUPPLIER = int(os.getenv("KEEPER_SUPPLIER", "250154985"))
DEST = int(os.getenv("WB_DEST", "-1257786"))
DATA = os.getenv("KEEPER_DATA", "/data")
IMPERSONATE = "chrome146"  # TLS-отпечаток Chrome; UA и sec-ch-ua берём у живого браузера

# с 2026-10 сайт ходит через u-catalog / u-card; старые catalog/card отдают 403
CATALOG_URL = "https://www.wildberries.ru/__internal/u-catalog/sellers/v4/catalog"
DETAIL_URL = "https://www.wildberries.ru/__internal/u-card/cards/v4/detail"
# ponytail: копия B2B_PARAMS из app/wb/client.py — эксперимент живёт отдельно от бота
B2B_PARAMS = {
    "appType": 1, "curr": "rub", "dest": -446112, "spp": 30,
    "hide_vflags": 4294967296, "hide_dflags": 131072, "hide_dtype": "11;13;14;15",
    "b2b": "true", "mdg": 3, "mtype": 257, "lang": "ru", "ab_testing": "false",
}
# эти заголовки curl ставит сам (host/сжатие) или они HTTP/2-служебные (":path" и т.п.)
SKIP_HEADERS = {"host", "accept-encoding", "content-length"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%d.%m %H:%M",
    handlers=[logging.StreamHandler(), logging.FileHandler(f"{DATA}/keeper.log")],
)
log = logging.getLogger("keeper")


def cdp_get(path: str):
    """Ждём, пока Chrome поднимет CDP, и читаем его http-эндпоинт."""
    while True:
        try:
            with urllib.request.urlopen(f"{CDP}{path}", timeout=5) as r:
                return json.load(r)
        except OSError:
            time.sleep(3)


async def cdp(ws_url: str, method: str, **params) -> dict:
    """Одна команда CDP (своё соединение на команду — просто и надёжно)."""
    async with AsyncSession() as s:
        ws = await s.ws_connect(ws_url)
        try:
            await ws.send_json({"id": 1, "method": method, "params": params})
            while True:
                msg = await ws.recv_json(timeout=30)
                if msg.get("id") == 1:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})
        finally:
            await ws.close()


async def open_and_capture(browser_ws: str) -> tuple[str, dict | None]:
    """Открывает свою вкладку с магазином и ловит полные заголовки запроса каталога.

    Network.requestWillBeSentExtraInfo даёт заголовки «как ушли в сеть» — с куками и
    sec-ch-ua, которых нет в обычном requestWillBeSent. Ждём до 40 с."""
    tab = (await cdp(browser_ws, "Target.createTarget", url="about:blank"))["targetId"]
    page_ws = f"ws://127.0.0.1:9222/devtools/page/{tab}"
    catalog_id, extra = None, {}
    async with AsyncSession() as s:
        ws = await s.ws_connect(page_ws)
        try:
            await ws.send_json({"id": 1, "method": "Network.enable", "params": {}})
            await ws.send_json({"id": 2, "method": "Page.navigate",
                                "params": {"url": f"https://www.wildberries.ru/seller/{SUPPLIER}"}})
            end = time.time() + 40
            while time.time() < end:
                try:
                    msg = await ws.recv_json(timeout=5)
                except Exception:
                    continue  # тишина в сети — ждём дальше до дедлайна
                p = msg.get("params", {})
                if msg.get("method") == "Network.requestWillBeSent" \
                        and "u-catalog/sellers/v4/catalog" in p["request"]["url"]:
                    catalog_id = p["requestId"]
                elif msg.get("method") == "Network.requestWillBeSentExtraInfo":
                    extra[p["requestId"]] = p["headers"]  # может прийти раньше основного
                if catalog_id and catalog_id in extra:
                    break
        finally:
            await ws.close()
    raw = extra.get(catalog_id) if catalog_id else None
    if not raw:
        return tab, None
    return tab, {k: v for k, v in raw.items() if not k.startswith(":") and k.lower() not in SKIP_HEADERS}


def product_price(p: dict) -> int | None:
    for size in p.get("sizes") or []:
        if (size.get("price") or {}).get("product"):
            return size["price"]["product"]
    return None


async def check(hdrs: dict) -> str:
    """Каталог и b2b-detail через curl теми же заголовками, что у сайта (как будет у бота)."""
    async with AsyncSession(impersonate=IMPERSONATE, proxy=PROXY, timeout=20, default_headers=False) as s:
        r = await s.get(
            CATALOG_URL, headers=hdrs,
            params={"appType": 1, "curr": "rub", "dest": DEST, "sort": "popular", "spp": 30,
                    "supplier": SUPPLIER, "page": 1, "limit": 300, "xsubject": 515},
        )
        nms = [p["id"] for p in r.json().get("products") or []][:20] if r.status_code == 200 else []
        out = f"каталог {r.status_code} ({len(nms)})"
        if not nms:
            return out + ", detail —"
        await asyncio.sleep(2)
        r = await s.get(
            DETAIL_URL, params={**B2B_PARAMS, "nm": ";".join(map(str, nms))},
            headers={**hdrs, "referer": f"https://www.wildberries.ru/catalog/{nms[0]}/detail.aspx"},
        )
        got = {}
        if r.status_code == 200:
            got = {p["id"]: product_price(p) for p in r.json().get("products") or []}
            got = {k: v for k, v in got.items() if v is not None}
        # цена одного товара — утром сверить с сайтом: бизнес она или розница
        nm = next((n for n in nms if n in got), None)
        one = f", nm {nm} = {got[nm] // 100} ₽" if nm else ""
        return out + f", detail {r.status_code} (цены {len(got)}/{len(nms)}{one})"


def token_of(hdrs: dict) -> str | None:
    for part in hdrs.get("cookie", "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "x_wbaas_token":
            return v
    return None


async def cycle(browser_ws: str, my_tabs: list[str], last_token: str | None):
    tab, hdrs = await open_and_capture(browser_ws)
    await asyncio.sleep(5)  # пусть страница догрузится (антибот-скрипты WB)
    for old in my_tabs:
        try:
            await cdp(browser_ws, "Target.closeTarget", targetId=old)
        except RuntimeError:
            pass  # вкладку уже закрыли руками
    if not hdrs:
        log.warning("запрос каталога сайта не пойман (страница не загрузилась/WB сменил адрес)")
        return [tab], last_token
    with open(f"{DATA}/headers.json", "w") as f:
        json.dump(hdrs, f, ensure_ascii=False, indent=1)
    token = token_of(hdrs)
    state = "токена НЕТ" if not token else f"токен {'ОБНОВЛЁН' if token != last_token else 'тот же'}"
    try:
        result = await check(hdrs)
    except Exception as e:
        result = f"ошибка {type(e).__name__}: {e}"
    log.info("%s | spa %s | кук %d | %s", state, hdrs.get("x-spa-version"),
             hdrs.get("cookie", "").count("="), result)
    return [tab], token


async def main():
    info = cdp_get("/json/version")
    log.info("Chrome готов: %s | прокси %s | цикл %d с", info["User-Agent"], PROXY, INTERVAL)
    my_tabs: list[str] = []
    last_token = None
    while True:
        try:
            my_tabs, last_token = await cycle(info["webSocketDebuggerUrl"], my_tabs, last_token)
        except Exception as e:
            log.warning("цикл упал: %s: %s", type(e).__name__, e)
            info = cdp_get("/json/version")  # Chrome мог перезапуститься — ws-адрес новый
            my_tabs = []
        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
