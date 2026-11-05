"""Логика мониторинга: синхронизация магазинов, детект изменений, рассылка, отчёты."""

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile

from app.config import settings
from app.crypto import decrypt_json
from app.db import repo
from app.db.base import Session
from app.services import reporting
from app.wb.client import guardium_expires_at, wb_client
from app.wb.parser import NormProduct
from app.wb.web_session import account_cookies, is_logged

log = logging.getLogger(__name__)

# Один общий лок на «проход»: мониторинг, ручная проверка и сид нового магазина не идут
# параллельно. Иначе две корутины синхронят один магазин из разных Session → свежая
# цена затирается старой + ложное «цена снизилась» / флуд «новинок» до отметки сида.
# ponytail: грубый лок на весь проход; при долгом сиде очередной цикл ждёт (коалесится).
_pass_lock = asyncio.Lock()


async def recipient_ids(s) -> list[int]:
    """Получатели системных алертов: владелец (из .env) + whitelist из БД, без дублей."""
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


async def _business_cookies(s, sources) -> dict[int, tuple[int, str]]:
    """user_id → (account_id, куки) бизнес-аккаунта каждого владельца b2b-источника."""
    out: dict[int, tuple[int, str]] = {}
    for uid in {src.user_id for src in sources if src.b2b}:
        acc = await repo.business_account(s, uid)
        if acc is None:
            continue
        try:
            state = decrypt_json(acc.state_enc)
        except Exception as e:
            log.warning("аккаунт %s: сессию не расшифровать: %s", acc.id, type(e).__name__)
            continue
        if is_logged(state):
            out[uid] = (acc.id, account_cookies(state))
    return out


def _source_notes(src, fetched, old, acc, biz_old, biz_new) -> list:
    """Новинки и изменения глазами владельца источника: [(src, товар, события|None)].

    Розница — цена каталога. Бизнес — цена под его бизнес-аккаунтом `acc`
    (account_id, куки): не пришла в этот цикл — прежняя, помечена неподтверждённой
    (и события цены нет). Новинка = nm_id, которого не было в items.
    """
    acc_id = acc[0] if acc else None
    was_biz, new_biz = biz_old.get(acc_id, {}), biz_new.get(acc_id, {})
    notes = []
    for p in fetched:
        o = old.get(p.nm_id)
        if src.b2b:
            was, fresh = was_biz.get(p.nm_id), new_biz.get(p.nm_id)
            view = replace(p, price=fresh if fresh is not None else was, price_stale=fresh is None)
            old_price = was
        else:
            view, old_price = p, (o.price if o else None)
        if o is None:
            notes.append((src, view, None))
        elif ev := detect_changes(old_price, o.stock, view):
            notes.append((src, view, ev))
    return notes


async def sync_seller(supplier_id: int, *, slot=None):
    """Тянет каталог продавца и пишет снимок. Возвращает (продавец, товары, заметки).

    товары: [] — магазин пуст, None — каталог не получен (магазин пропущен).
    заметки — что разослать владельцам источников (см. _source_notes). Источник без
    первой загрузки (seeded_at пуст) молча помечается загруженным: весь ассортимент
    нового магазина «новинками» не шлём.
    Бизнес-цены — по запросу на каждый бизнес-аккаунт владельцев b2b-источников.
    """
    async with Session() as s:
        seller, sources = await repo.seller_sources(s, supplier_id)
        accounts = await _business_cookies(s, sources)
    if seller is None or not sources:
        return seller, [], []
    fetched = await wb_client.fetch_seller_catalog(supplier_id, slot=slot)
    notes: list = []
    biz_new: dict[int, dict[int, int]] = {}
    if fetched is not None:
        for acc_id, cookies in set(accounts.values()):
            biz_new[acc_id] = await wb_client.business_prices(fetched, cookies)
        nm_ids = [p.nm_id for p in fetched]
        async with Session() as s:
            old = await repo.get_items(s, supplier_id)
            biz_old = {a: await repo.get_account_prices(s, a, nm_ids) for a in biz_new}
            moved = [p for p in fetched
                     if (o := old.get(p.nm_id)) is None or (o.price, o.stock) != (p.shelf_price, p.stock)]
            await repo.upsert_items(s, fetched)  # раньше цен аккаунтов: на items ссылается FK
            await repo.add_history(s, moved)
            await repo.deactivate_missing(s, supplier_id, set(nm_ids))
            for acc_id, prices in biz_new.items():
                await repo.upsert_account_prices(s, acc_id, prices)
            for src in sources:
                if src.seeded_at is not None:
                    notes += _source_notes(src, fetched, old, accounts.get(src.user_id), biz_old, biz_new)
            await repo.mark_seeded(s, [src.id for src in sources if src.seeded_at is None])
            await repo.touch_seller(s, supplier_id)
            await s.commit()
    log.info(
        "sync продавца %s: товаров %d, бизнес-цен %s, заметок %d",
        supplier_id, len(fetched or []),
        "/".join(str(len(v)) for v in biz_new.values()) or "-", len(notes),
    )
    return seller, fetched, notes


