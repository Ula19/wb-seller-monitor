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
# ponytail: грубый лок на весь проход; при долгом ре-синке очередной цикл ждёт (коалесится).
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


async def sync_seller(
    seller: models.Seller, *, silent_seed: bool = False, slot=None
):
    """Тянет каталог (без куки), обновляет БД. Возвращает (все_товары, новые, изменения).

    Розница (`b2b=False`): каталог уже даёт точную цену — куку не трогаем вовсе.
    Бизнес (`b2b=True`): каждый вызов навешиваем бизнес-цену из detail (`enrich_prices`,
    с кукой) на ВСЕ товары — без триггеров и sweep; товары без свежей бизнес-цены
    сохраняют прежнюю и помечаются price_stale. Сток/наличие — всегда из каталога.
    silent_seed=True — первичная загрузка: товары помечаются известными, не шумим.
    """
    fetched = await wb_client.fetch_seller_catalog(seller.supplier_id, slot=slot)
    new = []
    changes = []
    priced: set[int] = set()
    if fetched:
        async with Session() as s:
            rows = await repo.get_products(s, seller.supplier_id)
            existing = {r.nm_id: r for r in rows}
            # Розница: каталог уже даёт точную цену (p.price = shelf_price из normalize).
            # Бизнес-цена видна только с кукой → detail на все товары каждый цикл.
            if seller.b2b:
                priced = await wb_client.enrich_prices(fetched)
                # без свежей бизнес-цены — сохраняем прежнюю (в p.price сейчас витрина)
                # и помечаем: цена не подтверждена (кука мертва/detail упал)
                for p in fetched:
                    if p.nm_id not in priced:
                        p.price_stale = True
                        old = existing.get(p.nm_id)
                        if old is not None and old.price is not None:
                            p.price = old.price
                        elif old is None:
                            # новинка без подтверждённой цены: НЕ сохраняем витрину как
                            # b2b-цену — иначе следующий удачный enrich даст ложное
                            # «цена снизилась» (бизнес-цена ниже витрины)
                            p.price = None
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


async def sync_and_notify(bot, seller, slot=None) -> list:
    """Синхронизирует магазин и сразу рассылает новинки/изменения."""
    fetched, new, changes = await sync_seller(seller, slot=slot)
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

    b2b дёргает detail каждый цикл — при мёртвой куке порог 3 набирается за ~3 мин.
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


# счётчик проходов: сдвигает раскладку магазин→слот, чтобы забаненный слот
# не морил одни и те же магазины каждый цикл
_pass_no = 0


async def monitoring_job(bot) -> None:
    """Единый проход по ВСЕМ магазинам раз в monitor_interval_seconds.

    Каждый магазин каждый цикл: каталог без куки (розница = готовая цена),
    b2b — плюс detail с кукой на все товары внутри sync_seller. Никаких
    приоритетов/триггеров/sweep — все магазины равны, всё свежее каждую минуту.
    """
    global _pass_no
    start, end = await _work_window()
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    if not _within_work_hours(start, end, now.hour):
        log.info("мониторинг пропущен: вне рабочих часов (%s–%s), сейчас %d",
                 start, end, now.hour)
        return
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    log.info("мониторинг: старт, магазинов %d", len(sellers))
    t0 = asyncio.get_event_loop().time()

    async def _run_bucket(slot, bucket) -> list[int]:
        skipped = []  # магазины этого слота с пустым каталогом (429/бан/сеть)
        for seller in bucket:
            try:
                fetched = await sync_and_notify(bot, seller, slot=slot)
                if not fetched:
                    skipped.append(seller.supplier_id)
            except Exception as e:
                log.exception("синхронизация %s упала: %s", seller.supplier_id, e)
        return skipped

    async with _pass_lock:  # весь проход не пересекается с ре-синком куки/ручной проверкой
        # слоты берём ВНУТРИ лока (снаружи снапшот мог бы устареть, пока ждём лок).
        # Ротация между проходами: при 429/бане одного IP страдают разные магазины.
        off = _pass_no % len(wb_client.slots)
        _pass_no += 1
        slots = wb_client.slots[off:] + wb_client.slots[:off]
        for sl in slots:
            sl.err429 = 0  # пер-слот счётчик 429 на этот проход
        # магазины раскидываем по слотам round-robin: слоты идут параллельно, внутри
        # слота — последовательно (свой троттлинг). Даёт реальный минутный цикл.
        buckets = [sellers[i::len(slots)] for i in range(len(slots))]
        by_slot = await asyncio.gather(*(_run_bucket(sl, b) for sl, b in zip(slots, buckets)))
        await _check_cookie_health(bot)
    took = asyncio.get_event_loop().time() - t0
    skipped = [sid for sk in by_slot for sid in sk]
    per_slot = "; ".join(
        f"{sl.proxy or 'direct'}: 429×{sl.err429}, пропустил {len(sk)}"
        for sl, sk in zip(slots, by_slot)
    )
    log.info("мониторинг: завершён за %.1fс, магазинов %d, пропущено %d%s | %s",
             took, len(sellers), len(skipped), f" {skipped}" if skipped else "", per_slot)


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
    # ротация слотов: сдвиг покрывает все позиции и возвращается к исходной
    slots3 = ["a", "b", "c"]
    rotations = {tuple(slots3[o % 3:] + slots3[:o % 3]) for o in range(6)}
    assert len(rotations) == 3 and ("a", "b", "c") in rotations
    print("ok")
