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
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
DELIVERY_URL = "https://www.wildberries.ru/lk/myorders/delivery"  # тут сайт показывает «Код получения NNN NNN»
CODE_WAIT_SEC = 20  # SPA дорисовывает блок с кодом — ждём до стольких секунд
SELECT_WAIT_SEC = 10  # экран «В какой аккаунт хотите войти?» дорисовывается не сразу
SELECT_TITLE = "В какой аккаунт хотите войти?"
PHONE_ON_PAGE = re.compile(r"\+7 \d{3} \d{3}-\d{2}-\d{2}")  # «+7 988 156-78-17» на экране выбора
# почему визит не удался — keeper пишет в wb_accounts.visit_error, сайт показывает
VISIT_SESSION = "сессия сайта WB истекла — войдите заново"
VISIT_PAGE = "WB не открыл страницу — проверьте прокси"
VISIT_NO_CODE = "на странице WB кода нет — товаров в ПВЗ нет?"
# служебные заголовки запроса браузера, которые curl ставит сам (cookie — свои)
SKIP_HEADERS = {"host", "content-length", "connection", "cookie"}
# вход завершён, когда сайт доделал обмен кода WB ID на свою сессию: callback → (переход на returnUrl) →
# webapi/security/login/data → webapi/security/afterauth. Куки www — НЕ признак: у аккаунта со старыми куками они есть
# ещё до входа, и ранний goto/закрытие браузера обрывает обмен (визит Ани 2026-11-09: «успех» через 1,3 с)
AFTERAUTH_PATH = "/webapi/security/afterauth"
LOGIN_DONE_WAIT_SEC = 60  # всего ждём завершения входа (главный признак — afterauth)
URL_FALLBACK_SEC = 15  # afterauth нет, а страница стоит на www столько секунд — засчитываем по URL (и ещё 3 с)
RETURN_WAIT_SEC = 10  # после входа сайт сам уходит с callback на returnUrl — ждём, прежде чем решать про goto
# страница кода WB ID (тексты из id.wb.ru/login/assets/locales/wb/ru.json)
SEND_CODE_PATH = "/auth/flow/send-code"  # ответ: {"confirmationType": "PUSH"|"SMS", "denyResendUntil": "<unix>"}
CODE_INPUT_SELS = ("input[autocomplete=one-time-code]", "input[inputmode=numeric]", "input")
RESEND_NAME = "Получить новый код"
CHOOSE_TITLE = "Куда отправить код?"
VIA_OPTION = {"sms": "На телефон в СМС", "push": "В колокольчик на сайт и в приложение Wildberries"}
TOO_MANY = "Слишком много попыток"


class LoginFailed(Exception):
    """Код введён, но вход не завершился; текст — для пользователя (почему)."""


class LoginCancelled(Exception):
    """Вход отменили с сайта — бросает get_code; login() закрывает браузер."""


@dataclass(frozen=True)
class Resend:
    """get_code вернул просьбу прислать код заново вместо кода."""
    via: str  # push | sms


def parse_send_code(d) -> tuple[str | None, datetime | None]:
    """Ответ send-code → (куда ушёл код PUSH/SMS, до какого времени WB не даст повторить)."""
    if not isinstance(d, dict):
        return None, None
    via = d.get("confirmationType")
    until = str(d.get("denyResendUntil") or "")
    return (str(via)[:8] if via else None,
            datetime.fromtimestamp(int(until), timezone.utc) if until.isdigit() else None)


def _send_code_listener(on_sent):
    """Обработчик page.on("response"): ответ send-code → on_sent(via, until). Сбой — только в лог."""
    async def handler(resp):
        if SEND_CODE_PATH not in resp.url:
            return
        try:
            via, until = parse_send_code(await resp.json())
            log.info("веб-логин: WB отправил код: %s, повтор после %s", via, until)
            await on_sent(via, until)
        except Exception as e:
            log.warning("веб-логин: ответ send-code не разобран: %s: %s", type(e).__name__, str(e)[:200])
    return handler


async def _page_text(page) -> str:
    try:
        return " ".join((await asyncio.wait_for(page.inner_text("body"), 5)).split())
    except Exception as e:
        return f"<текст не прочитан: {type(e).__name__}>"


