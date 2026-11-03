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
# в Docker под root chromium без песочницы не стартует; dev-shm мал — выносим в /tmp
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def web_proxy() -> str | None:
    """Выделенный прокси для веб-сессии: wb_web_proxy, иначе первый из wb_proxies."""
    if settings.wb_web_proxy.strip():
        return settings.wb_web_proxy.strip()
    first = [x.strip() for x in settings.wb_proxies.split(",") if x.strip()]
    return first[0] if first else None


def curl_proxy(url: str | None) -> str | None:
    """curl_cffi к socks5-прокси требует socks5h (remote DNS), иначе ошибка 97."""
    if url and url.startswith("socks5://"):
        return "socks5h://" + url[len("socks5://"):]
    return url


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


# куки, по которым WB узнаёт аккаунт. Без них остаются только антибот-куки
# (x_wbaas_token и т.п.) — запрос идёт как от анонима, цена = розница.
ACCOUNT_COOKIE_PREFIXES = ("wbid", "wbx-validation", "WILDAUTH")


def cookie_header(storage_state: dict | str | None, *, anonymous: bool = False) -> str:
    """Собирает заголовок Cookie для wildberries.ru из storageState (для curl_cffi).

    anonymous=True — без кук аккаунта (для розничного каталога).
    """
    if not storage_state:
        return ""
    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    pairs = [
        f"{c['name']}={c['value']}"
        for c in storage_state.get("cookies", [])
        if _for_www(c) and not (anonymous and c["name"].startswith(ACCOUNT_COOKIE_PREFIXES))
    ]
    return "; ".join(pairs)


def _for_www(cookie: dict) -> bool:
    """Кука уходит на www.wildberries.ru (как решил бы браузер по домену).

    SSO-куки id.wb.ru (wbid-refresh и т.п.) на www браузер не шлёт — и мы не шлём.
    """
    domain = cookie.get("domain", "").lstrip(".")
    return domain == WWW_HOST or WWW_HOST.endswith("." + domain)


WWW_HOST = "www.wildberries.ru"
# куки залогиненного аккаунта НА САЙТЕ (www) — их ставит callback после обмена кода.
# Одних SSO-кук id.wb.ru мало: сайт по ним аноним, бизнес-цен не будет.
WWW_SESSION_COOKIES = ("wbx-validation-key", "wbid-sdk-id-token", "wbid-sdk-refresh")


def is_logged(storage_state: dict | str | None) -> bool:
    """Залогинен ли аккаунт именно на сайте (www), а не только в SSO id.wb.ru."""
    if not storage_state:
        return False
    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    return any(c["name"] in WWW_SESSION_COOKIES and _for_www(c)
               for c in storage_state.get("cookies", []))


async def _wait_www_session(ctx, page) -> dict:
    """После callback ждём, пока сайт обменяет код на куки www (до ~45с).

    Раньше банку забирали сразу на callback — успевали только SSO-куки id.wb.ru,
    и сайт оставался анонимным. Если за 30с не появились — заходим на главную
    (она тоже доделывает вход) и ждём ещё.
    """
    for i in range(45):
        state = await ctx.storage_state()
        if is_logged(state):
            return state
        if i == 30:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
        await asyncio.sleep(1)
    names = sorted(f"{c['domain']}:{c['name']}" for c in state.get("cookies", []))
    log.warning("веб-логин: куки сайта не появились, есть только: %s", ", ".join(names))
    return state


async def login(phone: str, get_code) -> dict | None:
    """Логин аккаунта WB по телефону. get_code() — корутина, возвращает код из пуша.

    Возвращает storageState (банку кук) или None, если логин не удался.
    """
    from patchright.async_api import async_playwright

    proxy = _pw_proxy(web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy, args=LAUNCH_ARGS)
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
                log.warning("веб-логин: редиректа на callback нет (неверный код?), жду куки сайта")
            state = await _wait_www_session(ctx, page)
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
    proxy = _pw_proxy(web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy, args=LAUNCH_ARGS)
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
    assert is_logged(st) is False  # только SSO-кука — на сайте ещё аноним
    sso = {"cookies": [{"name": "wbid-refresh", "value": "r", "domain": ".id.wb.ru"},
                       {"name": "x_wbaas_token", "value": "t", "domain": "www.wildberries.ru"}]}
    assert cookie_header(sso) == "x_wbaas_token=t"  # куки id.wb.ru на www не уходят
    assert is_logged(sso) is False
    assert is_logged({"cookies": []}) is False
    assert cookie_header(json.dumps(st)) == h  # строка и dict дают одно
    st["cookies"].append({"name": "wbx-validation-key", "value": "v", "domain": ".wildberries.ru"})
    assert is_logged(st) is True  # wbx-validation-key на .wildberries.ru = вход на сайте
    anon = cookie_header(st, anonymous=True)
    assert anon == "x_wbaas_token=xyz", anon  # аноним: только антибот, без кук аккаунта
    assert curl_proxy("socks5://h:1") == "socks5h://h:1" and curl_proxy(None) is None
    print("ok")
