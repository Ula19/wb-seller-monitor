"""Keeper аккаунтов WB: вход по заявкам сайта, освежение сессий, опросы, хранение в wb_accounts.

Сессия = storageState браузера ЦЕЛИКОМ (куки + localStorage: там Bearer wbx__tokenData,
нужен лимиту и заказам), в БД — зашифрованной (app.crypto). У пользователя 1–10
аккаунтов; бизнес-цены его источников идут под его бизнес-аккаунтом. Логин: юзер шлёт
номер, бот вводит его в браузере, код приходит пушем в приложение WB, юзер шлёт его в чат.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.config import settings
from app.crypto import decrypt_json, encrypt_json
from app.db import models, repo
from app.db.base import Session
from app.wb import account_api, media, pickups, proxy_check, web_session
from app.wb.web_session import ProxyDown

log = logging.getLogger(__name__)

CODE_TIMEOUT = 300  # сколько ждём код с сайта, сек
PROXY_ALERT_AFTER = repo.PROXY_ALERT_AFTER  # один таймаут резидентного прокси — норма, пушим с третьего подряд


phone_mask = repo.phone_mask


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
        acc = await repo.save_account(s, uid, phone, kind, encrypt_json(state), profile, proxy_id)
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


# один браузер на аккаунт за раз: два параллельных рефреша провернули бы wbid-refresh (он ротируется) — разлогин.
# ponytail: замки в памяти одного worker'а; второй worker с браузером — нужен замок в БД (advisory lock)
_acc_locks: dict[int, asyncio.Lock] = {}


class AccountGone(Exception):
    """Аккаунт удалили, пока до него шла очередь."""


async def _attach_offices(rows: list[dict], key: str) -> None:
    """Адрес и часы ПВЗ — в строки (API в другом контейнере, кэша справочника не видит)."""
    offices = await pickups.lookup(r[key] for r in rows)
    for r in rows:
        r["office_address"], r["office_hours"] = offices.get(r[key], (None, None))


async def _refresh(acc: models.WbAccount, *, visit: bool = False, want_code: bool = False) -> dict | None:
    """Браузерный рефреш сессии; живая → сохраняем. None — протухла. ProxyDown — прокси не отвечает.
    visit — заодно история баланса и (want_code) код получения. Сессию берём из БД уже под замком аккаунта:
    предыдущий рефреш мог её обновить."""
    async with _acc_locks.setdefault(acc.id, asyncio.Lock()):
        async with Session() as s:
            row = await s.get(models.WbAccount, acc.id)
            if row is None:
                raise AccountGone(acc.id)
            state, proxy_id, phone = decrypt_json(row.state_enc), row.proxy_id, row.phone
        new = await web_session.refresh(state, await proxy_url(proxy_id), visit=visit, want_code=want_code, phone=phone)
        if new is None:
            return None
        profile = new.pop(account_api.PROFILE_KEY, None)
        seen = new.pop(account_api.VISIT_KEY, None) or {}
        ops = account_api.parse_balance_history(seen["balance"]) if seen.get("balance") else None
        async with Session() as s:
            row = await s.get(models.WbAccount, acc.id)
            if row is not None:
                row.state_enc = encrypt_json(new)
                repo.apply_profile(row, profile)  # имя, полный телефон, процент выкупа — без перелогина
                if seen:  # ЛК открывался — итог (None = всё хорошо) на сайт
                    row.visit_error = seen.get("error")
                if seen.get("pickup_code"):
                    row.pickup_code, row.pickup_code_date = seen["pickup_code"], repo.msk_today()
                if ops:
                    await repo.upsert_balance_ops(s, acc.id, ops)
                await s.commit()
        if visit or seen.get("error"):
            log.info("keeper: визит аккаунта %s: код получения %s, операций баланса %s%s", acc.title,
                     "есть" if seen.get("pickup_code") else "нет", len(ops) if ops is not None else "—",
                     f" — {seen['error']}" if seen.get("error") else "")
        return new


async def refresh_job(bot) -> None:
    """Освежает сессии активных аккаунтов по очереди (00:05 и 12:05 МСК) и заодно визит: история баланса и код
    получения — его ждём, только если есть заказы в ПВЗ. Протухла → «нужен вход» владельцу."""
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        try:
            async with Session() as s:
                want_code = await repo.has_pickup_orders(s, acc.id)
            state = await _refresh(acc, visit=True, want_code=want_code)
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
            state = await _refresh(acc)
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
    if st.get("orders"):
        await _attach_offices(st["orders"], "dst_office_id")
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


async def _flag_loop(flag, step, what: str) -> None:
    """Фоновая задача worker'а для флага с сайта (polling, browser_sync): при старте снимаем зависший флаг
    (работу, прерванную рестартом, уже не закончит никто), дальше берём по одному аккаунту."""
    async with Session() as s:
        await s.execute(update(models.WbAccount).where(flag).values({flag.key: False}))
        await s.commit()
    while True:
        try:
            if await step():
                continue
        except Exception:
            log.exception("keeper: цикл %s", what)
        await asyncio.sleep(LOGIN_POLL_SEC)


async def account_polls_loop(bot) -> None:
    """↻ «Обновить» с сайта (плановый опрос — state_job по расписанию)."""
    await _flag_loop(models.WbAccount.polling, lambda: process_account_poll(bot), "опроса аккаунтов по кнопке")


async def process_browser_sync(bot) -> bool:
    """Визит в браузере по кнопке с сайта («Готовы забрать», «Обновить» баланса) — всегда с кодом получения:
    товар мог прийти днём, после планового визита. False — некого."""
    async with Session() as s:
        acc = await s.scalar(select(models.WbAccount).where(models.WbAccount.browser_sync)
                             .order_by(models.WbAccount.id).limit(1))
        if acc is None:
            return False
    try:
        try:
            if await _refresh(acc, visit=True, want_code=True) is None:
                await _relogin(bot, acc)
        except ProxyDown as e:
            await _proxy_alert(bot, acc, e)
        except AccountGone:
            pass
    finally:  # иначе на сайте «Получаю код…» висит, а цикл крутит один и тот же аккаунт
        async with Session() as s:
            await s.execute(update(models.WbAccount).where(models.WbAccount.id == acc.id).values(browser_sync=False))
            await s.commit()
    return True


async def browser_syncs_loop(bot) -> None:
    await _flag_loop(models.WbAccount.browser_sync, lambda: process_browser_sync(bot), "визитов в браузере")


async def _purchases(acc: models.WbAccount) -> None:
    """История заказов одного аккаунта. Пока полная загрузка ни разу не дошла до последней страницы
    (purchases_full_at пуст) — грузим целиком: прерванная на середине первая загрузка иначе оставила бы старые
    покупки недогруженными навсегда. Потом — только догрузка до страницы, где всё уже известно."""
    known = None
    if acc.purchases_full_at:
        async with Session() as s:
            known = set(await s.scalars(select(models.AccountPurchase.rid)
                                        .where(models.AccountPurchase.account_id == acc.id)))
    items, complete = await account_api.fetch_purchases(decrypt_json(acc.state_enc), await proxy_url(acc.proxy_id), known)
    await _attach_offices(items, "office_id")
    async with Session() as s:
        await repo.upsert_purchases(s, acc.id, items)
        if known is None and complete:
            await s.execute(update(models.WbAccount).where(models.WbAccount.id == acc.id)
                            .values(purchases_full_at=datetime.now(timezone.utc)))
        await s.commit()
    log.info("keeper: история заказов %s: %s, %d позиций", acc.title,
             "догрузка" if known is not None else "полная" if complete else "полная, не до конца", len(items))


async def purchases_job(bot=None) -> None:
    """История заказов (полученные) всех активных аккаунтов — раз в час; копим по rid. Сбой одного — дальше."""
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        try:
            await _purchases(acc)
        except Exception as e:  # 401/прокси — разберётся опрос аккаунта (у него рефреш и тревоги)
            log.warning("keeper: история заказов %s не получена: %s: %s", acc.title, type(e).__name__, str(e)[:200])


# ---------- активация сертификатов (certificates: pending с сайта) ----------
# Очередь — по прокси: один прокси = один IP у нескольких аккаунтов, параллель на одном IP даёт «слишком много
# запросов». Разные прокси — параллельно. Без своего прокси — общий из .env (ключ None).
CERT_PAUSE_SEC = 3  # между активациями на одном прокси
CERT_UNKNOWN = "WB не ответил — сертификат мог активироваться, проверьте баланс аккаунта"
_cert_lanes: set[int | None] = set()  # прокси, по которым уже идёт очередь (ponytail: в памяти одного worker'а)


def _cert_tail(code: str) -> str:
    return "…" + code[-4:]  # код — это деньги: в лог целиком не пишем


async def _finish_cert(cert_id: int, status: str, **values) -> None:
    async with Session() as s:
        await s.execute(update(models.Certificate).where(models.Certificate.id == cert_id).values(
            status=status, done_at=datetime.now(timezone.utc), **values))
        await s.commit()


async def _activate_cert(cert_id: int) -> None:
    """Один сертификат: sending (коммит ДО запроса — рестарт посреди отправки не повторит его) → ok | error | unknown.
    Успех → свежий баланс аккаунта (funds) в БД."""
    async with Session() as s:
        cert = await s.get(models.Certificate, cert_id)
        acc = cert and cert.account_id and await s.get(models.WbAccount, cert.account_id)
        if cert is None or cert.status != "pending":
            return
        if not acc:
            cert.status, cert.error_text, cert.done_at = "error", "аккаунт удалён", datetime.now(timezone.utc)
            await s.commit()
            return
        code, title, state, proxy_id, acc_id = cert.code, acc.title, decrypt_json(acc.state_enc), acc.proxy_id, acc.id
        cert.status = "sending"
        await s.commit()
    proxy = await proxy_url(proxy_id)
    try:
        res = await account_api.activate_cert(state, proxy, code)
    except (account_api.Unauthorized, account_api.NoBearer):
        await _finish_cert(cert_id, "error", error_text="нужен вход в аккаунт — войдите заново и добавьте ещё раз")
        log.warning("keeper: сертификат %s → %s: нужен вход", _cert_tail(code), title)
        return
    except ProxyDown as e:  # до WB не дошли — не активирован
        await _finish_cert(cert_id, "error", error_text="прокси не отвечает — проверьте его в разделе «Прокси»")
        log.warning("keeper: сертификат %s → %s: прокси не отвечает: %s", _cert_tail(code), title, e)
        return
    except Exception as e:  # CertUnknown и всё неожиданное: запрос мог дойти — не повторяем
        await _finish_cert(cert_id, "unknown", error_text=CERT_UNKNOWN)
        log.warning("keeper: сертификат %s → %s: ответа нет (%s: %s) — проверьте баланс", _cert_tail(code), title,
                    type(e).__name__, str(e)[:200])
        return
    if not res["ok"]:
        await _finish_cert(cert_id, "error", error_number=res["error_number"], error_text=res["error_text"] or "ошибка WB")
        log.info("keeper: сертификат %s → %s: ошибка %s %s", _cert_tail(code), title, res["error_number"], res["error_text"])
        return
    await _finish_cert(cert_id, "ok", nominal=res["nominal"], wb_cert_id=res["id"])
    log.info("keeper: сертификат %s → %s: зачислено %s ₽", _cert_tail(code), title, res["nominal"])
    try:  # «баланс теперь» на сайте; сбой — не беда, обычный опрос догонит
        bal = await account_api.fetch_balance(state, proxy)
        async with Session() as s:
            await s.execute(update(models.WbAccount).where(models.WbAccount.id == acc_id).values(
                state_at=datetime.now(timezone.utc), **{k: v for k, v in bal.items() if k in repo.ACCOUNT_STATE_FIELDS}))
            await s.commit()
    except Exception as e:
        log.warning("keeper: баланс %s после сертификата не получен: %s: %s", title, type(e).__name__, str(e)[:200])


async def _cert_lane(proxy_id: int | None) -> None:
    """Очередь одного прокси: по одному, пауза CERT_PAUSE_SEC между активациями; пусто — выходим."""
    try:
        while True:
            async with Session() as s:
                cid = await s.scalar(select(models.Certificate.id).join(
                    models.WbAccount, models.WbAccount.id == models.Certificate.account_id).where(
                    models.Certificate.status == "pending",
                    models.WbAccount.proxy_id.is_(None) if proxy_id is None else models.WbAccount.proxy_id == proxy_id)
                    .order_by(models.Certificate.id).limit(1))
            if cid is None:
                return
            try:
                await _activate_cert(cid)
            except Exception as e:  # иначе строка зависнет в sending/pending, а очередь крутит её же
                log.exception("keeper: активация сертификата %s", cid)
                async with Session() as s:
                    c = models.Certificate
                    now = datetime.now(timezone.utc)
                    await s.execute(update(c).where(c.id == cid, c.status == "sending").values(
                        status="unknown", error_text=CERT_UNKNOWN, done_at=now))
                    await s.execute(update(c).where(c.id == cid, c.status == "pending").values(  # до WB не дошли
                        status="error", error_text=f"ошибка: {type(e).__name__}", done_at=now))
                    await s.commit()
            await asyncio.sleep(CERT_PAUSE_SEC)
    finally:
        _cert_lanes.discard(proxy_id)


async def start_cert_lanes() -> int:
    """Запускает очереди прокси, у которых есть pending, а очередь ещё не идёт. → сколько запущено."""
    async with Session() as s:
        orphan = await s.execute(update(models.Certificate).where(  # аккаунт удалили, пока ждал
            models.Certificate.status == "pending", models.Certificate.account_id.is_(None)).values(
            status="error", error_text="аккаунт удалён", done_at=datetime.now(timezone.utc)))
        lanes = set(await s.scalars(select(models.WbAccount.proxy_id).distinct().join(
            models.Certificate, models.Certificate.account_id == models.WbAccount.id).where(
            models.Certificate.status == "pending")))
        if orphan.rowcount:
            await s.commit()
    new = lanes - _cert_lanes
    for lane in new:
        _cert_lanes.add(lane)
        asyncio.create_task(_cert_lane(lane))
    return len(new)


async def certificates_loop() -> None:
    """Фоновая задача worker'а. При старте sending → unknown (могли успеть отправить), pending остаются в очереди."""
    async with Session() as s:
        res = await s.execute(update(models.Certificate).where(models.Certificate.status == "sending").values(
            status="unknown", error_text=CERT_UNKNOWN, done_at=datetime.now(timezone.utc)))
        await s.commit()
    if res.rowcount:
        log.warning("keeper: %d сертификатов прервано рестартом посреди отправки — статус «проверьте баланс»", res.rowcount)
    while True:
        try:
            await start_cert_lanes()
        except Exception:
            log.exception("keeper: цикл сертификатов")
        await asyncio.sleep(LOGIN_POLL_SEC)


async def img_hosts_job() -> None:
    """Карта basket-хостов картинок — раз в сутки в app_settings (сайт собирает URL сам, API в сеть не ходит)."""
    try:
        hosts = await media.fetch_hosts()
    except Exception as e:
        log.warning("keeper: карта картинок не получена: %s: %s", type(e).__name__, str(e)[:200])
        return
    if hosts:
        async with Session() as s:
            await repo.set_setting(s, media.SETTING_KEY, json.dumps(hosts))
            await s.commit()


if __name__ == "__main__":  # self-check: маска номера, Bearer в сессии
    assert phone_mask("9161234567") == "+7 ••• •••-45-67" and repo.phone_pretty("9161234567") == "+7 916 123-45-67"
    assert has_bearer({"origins": [{"origin": "https://www.wildberries.ru", "localStorage": [
        {"name": "wbid-oauth-sdk-access-token", "value": "eyJa.eyJb.c"}]}]})
    assert not has_bearer({"cookies": [], "origins": []})
    print("ok")
