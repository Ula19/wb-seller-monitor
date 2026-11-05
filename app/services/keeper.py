"""Keeper аккаунтов WB: логин через Telegram, освежение сессий, хранение в wb_accounts.

Сессия = storageState браузера ЦЕЛИКОМ (куки + localStorage: там Bearer wbx__tokenData,
нужен лимиту и заказам), в БД — зашифрованной (app.crypto). У пользователя 1–10
аккаунтов; бизнес-цены его источников идут под его бизнес-аккаунтом. Логин: юзер шлёт
номер, бот вводит его в браузере, код приходит пушем в приложение WB, юзер шлёт его в чат.
"""
import asyncio
import logging

from app.crypto import decrypt_json, encrypt_json
from app.db import models, repo
from app.db.base import Session
from app.wb import account_api, web_session

log = logging.getLogger(__name__)

CODE_TIMEOUT = 300  # сколько ждём код из чата, сек
TOKEN_KEY = "wbx__tokenData"  # Bearer в localStorage www.wildberries.ru

# ожидание кода из чата по telegram_id (логин — долгая корутина, код приходит отдельно)
_pending_code: dict[int, asyncio.Future] = {}
_login_tasks: dict[int, asyncio.Task] = {}


def phone_mask(phone: str) -> str:
    """10 цифр без кода страны → «+7 ••• •••-12-34» (номер целиком не храним)."""
    return f"+7 ••• •••-{phone[-4:-2]}-{phone[-2:]}"


def has_bearer(state: dict) -> bool:
    return any(item.get("name") == TOKEN_KEY
               for origin in state.get("origins") or [] for item in origin.get("localStorage") or [])


async def save_login(uid: int, phone: str, kind: str, state: dict) -> models.WbAccount:
    if not has_bearer(state):  # без Bearer не будет лимита и заказов (шаг account_state)
        log.warning("keeper: в сессии нет %s — лимит/заказы аккаунта будут недоступны", TOKEN_KEY)
    async with Session() as s:
        acc = await repo.save_account(s, uid, phone_mask(phone), kind, encrypt_json(state))
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
    log.warning("keeper: аккаунт %s (%s) разлогинен", acc.id, acc.label)
    try:
        await bot.send_message(acc.user_id, f"‼️ <b>Аккаунт WB {acc.label} разлогинен</b> — "
                                            "бизнес-цены, лимит и заказы под ним не идут.\n👉 Войди заново: /wblogin",
                               parse_mode="HTML")
    except Exception:
        pass


async def _refresh(acc: models.WbAccount, state: dict) -> dict | None:
    """Браузерный рефреш сессии; живая → сохраняем. None — протухла."""
    new = await web_session.refresh(state)
    if new is not None:
        async with Session() as s:
            row = await s.get(models.WbAccount, acc.id)
            if row is not None:
                row.state_enc = encrypt_json(new)
                await s.commit()
    return new


async def refresh_job(bot) -> None:
    """Освежает сессии активных аккаунтов по очереди. Протухла → «нужен вход» владельцу."""
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        try:
            state = await _refresh(acc, decrypt_json(acc.state_enc))
        except Exception as e:
            log.warning("keeper: рефреш аккаунта %s упал: %s", acc.id, e)
            continue
        if state is None:
            await _relogin(bot, acc)


async def state_job(bot) -> None:
    """Лимит, % Кошелька, балансы и активные заказы всех активных аккаунтов.

    401 = детектор разлогина, но не сразу: access живёт 30 дн., сначала один рефреш
    и повтор; relogin — только если и после рефреша 401.
    """
    async with Session() as s:
        accounts = await repo.active_accounts(s)
    for acc in accounts:
        state = decrypt_json(acc.state_enc)
        try:
            try:
                st = await account_api.fetch(state)
            except account_api.Unauthorized as e:
                log.info("keeper: аккаунт %s: 401 (%s), освежаю сессию и повторяю", acc.id, e)
                state = await _refresh(acc, state)
                if state is None:
                    await _relogin(bot, acc)
                    continue
                st = await account_api.fetch(state)
        except account_api.Unauthorized:
            await _relogin(bot, acc)
            continue
        except Exception as e:
            log.warning("keeper: состояние аккаунта %s не получено: %s: %s", acc.id, type(e).__name__, str(e)[:200])
            continue
        async with Session() as s:
            await repo.save_account_state(s, acc.id, st)
            await s.commit()
        log.info("keeper: аккаунт %s: Кошелёк %s%%, лимит %s ₽, заказов %d", acc.label,
                 st.get("wallet_pct") if st.get("wallet_pct") is not None else "—",
                 f"{st['limit_left']:,}".replace(",", " ") if st.get("limit_left") is not None else "—",
                 len(st["orders"]))


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
                msg = f"✅ Вошёл в WB: аккаунт {acc.label} ({'бизнес' if kind == 'business' else 'обычный'})."
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
    assert has_bearer({"origins": [{"localStorage": [{"name": TOKEN_KEY, "value": "t"}]}]})
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
