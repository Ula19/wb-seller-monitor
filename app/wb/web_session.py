"""Веб-сессия WB: логин бизнес-аккаунта через headless-браузер и поддержание кук.

Банка кук (storageState) — это кредентиал: с ней HTTP-клиент ходит в веб-каталог и
detail БЕЗ guardium. Антибот WB ловит автоматизацию, поэтому браузер — patchright
(стелс-форк Playwright) + headful под Xvfb + выделенный прокси (прямой IP сервера
палится). Логин редкий (сессия живёт неделями), код приходит пушем в приложение WB.
"""
import asyncio
import json
import logging
from urllib.parse import urlparse

from app.config import settings

# patchright тянет за собой браузер — импортируем лениво внутри login/refresh,
# чтобы cookie_header/is_logged (их зовёт HTTP-клиент) работали без браузера.

log = logging.getLogger(__name__)

HOME_URL = "https://www.wildberries.ru/"
LOGIN_URL = "https://www.wildberries.ru/security/login"
# поле номера — обёртка <ui-field-phone id=phone-number>, реальный input внутри
PHONE_SEL = "#phone-number input, input#phone-number, [data-test-id=phone_input] input"


def _web_proxy() -> str | None:
    """Выделенный прокси для веб-сессии: wb_web_proxy, иначе первый из wb_proxies."""
    if settings.wb_web_proxy.strip():
        return settings.wb_web_proxy.strip()
    first = [x.strip() for x in settings.wb_proxies.split(",") if x.strip()]
    return first[0] if first else None


def _pw_proxy(url: str | None):
    """URL прокси -> аргумент proxy для Playwright (socks-auth PW не умеет)."""
    if not url:
        return None
    u = urlparse(url)
    scheme = "socks5" if u.scheme.startswith("socks") else u.scheme
    d = {"server": f"{scheme}://{u.hostname}:{u.port}"}
    if u.username and not scheme.startswith("socks"):
        d["username"], d["password"] = u.username, u.password
    return d


def cookie_header(storage_state: dict | str | None) -> str:
    """Собирает заголовок Cookie для wildberries.ru из storageState (для curl_cffi)."""
    if not storage_state:
        return ""
    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    pairs = [
        f"{c['name']}={c['value']}"
        for c in storage_state.get("cookies", [])
        if "wildberries" in c.get("domain", "") or "wb.ru" in c.get("domain", "")
    ]
    return "; ".join(pairs)


def is_logged(storage_state: dict | str | None) -> bool:
    """Есть ли в банке кук признаки залогиненного аккаунта (refresh/validation)."""
    header = cookie_header(storage_state)
    return any(k in header for k in ("wbid-refresh", "wbid-validation", "wbx-validation"))


async def login(phone: str, get_code) -> dict | None:
    """Логин аккаунта WB по телефону. get_code() — корутина, возвращает код из пуша.

    Возвращает storageState (банку кук) или None, если логин не удался.
    """
    from patchright.async_api import async_playwright

    proxy = _pw_proxy(_web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy)
        try:
            ctx = await browser.new_context(locale="ru-RU")
            page = await ctx.new_page()
            # прогрев: сначала главная (антибот-куки), потом строгая страница логина
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(6)
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            try:
                await page.wait_for_selector("#phone-number", timeout=40000)
            except Exception:
                log.warning("веб-логин: форма не появилась (антибот?)")
                return None
            field = page.locator(PHONE_SEL).first
            await field.click()
            await field.type(phone, delay=100)
            await asyncio.sleep(0.5)
            await page.click("button[type=submit]")
            await asyncio.sleep(6)
            code = await get_code()
            if not code:
                log.warning("веб-логин: код не получен")
                return None
            await page.keyboard.type(str(code).strip(), delay=120)
            # ждём редирект на callback = успех
            try:
                await page.wait_for_url("**/wb-id/callback**", timeout=30000)
            except Exception:
                await asyncio.sleep(6)
            state = await ctx.storage_state()
            if is_logged(state):
                log.info("веб-логин: успех, куки аккаунта получены")
                return state
            log.warning("веб-логин: куки аккаунта не появились (неверный код?)")
            return None
        finally:
            await browser.close()


async def refresh(storage_state: dict | str) -> dict | None:
    """Поддержание сессии: заходим на сайт с куками, возвращаем обновлённую банку.

    None — если сессия протухла (разлогинило). Зовётся по таймеру и при 401/403.
    """
    from patchright.async_api import async_playwright

    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    proxy = _pw_proxy(_web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy)
        try:
            ctx = await browser.new_context(locale="ru-RU", storage_state=storage_state)
            page = await ctx.new_page()
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            body = (await page.inner_text("body"))[:300]
            state = await ctx.storage_state()
            if "Войти" in body and not is_logged(state):
                log.warning("веб-рефреш: сессия протухла")
                return None
            log.info("веб-рефреш: сессия жива, куки обновлены")
            return state
        finally:
            await browser.close()


if __name__ == "__main__":  # self-check чистых функций (без браузера)
    st = {"cookies": [
        {"name": "wbid-refresh", "value": "abc", "domain": ".wildberries.ru"},
        {"name": "x_wbaas_token", "value": "xyz", "domain": "www.wildberries.ru"},
        {"name": "other", "value": "1", "domain": "example.com"},
    ]}
    h = cookie_header(st)
    assert "wbid-refresh=abc" in h and "x_wbaas_token=xyz" in h, h
    assert "other=1" not in h, "чужой домен не должен попасть в Cookie"
    assert is_logged(st) is True
    assert is_logged({"cookies": []}) is False
    assert cookie_header(json.dumps(st)) == h  # строка и dict дают одно
    print("ok")
