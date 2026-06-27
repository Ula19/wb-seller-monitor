"""Логика мониторинга: синхронизация магазинов, детект изменений, рассылка, отчёты."""

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


async def recipient_ids(s) -> list[int]:
    """Получатели рассылки: владелец (из .env) + whitelist из БД, без дублей."""
    return list({settings.owner_id, *await repo.list_user_ids(s)})


def detect_changes(old_price, old_stock, p):
    """Сравнивает старые цену/остаток с новыми. Возвращает список событий."""
    events = []
    if old_price and p.price and old_price != p.price:
        pct = abs(p.price - old_price) / old_price * 100
        if pct >= settings.price_change_threshold_pct:
            events.append(("price", old_price, p.price))
    old_in = (old_stock or 0) > 0
    new_in = (p.stock or 0) > 0
    if old_in != new_in:
        events.append(("availability", old_stock or 0, p.stock or 0))
    return events


async def sync_seller(seller: models.Seller, *, silent_seed: bool = False):
    """Тянет каталог, обновляет БД. Возвращает (все_товары, новые, изменения).

    silent_seed=True — первичная загрузка при добавлении магазина:
    товары помечаются известными, новинки/изменения не формируются.
    """
    async with Session() as s:
        subjects = await repo.get_subject_ids(s)
    fetched = await wb_client.fetch_seller_catalog(seller.supplier_id, seller.b2b, subjects)
    new = []
    changes = []
    if fetched:
        async with Session() as s:
            rows = await repo.get_products(s, seller.supplier_id)
            existing = {r.nm_id: (r.price, r.stock) for r in rows}
            seen: set[int] = set()
            for p in fetched:
                seen.add(p.nm_id)
                old = existing.get(p.nm_id)
                if old is None:
                    new.append(p)
                elif not silent_seed:
                    ev = detect_changes(old[0], old[1], p)
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
        "sync продавца %s: товаров %d, новых %d, изменений %d",
        seller.supplier_id, len(fetched), len(new), len(changes),
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
    """Рассылает новинки и изменения цены/наличия."""
    async with Session() as s:
        user_ids = await recipient_ids(s)
    if not user_ids:
        return
    for p in new:
        await broadcast_new(bot, user_ids, seller, p)
    for p, events in changes:
        await broadcast_change(bot, user_ids, seller, p, events)


async def sync_and_notify(bot, seller) -> list:
    """Синхронизирует магазин и сразу рассылает новинки/изменения."""
    fetched, new, changes = await sync_seller(seller)
    await notify_seller(bot, seller, new, changes)
    return fetched


async def send_report_to(bot, user_ids, seller, products) -> None:
    name = seller.name or str(seller.supplier_id)
    if len(products) >= settings.big_shop_threshold:
        data = reporting.build_excel(name, products)
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
    for seller in sellers:
        try:
            await sync_seller(seller, silent_seed=True)
        except Exception as e:
            log.warning("тихий ре-синк %s упал: %s", seller.supplier_id, e)


async def _check_cookie_health(bot) -> None:
    """Если b2b-цены не приходят несколько раз подряд — кука протухла, зовём владельца."""
    if wb_client.b2b_fail_streak >= 2 and not wb_client.cookie_alerted:
        wb_client.cookie_alerted = True
        try:
            await bot.send_message(
                settings.owner_id,
                "⚠️ Кука WB протухла — бизнес-цены не приходят. "
                "Обнови её кнопкой «🔑 Куки».",
            )
        except Exception as e:
            log.warning("алерт о куке не доставлен: %s", e)
    elif wb_client.b2b_fail_streak == 0:
        wb_client.cookie_alerted = False


async def monitoring_job(bot, only_fast: bool = False) -> None:
    """Проход по магазинам: новинки + изменения цены/наличия.

    only_fast=True — быстрый джоб (раз в минуту) только по приоритетным магазинам;
    only_fast=False — обычный джоб по остальным. Так приоритетные не опрашиваются дважды.
    """
    async with Session() as s:
        sellers = await repo.list_sellers(s, fast=True if only_fast else False)
    tag = "быстрый" if only_fast else "обычный"
    log.info("мониторинг (%s): старт, магазинов %d", tag, len(sellers))
    for seller in sellers:
        try:
            await sync_and_notify(bot, seller)
        except Exception as e:
            log.exception("синхронизация %s упала: %s", seller.supplier_id, e)
    if not only_fast:  # проверку куки гоняем в обычном джобе, не каждую минуту
        await _check_cookie_health(bot)
    log.info("мониторинг (%s): завершён", tag)


async def report_job(bot) -> None:
    """Часовой отчёт по всем магазинам из БД (без обращения к WB).

    Шлём только в выбранные часы (МСК); список пуст — каждый час.
    """
    async with Session() as s:
        hours_csv = await repo.get_setting(s, "report_hours")
    if hours_csv:
        selected = {int(x) for x in hours_csv.split(",") if x.strip()}
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


if __name__ == "__main__":  # self-check разбора часов
    parse = lambda csv: {int(x) for x in csv.split(",") if x.strip()}
    assert parse("9,13,18") == {9, 13, 18}
    assert parse("") == set()
    assert 13 in parse("9,13,18") and 12 not in parse("9,13,18")
    print("ok")
