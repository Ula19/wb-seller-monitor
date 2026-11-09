"""Keeper аккаунтов WB: логин через Telegram, освежение сессий, хранение в wb_accounts.

Сессия = storageState браузера ЦЕЛИКОМ (куки + localStorage: там Bearer wbx__tokenData,
нужен лимиту и заказам), в БД — зашифрованной (app.crypto). У пользователя 1–10
аккаунтов; бизнес-цены его источников идут под его бизнес-аккаунтом. Логин: юзер шлёт
номер, бот вводит его в браузере, код приходит пушем в приложение WB, юзер шлёт его в чат.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import settings
from app.crypto import decrypt_json, encrypt_json
from app.db import models, repo
from app.db.base import Session
from app.wb import account_api, pickups, proxy_check, web_session
from app.wb.web_session import ProxyDown

log = logging.getLogger(__name__)

CODE_TIMEOUT = 300  # сколько ждём код из чата, сек

# ожидание кода из чата по telegram_id (логин — долгая корутина, код приходит отдельно)
_pending_code: dict[int, asyncio.Future] = {}
_login_tasks: dict[int, asyncio.Task] = {}
# ponytail: в памяти — после рестарта worker'а пуш «прокси не отвечает» может прийти ещё раз
_proxy_down: set[int] = set()


def phone_mask(phone: str) -> str:
    """10 цифр без кода страны → «+7 ••• •••-12-34» (номер целиком не храним)."""
    return f"+7 ••• •••-{phone[-4:-2]}-{phone[-2:]}"


def has_bearer(state: dict) -> bool:
    return account_api.bearer(state) is not None


async def proxy_url(proxy_id: int | None) -> str | None:
    """Адрес прокси аккаунта (расшифрованный); нет своего — None (тогда общий web_proxy())."""
    if proxy_id is None:
        return None
    async with Session() as s:
        p = await s.get(models.Proxy, proxy_id)
    return decrypt_json(p.url_enc) if p else None


async def save_login(uid: int, phone: str, kind: str, state: dict,
                     proxy_id: int | None = None) -> models.WbAccount:
    profile = state.pop(account_api.PROFILE_KEY, None)  # профиль — в колонки, не в сессию
    if not has_bearer(state):  # без Bearer не будет лимита и заказов (шаг account_state)
        log.warning("keeper: в сессии нет Bearer сайта — лимит/заказы появятся после рефреша, если он его допишет")
    async with Session() as s:
        acc = await repo.save_account(s, uid, phone_mask(phone), kind, encrypt_json(state), profile, proxy_id)
        await s.commit()
        return acc


async def _relogin(bot, acc: models.WbAccount) -> None:
    """Сессия мертва: статус relogin + сообщение владельцу аккаунта."""
    async with Session() as s:
        row = await s.get(models.WbAccount, acc.id)
        if row is None:
            return
        row.status = "relogin"
        await s.commit()
    log.warning("keeper: аккаунт %s (%s) разлогинен", acc.id, acc.title)
    where = f"на сайте https://{settings.site_domain}/accounts" if settings.site_domain else "/wblogin"
    for uid in settings.superadmins:  # одно пространство — всем из списка
        try:
            await bot.send_message(uid, f"‼️ <b>Аккаунт WB {acc.title} разлогинен</b> — бизнес-цены, лимит "
                                        f"и заказы под ним не идут.\n👉 Войдите заново: {where}", parse_mode="HTML")
        except Exception:
            pass


async def _proxy_alert(bot, acc: models.WbAccount, e: Exception) -> None:
    """Сеть через прокси аккаунта не работает: сессия цела, НЕ relogin. Пуш один раз до восстановления."""
    log.warning("keeper: аккаунт %s (%s): прокси не отвечает: %s", acc.id, acc.title, e)
    if acc.id in _proxy_down:
        return
    _proxy_down.add(acc.id)
    where = f"https://{settings.site_domain}/proxies" if settings.site_domain else "раздел «Прокси» на сайте"
    for uid in settings.superadmins:
        try:
            await bot.send_message(uid, f"⚠️ <b>Прокси аккаунта WB {acc.title} не отвечает</b> — лимит, заказы и "
                                        f"рефреш сессии под ним не идут. Сессия цела, входить заново не нужно.\n"
                                        f"👉 Проверьте прокси: {where}", parse_mode="HTML")
        except Exception:
            pass


async def _refresh(acc: models.WbAccount, state: dict) -> dict | None:
    """Браузерный рефреш сессии; живая → сохраняем. None — протухла. ProxyDown — прокси не отвечает."""
    new = await web_session.refresh(state, await proxy_url(acc.proxy_id))
    if new is not None:
        profile = new.pop(account_api.PROFILE_KEY, None)
        async with Session() as s:
            row = await s.get(models.WbAccount, acc.id)
            if row is not None:
                row.state_enc = encrypt_json(new)
                repo.apply_profile(row, profile)  # имя, полный телефон, процент выкупа — без перелогина
                await s.commit()
    return new


async def refresh_job(bot) -> None:
    """Освежает сессии активных аккаунтов по очереди. Протухла → «нужен вход» владельцу."""
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        try:
            state = await _refresh(acc, decrypt_json(acc.state_enc))
        except ProxyDown as e:
            await _proxy_alert(bot, acc, e)
            continue
        except Exception as e:
            log.warning("keeper: рефреш аккаунта %s упал: %s", acc.id, e)
            continue
        if state is None:
            await _relogin(bot, acc)


async def state_job(bot) -> None:
    """Лимит, % Кошелька, балансы и активные заказы всех активных аккаунтов."""
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        await poll_account(bot, acc)


async def poll_account(bot, acc: models.WbAccount) -> None:
    """Состояние одного аккаунта (по таймеру и сразу после входа).

    401 = детектор разлогина, но не сразу: access живёт 30 дн., сначала один рефреш
    и повтор; relogin — только если и после рефреша 401. Прокси не отвечает — не relogin.
    """
    state = decrypt_json(acc.state_enc)
    proxy = await proxy_url(acc.proxy_id)
    try:
        try:
            st = await account_api.fetch(state, proxy)
        except (account_api.Unauthorized, account_api.NoBearer) as e:
            log.info("keeper: аккаунт %s: %s, освежаю сессию и повторяю",
                     acc.id, f"401 ({e})" if isinstance(e, account_api.Unauthorized) else "нет Bearer")
            state = await _refresh(acc, state)
            if state is None:
                await _relogin(bot, acc)
                return
            st = await account_api.fetch(state, proxy)
    except ProxyDown as e:
        await _proxy_alert(bot, acc, e)
        return
    except account_api.Unauthorized:
        await _relogin(bot, acc)
        return
    except account_api.NoBearer:  # сессия жива (рефреш прошёл), бизнес-цены по кукам идут — не relogin
        log.warning("keeper: аккаунт %s: Bearer нет и после рефреша — лимит/заказы недоступны", acc.id)
        return
    except Exception as e:
        log.warning("keeper: состояние аккаунта %s не получено: %s: %s", acc.id, type(e).__name__, str(e)[:200])
        return
    _proxy_down.discard(acc.id)
    if st.get("orders"):  # адрес и часы ПВЗ — в строку заказа: API в другом контейнере, кэша справочника не видит
        offices = await pickups.lookup(o["dst_office_id"] for o in st["orders"])
        for o in st["orders"]:
            o["office_address"], o["office_hours"] = offices.get(o["dst_office_id"], (None, None))
    async with Session() as s:
        await repo.save_account_state(s, acc.id, st)
        await s.commit()
    log.info("keeper: аккаунт %s: Кошелёк %s%%, лимит %s ₽, заказов %d", acc.title,
             st.get("wallet_pct") if st.get("wallet_pct") is not None else "—",
             f"{st['limit_left']:,}".replace(",", " ") if st.get("limit_left") is not None else "—",
             len(st["orders"]))


# ---------- вход по заявке с сайта (wb_login_requests) ----------
LOGIN_POLL_SEC = 2  # как часто worker смотрит новые заявки


async def _set_request(req_id: int, **values) -> None:
    async with Session() as s:
        row = await s.get(models.WbLoginRequest, req_id)
        if row is not None:
            for k, v in values.items():
                setattr(row, k, v)
            await s.commit()


async def process_login_request(bot=None) -> bool:
    """Берёт самую старую новую заявку и ведёт вход до конца. False — заявок нет.

    Один вход за раз на процесс: остальные ждут в new («в очереди» на сайте).
    """
    async with Session() as s:
        req = await repo.next_login_request(s)
        if req is None:
            return False
        req.status = "running"
        await s.commit()
    asked = got = False

    async def get_code():
        nonlocal asked, got
        asked = True
        await _set_request(req.id, status="wait_code")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CODE_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(1)
            async with Session() as s:
                code = await s.scalar(select(models.WbLoginRequest.code).where(models.WbLoginRequest.id == req.id))
            if code:
                got = True
                await _set_request(req.id, status="running")
                return code
        return None

    acc = None
    try:
        state = await web_session.login(req.phone, get_code, await proxy_url(req.proxy_id))
        if state:
            acc = await save_login(req.user_id, req.phone, req.kind, state, req.proxy_id)
            status, error, acc_id = "done", None, acc.id
        else:
            status, acc_id = "error", None
            error = ("неверный код или WB не пустил — попробуйте ещё раз" if got
                     else f"код не введён за {CODE_TIMEOUT // 60} мин" if asked
                     else "WB не показал форму входа (антибот) — попробуйте позже")
    except ProxyDown as e:
        log.warning("keeper: вход по заявке %s: прокси не отвечает: %s", req.id, e)
        status, error, acc_id = "error", "прокси не отвечает — проверьте его в разделе «Прокси»", None
    except Exception as e:
        log.exception("keeper: вход по заявке %s упал", req.id)
        status, error, acc_id = "error", f"ошибка входа: {type(e).__name__}", None
    async with Session() as s:
        await repo.finish_login_request(s, req.id, status, error=error, account_id=acc_id)
        await s.commit()
    log.info("keeper: заявка на вход %s (пользователь %s): %s%s", req.id, req.user_id, status,
             f" — {error}" if error else "")
    if acc is not None:  # карточка на сайте заполняется сразу, а не через 10 минут
        async with Session() as s:
            acc = await s.get(models.WbAccount, acc.id)
        await poll_account(bot, acc)
    return True


async def login_requests_loop(bot) -> None:
    """Фоновая задача worker'а: заявки на вход с сайта (браузер есть только тут)."""
    async with Session() as s:
        if n := await repo.fail_stale_logins(s):
            log.warning("keeper: %d заявок на вход прервано прошлым рестартом", n)
        await s.commit()
    while True:
        try:
            if await process_login_request(bot):
                continue  # сразу следующую из очереди
        except Exception:
            log.exception("keeper: цикл заявок на вход")
        await asyncio.sleep(LOGIN_POLL_SEC)