async def _first_visible(locator):
    """Первый видимый элемент локатора или None (страница в переходе — тоже None)."""
    try:
        for i in range(await locator.count()):
            if await locator.nth(i).is_visible():
                return locator.nth(i)
    except Exception:
        pass
    return None


async def _focus_code_input(page) -> None:
    """Клик в поле кода: после первой отправки фокус там ставит сам WB, после повторной — он на нажатой кнопке."""
    for sel in CODE_INPUT_SELS:
        if el := await _first_visible(page.locator(sel)):
            await el.click()
            return
    log.warning("веб-логин: поле кода не нашёл — ввожу без клика")


async def _resend(page, via: str) -> None:
    """«Получить новый код» → (если WB спросил) «Куда отправить код?» → нужный вариант. Куда ушло на самом деле —
    из ответа send-code (_send_code_listener). Кнопки нет — не падаем, ждём код дальше."""
    btn = None
    for role in ("button", "link"):
        if btn := await _first_visible(page.get_by_role(role, name=RESEND_NAME, exact=True)):
            break
    if btn is None:
        log.warning("веб-логин: кнопки «%s» нет (таймер ещё идёт?) — жду код дальше", RESEND_NAME)
        return
    await btn.click()
    did = f"«{RESEND_NAME}»"
    await asyncio.sleep(3)
    if await _first_visible(page.get_by_text(CHOOSE_TITLE)):
        if opt := await _first_visible(page.get_by_text(VIA_OPTION[via], exact=True)):
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await opt.click()
            did += f" → «{VIA_OPTION[via]}»"
            await asyncio.sleep(2)
        else:
            did += f" → выбор есть, варианта «{VIA_OPTION[via]}» нет"
    else:
        did += " (выбора нет — тем же способом)"
    text = await _page_text(page)
    log.info("веб-логин: повторный код: нажал %s; страница: %s", did, text[:200])
    if TOO_MANY in text:
        raise LoginFailed(f"WB: «{TOO_MANY}» — подождите и начните вход заново")


async def _enter_code(page, get_code) -> "_AfterAuth | None":
    """Ждём код с сайта (или просьбу прислать заново) и вводим его. → слушатель afterauth (повешен прямо перед
    вводом кода) или None — код не пришёл."""
    while True:
        got = await get_code()  # у каждого ожидания свой таймаут — после повторной отправки отсчёт заново
        if isinstance(got, Resend):
            await _resend(page, got.via)
            continue
        if not got:
            log.warning("веб-логин: код не получен")
            return None
        await _focus_code_input(page)
        after = _AfterAuth(page)
        await page.keyboard.type(str(got).strip(), delay=120)
        return after


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


