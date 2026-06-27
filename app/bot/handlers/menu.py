"""Inline-меню: навигация по callback'ам и пошаговые диалоги (FSM)."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot import views
from app.bot.access import access
from app.bot.states import AddSeller, AddUser, SetCookie
from app.bot.utils import parse_seller_slug, parse_supplier_id
from app.config import settings
from app.db import repo
from app.db.base import Session
from app.emoji import esc, tge
from app.services import reporting
from app.services.monitor import silent_resync_all, sync_seller
from app.wb.client import wb_client

log = logging.getLogger(__name__)
router = Router()
_bg_tasks: set = set()  # ссылки на фоновые задачи загрузки, чтобы их не убил GC


async def _edit(cb: CallbackQuery, text: str, markup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # текст не изменился — игнорируем


async def _deny_if_not_owner(cb: CallbackQuery) -> bool:
    if cb.from_user.id != settings.owner_id:
        await cb.answer("⛔ Только для владельца", show_alert=True)
        return True
    return False


async def _deny_if_not_admin(cb: CallbackQuery) -> bool:
    if not access.is_admin(cb.from_user.id):
        await cb.answer("⛔ Только для администратора", show_alert=True)
        return True
    return False


# ---------- вход в меню ----------
@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await views.view_main(m.from_user.id)
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


# ---------- reply-кнопки главного меню ----------
@router.message(F.text == kb.RB_SELLERS)
async def rb_sellers(m: Message, state: FSMContext):
    await state.clear()
    text, markup = await views.view_sellers(m.from_user.id)
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == kb.RB_USERS)
async def rb_users(m: Message, state: FSMContext):
    if m.from_user.id != settings.owner_id:
        return
    await state.clear()
    text, markup = await views.view_users_menu()
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


@router.message(F.text == kb.RB_STATS)
async def rb_stats(m: Message, state: FSMContext):
    if not access.is_admin(m.from_user.id):
        return
    await state.clear()
    text, markup = await views.view_stats()
    await m.answer(text, reply_markup=markup, parse_mode="HTML")


# ---------- обновление WB-куки (FSM) ----------
@router.message(F.text == kb.RB_COOKIE)
async def rb_cookie(m: Message, state: FSMContext):
    if not access.is_admin(m.from_user.id):
        return
    await state.set_state(SetCookie.waiting)
    await m.answer(
        f"{tge('shop')} <b>Обновление куки WB</b>\n\n"
        "1. Открой <b>wildberries.ru</b> на ПК, залогинься в бизнес-аккаунт.\n"
        "2. F12 → вкладка <b>Network</b> (Сеть), обнови страницу.\n"
        "3. Кликни любой запрос → <b>Headers</b> → <b>Request Headers</b> → "
        "скопируй целиком строку <code>Cookie:</code> (без слова Cookie).\n"
        "4. Пришли её одним сообщением сюда.",
        reply_markup=kb.cancel_kb(),
        parse_mode="HTML",
    )


@router.message(SetCookie.waiting)
async def cookie_input(m: Message, state: FSMContext):
    raw = (m.text or "").strip()
    if "=" not in raw or len(raw) < 20:
        await m.answer(
            "Это не похоже на строку Cookie. Пришли ещё раз или нажмите Отмена.",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.clear()
    n = await wb_client.set_cookie(raw)
    async with Session() as s:
        await repo.set_setting(s, "wb_cookie", raw)
        await s.commit()
    status = await m.answer(
        f"{tge('ok')} Кука обновлена ({n} полей). Обновляю цены, подожди...",
        parse_mode="HTML",
    )
    await silent_resync_all()
    await status.edit_text(
        f"{tge('ok')} Кука обновлена ({n} полей). Цены обновлены.", parse_mode="HTML"
    )


# ---------- часы отчётов ----------
def _parse_hours(csv) -> set[int]:
    return {int(x) for x in (csv or "").split(",") if x.strip()}


def _hours_caption(selected: set[int]) -> str:
    if not selected:
        return "🕐 Часы отчёта: <b>каждый час</b>.\nОтметь часы, когда слать отчёт:"
    hrs = ", ".join(f"{h:02d}:00" for h in sorted(selected))
    return f"🕐 Отчёт в: <b>{hrs}</b> (МСК).\nЖми час, чтобы вкл/выкл:"


@router.message(F.text == kb.RB_HOURS)
async def rb_hours(m: Message, state: FSMContext):
    if not access.is_admin(m.from_user.id):
        return
    await state.clear()
    async with Session() as s:
        selected = _parse_hours(await repo.get_setting(s, "report_hours"))
    await m.answer(
        _hours_caption(selected), reply_markup=kb.hours_grid(selected), parse_mode="HTML"
    )


@router.callback_query(kb.HourCB.filter())
async def toggle_hour(cb: CallbackQuery, callback_data: kb.HourCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        selected = _parse_hours(await repo.get_setting(s, "report_hours"))
        selected.symmetric_difference_update({callback_data.hour})  # toggle
        await repo.set_setting(s, "report_hours", ",".join(map(str, sorted(selected))))
        await s.commit()
    await _edit(cb, _hours_caption(selected), kb.hours_grid(selected))
    await cb.answer()


# ---------- навигация ----------
@router.callback_query(kb.Nav.filter(F.to == "main"))
async def nav_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _edit(cb, f"{tge('list')} Главное меню — выберите внизу 👇", None)
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "cancel"))
async def nav_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _edit(cb, f"{tge('list')} Главное меню — выберите внизу 👇", None)
    await cb.answer("Отменено")


@router.callback_query(kb.Nav.filter(F.to == "sellers"))
async def nav_sellers(cb: CallbackQuery):
    text, markup = await views.view_sellers(cb.from_user.id)
    await _edit(cb, text, markup)
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "list_sellers"))
async def nav_list_sellers(cb: CallbackQuery):
    text, markup = await views.view_list_sellers()
    await _edit(cb, text, markup)
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "users"))
async def nav_users(cb: CallbackQuery):
    if await _deny_if_not_owner(cb):
        return
    text, markup = await views.view_users_menu()
    await _edit(cb, text, markup)
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "list_users"))
async def nav_list_users(cb: CallbackQuery):
    if await _deny_if_not_owner(cb):
        return
    text, markup = await views.view_list_users()
    await _edit(cb, text, markup)
    await cb.answer()


@router.callback_query(kb.Nav.filter(F.to == "stats"))
async def nav_stats(cb: CallbackQuery):
    if await _deny_if_not_admin(cb):
        return
    text, markup = await views.view_stats()
    await _edit(cb, text, markup)
    await cb.answer()


# ---------- проверка выбранного магазина ----------
@router.callback_query(kb.Nav.filter(F.to == "check_seller"))
async def nav_check_seller(cb: CallbackQuery):
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        await cb.answer("Список магазинов пуст", show_alert=True)
        return
    await _edit(cb, "Выберите магазин для проверки:", kb.sellers_check_list(sellers))
    await cb.answer()


FAST_HINT = (
    f"{tge('clock')} Приоритетные магазины (⚡) проверяются раз в минуту, "
    "остальные — реже. Жми магазин, чтобы вкл/выкл:"
)


@router.callback_query(kb.Nav.filter(F.to == "fast_sellers"))
async def nav_fast_sellers(cb: CallbackQuery):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        await cb.answer("Список магазинов пуст", show_alert=True)
        return
    await _edit(cb, FAST_HINT, kb.sellers_fast_list(sellers))
    await cb.answer()


@router.callback_query(kb.SellerCB.filter(F.action == "fast"))
async def toggle_fast(cb: CallbackQuery, callback_data: kb.SellerCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sl = await repo.get_seller(s, callback_data.sid)
        new_val = not sl.is_fast if sl else False
        if sl:
            await repo.set_seller_fast(s, callback_data.sid, new_val)
            await s.commit()
        sellers = await repo.list_sellers(s)
    await _edit(cb, FAST_HINT, kb.sellers_fast_list(sellers))
    await cb.answer("⚡ Приоритет включён" if new_val else "Приоритет снят")


PRICE_HINT = (
    f"{tge('clock')} Режим цены: 🏢 бизнес-цена (нужна кука) / 👤 розница. "
    "Жми магазин, чтобы переключить:"
)


@router.callback_query(kb.Nav.filter(F.to == "price_sellers"))
async def nav_price_sellers(cb: CallbackQuery):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        await cb.answer("Список магазинов пуст", show_alert=True)
        return
    await _edit(cb, PRICE_HINT, kb.sellers_price_list(sellers))
    await cb.answer()


@router.callback_query(kb.SellerCB.filter(F.action == "mode"))
async def toggle_mode(cb: CallbackQuery, callback_data: kb.SellerCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sl = await repo.get_seller(s, callback_data.sid)
        new_b2b = not sl.b2b if sl else True
        if sl:
            await repo.set_seller_b2b(s, callback_data.sid, new_b2b)
            await s.commit()
        sellers = await repo.list_sellers(s)
    await _edit(cb, PRICE_HINT, kb.sellers_price_list(sellers))
    await cb.answer(f"Режим: {reporting.mode_tag(new_b2b)}")


def _subj_caption(selected: set[int]) -> str:
    if not selected:
        return (
            "📂 Фильтр предметов: <b>все категории</b>.\n"
            "Отметь, что мониторить (пусто = всё подряд):"
        )
    return (
        f"📂 В мониторинге предметов: <b>{len(selected)}</b>.\n"
        "Жми, чтобы вкл/выкл (применится при следующем синке):"
    )


@router.callback_query(kb.Nav.filter(F.to == "subjects"))
async def nav_subjects(cb: CallbackQuery):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        overview = await repo.subjects_overview(s)
        selected = await repo.get_subject_ids(s)
    if not overview:
        await cb.answer(
            "Каталог ещё не загружен — предметы появятся после первого опроса магазинов",
            show_alert=True,
        )
        return
    await _edit(cb, _subj_caption(selected), kb.subjects_filter_list(overview, selected))
    await cb.answer()


@router.callback_query(kb.SubjectCB.filter())
async def toggle_subject(cb: CallbackQuery, callback_data: kb.SubjectCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        selected = await repo.get_subject_ids(s)
        selected.symmetric_difference_update({callback_data.sid})  # toggle
        await repo.set_subject_ids(s, selected)
        await s.commit()
        overview = await repo.subjects_overview(s)
    await _edit(cb, _subj_caption(selected), kb.subjects_filter_list(overview, selected))
    await cb.answer("Фильтр обновлён")


@router.callback_query(kb.SellerCB.filter(F.action == "check"))
async def check_seller_do(cb: CallbackQuery, callback_data: kb.SellerCB):
    await cb.answer("Проверяю...")
    async with Session() as s:
        seller = await repo.get_seller(s, callback_data.sid)
    name = seller.name if seller and seller.name else callback_data.sid
    await _edit(cb, f"{tge('clock')} Проверяю магазин «{esc(name)}»...", None)
    ok = await views.run_checknow_one(cb.bot, cb.from_user.id, callback_data.sid)
    _, markup = await views.view_sellers(cb.from_user.id)
    msg = f"{tge('ok')} Готово." if ok else "Магазин не найден."
    # редактируем «Проверяю...» в результат, чтобы не плодить лишнее сообщение
    await _edit(cb, msg, markup)


# ---------- добавление магазина (FSM) ----------
@router.callback_query(kb.Nav.filter(F.to == "add_seller"))
async def nav_add_seller(cb: CallbackQuery, state: FSMContext):
    if await _deny_if_not_admin(cb):
        return
    await state.set_state(AddSeller.waiting_id)
    await _edit(cb, f"{tge('add')} Пришлите ID или ссылку на магазин:", kb.cancel_kb())
    await cb.answer()


@router.message(AddSeller.waiting_id)
async def add_seller_input(m: Message, state: FSMContext):
    sid = parse_supplier_id(m.text or "")
    if not sid:
        slug = parse_seller_slug(m.text or "")
        if slug:
            sid = await wb_client.resolve_seller_slug(slug)
    if not sid:
        await m.answer(
            "Не похоже на ID или ссылку. Пришлите ещё раз или нажмите Отмена.",
            reply_markup=kb.cancel_kb(),
        )
        return
    async with Session() as s:
        if await repo.get_seller(s, sid):
            await state.clear()
            text, markup = await views.view_sellers(m.from_user.id)
            await m.answer(f"Магазин {sid} уже в списке.", reply_markup=markup)
            return
    info = await wb_client.fetch_supplier_info(sid)
    name = (info or {}).get("trademark") or (info or {}).get("supplierName")
    brand = (info or {}).get("trademark")
    # имя/бренд держим в FSM, магазин создаём после выбора режима цены
    await state.update_data(sid=sid, name=name, brand=brand)
    await state.set_state(AddSeller.waiting_mode)
    await m.answer(
        f"{tge('add')} Магазин «{esc(name or sid)}». Каким аккаунтом следить за ценой?",
        reply_markup=kb.price_mode_kb(),
        parse_mode="HTML",
    )


def _start_seed(status, seller, name, sid) -> None:
    """Фоновая первичная загрузка ассортимента (у крупных — до 10+ мин)."""
    async def _seed():
        try:
            fetched, _, _ = await sync_seller(seller, silent_seed=True)
            await status.edit_text(
                f"{tge('ok')} Магазин «{esc(name or sid)}» загружен. Товаров: {len(fetched)}.",
                parse_mode="HTML",
            )
        except Exception as e:
            await status.edit_text(
                f"{tge('warn')} «{esc(name or sid)}»: загрузка не удалась: {esc(e)}",
                parse_mode="HTML",
            )
    task = asyncio.create_task(_seed())
    _bg_tasks.add(task)  # держим ссылку, иначе задачу может убрать GC
    task.add_done_callback(_bg_tasks.discard)


@router.callback_query(AddSeller.waiting_mode, kb.PriceModeCB.filter())
async def choose_price_mode(cb: CallbackQuery, callback_data: kb.PriceModeCB, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    sid, name, brand = data.get("sid"), data.get("name"), data.get("brand")
    if not sid:
        await cb.answer("Сессия истекла, начните заново", show_alert=True)
        return
    b2b = bool(callback_data.b2b)
    async with Session() as s:
        await repo.add_seller(s, sid, name=name, brand=brand, b2b=b2b)
        await s.commit()
        seller = await repo.get_seller(s, sid)
    status = await cb.message.edit_text(
        f"{tge('clock')} Магазин «{esc(name or sid)}» ({reporting.mode_tag(b2b)}) добавлен. "
        "Гружу ассортимент в фоне — пришлю, как закончу.",
        parse_mode="HTML",
    )
    _start_seed(status, seller, name, sid)
    await cb.answer()
    text, markup = await views.view_sellers(cb.from_user.id)
    await cb.bot.send_message(cb.from_user.id, text, reply_markup=markup, parse_mode="HTML")


# ---------- удаление магазина ----------
@router.callback_query(kb.Nav.filter(F.to == "del_seller"))
async def nav_del_seller(cb: CallbackQuery):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        await cb.answer("Список магазинов пуст", show_alert=True)
        return
    await _edit(cb, "Выберите магазин для удаления:", kb.sellers_delete_list(sellers))
    await cb.answer()


@router.callback_query(kb.SellerCB.filter(F.action == "del"))
async def del_seller_ask(cb: CallbackQuery, callback_data: kb.SellerCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        sl = await repo.get_seller(s, callback_data.sid)
    name = sl.name if sl and sl.name else callback_data.sid
    await _edit(cb, f"Точно удалить «{esc(name)}»?", kb.seller_delete_confirm(callback_data.sid))
    await cb.answer()


@router.callback_query(kb.SellerCB.filter(F.action == "delc"))
async def del_seller_do(cb: CallbackQuery, callback_data: kb.SellerCB):
    if await _deny_if_not_admin(cb):
        return
    async with Session() as s:
        await repo.remove_seller(s, callback_data.sid)
        await s.commit()
        sellers = await repo.list_sellers(s)
    await cb.answer("Удалён")
    if sellers:
        await _edit(cb, "Выберите магазин для удаления:", kb.sellers_delete_list(sellers))
    else:
        await _edit(cb, "Все магазины удалены.", kb.back_kb("sellers"))


# ---------- выдача доступа (FSM) ----------
@router.callback_query(kb.Nav.filter(F.to == "add_user"))
async def nav_add_user(cb: CallbackQuery, state: FSMContext):
    if await _deny_if_not_owner(cb):
        return
    await state.set_state(AddUser.waiting_id)
    await _edit(cb, f"{tge('add')} Пришлите Telegram ID пользователя:", kb.cancel_kb())
    await cb.answer()


@router.message(AddUser.waiting_id)
async def add_user_input(m: Message, state: FSMContext):
    arg = (m.text or "").strip()
    if not arg.isdigit():
        await m.answer(
            "Нужен числовой Telegram ID. Пришлите ещё раз или нажмите Отмена.",
            reply_markup=kb.cancel_kb(),
        )
        return
    uid = int(arg)
    await state.clear()
    async with Session() as s:
        ok = await repo.add_user(s, uid)
        await s.commit()
    access.add(uid)
    result = f"{tge('ok')} Доступ выдан {uid}." if ok else f"{uid} уже имеет доступ."
    text, markup = await views.view_users_menu()
    await m.answer(result, reply_markup=markup, parse_mode="HTML")


# ---------- отзыв доступа ----------
@router.callback_query(kb.Nav.filter(F.to == "del_user"))
async def nav_del_user(cb: CallbackQuery):
    if await _deny_if_not_owner(cb):
        return
    async with Session() as s:
        users = await repo.list_users(s)
    markup = kb.users_delete_list(users, settings.owner_id)
    await _edit(cb, "Выберите пользователя для отзыва доступа:", markup)
    await cb.answer()


@router.callback_query(kb.UserCB.filter(F.action == "del"))
async def del_user_ask(cb: CallbackQuery, callback_data: kb.UserCB):
    if await _deny_if_not_owner(cb):
        return
    if callback_data.uid == settings.owner_id:
        await cb.answer("Нельзя забрать доступ у владельца", show_alert=True)
        return
    await _edit(
        cb,
        f"Точно забрать доступ у {callback_data.uid}?",
        kb.user_delete_confirm(callback_data.uid),
    )
    await cb.answer()


@router.callback_query(kb.UserCB.filter(F.action == "delc"))
async def del_user_do(cb: CallbackQuery, callback_data: kb.UserCB):
    if await _deny_if_not_owner(cb):
        return
    if callback_data.uid == settings.owner_id:
        await cb.answer("Нельзя забрать доступ у владельца", show_alert=True)
        return
    async with Session() as s:
        await repo.remove_user(s, callback_data.uid)
        await s.commit()
        users = await repo.list_users(s)
    access.remove(callback_data.uid)
    await cb.answer("Доступ отозван")
    await _edit(
        cb,
        "Выберите пользователя для отзыва доступа:",
        kb.users_delete_list(users, settings.owner_id),
    )