# ---------- проверка прокси по кнопке с сайта (proxies.checking) ----------
async def process_proxy_check() -> bool:
    """Самый старый прокси с checking: внешний IP ×2, IP мониторинга, доступность WB. False — проверять нечего."""
    async with Session() as s:
        p = await s.scalar(select(models.Proxy).where(models.Proxy.checking).order_by(models.Proxy.id).limit(1))
        if p is None:
            return False
        pid, url = p.id, decrypt_json(p.url_enc)
    try:
        res = await proxy_check.check(url)
    except Exception as e:  # иначе checking висит и цикл крутит одно и то же
        res = {"exit_ip": None, "ok": False, "error": f"проверка упала: {type(e).__name__}"}
    async with Session() as s:
        row = await s.get(models.Proxy, pid)
        if row is not None:
            row.exit_ip, row.ok, row.error = res["exit_ip"], res["ok"], res["error"]
            row.checking, row.checked_at = False, datetime.now(timezone.utc)
            await s.commit()
    log.info("keeper: прокси %s: %s", pid, "ок, IP " + res["exit_ip"] if res["ok"] else res["error"])
    return True


async def proxy_checks_loop() -> None:
    """Фоновая задача worker'а: отдельно от входа, чтобы проверка не ждала код из чата."""
    while True:
        try:
            if await process_proxy_check():
                continue
        except Exception:
            log.exception("keeper: цикл проверки прокси")
        await asyncio.sleep(LOGIN_POLL_SEC)


