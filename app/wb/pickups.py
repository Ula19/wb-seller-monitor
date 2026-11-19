"""Справочник ПВЗ WB в БД (таблица wb_pickup_points): адрес и часы для заказов аккаунтов, а для заказа товара —
dest, координаты, typePoint и подпись ПВЗ (sign, signV2: без неё WB отвечает «empty delivery sign»).

Источник — публичный all-poo-fr-v12.json (~75 МБ, без авторизации): список {country, items[]}, в items ПВЗ
{id, address, coordinates [lat, lon], dest, typePoint, sign, signV2, status, timeTableV2 — СТРОКА с JSON
[{"monday": {"open", "close"}, …}]}. Закрытые WB держит в отдельном all-poo-frn, поэтому пропавший из fr = закрыт.
sync_job качает 2 раза в сутки; пустой/битый ответ — таблицу не трогаем.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from curl_cffi.requests import AsyncSession
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import Session
from app.db.models import PickupPoint

URL = "https://static-basket-01.wbbasket.ru/vol0/data/all-poo-fr-v12.json"
MIN_POINTS = 50_000  # живой справочник ~96 тыс.; меньше — битый/обрезанный ответ, по нему ничего не закрываем
STALE = timedelta(hours=12)  # при старте качаем, только если справочник старше
BATCH = 2000  # строк в одном INSERT (12 параметров на строку, у Postgres предел 32 767)
MSK = timezone(timedelta(hours=3))  # в контейнере может не быть tzdata, а у Москвы нет перехода на летнее время
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
FIELDS = ("country", "address", "dest", "lat", "lon", "type_point", "sign", "sign_v2", "status", "week")

log = logging.getLogger(__name__)


def _week(raw) -> tuple:
    """timeTableV2 → 7 строк «10:00–22:00» (None — выходной/нет данных), пн…вс."""
    try:
        t = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return ()
    days: dict = {}
    for d in t if isinstance(t, list) else [t]:
        if isinstance(d, dict):
            days.update(d)
    return tuple(f"{v['open']}–{v['close']}" if isinstance(v, dict) and v.get("open") and v.get("close") else None
                 for v in (days.get(name) for name in DAYS))


def _num(v, kind=int):
    try:
        return None if isinstance(v, bool) or v is None else kind(v)
    except (TypeError, ValueError):
        return None


def parse(raw: bytes) -> list[dict]:
    """Весь справочник (все страны) → строки таблицы; id числом или строкой цифр, повторы id — последний."""
    rows = {}
    for country in json.loads(raw):
        for it in country.get("items") or []:
            if not str(it.get("id")).isdigit():
                continue
            coords = list(it.get("coordinates") or []) + [None, None]
            week = _week(it.get("timeTableV2"))
            rows[int(it["id"])] = {
                "id": int(it["id"]), "country": country.get("country"), "address": it.get("address") or None,
                "dest": _num(it.get("dest")), "lat": _num(coords[0], float), "lon": _num(coords[1], float),
                "type_point": _num(it.get("typePoint")), "sign": it.get("sign") or None,
                "sign_v2": it.get("signV2") or None, "status": _num(it.get("status")), "week": list(week) if any(week) else None}
    return list(rows.values())


def hours(week, now: datetime | None = None) -> str | None:
    """Все дни одинаковые — «ежедн. 10:00–22:00», иначе часы на сегодня (МСК)."""
    week = tuple(week or ())
    if not any(week):
        return None
    if len(week) == 7 and len(set(week)) == 1:
        return f"ежедн. {week[0]}"
    today = week[(now or datetime.now(MSK)).weekday()]
    return f"сегодня {today}" if today else "сегодня закрыт"


async def _download() -> bytes:
    async with AsyncSession(impersonate="chrome", timeout=180) as s:  # статика CDN — без прокси
        r = await s.get(URL)
    r.raise_for_status()
    return r.content


async def _stale() -> bool:
    async with Session() as s:
        last = await s.scalar(select(func.max(PickupPoint.seen_at)))
    return last is None or datetime.now(timezone.utc) - last > STALE


async def sync_job(only_if_stale: bool = False) -> None:
    """Справочник → таблица одной транзакцией: upsert всех (seen_at = сейчас, closed_at снят), кого нет в ответе —
    closed_at. Сбой скачивания, битый JSON или < MIN_POINTS пунктов — таблицу не трогаем."""
    if only_if_stale and not await _stale():
        return
    started = datetime.now(timezone.utc)
    try:
        # ponytail: json.loads 75 МБ держит GIL ~1 с и в потоке — бот замирает на секунду 2 раза в сутки;
        # станет мешать — разбор в отдельном процессе (ProcessPoolExecutor)
        rows = await asyncio.to_thread(parse, await _download())
    except Exception as e:
        log.warning("pickups: справочник ПВЗ не загружен: %s: %s", type(e).__name__, str(e)[:200])
        return
    if len(rows) < MIN_POINTS:
        log.warning("pickups: в справочнике ПВЗ всего %d пунктов — похоже на битый ответ, таблицу не трогаю", len(rows))
        return
    # один INSERT на все пачки (executemany): .values(список) пересобирал бы SQL на каждую пачку — 20+ с CPU в event loop
    stmt = pg_insert(PickupPoint)
    stmt = stmt.on_conflict_do_update(index_elements=[PickupPoint.id], set_={
        **{f: stmt.excluded[f] for f in FIELDS}, "seen_at": stmt.excluded.seen_at, "closed_at": None})
    async with Session() as s:
        for i in range(0, len(rows), BATCH):
            await s.execute(stmt, [{**r, "seen_at": started} for r in rows[i:i + BATCH]])
            await asyncio.sleep(0)  # дать боту ответить между пачками
        closed = (await s.execute(update(PickupPoint).where(PickupPoint.seen_at < started, PickupPoint.closed_at.is_(None))
                                  .values(closed_at=started))).rowcount
        await s.commit()
    log.info("pickups: справочник ПВЗ обновлён: %d пунктов, закрылось %d", len(rows), closed)


async def lookup(ids) -> dict[int, tuple[str | None, str | None]]:
    """{id: (адрес, часы)} для истории заказов — и закрытые тоже (заказ мог уйти туда раньше)."""
    ids = {i for i in ids if isinstance(i, int)}
    if not ids:
        return {}
    async with Session() as s:
        rows = (await s.execute(select(PickupPoint.id, PickupPoint.address, PickupPoint.week)
                                .where(PickupPoint.id.in_(ids)))).all()
    return {i: (address, hours(week)) for i, address, week in rows}


if __name__ == "__main__":  # self-check разбора на маленьком справочнике той же формы (БД — scratch_tests/pickups_db_test)
    same = json.dumps([{d: {"open": "10:00", "close": "22:00"} for d in DAYS}])
    diff = json.dumps([{**{d: {"open": "09:00", "close": "21:00"} for d in DAYS[:5]},
                        "saturday": {"open": "10:00", "close": "18:00"}}])
    fixture = json.dumps([
        {"country": "ru", "items": [
            {"id": 50017750, "address": "Москва, Ореховый Проезд 39а", "timeTableV2": same, "coordinates": [55.616392, 37.733716],
             "dest": -531264, "typePoint": 8, "sign": "9yoinOGROqo=", "signV2": "79nDjen0sJQ=", "dest2": -1, "rate": 5},
            {"id": 50031070, "address": "Сочи", "timeTableV2": diff, "coordinates": [43.5, 39.7], "dest": "123",
             "typePoint": 14, "sign": "s", "signV2": "s2", "status": 3},
            {"id": 7, "address": "", "timeTableV2": "не json", "coordinates": [], "dest": None, "status": True},
            {"id": "x1", "address": "без номера"}, {"address": "без id"}]},
        {"country": "by", "items": [{"id": "8", "address": "Минск, Пушкина 1", "timeTableV2": same},
                                    {"id": 50017750, "address": "Москва, Ореховый Проезд 39а (повтор)"}]},
    ]).encode()

    p = {r["id"]: r for r in parse(fixture)}
    assert sorted(p) == [7, 8, 50017750, 50031070], sorted(p)
    assert p[50031070] == {"id": 50031070, "country": "ru", "address": "Сочи", "dest": 123, "lat": 43.5, "lon": 39.7,
                           "type_point": 14, "sign": "s", "sign_v2": "s2", "status": 3,
                           "week": ["09:00–21:00"] * 5 + ["10:00–18:00", None]}, p[50031070]
    assert p[7] == {"id": 7, "country": "ru", "address": None, "dest": None, "lat": None, "lon": None, "type_point": None,
                    "sign": None, "sign_v2": None, "status": None, "week": None}, p[7]
    assert p[8]["country"] == "by" and p[8]["week"] == ["10:00–22:00"] * 7
    assert p[50017750]["address"].endswith("(повтор)") and p[50017750]["country"] == "by"  # один id — одна строка
    assert set(p[8]) == {"id", *FIELDS}
    assert hours(p[8]["week"]) == "ежедн. 10:00–22:00" and hours(None) is None and hours([None] * 7) is None
    sat, sun, mon = (datetime(2026, 11, d, 12, tzinfo=MSK) for d in (14, 15, 16))
    w = p[50031070]["week"]
    assert (hours(w, sat), hours(w, sun), hours(w, mon)) == ("сегодня 10:00–18:00", "сегодня закрыт", "сегодня 09:00–21:00")
    print("ok")