async def notify(bot, seller, notes) -> None:
    """Каждая заметка — отдельным сообщением владельцу источника."""
    name = seller.name or str(seller.supplier_id)
    for src, p, events in notes:
        text = (reporting.new_caption(name, p, src.b2b) if events is None
                else reporting.change_caption(name, p, events, src.b2b))
        try:
            await bot.send_message(src.user_id, text, parse_mode="HTML",
                                   reply_markup=reporting.wb_button(p.url))
        except Exception as e:
            log.warning("уведомление не доставлено %s: %s", src.user_id, e)


async def sync_and_notify(bot, supplier_id: int, slot=None) -> list[NormProduct] | None:
    """Синхронизирует продавца и сразу рассылает новинки/изменения."""
    seller, fetched, notes = await sync_seller(supplier_id, slot=slot)
    if notes:
        await notify(bot, seller, notes)
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


GUARDIUM_WARN_HOURS = 6  # за сколько часов до конца guardium предупредить


def _guardium_problem() -> str | None:
    """Текст алерта, если guardium мёртв или скоро протухнет, иначе None."""
    if wb_client.guardium_fail_streak >= 3:
        return "каталог отвечает 498 — guardium протух, магазины не проверяются"
    exp = guardium_expires_at(wb_client.guardium)
    if exp and exp - time.time() < GUARDIUM_WARN_HOURS * 3600:
        left = datetime.fromtimestamp(exp, ZoneInfo("Europe/Moscow")).strftime("%d.%m %H:%M")
        return f"guardium истекает {left} МСК"
    return None


async def alert_all(bot, text: str) -> None:
    async with Session() as s:
        user_ids = await recipient_ids(s)
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e:
            log.warning("алерт не доставлен %s: %s", uid, e)


async def _check_health(bot) -> None:
    """Веб-каталог выключился или guardium протухает — шумим один раз на каждое падение."""
    path = wb_client.web_catalog  # флаг alerted сбрасывается, когда путь снова заработал
    if path.down and not path.alerted:
        path.alerted = True
        await alert_all(bot, "⚠️ <b>Веб-каталог WB упал</b> — розница идёт через api-ios (guardium).\n"
                             "👉 Проверь прокси и логи «антибот»; для api-ios нужен свежий guardium.")
    # guardium (каталог): шумим один раз, флаг сбрасывает set_guardium
    problem = _guardium_problem()
    if problem and not wb_client.guardium_alerted:
        wb_client.guardium_alerted = True
        await alert_all(bot, f"‼️ <b>GUARDIUM WB</b>: {problem}.\n"
                              "👉 Сними новый из приложения и пришли кнопкой «🔑 Токены».")


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


# проход дольше этого = завис (норма ~30с): прерываем, следующий запуск начнёт заново.
# 2026-11-03 завис выпуск антибота — без сторожа мониторинг стоял 15 часов молча.
PASS_TIMEOUT_SEC = 300
_pass_hung = False  # уже предупредили о зависании (сбрасывает удачный проход)


