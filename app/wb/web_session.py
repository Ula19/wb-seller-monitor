"""Веб-сессия WB через браузер: логин аккаунта, поддержание его кук, выпуск антибот-куки.

Две разные вещи:
- куки АККАУНТА (storageState после логина) — живут неделями, нужны бизнес-ценам;
- антибот-кука x_wbaas_token — живёт МИНУТЫ (проверено 2026-11-03: из анонимного
  браузера < 5 мин, из залогиненного 10–15), выпускается анонимным браузером без
  участия аккаунта. Без неё веб отвечает 498, без заголовков браузера — 403.
Антибот WB ловит автоматизацию, поэтому браузер — patchright (стелс-форк Playwright)
+ headful под Xvfb + выделенный прокси (прямой IP сервера палится).
"""
import asyncio
import json
import logging
import random
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
SELLER_URL = "https://www.wildberries.ru/seller/{}"
# служебные заголовки запроса браузера, которые curl ставит сам (cookie — свои)
SKIP_HEADERS = {"host", "content-length", "connection", "cookie"}
CALLBACK_WAIT_SEC = 45  # после кода: ждём callback (и экран согласия WB ID у нового аккаунта)


class LoginFailed(Exception):
    """Код введён, но вход не завершился; текст — для пользователя (почему)."""


def web_proxy() -> str | None:
    """Выделенный прокси для веб-сессии: wb_web_proxy, иначе первый из wb_proxies."""
    if settings.wb_web_proxy.strip():
        return settings.wb_web_proxy.strip()
    first = [x.strip() for x in settings.wb_proxies.split(",") if x.strip()]
    return first[0] if first else None


class ProxyDown(Exception):
    """Сеть до WB через прокси аккаунта не работает (таймаут, обрыв, 407). Сессия при этом цела —
    не relogin, а «прокси не отвечает»."""


async def _open_home(page) -> None:
    """Главная WB; сбой навигации (net::ERR_PROXY…, ERR_TUNNEL…, таймаут) = прокси не отвечает."""
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded")
    except Exception as e:
        raise ProxyDown(f"{type(e).__name__}: {str(e).splitlines()[0][:160]}") from e


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


def account_cookies(storage_state: dict | str | None) -> str:
    """Только куки аккаунта для www (без антибота — он выпускается отдельно и свежий)."""
    if not storage_state:
        return ""
    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    return "; ".join(
        f"{c['name']}={c['value']}"
        for c in storage_state.get("cookies", [])
        if _for_www(c) and c["name"].startswith(ACCOUNT_COOKIE_PREFIXES)
    )


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


