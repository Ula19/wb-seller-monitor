from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot import views
from app.emoji import tge

router = Router()


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await views.view_main(m.from_user.id)
    await m.answer(
        f"{tge('wave')} Бот мониторинга продавцов Wildberries.\n\n" + text,
        reply_markup=markup,
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def help_cmd(m: Message):
    text, markup = await views.view_main(m.from_user.id)
    await m.answer(
        f"{tge('info')} Управление — через кнопки ниже.\n"
        "Команды тоже работают: /menu, /listsellers"
        " (для админа: /addseller, /removeseller, /grant, /revoke, /users, /stats).\n\n"
        + text,
        reply_markup=markup,
        parse_mode="HTML",
    )