async def monitoring_job(bot) -> None:
    """Проход мониторинга под сторожем: завис — прерываем и предупреждаем владельца."""
    global _pass_hung
    try:
        await asyncio.wait_for(_monitoring_pass(bot), PASS_TIMEOUT_SEC)
    except TimeoutError:
        log.error("мониторинг: проход дольше %d с — завис, прерван", PASS_TIMEOUT_SEC)
        if not _pass_hung:
            _pass_hung = True
            await alert_all(bot, f"⚠️ Мониторинг завис (проход дольше {PASS_TIMEOUT_SEC // 60} мин) — "
                                 "прерываю и пробую снова каждую минуту. Смотри логи бота.")
        return
    if _pass_hung:
        _pass_hung = False
        log.info("мониторинг: снова работает")
        await alert_all(bot, "✅ Мониторинг снова работает.")


async def _monitoring_pass(bot) -> None:
    """Единый проход по ВСЕМ магазинам раз в monitor_interval_seconds.

    Каждый магазин каждый цикл: каталог (розница = готовая цена),
    b2b — плюс detail под бизнес-аккаунтом владельца источника. Никаких
    приоритетов/триггеров/sweep — все магазины равны, всё свежее каждую минуту.
    Слоты разбирают ОБЩУЮ очередь магазинов (work-stealing): кто освободился —
    берёт следующего; завязший в 429 слот возьмёт меньше, его хвост подстрахуют
    свободные. Статическая раскладка/ротация не нужны — очередь балансирует сама.
    """
    start, end = await _work_window()
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    if not _within_work_hours(start, end, now.hour):
        log.info("мониторинг пропущен: вне рабочих часов (%s–%s), сейчас %d",
                 start, end, now.hour)
        return
    async with Session() as s:
        sellers = await repo.monitored_supplier_ids(s)
    log.info("мониторинг: старт, магазинов %d", len(sellers))
    t0 = asyncio.get_event_loop().time()

    failed: list = []   # (seller, слот-неудачник): каталог пуст после всех ретраев
    skipped: list[int] = []  # не достучались и повтором — пропуск до следующего цикла

    async def _worker(slot, queue) -> int:
        served = 0
        # общий итератор безопасен: asyncio переключает корутины только на await,
        # между next() и первым await вклиниться некому
        for seller in queue:
            served += 1
            try:
                fetched = await sync_and_notify(bot, seller, slot=slot)
                if fetched is None:  # [] = магазин пуст, это не сбой
                    failed.append((seller, slot))
            except Exception as e:
                log.exception("синхронизация %s упала: %s", seller, e)
        return served

    async def _retry(seller, slot) -> None:
        try:
            fetched = await sync_and_notify(bot, seller, slot=slot)
            if fetched is None:
                skipped.append(seller)
        except Exception as e:
            log.exception("повтор %s упал: %s", seller, e)
            skipped.append(seller)

    async with _pass_lock:  # весь проход не пересекается с ручной проверкой/сидом
        slots = wb_client.slots  # снапшот внутри лока (снаружи мог бы устареть)
        for sl in slots:
            sl.err429 = 0  # пер-слот счётчик 429 на этот проход
        queue = iter(sellers)  # общая очередь: слоты растаскивают по мере готовности
        served_by = await asyncio.gather(*(_worker(sl, queue) for sl in slots))
        if failed:
            # ОДИН повтор ДРУГИМ IP (слот-неудачник шанса лишается). Второй раз не
            # пробуем: если WB зажал магазин для двух разных IP — долбиться
            # остальными нет смысла, следующий цикл через минуту повторит.
            if len(slots) > 1:
                retries = [
                    _retry(seller, [s for s in slots if s is not bad][i % (len(slots) - 1)])
                    for i, (seller, bad) in enumerate(failed)
                ]
            else:  # слот один — повторить можно только им же
                retries = [_retry(s, b) for s, b in failed]
            await asyncio.gather(*retries)
        await _check_health(bot)
    took = asyncio.get_event_loop().time() - t0
    saved = len(failed) - len(skipped)  # спас повтор другим IP
    fails_by = {id(sl): 0 for sl in slots}
    for _, bad in failed:
        fails_by[id(bad)] += 1
    per_slot = "; ".join(
        f"{sl.proxy or 'direct'}: 429×{sl.err429}, обслужил {srv}, не смог {fails_by[id(sl)]}"
        for sl, srv in zip(slots, served_by)
    )
    log.info("мониторинг: завершён за %.1fс, магазинов %d, пропущено %d%s, повтор спас %d | %s",
             took, len(sellers), len(skipped), f" {skipped}" if skipped else "", saved, per_slot)


