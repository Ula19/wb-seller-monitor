"""Построители экранов меню (текст + клавиатура) и общие действия."""

from app.bot import keyboards as kb
from app.config import settings
from app.db import repo
from app.db.base import Session
from app.emoji import esc, tge
from app.services.monitor import send_report_to, sync_and_notify

HTML = "HTML"


def is_owner(uid: int | None) -> bool:
    return uid == settings.owner_id


async def view_main(uid: int):
    return f"{tge('list')} Главное меню. Выберите действие:", kb.main_reply(is_owner(uid))


async def view_sellers(uid: int):
    return f"{tge('shop')} Магазины:", kb.sellers_menu(is_owner(uid))


async def view_list_sellers():
    async with Session() as s:
        sellers = await repo.list_sellers(s)
        counts = {
            sl.supplier_id: await repo.count_active_products(s, sl.supplier_id)
            for sl in sellers
        }
    if not sellers:
        return "Список магазинов пуст.", kb.back_kb("sellers")
    text = f"{tge('shop')} Отслеживаемые магазины:\n\n" + "\n".join(
        f"• {esc(sl.name or '—')} (ID {sl.supplier_id}) — {counts[sl.supplier_id]} тов."
        for sl in sellers
    )
    return text, kb.back_kb("sellers")


async def view_users_menu():
    return f"{tge('users')} Управление пользователями:", kb.users_menu()


async def view_list_users():
    async with Session() as s:
        us = await repo.list_users(s)
    text = f"{tge('users')} Пользователи:\n\n" + "\n".join(
        f"• {u.telegram_id} — {u.role}" + (f" (@{esc(u.username)})" if u.username else "")
        for u in us
    )
    return text, kb.back_kb("users")


async def view_stats():
    async with Session() as s:
        sellers, products, users_c = await repo.stats(s)
    text = (
        f"{tge('stats')} Статистика:\n"
        f"Магазинов: {sellers}\n"
        f"Товаров: {products}\n"
        f"Пользователей: {users_c}"
    )
    return text, kb.back_kb("main")


async def run_checknow_one(bot, user_id: int, supplier_id: int) -> bool:
    """Внеочередная проверка одного магазина + отчёт запросившему."""
    async with Session() as s:
        seller = await repo.get_seller(s, supplier_id)
    if not seller:
        return False
    try:
        await sync_and_notify(bot, seller)
    except Exception:
        pass
    async with Session() as s:
        seller = await repo.get_seller(s, supplier_id)
        products = await repo.get_active_products(s, supplier_id)
    await send_report_to(bot, [user_id], seller, products)
    return True
