from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import views
from app.emoji import tge

router = Router()


# catch-all для колбэков без хендлера (кнопки из старых сообщений после апдейта,
# напр. удалённая «⚡ Ежеминутные»): без ответа у пользователя ~30с крутится спиннер.
# router common подключается ПОСЛЕДНИМ — сюда падает только неопознанное.
@router.callback_query()
async def stale_callback(cb: CallbackQuery):
    await cb.answer("Кнопка устарела — откройте меню заново (/start)", show_alert=True)


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