async def report_job(bot) -> None:
    """Часовой отчёт: каждому — по его магазинам, из БД (без обращения к WB).

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
    data = []
    async with Session() as s:
        for uid in await recipient_ids(s):
            for src in await repo.list_sources(s, uid):
                if src.enabled:
                    data.append((uid, src, await repo.source_products(s, src)))
    for uid, src, products in data:
        await send_report_to(bot, [uid], src, products)


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
    # _source_notes: розница по каталогу, бизнес — по цене СВОЕГО аккаунта
    def np(nm, price, stock=5):
        return NormProduct(nm_id=nm, name="x", brand=None, price=price, stock=stock, pics=0,
                           supplier_id=1, shelf_price=price)
    old = {1: SimpleNamespace(price=1000, stock=5), 2: SimpleNamespace(price=500, stock=5)}
    fetched = [np(1, 900), np(2, 500, stock=0), np(3, 700)]  # 1 подешевел, 2 кончился, 3 новинка
    retail = SimpleNamespace(b2b=False, user_id=10)
    got = {p.nm_id: ev for _, p, ev in _source_notes(retail, fetched, old, None, {}, {})}
    assert kinds(got[1]) == ["price"] and kinds(got[2]) == ["availability"] and got[3] is None, got
    biz = SimpleNamespace(b2b=True, user_id=20)
    acc = (7, "cookies")
    # бизнес: 1 под аккаунтом 800 → 700 (снижение), у 2 цена не пришла — прежняя, без события цены
    notes = _source_notes(biz, fetched, old, acc, {7: {1: 800, 2: 400}}, {7: {1: 700, 3: 650}})
    got = {p.nm_id: (p, ev) for _, p, ev in notes}
    assert got[1][0].price == 700 and kinds(got[1][1]) == ["price"] and not got[1][0].price_stale
    assert got[2][0].price == 400 and got[2][0].price_stale and kinds(got[2][1]) == ["availability"]
    assert got[3][0].price == 650 and got[3][1] is None
    # бизнес без аккаунта: цен нет, только наличие и новинки; розничный товар не тронут
    got = {p.nm_id: (p, ev) for _, p, ev in _source_notes(biz, fetched, old, None, {}, {})}
    assert got[3][0].price is None and got[3][0].price_stale and 1 not in got
    assert fetched[0].price == 900  # replace() не портит общий товар каталога
    # work-stealing: общий итератор — каждый магазин обслужен РОВНО одним воркером,
    # медленный воркер берёт меньше, быстрый подстраховывает его хвост
    async def _ws_demo():
        served = {"fast": [], "slow": []}
        queue = iter(range(10))
        async def worker(name, delay):
            for item in queue:
                served[name].append(item)
                await asyncio.sleep(delay)
        await asyncio.gather(worker("fast", 0), worker("slow", 0.02))
        return served
    served = asyncio.run(_ws_demo())
    got = sorted(served["fast"] + served["slow"])
    assert got == list(range(10)), got                      # все и без дублей
    assert len(served["fast"]) > len(served["slow"]), served  # быстрый взял больше
    # сторож: завис → прерван, ОДИН алерт за серию; удачный проход → «снова работает»
    async def _watchdog_demo():
        global PASS_TIMEOUT_SEC, _monitoring_pass, alert_all
        sent = []
        async def fake_alert(bot, text):
            sent.append(text)
        async def hang(bot):
            await asyncio.sleep(3600)
        async def fine(bot):
            pass
        PASS_TIMEOUT_SEC, alert_all = 0.05, fake_alert
        _monitoring_pass = hang
        await monitoring_job(None); await monitoring_job(None)
        assert len(sent) == 1 and "завис" in sent[0], sent
        _monitoring_pass = fine
        await monitoring_job(None); await monitoring_job(None)
        assert len(sent) == 2 and "снова работает" in sent[1], sent
    asyncio.run(_watchdog_demo())
    print("ok")
