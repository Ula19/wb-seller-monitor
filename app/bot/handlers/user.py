from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot import views

router = Router()


@router.message(Command("listsellers"))
async def listsellers(m: Message):
    text, markup = await views.view_list_sellers()
    await m.answer(text, reply_markup=markup)


@router.message(Command("checknow"))
async def checknow(m: Message):
    await m.answer("⏳ Запускаю внеочередную проверку всех магазинов...")
    ok = await views.run_checknow(m.bot, m.from_user.id)
    text, markup = await views.view_main(m.from_user.id)
    await m.answer("✅ Готово." if ok else "Нет магазинов для проверки.", reply_markup=markup)
