"""FSM-состояния для пошаговых диалогов ввода."""

from aiogram.fsm.state import State, StatesGroup


class AddSeller(StatesGroup):
    waiting_id = State()


class AddUser(StatesGroup):
    waiting_id = State()


class SetCookie(StatesGroup):
    waiting = State()
