"""FSM-состояния для пошаговых диалогов ввода."""

from aiogram.fsm.state import State, StatesGroup


class AddSeller(StatesGroup):
    waiting_id = State()
    waiting_mode = State()  # выбор режима цены: бизнес / розница


class AddUser(StatesGroup):
    waiting_id = State()


class SetCookie(StatesGroup):
    waiting = State()


class CheckBrands(StatesGroup):
    pick_sellers = State()  # мультивыбор магазинов
    pick_brands = State()   # мультивыбор брендов


class WorkHours(StatesGroup):
    pick_start = State()  # выбор часа начала работы бота
    pick_end = State()    # выбор часа конца