async def login(phone: str, get_code, proxy: str | None = None, on_sent=None) -> dict | None:
    """Логин аккаунта WB по телефону. get_code() — корутина: код, Resend(via) (прислать заново) или
    LoginCancelled (отмена). on_sent(via, until) — куда WB отправил код и когда можно повторить.
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
            if on_sent:
                page.on("response", _send_code_listener(on_sent))
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
            after = await _enter_code(page, get_code)
            if after is None:
                return None
            state = await _finish_login(ctx, page, after)
            from app.wb.account_api import PROFILE_KEY
            state[PROFILE_KEY] = await _profile(page)  # keeper заберёт и не сохранит в сессию
            return state
        finally:
            await browser.close()


class _AfterAuth:
    """Ловит ответ webapi/security/afterauth — сайт закончил вход. Вешать ПЕРЕД действием (ввод кода, выбор
    аккаунта): ответ приходит через секунды, раньше — от обычной загрузки страниц — ловить нельзя."""

    def __init__(self, page):
        self.done = False
        page.on("response", self._seen)

    def _seen(self, resp) -> None:
        if AFTERAUTH_PATH in resp.url:
            self.done = True


def _site_page(url: str) -> bool:
    """Страница сайта, а не промежуточный шаг входа."""
    return _on_www(url) and "/wb-id/callback" not in url and "/security/login" not in url


async def _wait_site_login(page, after: _AfterAuth) -> bool:
    """Ждём завершения входа: afterauth (главный признак). Запасной — страница сайта, но не сразу: обмен
    доделывается уже на ней (у Ани: callback → ЛК → login/data → afterauth), поэтому засчитываем, только если
    afterauth не пришёл за URL_FALLBACK_SEC, и даём ещё 3 с. По пути — экран согласия (новый аккаунт): жмём
    «Принять» один раз, как человек, с паузой; «Отклонить» не трогаем. Застряли на callback — главная доделывает вход."""
    accept = page.get_by_role("button", name="Принять")
    clicked, on_site = False, None
    for i in range(LOGIN_DONE_WAIT_SEC):
        if after.done:
            return True
        if _site_page(page.url):
            on_site = i if on_site is None else on_site
            if i - on_site >= URL_FALLBACK_SEC:
                log.warning("веб-логин: afterauth не пришёл, страница сайта %s — считаю вход завершённым", page.url)
                await asyncio.sleep(3)
                return True
        else:
            on_site = None
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
        if i == 30 and "/wb-id/callback" in page.url:
            await page.goto(HOME_URL, wait_until="domcontentloaded")
        await asyncio.sleep(1)
    return False


async def _finish_login(ctx, page, after: _AfterAuth) -> dict:
    """После ввода кода / выбора аккаунта: ждём завершения входа сайтом и только потом берём куки.
    Не вышло — LoginFailed с причиной по кукам."""
    done = await _wait_site_login(page, after)
    state = await ctx.storage_state()
    if done and is_logged(state):
        log.info("веб-логин: успех, вход завершён%s", "" if after.done else " (по странице сайта)")
        return state
    text = (await _page_text(page))[:200]  # следующий незнакомый экран будет виден в логе сразу
    log.warning("веб-логин: вход не завершён, страница %s: %s", page.url, text)
    if TOO_MANY in text:
        raise LoginFailed(f"WB: «{TOO_MANY}» — подождите и начните вход заново")
    sso = any(c.get("name") == "wbid-refresh" and "id.wb.ru" in c.get("domain", "") for c in state.get("cookies", []))
    raise LoginFailed("код принят, но WB не завершил вход на сайт — попробуйте ещё раз" if sso
                      else "код не принят — проверьте код")


def _on_www(url: str) -> bool:
    return urlparse(url).hostname == WWW_HOST


async def _select_account(page, phone: str | None) -> bool:
    """Экран WB ID «В какой аккаунт хотите войти?»: клик по номеру этого аккаунта (по последним 4 цифрам),
    не нашёлся — по первому номеру в списке. «Нет нужного аккаунта» не трогаем. False — экрана нет."""
    for _ in range(SELECT_WAIT_SEC):
        if await _first_visible(page.get_by_text(SELECT_TITLE)):
            break
        await asyncio.sleep(1)
    else:
        return False
    digits = re.sub(r"\D", "", phone or "")
    tail = f"{digits[-4:-2]}-{digits[-2:]}" if len(digits) >= 4 else None
    el = await _first_visible(page.get_by_text(tail)) if tail else None
    picked = f"…{tail}"
    if el is None:
        el = await _first_visible(page.get_by_text(PHONE_ON_PAGE))
        picked = "первый номер в списке"
    if el is None:
        return False
    await asyncio.sleep(random.uniform(1, 2))
    await el.click()
    log.info("визит: WB ID попросил выбрать аккаунт — выбрал %s", picked)
    return True


async def _open_lk(ctx, page, phone: str | None) -> str | None:
    """Открыть ЛК (страницу доставок) на www — заодно проверка, что сессия САЙТА жива (куки есть и у анонима,
    главная открывается и анонимно). WB ID просит выбрать аккаунт — выбираем и доводим вход, как после кода.
    → None — ЛК открыт; иначе причина (VISIT_SESSION / VISIT_PAGE)."""
    try:
        await page.goto(DELIVERY_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)  # редирект на id.wb.ru бывает и после загрузки
    except Exception as e:
        log.warning("визит: ЛК не открылся: %s: %s", type(e).__name__, str(e)[:200])
        return VISIT_PAGE
    if _on_www(page.url):
        return None
    if urlparse(page.url).hostname == "id.wb.ru":
        try:
            after = _AfterAuth(page)  # до клика по номеру
            if await _select_account(page, phone):
                await _finish_login(ctx, page, after)  # согласие → callback → сайт доделывает вход (afterauth)
                for _ in range(RETURN_WAIT_SEC):  # сайт сам уходит на returnUrl — не перебиваем его goto
                    if _site_page(page.url):
                        break
                    await asyncio.sleep(1)
                if urlparse(page.url).path.rstrip("/") != urlparse(DELIVERY_URL).path:
                    await page.goto(DELIVERY_URL, wait_until="domcontentloaded")
                    await asyncio.sleep(3)
                if _on_www(page.url):
                    return None
        except Exception as e:  # LoginFailed и сбои навигации — одна причина: сайт не пустил
            log.warning("визит: после выбора аккаунта вход не завершён: %s: %s", type(e).__name__, str(e)[:200])
        log.warning("визит: сессия сайта истекла, страница %s: %s", page.url, (await _page_text(page))[:200])
        return VISIT_SESSION
    log.warning("визит: ЛК увёл на %s: %s", page.url, (await _page_text(page))[:200])
    return VISIT_PAGE


async def _visit(page, want_code: bool) -> dict:
    """ЛК уже открыт (_open_lk): код получения со страницы доставок и история баланса (mywallet) — то, что WB
    считает/отдаёт только в браузере. Части независимы: сбой одной не мешает другой и не ломает рефреш."""
    from app.wb.account_api import WALLET_JS, parse_pickup_code
    out = {"pickup_code": None, "balance": None, "error": None}
    if want_code:
        text = ""
        try:
            for _ in range(CODE_WAIT_SEC):
                await asyncio.sleep(1)
                text = await page.evaluate("document.body ? document.body.innerText : ''")
                if code := parse_pickup_code(text):
                    out["pickup_code"] = code
                    break
        except Exception as e:
            log.warning("визит: код получения не прочитан: %s: %s", type(e).__name__, str(e)[:200])
        if not out["pickup_code"]:
            i = (text or "").find("Доставк")  # начало блока заказов, иначе начало страницы
            log.info("визит: кода получения нет, страница %s: %s", page.url,
                     " ".join((text or "")[max(i, 0):max(i, 0) + 400].split())[:200])
            out["error"] = VISIT_NO_CODE
    try:
        res = await asyncio.wait_for(page.evaluate(WALLET_JS), 20) or {}
        if res.get("ok"):
            out["balance"] = res.get("data")
        else:
            log.warning("визит: история баланса не получена: HTTP %s: %s", res.get("status"), (res.get("text") or "")[:200])
    except Exception as e:
        log.warning("визит: история баланса не получена: %s: %s", type(e).__name__, str(e)[:200])
    return out


async def refresh(storage_state: dict | str, proxy: str | None = None, *,
                  visit: bool = False, want_code: bool = False, phone: str | None = None) -> dict | None:
    """Поддержание сессии: заходим на сайт с куками, возвращаем обновлённую банку.

    None — если сессия протухла (разлогинило). ProxyDown — прокси не отвечает (сессия не тронута).
    Зовётся по таймеру и при 401/403. visit — заодно визит (_visit): история баланса и, если want_code, код получения.
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
            # кук сайта нет вовсе — разлогинило (relogin); по слову «Войти» нельзя — оно есть и у залогиненного
            if not is_logged(await ctx.storage_state()):
                log.warning("веб-рефреш: сессия протухла (кук сайта нет)")
                return None
            from app.wb.account_api import PROFILE_KEY, VISIT_KEY
            profile = await _profile(page)  # имя/телефон/выкуп обновляются вместе с сессией
            # жива ли сессия САЙТА — по тому, открылся ли ЛК на www (главная открывается и анонимно).
            # Не открылся — НЕ relogin: HTTP-опрос по Bearer при этом жив; причина уходит на сайт
            error = await _open_lk(ctx, page, phone)
            extra = {"pickup_code": None, "balance": None, "error": error}
            if error:
                log.warning("веб-рефреш: куки есть, но ЛК не открылся — %s (бизнес-цены могут идти анонимно)", error)
            else:
                log.info("веб-рефреш: сессия жива, ЛК открылся")
                if visit:
                    extra = await _visit(page, want_code)
            state = await ctx.storage_state()  # после визита: куки могли обновиться
            state[PROFILE_KEY] = profile
            state[VISIT_KEY] = extra
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

    # вход после кода — на заглушках браузера с часами: asyncio.sleep двигает время, страница живёт по расписанию
    from types import SimpleNamespace
    from unittest.mock import patch

    SSO = {"name": "wbid-refresh", "value": "r", "domain": ".id.wb.ru"}
    WWW_OK = {"name": "wbx-validation-key", "value": "v", "domain": ".wildberries.ru"}
    CALLBACK = "https://www.wildberries.ru/wb-id/callback?code=z"
    SELECT = "https://id.wb.ru/login/oauth2/authorize?prompt=select_account"
    clock = SimpleNamespace(t=0.0, pages=[])

    async def tick(s):
        clock.t += s
        for pg in clock.pages:
            pg.run_due()

    class Node:
        def __init__(self, pg, key, visible=lambda: True, on_click=None):
            self.pg, self.key, self.visible, self.on_click = pg, key, visible, on_click

        async def is_visible(self):
            return self.visible()

        async def click(self):
            self.pg.clicks.append((clock.t, self.key))
            if self.on_click:
                self.on_click()

    class Loc:
        def __init__(self, els):
            self.els = els

        async def count(self):
            return len(self.els)

        def nth(self, i):
            return self.els[i]

    class FakePage:
        """Сайт WB в миниатюре. after_click — что делает WB после «Принять»/клика по номеру: список (через сколько
        секунд, действие): "site:<url>" — сам перешёл, "afterauth" — ответ afterauth, "cookies" — куки www."""
        def __init__(self, url="", cookies=(), consent=False, redirect=None, phones=(), after_click=(),
                     texts=("",), wallet=None, goto_fails=False):
            self.url, self.cookies, self.consent, self.redirect = url, list(cookies), consent, redirect
            self.phones, self.after_click, self.texts = list(phones), list(after_click), list(texts)
            self.wallet = wallet if wallet is not None else {"ok": True, "data": {"value": 1}}
            self.goto_fails, self.handlers, self.clicks, self.gotos, self.due = goto_fails, [], [], [], []
            self.selected = False
            clock.pages.append(self)

        def on(self, event, fn):
            self.handlers.append(fn)

        def run_due(self):
            for at, what in [d for d in self.due if d[0] <= clock.t]:
                self.due.remove((at, what))
                if what == "afterauth":
                    for h in self.handlers:
                        h(SimpleNamespace(url="https://www.wildberries.ru" + AFTERAUTH_PATH))
                elif what == "cookies":
                    self.cookies.append(WWW_OK)
                else:
                    self.url = what[len("site:"):]

        def _go_on(self):  # клик по «Принять» или номеру: WB ведёт на callback, дальше — по расписанию
            self.selected, self.consent, self.url = True, False, CALLBACK
            self.due += [(clock.t + dt, what) for dt, what in self.after_click]

        async def goto(self, url, **kw):
            if self.goto_fails:
                raise TimeoutError("goto")
            self.gotos.append((clock.t, url))
            self.url = self.redirect if (url == DELIVERY_URL and self.redirect and not self.selected) else url

        def get_by_role(self, role, name, exact=False):  # как Locator: is_visible/click прямо у него
            assert name == "Принять", name  # «Отклонить» не ищем вовсе
            return Node(self, "Принять", lambda: self.consent, self._go_on)

        def get_by_text(self, what, exact=False):
            if self.url != SELECT:
                return Loc([])
            if what == SELECT_TITLE:
                return Loc([Node(self, "title")])
            match = (lambda s: what.search(s)) if hasattr(what, "search") else (lambda s: what in s)
            return Loc([Node(self, ph, on_click=self._go_on) for ph in self.phones if match(ph)])

        async def evaluate(self, js):
            from app.wb.account_api import WALLET_JS
            if js == WALLET_JS:
                return self.wallet
            return self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]

        async def inner_text(self, sel):
            return "Wildberries   получит доступ\n к данным WB ID " + "x" * 300

    class Ctx:
        def __init__(self, pg):
            self.pg, self.saved_at = pg, []

        async def storage_state(self):
            self.saved_at.append(clock.t)
            return {"cookies": list(self.pg.cookies), "origins": []}

    async def nosleep(_):
        pass

    async def login_after_code(pg):
        """Как в login(): слушатель — перед вводом кода; код «ввели» — WB ведёт дальше без кликов."""
        after = _AfterAuth(pg)
        if not pg.consent:
            pg._go_on()
        ctx = Ctx(pg)
        return ctx, await _finish_login(ctx, pg, after)

    async def demo():
        with patch.object(asyncio, "sleep", tick):
            # новый аккаунт: экран согласия → «Принять» один раз → callback → ЛК → afterauth (куки — тогда же)
            pg = FakePage("https://id.wb.ru/login/oauth2/authorize?prompt=consent", [SSO], consent=True,
                          after_click=[(1, "site:https://www.wildberries.ru/"), (3, "cookies"), (3, "afterauth")])
            ctx, st = await login_after_code(pg)
            assert is_logged(st) and [k for _, k in pg.clicks] == ["Принять"]
            # перелогин со СТАРЫМИ куками www: страница на сайте через 1 с, afterauth через 3 с →
            # состояние берём только после afterauth (раньше «успех» был на первой же секунде)
            t0 = clock.t
            pg = FakePage("https://www.wildberries.ru/security/login", [SSO, WWW_OK],
                          after_click=[(1, "site:https://www.wildberries.ru/lk/myorders/delivery"), (3, "afterauth")])
            ctx, st = await login_after_code(pg)
            assert is_logged(st) and ctx.saved_at == [t0 + 3], (ctx.saved_at, t0)
            # afterauth так и не пришёл, страница на сайте с 1-й секунды → по URL через 15 с и ещё 3 с
            t0 = clock.t
            pg = FakePage("https://www.wildberries.ru/security/login", [SSO, WWW_OK],
                          after_click=[(1, "site:https://www.wildberries.ru/lk/myorders/delivery")])
            ctx, st = await login_after_code(pg)
            assert is_logged(st) and ctx.saved_at[-1] - t0 >= 1 + URL_FALLBACK_SEC + 3, (ctx.saved_at, t0)
            # SSO-куки есть, www нет → «код принят, но…»; нет SSO → «код не принят»
            for cookies, want in (([SSO], "код принят, но WB не завершил вход на сайт — попробуйте ещё раз"),
                                  ([], "код не принят — проверьте код")):
                pg = FakePage("https://id.wb.ru/login?x=1", cookies, consent=True)  # «Принять» нет — его не жмут
                pg.consent = False
                try:
                    await _finish_login(Ctx(pg), pg, _AfterAuth(pg))
                    raise AssertionError("ждали LoginFailed")
                except LoginFailed as e:
                    assert str(e) == want, e
                assert pg.clicks == []

    with patch.object(log, "warning") as warn, patch.object(log, "info"):
        asyncio.run(demo())
    last = warn.call_args_list[-1].args  # URL и первые 200 символов текста страницы — в лог
    assert last[1] == "https://id.wb.ru/login?x=1" and last[2].startswith("Wildberries получит доступ к данным") \
        and len(last[2]) == 200, last

    # повторная отправка кода и ввод: заглушка страницы кода WB ID
    class El:
        def __init__(self, pg, key, visible=True, on_click=None):
            self.pg, self.key, self.visible, self.on_click = pg, key, visible, on_click

        async def is_visible(self):
            return self.visible

        async def click(self):
            self.pg.focus = self.key
            self.pg.clicks.append(self.key)
            if self.on_click:
                self.on_click()


    class Kb:
        def __init__(self, pg):
            self.pg = pg

        async def type(self, text, delay=0):
            self.pg.typed.append((self.pg.focus, text))

    class CodePage:
        def __init__(self, choice=True, button=True, after_text="Введите код из СМС"):
            self.focus, self.clicks, self.typed, self.els = "button:" + RESEND_NAME, [], [], {}
            self.keyboard, self.text = Kb(self), "Введите код из уведомления"
            self.add("css:input", visible=False)  # скрытый input — не он
            self.add("css:input[inputmode=numeric]")
            if button:
                self.add("button:" + RESEND_NAME, on_click=lambda: choice and self.show_choice())
            self.after_text = after_text

        def add(self, key, **kw):
            self.els.setdefault(key, []).append(El(self, key, **kw))

        def on(self, event, fn):  # слушатель afterauth вешается перед вводом кода
            pass

        def show_choice(self):
            self.add("text:" + CHOOSE_TITLE)
            for v in VIA_OPTION.values():
                self.add("text:" + v, on_click=lambda: setattr(self, "text", self.after_text))

        def locator(self, sel):
            return Loc(self.els.get("css:" + sel, []))

        def get_by_role(self, role, name, exact=False):
            return Loc(self.els.get(f"{role}:{name}", []))

        def get_by_text(self, text, exact=False):
            return Loc(self.els.get("text:" + text, []))

        async def inner_text(self, sel):
            return self.text

    def codes(*answers):
        it = iter(answers)

        async def get_code():
            return next(it)
        return get_code

    async def demo2():
        with patch.object(asyncio, "sleep", nosleep):
            # с выбором: «Получить новый код» → «На телефон в СМС» → фокус в поле кода → ввод
            pg = CodePage()
            assert await _enter_code(pg, codes(Resend("sms"), " 1234 "))
            assert pg.clicks == ["button:" + RESEND_NAME, "text:" + VIA_OPTION["sms"], "css:input[inputmode=numeric]"]
            assert pg.typed == [("css:input[inputmode=numeric]", "1234")]  # код ушёл в поле, а не в кнопку
            # без выбора — ушло тем же способом, вариант не жмём; one-time-code главнее numeric
            pg = CodePage(choice=False)
            pg.add("css:input[autocomplete=one-time-code]")
            assert await _enter_code(pg, codes(Resend("push"), "5678"))
            assert pg.clicks == ["button:" + RESEND_NAME, "css:input[autocomplete=one-time-code]"]
            assert pg.typed == [("css:input[autocomplete=one-time-code]", "5678")]
            # кнопки нет (таймер) — не падаем, ждём код; поля нет — warning и ввод как есть
            pg = CodePage(button=False)
            pg.els.pop("css:input[inputmode=numeric]")
            assert await _enter_code(pg, codes(Resend("push"), "1111")) and pg.typed == [(pg.focus, "1111")]
            assert await _enter_code(CodePage(), codes(None)) is None  # код не пришёл
            # «Слишком много попыток» после повтора → LoginFailed с этим текстом
            pg = CodePage(after_text="Слишком много попыток — запросите код ещё раз")
            try:
                await _enter_code(pg, codes(Resend("push"), "1"))
                raise AssertionError("ждали LoginFailed")
            except LoginFailed as e:
                assert TOO_MANY in str(e) and not pg.typed
        # ответ send-code → on_sent; чужие ответы и битый JSON — мимо
        got = []

        async def on_sent(via, until):
            got.append((via, until))

        class Resp:
            def __init__(self, url, body):
                self.url, self.body = url, body

            async def json(self):
                if self.body is None:
                    raise ValueError("не json")
                return self.body
        h = _send_code_listener(on_sent)
        await h(Resp("https://id.wb.ru/wb-auth/v2/auth/flow/send-code", {"confirmationType": "SMS", "denyResendUntil": "1794220301"}))
        await h(Resp("https://id.wb.ru/other", {"confirmationType": "PUSH"}))
        await h(Resp("https://id.wb.ru/wb-auth/v2/auth/flow/send-code", None))
        assert got == [("SMS", datetime(2026, 11, 9, 10, 31, 41, tzinfo=timezone.utc))], got
        assert parse_send_code({}) == (None, None) and parse_send_code(None) == (None, None)

    with patch.object(log, "warning"), patch.object(log, "info"):
        asyncio.run(demo2())

    # визит: ЛК (с выбором аккаунта WB ID), код получения, история баланса; причины неудачи
    DONE_ON_LK = [(1, "site:" + DELIVERY_URL), (3, "afterauth")]  # как у Ани: сайт сам уходит в ЛК, afterauth — там

    async def lk(pg, want_code=True, phone="+7 988 156-78-17"):
        err = await _open_lk(Ctx(pg), pg, phone)
        return err, (await _visit(pg, want_code) if err is None else None)

    async def demo3():
        with patch.object(asyncio, "sleep", tick):
            # обычный случай: ЛК на www, код дорисовался не сразу, баланс пришёл
            pg = FakePage(texts=["Загрузка…", "Доставки\nКод получения\n112\u00a0396\n12 ноября"])
            assert await lk(pg) == (None, {"pickup_code": "112396", "balance": {"value": 1}, "error": None})
            # выбор аккаунта при ЖИВЫХ старых куках www: кликаем НАШ номер; до afterauth — ни одного goto,
            # сайт сам ушёл в ЛК → повторный goto не нужен; код прочитан
            pg = FakePage(redirect=SELECT, cookies=[SSO, WWW_OK], phones=["+7 900 111-22-33", "+7 988 156-78-17"],
                          after_click=DONE_ON_LK, texts=["Код получения 070 924"])
            assert await lk(pg) == (None, {"pickup_code": "070924", "balance": {"value": 1}, "error": None})
            (clicked_at, who), = pg.clicks
            assert who == "+7 988 156-78-17" and [u for _, u in pg.gotos] == [DELIVERY_URL], pg.gotos
            assert not any(clicked_at <= at < clicked_at + 3 for at, _ in pg.gotos)
            # сайт после входа остался на главной — тогда идём в ЛК сами (уже после afterauth)
            pg = FakePage(redirect=SELECT, cookies=[SSO, WWW_OK], phones=["+7 988 156-78-17"],
                          after_click=[(1, "site:https://www.wildberries.ru/"), (3, "afterauth")],
                          texts=["Код получения 070 924"])
            assert (await lk(pg))[0] is None and [u for _, u in pg.gotos] == [DELIVERY_URL, DELIVERY_URL]
            assert pg.gotos[1][0] >= pg.clicks[0][0] + 3
            # нашего номера на экране нет — первый в списке; «Нет нужного аккаунта» не номер — не жмём
            pg = FakePage(redirect=SELECT, cookies=[WWW_OK], phones=["+7 900 111-22-33"], after_click=DONE_ON_LK,
                          texts=["Код получения 554 433"])
            assert (await lk(pg))[0] is None and [k for _, k in pg.clicks] == ["+7 900 111-22-33"]
            # выбрали, но WB так и не впустил (callback, ни afterauth, ни кук сайта) → «сессия сайта истекла»
            pg = FakePage(redirect=SELECT, phones=["+7 988 156-78-17"])
            assert await lk(pg) == (VISIT_SESSION, None)
            # страница не открылась (прокси) и увела не туда
            assert await lk(FakePage(goto_fails=True)) == (VISIT_PAGE, None)
            assert await lk(FakePage(redirect="https://www.wildberries.ru.evil/x")) == (VISIT_PAGE, None)
            # ЛК открылся, кода нет → «товаров в ПВЗ нет?»; без want_code причины нет
            err, out = await lk(FakePage(texts=["Доставки\nУ вас нет заказов"]))
            assert err is None and out == {"pickup_code": None, "balance": {"value": 1}, "error": VISIT_NO_CODE}
            assert (await lk(FakePage(), want_code=False))[1]["error"] is None
            # баланс 405 — в лог статус и тело, сам баланс None
            err, out = await lk(FakePage(texts=["Код получения 112 396"],
                                         wallet={"ok": False, "status": 405, "text": "<html>405 Not Allowed</html>"}))
            assert out["balance"] is None and out["pickup_code"] == "112396"
    with patch.object(log, "warning") as warn, patch.object(log, "info") as info:
        asyncio.run(demo3())
    logged = " ".join(str(c) for c in warn.call_args_list + info.call_args_list)
    assert "405" in logged and "405 Not Allowed" in logged  # почему не пришёл баланс
    assert "…78-17" in logged and "первый номер в списке" in logged  # что выбрали
    assert "У вас нет заказов" in logged  # кусок страницы, где не нашёлся код
    print("ok")
