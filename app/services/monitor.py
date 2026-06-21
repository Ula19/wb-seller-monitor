"""Логика мониторинга: синхронизация магазинов, детект изменений, рассылка, отчёты."""

import logging
from datetime import datetime, timezone

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
    fetched = await wb_client.fetch_seller_catalog(seller.supplier_id)
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
    return fetched, new, changes


async def broadcast_new_item(bot, user_ids, seller, p) -> None:
    caption = reporting.new_item_caption(seller.name or str(seller.supplier_id), p)
    photo = None
    raw = await wb_client.download_photo(p.nm_id, p.pics)
    if raw:
        jpg = reporting.webp_to_jpeg(raw)
        if jpg:
            photo = BufferedInputFile(jpg, filename=f"{p.nm_id}.jpg")
    for uid in user_ids:
        try:
            if photo:
                await bot.send_photo(uid, photo, caption=caption, parse_mode="HTML")
            else:
                await bot.send_message(uid, caption, parse_mode="HTML")
        except Exception as e:
            log.warning("уведомление (новинка) не доставлено %s: %s", uid, e)


async def broadcast_change(bot, user_ids, seller, p, events) -> None:
    text = reporting.change_caption(seller.name or str(seller.supplier_id), p, events)
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            log.warning("уведомление (изменение) не доставлено %s: %s", uid, e)


async def notify_seller(bot, seller, new, changes) -> None:
    """Рассылает новинки (с защитой от дублей) и изменения всем пользователям."""
    async with Session() as s:
        user_ids = await recipient_ids(s)
    if not user_ids:
        return
    for p in new:
        async with Session() as s:
            if await repo.is_notified(s, p.supplier_id, p.nm_id):
                continue
            await repo.mark_notified(s, p.supplier_id, p.nm_id)
            await s.commit()
        await broadcast_new_item(bot, user_ids, seller, p)
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
        caption = f"🏪 {name}\nВсего товаров: {len(products)}"
        for uid in user_ids:
            try:
                await bot.send_document(uid, doc, caption=caption)
            except Exception as e:
                log.warning("отчёт (xlsx) не доставлен %s: %s", uid, e)
    else:
        for chunk in reporting.chunk_text(reporting.hourly_report_text(name, products)):
            for uid in user_ids:
                try:
                    await bot.send_message(uid, chunk, parse_mode="HTML")
                except Exception as e:
                    log.warning("отчёт не доставлен %s: %s", uid, e)


async def monitoring_job(bot) -> None:
    """Лёгкий проход каждые N минут: новинки + изменения цены/наличия."""
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    for seller in sellers:
        try:
            await sync_and_notify(bot, seller)
        except Exception as e:
            log.exception("синхронизация %s упала: %s", seller.supplier_id, e)


async def report_job(bot) -> None:
    """Часовой отчёт по всем магазинам из БД (без обращения к WB)."""
    async with Session() as s:
        sellers = await repo.list_sellers(s)
        user_ids = await recipient_ids(s)
        data = [(sl, await repo.get_active_products(s, sl.supplier_id)) for sl in sellers]
    for seller, products in data:
        await send_report_to(bot, user_ids, seller, products)
