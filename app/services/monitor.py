"""Логика мониторинга: синхронизация магазинов, детект изменений, рассылка, отчёты."""

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile

from app.config import settings
from app.db import models, repo
from app.db.base import Session
from app.services import reporting
from app.wb.client import wb_client

log = logging.getLogger(__name__)

# Один общий лок на «проход»: мониторинг, ручная проверка и ре-синк куки не идут
# параллельно. Иначе две корутины синхронят один магазин из разных Session → свежая
# цена затирается старой, а параллельный не-silent проход шлёт ложное «цена снизилась»,
# которое silent-ресинк как раз призван заглушить.
# ponytail: грубый лок на весь проход; при долгом ре-синке приоритетные ждут (коалесятся).
_pass_lock = asyncio.Lock()


async def recipient_ids(s) -> list[int]:
    """Получатели рассылки: владелец (из .env) + whitelist из БД, без дублей."""
    return list({settings.owner_id, *await repo.list_user_ids(s)})


def detect_changes(old_price, old_stock, p):
    """Сравнивает старые цену/остаток с новыми. Возвращает список событий."""
    events = []
    # Алертим только СНИЖЕНИЕ цены (демпинг конкурента); рост — не шлём.
    # Мягкий порог, чтобы мизерные копеечные сдвиги не спамили чат.
    if old_price and p.price and p.price < old_price:
        drop_pct = (old_price - p.price) / old_price * 100
        if drop_pct >= settings.price_drop_threshold_pct:
            events.append(("price", old_price, p.price))
    old_in = (old_stock or 0) > 0
    new_in = (p.stock or 0) > 0
    if old_in != new_in:
        events.append(("availability", old_stock or 0, p.stock or 0))
    return events


def _shelf_dropped(old_shelf, new_shelf) -> bool:
    """Витрина (каталожная цена) упала ≥ порога — триггер для enrich точной цены."""
    if not old_shelf or not new_shelf or new_shelf >= old_shelf:
        return False
    return (old_shelf - new_shelf) / old_shelf * 100 >= settings.price_drop_threshold_pct


async def sync_seller(
    seller: models.Seller, *, silent_seed: bool = False, full_enrich: bool = False
):
    """Тянет каталог (без куки), навешивает точную цену по триггеру/sweep, обновляет БД.

    Возвращает (все_товары, новые, изменения). Фаза 1: каталог → витринная цена
    (`shelf_price`) + сток по всем. Фаза 2: `enrich_prices` (detail с кукой) только там,
    где витрина упала (триггер) ИЛИ для всех при full_enrich (sweep/сид). Товары без
    свежей detail-цены сохраняют прежнюю «нашу» цену — не затираем витринной.
    silent_seed=True — первичная загрузка: товары помечаются известными, не шумим.
    """
    fetched = await wb_client.fetch_seller_catalog(seller.supplier_id)
    new = []
    changes = []
    priced: set[int] = set()
    full = full_enrich or silent_seed  # сид/ре-синк всегда обогащаем целиком
    if fetched:
        async with Session() as s:
            rows = await repo.get_products(s, seller.supplier_id)
            existing = {r.nm_id: r for r in rows}
            to_enrich = [
                p for p in fetched
                if full
                or p.nm_id not in existing
                or _shelf_dropped(existing[p.nm_id].shelf_price, p.shelf_price)
            ]
            if to_enrich:
                priced = await wb_client.enrich_prices(to_enrich, seller.b2b)
            # без свежей detail-цены — сохраняем прежнюю «нашу» (в p.price сейчас витрина)
            for p in fetched:
                if p.nm_id not in priced:
                    old = existing.get(p.nm_id)
                    if old is not None and old.price is not None:
                        p.price = old.price
            seen: set[int] = set()
            for p in fetched:
                seen.add(p.nm_id)
                old = existing.get(p.nm_id)
                if old is None:
                    new.append(p)
                elif not silent_seed:
                    ev = detect_changes(old.price, old.stock, p)
                    if ev:
                        changes.append((p, ev))
                await repo.upsert_product(s, p)
            await repo.deactivate_missing(s, seller.supplier_id, seen)
            sl = await s.get(models.Seller, seller.supplier_id)
            if sl:
                sl.last_check_at = datetime.now(timezone.utc)
            if silent_seed:
                for p in new:
                    await repo.mark_notified(s, p.supplier_id, p.nm_id)
            await s.commit()
        if silent_seed:
            new = []
    log.info(
        "sync продавца %s: товаров %d, обогащено %d, новых %d, изменений %d",
        seller.supplier_id, len(fetched), len(priced), len(new), len(changes),
    )
    return fetched, new, changes


async def broadcast_change(bot, user_ids, seller, p, events) -> None:
    text = reporting.change_caption(seller.name or str(seller.supplier_id), p, events, seller.b2b)
    markup = reporting.wb_button(p.url)
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.warning("уведомление (изменение) не доставлено %s: %s", uid, e)


