from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.access import access
from app.bot.utils import parse_seller_slug, parse_supplier_id
from app.config import settings
from app.db import repo
from app.db.base import Session
from app.services.monitor import sync_seller
from app.wb.client import wb_client

router = Router()
# хендлеры этого роутера — для админов (владелец + whitelist).
# управление пользователями (grant/revoke/users) дополнительно ограничено владельцем.
router.message.filter(lambda m: access.is_admin(m.from_user.id))


def _arg(text: str | None) -> str:
    return (text or "").partition(" ")[2].strip()


def _is_owner(m: Message) -> bool:
    return m.from_user.id == settings.owner_id


@router.message(Command("addseller"))
async def addseller(m: Message):
    arg = _arg(m.text)
    sid = parse_supplier_id(arg)
    if not sid:
        slug = parse_seller_slug(arg)
        if slug:
            sid = await wb_client.resolve_seller_slug(slug)
    if not sid:
        await m.answer("Использование: /addseller <ID или ссылка на магазин>")
        return
    async with Session() as s:
        if await repo.get_seller(s, sid):
            await m.answer(f"Магазин {sid} уже в списке.")
            return
    info = await wb_client.fetch_supplier_info(sid)
    name = (info or {}).get("trademark") or (info or {}).get("supplierName")
    brand = (info or {}).get("trademark")
    async with Session() as s:
        await repo.add_seller(s, sid, name=name, brand=brand)
        await s.commit()
        seller = await repo.get_seller(s, sid)
    await m.answer(f"⏳ Добавляю «{name or sid}», загружаю текущий ассортимент...")
    try:
        fetched, _, _ = await sync_seller(seller, silent_seed=True)
        await m.answer(f"✅ Магазин «{name or sid}» добавлен. Товаров: {len(fetched)}.")
    except Exception as e:
        await m.answer(f"⚠️ Магазин добавлен, но первичная загрузка не удалась: {e}")


@router.message(Command("removeseller"))
async def removeseller(m: Message):
    sid = parse_supplier_id(_arg(m.text))
    if not sid:
        await m.answer("Использование: /removeseller <ID>")
        return
    async with Session() as s:
        ok = await repo.remove_seller(s, sid)
        await s.commit()
    await m.answer("✅ Магазин удалён." if ok else "Магазин не найден.")


@router.message(Command("grant"))
async def grant(m: Message):
    if not _is_owner(m):
        await m.answer("⛔ Управление пользователями — только для владельца.")
        return
    arg = _arg(m.text)
    if not arg.isdigit():
        await m.answer("Использование: /grant <user_id>")
        return
    uid = int(arg)
    async with Session() as s:
        ok = await repo.add_user(s, uid)
        await s.commit()
    access.add(uid)
    await m.answer(f"✅ Доступ выдан {uid}." if ok else f"{uid} уже имеет доступ.")


@router.message(Command("revoke"))
async def revoke(m: Message):
    if not _is_owner(m):
        await m.answer("⛔ Управление пользователями — только для владельца.")
        return
    arg = _arg(m.text)
    if not arg.isdigit():
        await m.answer("Использование: /revoke <user_id>")
        return
    uid = int(arg)
    if uid == settings.owner_id:
        await m.answer("Нельзя забрать доступ у владельца.")
        return
    async with Session() as s:
        ok = await repo.remove_user(s, uid)
        await s.commit()
    access.remove(uid)
    await m.answer(f"✅ Доступ отозван у {uid}." if ok else f"{uid} не найден.")


@router.message(Command("users"))
async def users(m: Message):
    if not _is_owner(m):
        await m.answer("⛔ Управление пользователями — только для владельца.")
        return
    async with Session() as s:
        us = await repo.list_users(s)
    lines = ["👥 Пользователи:", ""]
    for u in us:
        suffix = f" (@{u.username})" if u.username else ""
        lines.append(f"• {u.telegram_id} — {u.role}{suffix}")
    await m.answer("\n".join(lines))


@router.message(Command("stats"))
async def stats(m: Message):
    async with Session() as s:
        sellers, products, users_c = await repo.stats(s)
    await m.answer(
        "📊 Статистика:\n"
        f"Магазинов: {sellers}\n"
        f"Товаров: {products}\n"
        f"Пользователей: {users_c}"
    )