# ---------- логин через Telegram ----------
async def start_login(bot, uid: int, phone: str, kind: str) -> str:
    """Запускает логин в фоне; код придёт из чата через submit_code. Возвращает статус."""
    if uid in _login_tasks and not _login_tasks[uid].done():
        return "already"
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending_code[uid] = fut

    async def get_code():
        try:
            return await asyncio.wait_for(fut, timeout=CODE_TIMEOUT)
        except asyncio.TimeoutError:
            return None

    async def run():
        try:
            state = await web_session.login(phone, get_code)
            if state:
                acc = await save_login(uid, phone, kind, state)
                msg = f"✅ Вошёл в WB: аккаунт {acc.title} ({'бизнес' if kind == 'business' else 'обычный'})."
            else:
                msg = "❌ Логин не удался (неверный код или антибот). Попробуй ещё раз: /wblogin"
        except Exception as e:
            log.exception("keeper: логин упал")
            msg = f"❌ Ошибка логина: {type(e).__name__}"
        finally:
            _pending_code.pop(uid, None)
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass

    _login_tasks[uid] = asyncio.create_task(run())
    return "started"


def submit_code(uid: int, code: str) -> bool:
    """Передать код из чата ожидающему логину. False — никто не ждёт."""
    fut = _pending_code.get(uid)
    if fut and not fut.done():
        fut.set_result(str(code).strip())
        return True
    return False


def awaiting_code(uid: int) -> bool:
    fut = _pending_code.get(uid)
    return bool(fut and not fut.done())


if __name__ == "__main__":  # self-check: маска номера, Bearer в сессии, оркестрация кода
    assert phone_mask("9161234567") == "+7 ••• •••-45-67"
    assert has_bearer({"origins": [{"origin": "https://www.wildberries.ru", "localStorage": [
        {"name": "wbid-oauth-sdk-access-token", "value": "eyJa.eyJb.c"}]}]})
    assert not has_bearer({"cookies": [], "origins": []})

    async def demo():
        uid = 1
        fut = asyncio.get_event_loop().create_future()
        _pending_code[uid] = fut
        assert awaiting_code(uid) is True
        assert submit_code(uid, " 1234 ") is True
        assert await fut == "1234"  # пробелы срезаны
        assert submit_code(uid, "9999") is False  # уже отдан
        assert awaiting_code(uid) is False
        print("ok")
    asyncio.run(demo())
