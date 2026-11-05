"""FSM-состояния для пошаговых диалогов ввода."""

from aiogram.fsm.state import State, StatesGroup


class AddSeller(StatesGroup):
    waiting_id = State()
    waiting_mode = State()  # выбор режима цены: бизнес / розница


class AddUser(StatesGroup):
    waiting_id = State()


class SetTokens(StatesGroup):
    waiting = State()


class WbLogin(StatesGroup):
    waiting_kind = State()   # бизнес или обычный аккаунт
    waiting_phone = State()  # номер аккаунта WB
    waiting_code = State()   # код из пуша в приложении WB


class CheckBrands(StatesGroup):
    pick_sellers = State()  # мультивыбор магазинов
    pick_brands = State()   # мультивыбор брендов


class WorkHours(StatesGroup):
    pick_start = State()  # выбор часа начала работы бота
    pick_end = State()    # выбор часа конца
