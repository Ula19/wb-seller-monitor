"""Keeper аккаунтов WB: логин через Telegram, освежение сессий, хранение в wb_accounts.

Сессия = storageState браузера ЦЕЛИКОМ (куки + localStorage: там Bearer wbx__tokenData,
нужен лимиту и заказам), в БД — зашифрованной (app.crypto). У пользователя 1–10
аккаунтов; бизнес-цены его источников идут под его бизнес-аккаунтом. Логин: юзер шлёт
номер, бот вводит его в браузере, код приходит пушем в приложение WB, юзер шлёт его в чат.
"""
import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.config import settings
from app.crypto import decrypt_json, encrypt_json
from app.db import models, repo
from app.db.base import Session
from app.wb import account_api, pickups, proxy_check, web_session
from app.wb.web_session import ProxyDown

log = logging.getLogger(__name__)

CODE_TIMEOUT = 300  # сколько ждём код с сайта, сек
PROXY_ALERT_AFTER = repo.PROXY_ALERT_AFTER  # один таймаут резидентного прокси — норма, пушим с третьего подряд


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
    where = f"на сайте https://{settings.site_domain}/accounts" if settings.site_domain else "на сайте, раздел «Аккаунты»"
    await _push_all(bot, f"‼️ <b>Аккаунт WB {acc.title} разлогинен</b> — бизнес-цены, лимит "
                         f"и заказы под ним не идут.\n👉 Войдите заново: {where}")


async def _push_all(bot, text: str) -> None:
    for uid in settings.superadmins:  # одно пространство — всем из списка
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception:
            pass


async def _proxy_alert(bot, acc: models.WbAccount, e: Exception) -> None:
    """Сеть через прокси аккаунта не работает: сессия цела, НЕ relogin. Счётчик сбоев — в БД (его видит сайт);
    пуш один раз, когда сбоев подряд стало PROXY_ALERT_AFTER."""
    async with Session() as s:
        n = await repo.proxy_fail(s, acc.id)
        await s.commit()
    log.warning("keeper: аккаунт %s (%s): прокси не отвечает (%s подряд): %s", acc.id, acc.title, n, e)
    if n != PROXY_ALERT_AFTER:
        return
    where = f"https://{settings.site_domain}/proxies" if settings.site_domain else "раздел «Прокси» на сайте"
    await _push_all(bot, f"⚠️ <b>Прокси аккаунта WB {acc.title} не отвечает</b> — лимит, заказы и "
                         f"рефреш сессии под ним не идут. Сессия цела, входить заново не нужно.\n"
                         f"👉 Проверьте прокси: {where}")


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
    if st.get("orders"):  # адрес и часы ПВЗ — в строку заказа: API в другом контейнере, кэша справочника не видит
        offices = await pickups.lookup(o["dst_office_id"] for o in st["orders"])
        for o in st["orders"]:
            o["office_address"], o["office_hours"] = offices.get(o["dst_office_id"], (None, None))
    async with Session() as s:
        fails = await repo.save_account_state(s, acc.id, st)
        await s.commit()
    if fails >= PROXY_ALERT_AFTER:  # о сбое пушили — скажем и о восстановлении
        log.info("keeper: аккаунт %s: прокси снова отвечает после %d сбоев", acc.title, fails)
        await _push_all(bot, f"✅ Прокси аккаунта WB <b>{acc.title}</b> снова отвечает")
    log.info("keeper: аккаунт %s: Кошелёк %s%%, лимит %s ₽, заказов %d", acc.title,
             st.get("wallet_pct") if st.get("wallet_pct") is not None else "—",
             f"{st['limit_left']:,}".replace(",", " ") if st.get("limit_left") is not None else "—",
             len(st["orders"]))


# ---------- вход по заявке с сайта (wb_login_requests) ----------
LOGIN_POLL_SEC = 2  # как часто worker смотрит новые заявки


async def _set_request(req_id: int, **values) -> bool:
    """Правка заявки, если её не отменили с сайта (иначе статус вернулся бы в активный, а при уже начатой новой
    заявке упал бы uq_login_active). False — заявка отменена."""
    async with Session() as s:
        res = await s.execute(update(models.WbLoginRequest).where(
            models.WbLoginRequest.id == req_id, models.WbLoginRequest.status != "cancelled").values(**values))
        await s.commit()
    return res.rowcount > 0