async def broadcast_new(bot, user_ids, seller, p) -> None:
    text = reporting.new_caption(seller.name or str(seller.supplier_id), p, seller.b2b)
    markup = reporting.wb_button(p.url)
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            log.warning("уведомление (новинка) не доставлено %s: %s", uid, e)


async def notify_seller(bot, seller, new, changes) -> None:
    """Рассылает новинки и изменения цены/наличия отдельными сообщениями в чат."""
    if not new and not changes:
        return
    async with Session() as s:
        user_ids = await recipient_ids(s)
    if not user_ids:
        return
    for p in new:
        await broadcast_new(bot, user_ids, seller, p)
    for p, events in changes:
        await broadcast_change(bot, user_ids, seller, p, events)


async def sync_and_notify(bot, seller, full_enrich: bool = False) -> list:
    """Синхронизирует магазин и сразу рассылает новинки/изменения."""
    fetched, new, changes = await sync_seller(seller, full_enrich=full_enrich)
    await notify_seller(bot, seller, new, changes)
    return fetched


async def send_report_to(bot, user_ids, seller, products) -> None:
    name = seller.name or str(seller.supplier_id)
    if len(products) >= settings.big_shop_threshold:
        data = reporting.build_excel(name, products, seller.b2b)
        doc = BufferedInputFile(data, filename=f"seller_{seller.supplier_id}.xlsx")
        caption = f"🏪 {name} ({reporting.mode_tag(seller.b2b)})\nВсего товаров: {len(products)}"
        for uid in user_ids:
            try:
                await bot.send_document(uid, doc, caption=caption)
            except Exception as e:
                log.warning("отчёт (xlsx) не доставлен %s: %s", uid, e)
    else:
        for chunk in reporting.chunk_text(reporting.hourly_report_text(name, products, seller.b2b)):
            for uid in user_ids:
                try:
                    await bot.send_message(uid, chunk, parse_mode="HTML")
                except Exception as e:
                    log.warning("отчёт не доставлен %s: %s", uid, e)


async def silent_resync_all() -> None:
    """Тихо пересинхронизирует все магазины: обновить цены без уведомлений.

    Зовём после обновления куки, чтобы протухшие цены сменились на бизнес-цены
    и при этом не сыпались ложные «цена изменилась».
    """
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    async with _pass_lock:  # не пересекаемся с джобами мониторинга (ложные алерты/затирание)
        for seller in sellers:
            try:
                await sync_seller(seller, silent_seed=True)
            except Exception as e:
                log.warning("тихий ре-синк %s упал: %s", seller.supplier_id, e)


COOKIE_ALERT = (
    "‼️‼️‼️ <b>ВНИМАНИЕ: КУКА WB ПРОТУХЛА</b> ‼️‼️‼️\n\n"
    "🔴 Бизнес-цены <b>не приходят</b>.\n"
    "👉 Срочно обнови куку кнопкой «🔑 Куки»."
)


async def _check_cookie_health(bot) -> None:
    """Если detail-цены не приходят несколько раз подряд — кука протухла, шумим всем.

    Порог низкий: enrich теперь редкий, но sweep дёргает detail по всем магазинам за
    проход — при мёртвой куке счётчик быстро наберёт несколько провалов.
    """
    if wb_client.b2b_fail_streak >= 3 and not wb_client.cookie_alerted:
        wb_client.cookie_alerted = True
        async with Session() as s:
            user_ids = await recipient_ids(s)
        for uid in user_ids:
            try:
                await bot.send_message(uid, COOKIE_ALERT, parse_mode="HTML")
            except Exception as e:
                log.warning("алерт о куке не доставлен %s: %s", uid, e)
    elif wb_client.b2b_fail_streak == 0:
        wb_client.cookie_alerted = False


def _within_work_hours(start, end, hour) -> bool:
    """Час hour внутри рабочего окна [start, end) (МСК). Пусто/вырождено = круглосуточно."""
    if start is None or end is None or start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # ночной переход, напр. 22→06


async def _work_window() -> tuple[int | None, int | None]:
    async with Session() as s:
        ws = await repo.get_setting(s, "work_start")
        we = await repo.get_setting(s, "work_end")
    start = int(ws) if ws not in (None, "") else None
    end = int(we) if we not in (None, "") else None
    return start, end


# only_fast -> время последнего полного прохода (МСК): интервальный дедуп в памяти.
# ponytail: in-memory; при рестарте первый цикл сделает sweep — безвредно (заодно освежит).
_last_sweep: dict[bool, datetime] = {}


def _due_sweep(only_fast: bool, now: datetime) -> bool:
    """Пора ли полный проход с кукой: прошло ≥ price_sweep_interval_minutes с прошлого.

    Дедуп раздельно по джобам (быстрый/обычный делят магазины) — каждый метёт свою часть.
    Гранулярность ограничена интервалом джоба (напр. обычный раз в 10 мин). 0 = выключено.
    """
    interval = settings.price_sweep_interval_minutes
    if interval <= 0:
        return False
    last = _last_sweep.get(only_fast)
    if last is not None and (now - last).total_seconds() < interval * 60:
        return False
    _last_sweep[only_fast] = now
    return True


