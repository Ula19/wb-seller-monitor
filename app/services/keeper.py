"""Keeper веб-сессии WB: хранит банку кук в БД, держит её живой, логинит аккаунт.

Сессия (storageState) лежит в app_settings. При старте грузится в клиент, фоновый
джоб периодически её освежает (заход на сайт). Логин — через Telegram: юзер шлёт
номер, бот вводит его в headless-браузере, код приходит пушем в приложение WB и
юзер присылает его в чат.
"""
import asyncio
import json
import logging

from app.config import settings
from app.db import repo
from app.db.base import Session
from app.wb import web_session
from app.wb.client import wb_client

log = logging.getLogger(__name__)

KEY = "wb_storage_state"
CODE_TIMEOUT = 300  # сколько ждём код из чата, сек

# ожидание кода из чата по telegram_id (логин — долгая корутина, код приходит отдельно)
_pending_code: dict[int, asyncio.Future] = {}
_login_tasks: dict[int, asyncio.Task] = {}


async def _save(state: dict) -> None:
    async with Session() as s:
        await repo.set_setting(s, KEY, json.dumps(state))
        await s.commit()


async def load_into_client() -> None:
    """При старте бота: поднять сохранённую веб-сессию в клиент."""
    async with Session() as s:
        raw = await repo.get_setting(s, KEY)
    if raw:
        wb_client.set_web_session(raw)
        log.info("keeper: веб-сессия загружена из БД, веб-путь активен")
    else:
        log.info("keeper: веб-сессии нет — работаем на мобильном пути (guardium)")


async def _alert(bot, text: str) -> None:
    async with Session() as s:
        ids = list({settings.owner_id, *await repo.list_user_ids(s)})
    for uid in ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            log.warning("keeper-алерт не доставлен %s: %s", uid, e)


async def refresh_job(bot) -> None:
    """Периодически освежает веб-сессию (заход на сайт). Протухла → алерт на релогин."""
    async with Session() as s:
        raw = await repo.get_setting(s, KEY)
    if not raw:
        return  # не залогинены — нечего освежать
    try:
        state = await web_session.refresh(raw)
    except Exception as e:
        log.warning("keeper: рефреш упал: %s", e)
        return
    if state is None:
        wb_client.set_web_session("")  # куки сняты → клиент уходит на мобильный fallback
        await _alert(bot, "‼️ <b>Веб-сессия WB протухла</b>. Войди заново: /wblogin")
        return
    await _save(state)
    wb_client.set_web_session(state)


# ---------- логин через Telegram ----------
async def _do_login(phone: str, get_code) -> bool:
    state = await web_session.login(phone, get_code)
    if not state:
        return False
    await _save(state)
    wb_client.set_web_session(state)
    return True


async def start_login(bot, uid: int, phone: str) -> str:
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
            ok = await _do_login(phone, get_code)
            msg = ("✅ Вошёл в WB — веб-путь активен, guardium больше не нужен."
                   if ok else "❌ Логин не удался (неверный код или антибот). Попробуй ещё раз: /wblogin")
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


if __name__ == "__main__":  # self-check оркестрации кода (без браузера)
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
