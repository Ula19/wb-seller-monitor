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
RB_COOKIE = "🔑 Куки"
RB_HOURS = "🕐 Часы отчётов"

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
    action: str  # del | check | fast | mode
    sid: int


class PriceModeCB(CallbackData, prefix="pm"):
    """Выбор режима цены при добавлении магазина."""

    b2b: int  # 1 — бизнес, 0 — розница


class UserCB(CallbackData, prefix="ud"):
    action: str  # del
    uid: int


# Бренды для выборки «Проверить по брендам» (порядок = как показываем).
BRANDS = ["Samsung", "Redmi", "Xiaomi", "Poco", "Honor", "Tecno", "Realme", "Infinix", "Huawei"]


class BCSeller(CallbackData, prefix="bcs"):
    """Тумблер магазина в выборке по брендам."""

    sid: int


class BCBrand(CallbackData, prefix="bcb"):
    """Тумблер бренда (idx — индекс в BRANDS)."""

    idx: int


class HourCB(CallbackData, prefix="rh"):
    """Тумблер часа отчёта (вкл/выкл)."""

    hour: int


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


def main_reply(is_admin: bool, is_owner: bool):
    """Главное меню — постоянные reply-кнопки внизу экрана."""
    b = ReplyKeyboardBuilder()
    b.button(text=RB_SELLERS)
    if is_admin:
        b.button(text=RB_STATS)
        b.button(text=RB_COOKIE)
        b.button(text=RB_HOURS)
    if is_owner:  # управление пользователями — только владелец
        b.button(text=RB_USERS)
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def hours_grid(selected: set[int]):
    """Чеклист часов 00–23: ✅ — отчёт шлётся в этот час."""
    b = InlineKeyboardBuilder()
    for h in range(24):
        mark = "✅" if h in selected else "▫️"
        b.button(text=f"{mark}{h:02d}", callback_data=HourCB(hour=h))
    _btn(b, "✔️", "Готово", Nav(to="main"))
    b.adjust(6, 6, 6, 6, 1)
    return b.as_markup()


def sellers_menu(is_admin: bool):
    b = InlineKeyboardBuilder()
    _btn(b, "📋", "Список магазинов", Nav(to="list_sellers"), style="primary", icon="list")
    _btn(b, "🔄", "Проверить магазин", Nav(to="check_seller"), style="success", icon="refresh")
    _btn(b, "🔎", "Проверить по брендам", Nav(to="check_brands"), style="primary")
    if is_admin:
        _btn(b, "➕", "Добавить", Nav(to="add_seller"), style="success", icon="add")
        _btn(b, "➖", "Удалить", Nav(to="del_seller"), style="danger", icon="remove")
        _btn(b, "⚡", "Ежеминутные", Nav(to="fast_sellers"), style="primary")
        _btn(b, "💰", "Режим цены", Nav(to="price_sellers"), style="primary")
    _btn(b, "⬅️", "Назад", Nav(to="main"), icon="back")
    b.adjust(1)
    return b.as_markup()


def price_mode_kb():
    """Выбор режима цены при добавлении магазина."""
    b = InlineKeyboardBuilder()
    b.button(text="🏢 Бизнес-цена", callback_data=PriceModeCB(b2b=1))
    b.button(text="👤 Розничная", callback_data=PriceModeCB(b2b=0))
    b.adjust(1)
    return b.as_markup()


def sellers_price_list(sellers):
    """Список магазинов с тумблером режима цены: 🏢 бизнес / 👤 розница."""
    b = InlineKeyboardBuilder()
    for sl in sellers:
        mark = "🏢" if sl.b2b else "👤"
        title = f"{mark} {sl.name or sl.supplier_id}"
        b.button(text=title, callback_data=SellerCB(action="mode", sid=sl.supplier_id))
    _btn(b, "⬅️", "Назад", Nav(to="sellers"), icon="back")
    b.adjust(1)
    return b.as_markup()


def sellers_fast_list(sellers):
    """Список магазинов с тумблером приоритета: ⚡ — опрос раз в минуту."""
    b = InlineKeyboardBuilder()
    for sl in sellers:
        mark = "⚡" if sl.is_fast else "▫️"
        title = f"{mark} {sl.name or sl.supplier_id}"
        b.button(text=title, callback_data=SellerCB(action="fast", sid=sl.supplier_id))
    _btn(b, "⬅️", "Назад", Nav(to="sellers"), icon="back")
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


def brand_sellers_kb(sellers, selected: set[int]):
    """Шаг 1 выборки по брендам: чеклист магазинов (✅ — выбран)."""
    b = InlineKeyboardBuilder()
    for sl in sellers:
        mark = "✅" if sl.supplier_id in selected else "▫️"
        b.button(text=f"{mark} {sl.name or sl.supplier_id}", callback_data=BCSeller(sid=sl.supplier_id))
    _btn(b, "➡️", "Дальше (бренды)", Nav(to="bc_brands"), style="success")
    _btn(b, "⬅️", "Назад", Nav(to="sellers"), icon="back")
    b.adjust(1)
    return b.as_markup()


def brand_pick_kb(selected: set[str]):
    """Шаг 2 выборки по брендам: чеклист брендов (✅ — выбран)."""
    b = InlineKeyboardBuilder()
    for i, name in enumerate(BRANDS):
        mark = "✅" if name in selected else "▫️"
        b.button(text=f"{mark} {name}", callback_data=BCBrand(idx=i))
    _btn(b, "📄", "Показать товары", Nav(to="bc_run"), style="success")
    _btn(b, "⬅️", "Назад", Nav(to="check_brands"), icon="back")
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
