from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot import views

router = Router()


@router.message(Command("listsellers"))
async def listsellers(m: Message):
    text, markup = await views.view_list_sellers(m.from_user.id)
    await m.answer(text, reply_markup=markup)