async def process_login_request(bot=None) -> bool:
    """Берёт самую старую новую заявку и ведёт вход до конца. False — заявок нет.

    Один вход за раз на процесс: остальные ждут в new («в очереди» на сайте).
    """
    async with Session() as s:
        req = await repo.next_login_request(s)
        if req is None:
            return False
    if not await _set_request(req.id, status="running"):  # отменили, пока брали из очереди
        return True
    asked = got = False

    async def get_code():
        """Код с сайта, Resend (просьба прислать заново) или LoginCancelled. None — не дождались."""
        nonlocal asked, got
        asked = True
        if not await _set_request(req.id, status="wait_code"):  # отменили, пока открывали WB
            raise web_session.LoginCancelled()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CODE_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(1)
            async with Session() as s:
                row = (await s.execute(select(models.WbLoginRequest.status, models.WbLoginRequest.code,
                                              models.WbLoginRequest.resend)
                                       .where(models.WbLoginRequest.id == req.id))).one()
            if row.status == "cancelled":
                raise web_session.LoginCancelled()
            if row.resend:
                await _set_request(req.id, resend=None)
                return web_session.Resend(row.resend)
            if row.code:
                got = True
                await _set_request(req.id, status="running")
                return row.code
        return None

    async def on_sent(via, until):
        await _set_request(req.id, sent_via=via, resend_after=until)

    acc = None
    try:
        state = await web_session.login(req.phone, get_code, await proxy_url(req.proxy_id), on_sent)
        async with Session() as s:
            cancelled = await s.scalar(select(models.WbLoginRequest.status)
                                       .where(models.WbLoginRequest.id == req.id)) == "cancelled"
        if cancelled:  # отменили уже после ввода кода — аккаунт не сохраняем
            raise web_session.LoginCancelled()
        if state:
            acc = await save_login(req.user_id, req.phone, req.kind, state, req.proxy_id)
            status, error, acc_id = "done", None, acc.id
        else:
            status, acc_id = "error", None
            error = ("неверный код или WB не пустил — попробуйте ещё раз" if got
                     else f"код не введён за {CODE_TIMEOUT // 60} мин" if asked
                     else "WB не показал форму входа (антибот) — попробуйте позже")
    except web_session.LoginCancelled:  # браузер уже закрыт (finally в login), заявку отменил сайт
        log.info("keeper: заявка на вход %s (пользователь %s): отменена с сайта", req.id, req.user_id)
        return True
    except web_session.LoginFailed as e:  # код введён, вход не завершён — причина по кукам
        status, error, acc_id = "error", str(e), None
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


async def process_account_poll(bot) -> bool:
    """Аккаунт, которому на сайте нажали ↻: внеочередной опрос. False — опрашивать некого."""
    async with Session() as s:
        acc = await s.scalar(select(models.WbAccount).where(models.WbAccount.polling)
                             .order_by(models.WbAccount.id).limit(1))
        if acc is None:
            return False
    try:
        await poll_account(bot, acc)
    finally:  # иначе при сбое «обновляю…» висит, а цикл крутит один и тот же аккаунт
        async with Session() as s:
            await s.execute(update(models.WbAccount).where(models.WbAccount.id == acc.id).values(polling=False))
            await s.commit()
    return True


async def account_polls_loop(bot) -> None:
    """Фоновая задача worker'а: ↻ «Обновить» с сайта (плановый опрос — state_job по расписанию)."""
    async with Session() as s:  # опрос, прерванный рестартом, уже не закончит никто
        await s.execute(update(models.WbAccount).where(models.WbAccount.polling).values(polling=False))
        await s.commit()
    while True:
        try:
            if await process_account_poll(bot):
                continue
        except Exception:
            log.exception("keeper: цикл опроса аккаунтов по кнопке")
        await asyncio.sleep(LOGIN_POLL_SEC)


if __name__ == "__main__":  # self-check: маска номера, Bearer в сессии
    assert phone_mask("9161234567") == "+7 ••• •••-45-67"
    assert has_bearer({"origins": [{"origin": "https://www.wildberries.ru", "localStorage": [
        {"name": "wbid-oauth-sdk-access-token", "value": "eyJa.eyJb.c"}]}]})
    assert not has_bearer({"cookies": [], "origins": []})
    print("ok")