async def _profile(page) -> dict | None:
    """Имя, телефон, процент выкупа — в ТОМ ЖЕ браузере, он уже на сайте (personalinfo за антиботом).
    Не вышло — None: вход и рефреш от этого не ломаются."""
    from app.wb.account_api import PROFILE_JS, parse_profile  # account_api сам импортирует этот модуль
    try:
        if not page.url.startswith(HOME_URL):
            await page.goto(HOME_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
        return parse_profile(await asyncio.wait_for(page.evaluate(PROFILE_JS), 20))
    except Exception as e:
        log.warning("профиль WB не получен: %s: %s", type(e).__name__, str(e)[:200])
        return None


async def login(phone: str, get_code, proxy: str | None = None) -> dict | None:
    """Логин аккаунта WB по телефону. get_code() — корутина, возвращает код из пуша.
    proxy — прокси аккаунта (нет — общий web_proxy()).

    Возвращает storageState (банку кук) или None, если до кода не дошли (форма, код не пришёл).
    LoginFailed — код введён, а вход не завершился (текст — причина). ProxyDown — прокси не отвечает.
    """
    from patchright.async_api import async_playwright

    proxy = _pw_proxy(proxy or web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(locale="ru-RU")
            page = await ctx.new_page()
            # прогрев: сначала главная (антибот-куки), потом строгая страница логина
            await _open_home(page)
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
            state = await _finish_login(ctx, page)
            from app.wb.account_api import PROFILE_KEY
            state[PROFILE_KEY] = await _profile(page)  # keeper заберёт и не сохранит в сессию
            return state
        finally:
            await browser.close()


async def _wait_callback(page) -> bool:
    """Код введён: ждём редирект на /wb-id/callback. Новому аккаунту WB ID показывает экран согласия
    («Wildberries получит доступ к данным WB ID … Принять | Отклонить», oauth2/authorize?prompt=consent) —
    жмём «Принять» один раз, как человек, с паузой. «Отклонить» не трогаем."""
    accept = page.get_by_role("button", name="Принять")
    clicked = False
    for _ in range(CALLBACK_WAIT_SEC):
        if "/wb-id/callback" in page.url:
            return True
        if not clicked:
            try:
                visible = await accept.is_visible()
            except Exception:  # страница в переходе — проверим на следующем шаге
                visible = False
            if visible:
                await asyncio.sleep(random.uniform(1, 2))
                await accept.click()
                clicked = True
                log.info("веб-логин: экран согласия WB ID — принял")
        await asyncio.sleep(1)
    return False


async def _finish_login(ctx, page) -> dict:
    """После ввода кода: callback (+ согласие) → куки сайта. Не вышло — LoginFailed с причиной по кукам."""
    if not await _wait_callback(page):
        log.warning("веб-логин: редиректа на callback нет, жду куки сайта")
    state = await _wait_www_session(ctx, page)
    if is_logged(state):
        log.info("веб-логин: успех, куки аккаунта получены")
        return state
    try:  # следующий незнакомый экран будет виден в логе сразу
        text = " ".join((await asyncio.wait_for(page.inner_text("body"), 5)).split())[:200]
    except Exception as e:
        text = f"<текст не прочитан: {type(e).__name__}>"
    log.warning("веб-логин: вход не завершён, страница %s: %s", page.url, text)
    sso = any(c.get("name") == "wbid-refresh" and "id.wb.ru" in c.get("domain", "") for c in state.get("cookies", []))
    raise LoginFailed("код принят, но WB не завершил вход на сайт — попробуйте ещё раз" if sso
                      else "код не принят — проверьте код")


async def refresh(storage_state: dict | str, proxy: str | None = None) -> dict | None:
    """Поддержание сессии: заходим на сайт с куками, возвращаем обновлённую банку.

    None — если сессия протухла (разлогинило). ProxyDown — прокси не отвечает (сессия не тронута).
    Зовётся по таймеру и при 401/403.
    """
    from patchright.async_api import async_playwright

    if isinstance(storage_state, str):
        storage_state = json.loads(storage_state)
    storage_state = {k: v for k, v in storage_state.items() if k in ("cookies", "origins")}
    proxy = _pw_proxy(proxy or web_proxy())
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, proxy=proxy, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(locale="ru-RU", storage_state=storage_state)
            page = await ctx.new_page()
            await _open_home(page)
            await asyncio.sleep(5)
            state = await ctx.storage_state()
            # по кукам сайта, а не по слову «Войти» — оно есть и на залогиненной странице
            if not is_logged(state):
                log.warning("веб-рефреш: сессия протухла (кук сайта нет)")
                return None
            log.info("веб-рефреш: сессия жива, куки обновлены")
            from app.wb.account_api import PROFILE_KEY
            state[PROFILE_KEY] = await _profile(page)  # имя/телефон/выкуп обновляются вместе с сессией
            return state
        finally:
            await browser.close()


MINT_WAIT_SEC = 45  # сколько ждём запрос каталога в браузере
CLOSE_TIMEOUT_SEC = 15  # закрытие браузера/драйвера не дольше этого


async def _bounded(coro, what: str) -> None:
    """Ждём не дольше CLOSE_TIMEOUT_SEC; зависло/упало — пишем в лог и идём дальше."""
    try:
        await asyncio.wait_for(coro, CLOSE_TIMEOUT_SEC)
    except Exception as e:
        log.warning("антибот: %s не удалось: %s", what, type(e).__name__)


async def mint_antibot(supplier_id: int) -> dict | None:
    """Свежая антибот-кука: анонимный браузер открывает страницу продавца и ловит
    настоящий запрос каталога — его куки (x_wbaas_token…) и заголовки браузера.

    Аккаунт НЕ участвует (контекст без кук). Возвращает {"cookie", "headers"} или None.
    """
    from patchright.async_api import async_playwright

    got: list = []

    async def on_resp(r):
        if "u-catalog/sellers" in r.url and "/catalog" in r.url and r.status == 200:
            got.append(await r.request.all_headers())

    stage = "запуск браузера"
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=False, proxy=_pw_proxy(web_proxy()), args=LAUNCH_ARGS)
        try:
            stage = "открытие страницы"
            page = await (await browser.new_context(locale="ru-RU")).new_page()
            page.on("response", on_resp)
            # не ждём загрузку страницы (через прокси бывает >30с) — ждём сам запрос каталога
            await page.goto(SELLER_URL.format(supplier_id), wait_until="commit", timeout=MINT_WAIT_SEC * 1000)
            stage = "ожидание каталога (прокрутка)"
            for _ in range(MINT_WAIT_SEC):  # антибот решается JS-ом, каталог приходит через секунды
                if got:
                    break
                try:
                    # у wheel нет таймаута: занятая JS-ом страница его не отпустит —
                    # от вечного зависания страхует общий таймаут выпуска в web_client
                    await page.mouse.wheel(0, 3000)
                except Exception:
                    pass  # страница ещё грузится — крутить нечего
                await asyncio.sleep(1)
            if not got:  # медленный прокси или страница-проверка WB — по заголовку видно
                stage = "чтение заголовка"
                try:
                    title = await page.title()
                except Exception:
                    title = "?"
                log.warning("антибот: запрос каталога не пойман за %dс (продавец %s), страница %s «%s»",
                            MINT_WAIT_SEC, supplier_id, page.url, title[:80])
        finally:
            # закрытие идёт через процесс браузера, зависшая страница его не держит; но и
            # его ограничиваем — finally выполняется и при отмене по таймауту
            await _bounded(browser.close(), "закрытие браузера")
    except asyncio.CancelledError:
        log.warning("антибот: выпуск прерван по таймауту на шаге «%s» (продавец %s)", stage, supplier_id)
        raise
    finally:
        await _bounded(pw.stop(), "остановка playwright")
    if not got:
        return None
    hh = got[0]
    cookie = hh.get("cookie", "")
    if "x_wbaas_token" not in cookie:
        log.warning("антибот: в запросе браузера нет x_wbaas_token")
        return None
    headers = {k: v for k, v in hh.items() if not k.startswith(":") and k.lower() not in SKIP_HEADERS}
    return {"cookie": cookie, "headers": headers}


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
    acc = account_cookies(st)  # только аккаунт, без антибота и чужих доменов
    assert acc == "wbid-refresh=abc; wbx-validation-key=v", acc

    # вход после кода — на заглушках браузера: экран согласия, обычный callback, две причины неудачи
    from unittest.mock import patch

    SSO = {"name": "wbid-refresh", "value": "r", "domain": ".id.wb.ru"}
    WWW_OK = {"name": "wbx-validation-key", "value": "v", "domain": ".wildberries.ru"}

    class Btn:
        def __init__(self, page):
            self.page, self.clicks = page, 0

        async def is_visible(self):
            return self.page.consent and "/wb-id/callback" not in self.page.url

        async def click(self):
            self.clicks += 1
            self.page.url = "https://www.wildberries.ru/wb-id/callback?code=x"  # «Принять» → редирект
            self.page.cookies.append(WWW_OK)

    class Page:
        def __init__(self, url, cookies, consent=False):
            self.url, self.cookies, self.consent = url, cookies, consent
            self.btn, self.roles = Btn(self), []

        def get_by_role(self, role, name):
            self.roles.append((role, name))
            return self.btn

        async def goto(self, url, **kw):
            self.url = url

        async def inner_text(self, sel):
            return "Wildberries   получит доступ\n к данным WB ID " + "x" * 300

    class Ctx:
        def __init__(self, page):
            self.page = page

        async def storage_state(self):
            return {"cookies": list(self.page.cookies), "origins": []}

    async def nosleep(_):
        pass

    async def demo():
        with patch.object(asyncio, "sleep", nosleep):
            # новый аккаунт: экран согласия → «Принять» один раз → callback → куки www
            pg = Page("https://id.wb.ru/login/oauth2/authorize?prompt=consent", [SSO], consent=True)
            assert is_logged(await _finish_login(Ctx(pg), pg)) and pg.btn.clicks == 1
            assert pg.roles == [("button", "Принять")]  # «Отклонить» не ищем вовсе
            # уже входивший: сразу callback, куки есть — кнопку не жмём
            pg = Page("https://www.wildberries.ru/wb-id/callback?code=y", [SSO, WWW_OK])
            assert is_logged(await _finish_login(Ctx(pg), pg)) and pg.btn.clicks == 0
            # SSO-куки есть, www нет → «код принят, но…»; нет SSO → «код не принят»
            for cookies, want in (([SSO], "код принят, но WB не завершил вход на сайт — попробуйте ещё раз"),
                                  ([], "код не принят — проверьте код")):
                pg = Page("https://id.wb.ru/login?x=1", cookies)
                try:
                    await _finish_login(Ctx(pg), pg)
                    raise AssertionError("ждали LoginFailed")
                except LoginFailed as e:
                    assert str(e) == want, e
                assert pg.btn.clicks == 0

    with patch.object(log, "warning") as warn:
        asyncio.run(demo())
    last = warn.call_args_list[-1].args  # URL и первые 200 символов текста страницы — в лог
    assert last[1] == "https://www.wildberries.ru/" and last[2].startswith("Wildberries получит доступ к данным") \
        and len(last[2]) == 200, last
    print("ok")
