"""«Общий забор», worker (SCOUT 2026-11-21, часть 2): pickup_sends pending → код получения → Telegram.

Свой цикл, не очередь прокси: визит в браузере ~30 с задержал бы сертификаты и оплаты. Строки — по аккаунтам: нет кода
на сегодня (МСК) — один визит за кодом (keeper._refresh — под замком аккаунта, два браузера на одном профиле не
откроются), потом одно сообщение на пару «аккаунт + ПВЗ» каждому из pickup_to через send_safe.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update

from app.bot.utils import send_safe
from app.db import models, repo
from app.db.base import Session
from app.pickup import pickup_text
from app.services import keeper
from app.wb.web_session import ProxyDown

POLL_SEC = 2
NO_TO = "в Настройках не указано, кому отправлять заборы"
NO_TG = "не доставлено ни одному получателю — нажали ли они /start у бота?"
log = logging.getLogger(__name__)
P = models.PickupSend


def parse_to(raw: str | None) -> list[int]:
    try:
        return [int(x) for x in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []


async def _finish(ids: list[int], **values) -> None:
    async with Session() as s:
        await s.execute(update(P).where(P.id.in_(ids)).values(**values))
        await s.commit()


def _today_code(acc: models.WbAccount) -> str | None:
    return acc.pickup_code if acc.pickup_code and acc.pickup_code_date == repo.msk_today() else None


async def _code(bot, acc: models.WbAccount) -> tuple[models.WbAccount | None, str | None, str | None]:
    """→ (свежий аккаунт, код, причина если кода нет). Нет кода на сегодня — визит за ним (как «Готовы забрать»)."""
    if code := _today_code(acc):
        return acc, code, None
    try:
        if await keeper._refresh(acc, visit=True, want_code=True) is None:
            await keeper._relogin(bot, acc)
            return acc, None, "сессия сайта истекла — войдите в аккаунт заново"
    except ProxyDown as e:
        await keeper._proxy_alert(bot, acc, e)
        return acc, None, "прокси не отвечает — проверьте его в разделе «Прокси»"
    except keeper.AccountGone:
        return None, None, "аккаунт удалён"
    except Exception as e:
        log.warning("забор: визит %s: %s: %s", acc.title, type(e).__name__, str(e)[:200])
        return acc, None, f"визит за кодом не удался: {type(e).__name__}"
    async with Session() as s:
        acc = await s.get(models.WbAccount, acc.id)
    if acc is None:
        return None, None, "аккаунт удалён"
    return acc, _today_code(acc), None if _today_code(acc) else (acc.visit_error or "WB не показал код получения")


async def _account(bot, acc_id: int | None, rows: list, to: list[int]) -> None:
    ids = [r.id for r in rows]
    if not to:
        return await _finish(ids, status="error", error=NO_TO)
    async with Session() as s:
        acc = acc_id and await s.get(models.WbAccount, acc_id)
    if not acc:
        return await _finish(ids, status="error", error="аккаунт удалён")
    acc, code, why = await _code(bot, acc)
    if not code:
        log.info("забор: %s — кода нет: %s", acc.title if acc else acc_id, why)
        return await _finish(ids, status="error", error=why[:256])
    for r in rows:
        text = pickup_text(r.address, acc.phone, acc.title, code, r.items)
        sent = [await send_safe(bot, uid, text) for uid in to]  # всем, без досрочного выхода
        ok = any(x is True for x in sent)
        await _finish([r.id], status="sent" if ok else "error", text=text, error=None if ok else NO_TG,
                      sent_at=datetime.now(timezone.utc) if ok else None)
        log.info("забор: %s, ПВЗ %s, %d шт.: %s", acc.title, r.office_id, sum(it["n"] for it in r.items),
                 f"отправлено {sum(x is True for x in sent)} из {len(to)}" if ok else NO_TG)


async def process_pickup_sends(bot) -> bool:
    """Все pending — по аккаунтам, по очереди. False — нечего слать. Режим работ закрыл «забор» — не шлём (перед каждым
    аккаунтом: отправку, что уже идёт, доделываем)."""
    if repo.closed(await keeper.maintenance(), "pickup"):
        return False
    async with Session() as s:
        rows = list(await s.scalars(select(P).where(P.status == "pending").order_by(P.id)))
        to = parse_to(await repo.get_setting(s, repo.PICKUP_TO))
    for acc_id in dict.fromkeys(r.account_id for r in rows):
        if repo.closed(await keeper.maintenance(), "pickup"):
            break
        try:
            await _account(bot, acc_id, [r for r in rows if r.account_id == acc_id], to)
        except Exception as e:  # иначе строки висят в pending, а цикл крутит их же
            log.exception("забор: аккаунт %s", acc_id)
            async with Session() as s:  # только ещё не отправленные — отмеченные sent/error не трогаем
                await s.execute(update(P).where(P.account_id == acc_id, P.status == "pending", P.id.in_([r.id for r in rows]))
                                .values(status="error", error=f"ошибка: {type(e).__name__}"))
                await s.commit()
    return bool(rows)


async def pickup_sends_loop(bot) -> None:
    # ponytail: рестарт между send_safe и отметкой sent — сообщение уйдёт повторно; редкость, дубль в Telegram безвреден
    while True:
        try:
            await process_pickup_sends(bot)
        except Exception:
            log.exception("забор: цикл")
        await asyncio.sleep(POLL_SEC)