async def monitoring_job(bot, only_fast: bool = False) -> None:
    """Проход по магазинам: новинки + изменения цены/наличия.

    only_fast=True — быстрый джоб (раз в минуту) только по приоритетным магазинам;
    only_fast=False — обычный джоб по остальным. Так приоритетные не опрашиваются дважды.
    Каждый цикл ходит только в каталог (без куки); detail с кукой — по триггеру внутри
    sync_seller или полным проходом в часы price_sweep_hours (full_enrich).
    """
    start, end = await _work_window()
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    if not _within_work_hours(start, end, now.hour):
        log.info("мониторинг пропущен: вне рабочих часов (%s–%s), сейчас %d",
                 start, end, now.hour)
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s, fast=True if only_fast else False)
    tag = "быстрый" if only_fast else "обычный"
    full = _due_sweep(only_fast, now)
    if full:
        log.info("мониторинг (%s): полный проход с кукой (sweep, интервал %d мин)",
                 tag, settings.price_sweep_interval_minutes)
    log.info("мониторинг (%s): старт, магазинов %d", tag, len(sellers))
    async with _pass_lock:  # не пересекаемся с другим проходом/ре-синком куки
        for seller in sellers:
            try:
                await sync_and_notify(bot, seller, full_enrich=full)
            except Exception as e:
                log.exception("синхронизация %s упала: %s", seller.supplier_id, e)
        if not only_fast:  # проверку куки гоняем в обычном джобе, не каждую минуту
            await _check_cookie_health(bot)
    log.info("мониторинг (%s): завершён", tag)


async def report_job(bot) -> None:
    """Часовой отчёт по всем магазинам из БД (без обращения к WB).

    Шлём только в выбранные часы (МСК); список пуст — отчёт не нужен вовсе.
    """
    async with Session() as s:
        hours_csv = await repo.get_setting(s, "report_hours")
    selected = {int(x) for x in (hours_csv or "").split(",") if x.strip()}
    if not selected:
        log.info("отчёт пропущен: часы не выбраны")
        return
    hour = datetime.now(ZoneInfo("Europe/Moscow")).hour
    if hour not in selected:
        log.info("отчёт пропущен: %d не в списке %s", hour, hours_csv)
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s)
        user_ids = await recipient_ids(s)
        data = [(sl, await repo.get_active_products(s, sl.supplier_id)) for sl in sellers]
    for seller, products in data:
        await send_report_to(bot, user_ids, seller, products)


if __name__ == "__main__":  # self-check разбора часов и рабочего окна
    parse = lambda csv: {int(x) for x in csv.split(",") if x.strip()}
    assert parse("9,13,18") == {9, 13, 18}
    assert parse("") == set()
    assert 13 in parse("9,13,18") and 12 not in parse("9,13,18")
    # окно [8,23): день внутри, ночь снаружи
    assert _within_work_hours(8, 23, 10) and not _within_work_hours(8, 23, 3)
    assert not _within_work_hours(8, 23, 23)  # конец не включаем
    # ночной переход 22→6: 23 и 2 внутри, 12 снаружи
    assert _within_work_hours(22, 6, 23) and _within_work_hours(22, 6, 2)
    assert not _within_work_hours(22, 6, 12)
    assert _within_work_hours(None, None, 3)  # не задано = круглосуточно
    # detect_changes: снижение ≥1% алертит, <1% и рост — молчат
    from types import SimpleNamespace
    settings.price_drop_threshold_pct = 1.0
    kinds = lambda evs: [e[0] for e in evs]
    assert "price" in kinds(detect_changes(1000, 5, SimpleNamespace(price=980, stock=5)))   # −2%
    assert "price" not in kinds(detect_changes(1000, 5, SimpleNamespace(price=995, stock=5)))  # −0.5%
    assert "price" not in kinds(detect_changes(1000, 5, SimpleNamespace(price=1200, stock=5)))  # рост
    # _shelf_dropped: витрина упала ≥1% → триггер enrich; рост/<порога/нет старой → нет
    assert _shelf_dropped(1000, 980)          # −2%
    assert not _shelf_dropped(1000, 995)      # −0.5%
    assert not _shelf_dropped(1000, 1200)     # рост
    assert not _shelf_dropped(None, 980)      # первый заход — старой витрины нет
    # _due_sweep: первый заход — да; до истечения интервала — нет; после — снова да; 0 — выкл
    settings.price_sweep_interval_minutes = 15
    _last_sweep.clear()
    t0 = datetime(2026, 7, 9, 10, 0)
    assert _due_sweep(False, t0)                            # первый заход
    assert not _due_sweep(False, t0.replace(minute=10))    # +10 мин < 15
    assert _due_sweep(False, t0.replace(minute=20))        # +20 мин ≥ 15
    settings.price_sweep_interval_minutes = 0
    assert not _due_sweep(True, t0)                         # выключено
    print("ok")
