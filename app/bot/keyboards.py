"""Inline-клавиатуры, цвета кнопок и схемы callback_data.

Цвета (Bot API 9.4): style = 'primary'(синяя) | 'success'(зелёная) | 'danger'(красная).
Премиум-эмодзи на кнопках: icon_custom_emoji_id. Иконки берутся из ICONS по
семантическому ключу. Пока ICONS пуст — кнопки показывают обычный эмодзи из text;
как только впишешь custom_emoji_id (вывод scripts/get_emoji_ids.py) — появится иконка.
"""

from aiogram.filters.callback_data import CallbackData
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

# Тексты reply-кнопок главного меню (премиум-эмодзи на reply не поддерживаются).
RB_SELLERS = "🏪 Магазины"
RB_USERS = "👥 Пользователи"
RB_STATS = "📊 Статистика"

# Семантический ключ -> custom_emoji_id из набора tgmacicons (t.me/addemoji/tgmacicons).
ICONS: dict[str, str] = {
    "shop":    "5257963315258204021",  # 🏘 buildings
    "refresh": "5260687119092817530",  # 🔄
    "users":   "5258486128742244085",  # 👥
    "stats":   "5258391025281408576",  # 📈
    "list":    "5257965174979042426",  # 📝
    "add":     "5359529383319084413",  # ➕
    "remove":  "5258130763148172425",  # 🗑
    "back":    "5258132936401624790",  # ⬅️
    "cancel":  "5267123797600783095",  # ❌
    "delete":  "5258130763148172425",  # 🗑
}


class Nav(CallbackData, prefix="nav"):
    """Навигация по меню. to — целевой экран/действие."""

    to: str


class SellerCB(CallbackData, prefix="sd"):
    action: str  # del | check
    sid: int


class UserCB(CallbackData, prefix="ud"):
    action: str  # del
    uid: int


def _btn(b: InlineKeyboardBuilder, emoji: str, label: str, cb, *, style=None, icon=None):
    """Добавляет кнопку: цвет style + премиум-иконка (если задана), иначе emoji в тексте."""
    kwargs = {"callback_data": cb}
    if style:
        kwargs["style"] = style
    ce_id = ICONS.get(icon) if icon else None
    if ce_id:
        kwargs["text"] = label
        kwargs["icon_custom_emoji_id"] = ce_id
    else:
        kwargs["text"] = f"{emoji} {label}"
    b.button(**kwargs)


def main_reply(is_owner: bool):
    """Главное меню — постоянные reply-кнопки внизу экрана."""
    b = ReplyKeyboardBuilder()
    b.button(text=RB_SELLERS)
    if is_owner:
        b.button(text=RB_USERS)
        b.button(text=RB_STATS)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def sellers_menu(is_owner: bool):
    b = InlineKeyboardBuilder()
    _btn(b, "📋", "Список магазинов", Nav(to="list_sellers"), style="primary", icon="list")
    _btn(b, "🔄", "Проверить магазин", Nav(to="check_seller"), style="success", icon="refresh")
    if is_owner:
        _btn(b, "➕", "Добавить", Nav(to="add_seller"), style="success", icon="add")
        _btn(b, "➖", "Удалить", Nav(to="del_seller"), style="danger", icon="remove")
    _btn(b, "⬅️", "Назад", Nav(to="main"), icon="back")
    b.adjust(1)
    return b.as_markup()


def users_menu():
    b = InlineKeyboardBuilder()
    _btn(b, "📋", "Список", Nav(to="list_users"), style="primary", icon="list")
    _btn(b, "➕", "Выдать доступ", Nav(to="add_user"), style="success", icon="add")
    _btn(b, "➖", "Забрать доступ", Nav(to="del_user"), style="danger", icon="remove")
    _btn(b, "⬅️", "Назад", Nav(to="main"), icon="back")
    b.adjust(1)
    return b.as_markup()


def back_kb(to: str = "main"):
    b = InlineKeyboardBuilder()
    _btn(b, "⬅️", "Назад", Nav(to=to), icon="back")
    return b.as_markup()


def cancel_kb():
    b = InlineKeyboardBuilder()
    _btn(b, "✖️", "Отмена", Nav(to="cancel"), style="danger", icon="cancel")
    return b.as_markup()


def sellers_delete_list(sellers):
    b = InlineKeyboardBuilder()
    for sl in sellers:
        title = sl.name or str(sl.supplier_id)
        _btn(b, "❌", title, SellerCB(action="del", sid=sl.supplier_id), style="danger", icon="delete")
    _btn(b, "⬅️", "Назад", Nav(to="sellers"), icon="back")
    b.adjust(1)
    return b.as_markup()


def seller_delete_confirm(sid: int):
    b = InlineKeyboardBuilder()
    _btn(b, "✅", "Да, удалить", SellerCB(action="delc", sid=sid), style="danger", icon="delete")
    _btn(b, "✖️", "Отмена", Nav(to="del_seller"), icon="cancel")
    b.adjust(1)
    return b.as_markup()


def user_delete_confirm(uid: int):
    b = InlineKeyboardBuilder()
    _btn(b, "✅", "Да, забрать", UserCB(action="delc", uid=uid), style="danger", icon="delete")
    _btn(b, "✖️", "Отмена", Nav(to="del_user"), icon="cancel")
    b.adjust(1)
    return b.as_markup()


def sellers_check_list(sellers):
    b = InlineKeyboardBuilder()
    for sl in sellers:
        title = sl.name or str(sl.supplier_id)
        _btn(b, "🔄", title, SellerCB(action="check", sid=sl.supplier_id), style="primary", icon="refresh")
    _btn(b, "⬅️", "Назад", Nav(to="sellers"), icon="back")
    b.adjust(1)
    return b.as_markup()


def users_delete_list(users, owner_id: int):
    b = InlineKeyboardBuilder()
    for u in users:
        if u.telegram_id == owner_id:
            continue
        label = str(u.telegram_id) + (f" @{u.username}" if u.username else "")
        _btn(b, "❌", label, UserCB(action="del", uid=u.telegram_id), style="danger", icon="delete")
    _btn(b, "⬅️", "Назад", Nav(to="users"), icon="back")
    b.adjust(1)
    return b.as_markup()
