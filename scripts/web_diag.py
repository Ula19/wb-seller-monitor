"""Диагностика веб-пути: что нужно WB, чтобы веб-каталог и бизнес-detail отвечали 200.

Браузер (patchright, сохранённая сессия бота из БД) открывает страницу продавца и
перехватывает НАСТОЯЩИЙ запрос каталога. Потом curl_cffi повторяет его в разных
вариантах: с заголовками браузера / без, с куками браузера / аккаунта / анонимными.
Опционально — наблюдение: раз в 5 мин повтор, чтобы понять, сколько живут заголовки.

Запуск в контейнере (~15 запросов к WB + один заход браузером):
    docker compose cp scripts/web_diag.py bot:/app/scripts/web_diag.py
    docker compose exec bot env PYTHONPATH=/app python scripts/web_diag.py <supplier_id> [минут_наблюдения]
"""
import asyncio
import json
import sys
import time
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession
from patchright.async_api import async_playwright

from app.db import repo
from app.db.base import Session, engine
from app.wb.web_client import B2B_PARAMS, DETAIL_URL
from app.wb.web_session import HOME_URL, LAUNCH_ARGS, _pw_proxy, cookie_header, curl_proxy, web_proxy

SUP = int(sys.argv[1]) if len(sys.argv) > 1 else 4145836
WATCH_MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 0
SKIP = {"host", "content-length", "connection"}


async def capture(state: str):
    """Заходим браузером на страницу продавца и ловим запрос каталога с его заголовками."""
    got = []
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=False, proxy=_pw_proxy(web_proxy()), args=LAUNCH_ARGS)
        try:
            ctx = await b.new_context(locale="ru-RU", storage_state=json.loads(state))
            page = await ctx.new_page()

            async def on_resp(r):
                if "u-catalog/sellers" in r.url and "/catalog" in r.url:
                    got.append((r.url, r.status, await r.request.all_headers()))

            page.on("response", on_resp)
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            await page.goto(f"https://www.wildberries.ru/seller/{SUP}", wait_until="domcontentloaded")
            for _ in range(25):
                if got:
                    break
                await page.mouse.wheel(0, 3000)
                await asyncio.sleep(1)
            body = (await page.inner_text("body"))[:300]
            print("браузер: залогинен" if "Войти" not in body else "браузер: НЕ залогинен (на странице «Войти»)")
        finally:
            await b.close()
    return got[0] if got else None


def first_price(r):
    try:
        ps = r.json().get("products") or (r.json().get("data") or {}).get("products") or []
    except Exception:
        return None, []
    if not ps:
        return None, []
    pr = (ps[0].get("sizes") or [{}])[0].get("price") or {}
    val = pr.get("product")
    return (val // 100 if val else None), [p["id"] for p in ps[:5]]


async def hit(s, label, url, headers):
    try:
        r = await s.get(url, headers=headers)
    except Exception as e:
        print(f"  {label:<55} сеть {type(e).__name__}")
        return None
    price, ids = first_price(r) if r.status_code == 200 else (None, [])
    print(f"  {label:<55} HTTP {r.status_code}  server={r.headers.get('server')}  цена 1-го={price}")
    return ids


async def run_variants(s, url, H, br_cookie, full_ck, anon_ck, antibot):
    sep = "&" if "?" in url else "?"
    u = lambda: f"{url}{sep}nc={time.time_ns()}"  # cache-bust, чтобы не получить ответ из кэша CDN
    no_ab = {k: v for k, v in H.items() if k not in antibot}
    ids = await hit(s, "1 заголовки браузера + куки браузера", u(), {**H, "cookie": br_cookie})
    await hit(s, "2 заголовки браузера БЕЗ кук", u(), H)
    await hit(s, "3 заголовки браузера + анонимные куки (розница)", u(), {**H, "cookie": anon_ck})
    await hit(s, "4 заголовки браузера + куки аккаунта (бизнес)", u(), {**H, "cookie": full_ck})
    await hit(s, "5 без antibot-заголовков + куки браузера", u(), {**no_ab, "cookie": br_cookie})
    await hit(s, "6 как бот сейчас: Accept + анонимные куки", u(),
              {"Accept": "*/*", "Accept-Language": "ru-RU,ru;q=0.9", "cookie": anon_ck})
    return ids


async def main():
    try:
        async with Session() as db:
            state = await repo.get_setting(db, "wb_storage_state")
        if not state:
            print("веб-сессии в БД нет — сначала /wblogin")
            return
        print(f"прокси веба: {web_proxy() or 'НЕТ'}; ловлю запрос каталога браузером…")
        cap = await capture(state)
        if not cap:
            print("запрос каталога браузером НЕ пойман (антибот? пустой магазин?)")
            return
        url, st, hh = cap
        print(f"браузер получил каталог: HTTP {st}\n  {url[:160]}")
        H = {k: v for k, v in hh.items() if not k.startswith(":") and k.lower() not in SKIP}
        br_cookie = H.pop("cookie", "")
        antibot = [k for k in H if "antibot" in k or "guardium" in k]
        print("заголовки браузера:", ", ".join(sorted(H)))
        print("antibot-заголовки:", {k: H[k][:12] + "…" for k in antibot} or "НЕТ")
        print(f"кук у браузера в запросе: {len(br_cookie.split(';')) if br_cookie else 0}")
        full_ck, anon_ck = cookie_header(state), cookie_header(state, anonymous=True)

        px = curl_proxy(web_proxy())
        async with AsyncSession(impersonate="chrome", timeout=20,
                                proxies={"http": px, "https": px} if px else None,
                                discard_cookies=True) as s:
            print("\nкаталог (curl_cffi chrome):")
            ids = await run_variants(s, url, H, br_cookie, full_ck, anon_ck, antibot)
            if ids:
                nm = ";".join(map(str, ids))
                b2b = f"{DETAIL_URL}?{urlencode({**B2B_PARAMS, 'nm': nm})}"
                plain = f"{DETAIL_URL}?{urlencode({k: v for k, v in B2B_PARAMS.items() if k not in ('b2b', 'mdg', 'udg')} | {'nm': nm})}"
                print("\ndetail (те же заголовки браузера):")
                await hit(s, "7 detail b2b + куки аккаунта", b2b, {**H, "cookie": full_ck})
                await hit(s, "8 detail b2b + куки браузера", b2b, {**H, "cookie": br_cookie})
                await hit(s, "9 detail без b2b + анонимные куки", plain, {**H, "cookie": anon_ck})
            for i in range(1, WATCH_MIN // 5 + 1):
                await asyncio.sleep(300)
                print(f"\n+{i * 5} мин — живы ли пойманные заголовки:")
                await run_variants(s, url, H, br_cookie, full_ck, anon_ck, antibot)
    finally:
        await engine.dispose()


asyncio.run(main())
