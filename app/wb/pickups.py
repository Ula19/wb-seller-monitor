"""Справочник ПВЗ WB: id → (адрес, часы работы) для заказов аккаунтов.

В заказах wbxoofex у ПВЗ только dst_office_id (full_address пустой). Адрес и расписание — в публичном
справочнике all-poo-fr-v12.json (~75 МБ, без авторизации): список {country, items[]}, в items ПВЗ
{id, address, timeTableV2 — СТРОКА с JSON [{"monday": {"open", "close"}, …}]}.
Качаем лениво (встретился неизвестный ПВЗ) и держим в памяти 24 ч; в памяти — только id → (адрес, неделя).
Сбой — адресов нет, опрос аккаунта не ломается.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from curl_cffi.requests import AsyncSession

URL = "https://static-basket-01.wbbasket.ru/vol0/data/all-poo-fr-v12.json"
TTL_SEC = 24 * 3600  # справочник свежий сутки
RETRY_SEC = 3600  # после сбоя — не чаще раза в час (75 МБ на каждый опрос каждого аккаунта — слишком)
MSK = timezone(timedelta(hours=3))  # в контейнере может не быть tzdata, а у Москвы нет перехода на летнее время
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

log = logging.getLogger(__name__)

_offices: dict[int, tuple[str | None, tuple]] = {}
_tried_at = 0.0  # последняя попытка загрузки (успех или сбой)
_ok = False
_lock = asyncio.Lock()


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


def parse(raw: bytes) -> dict[int, tuple[str | None, tuple]]:
    """Весь справочник (все страны) → {id: (адрес, неделя)}; сырой JSON живёт только внутри функции."""
    weeks: dict[tuple, tuple] = {}  # одинаковые расписания — один объект: у 90 тыс. ПВЗ их немного
    out = {}
    for country in json.loads(raw):
        for it in country.get("items") or []:
            if str(it.get("id")).isdigit():  # id числом или строкой цифр
                w = _week(it.get("timeTableV2"))
                out[int(it["id"])] = (it.get("address") or None, weeks.setdefault(w, w))
    return out


def hours(week: tuple, now: datetime | None = None) -> str | None:
    """Все дни одинаковые — «ежедн. 10:00–22:00», иначе часы на сегодня (МСК)."""
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


async def _refresh() -> None:
    global _offices, _tried_at, _ok
    async with _lock:  # два аккаунта опрашиваются разом — качаем один раз
        if time.time() - _tried_at < (TTL_SEC if _ok else RETRY_SEC):
            return
        _tried_at = time.time()
        try:
            _offices = await asyncio.to_thread(parse, await _download())
            _ok = True
            log.info("pickups: справочник ПВЗ загружен, %d пунктов", len(_offices))
        except Exception as e:  # старый справочник (если был) остаётся
            _ok = False
            log.warning("pickups: справочник ПВЗ не загружен: %s: %s", type(e).__name__, str(e)[:200])


async def lookup(ids) -> dict[int, tuple[str | None, str | None]]:
    """{id: (адрес, часы)} для известных ПВЗ; неизвестный id — повод обновить справочник (не чаще TTL/RETRY)."""
    ids = {i for i in ids if isinstance(i, int)}
    if ids - _offices.keys():
        await _refresh()
    return {i: (o[0], hours(o[1])) for i in ids if (o := _offices.get(i))}


if __name__ == "__main__":  # self-check на маленьком справочнике той же формы
    from unittest.mock import AsyncMock, patch

    same = json.dumps([{d: {"open": "10:00", "close": "22:00"} for d in DAYS}])
    diff = json.dumps([{**{d: {"open": "09:00", "close": "21:00"} for d in DAYS[:5]},
                        "saturday": {"open": "10:00", "close": "18:00"}}])
    fixture = json.dumps([
        {"country": "ru", "items": [
            {"id": 50017750, "address": "Москва, Ореховый Проезд 39а", "timeTableV2": same},
            {"id": 172986, "address": "г Москва, Новочерёмушкинская Улица 24", "timeTableV2": diff},
            {"id": 7, "address": "", "timeTableV2": "не json"}]},
        {"country": "by", "items": [{"id": "8", "address": "Минск, Пушкина 1", "timeTableV2": same}]},
    ]).encode()

    p = parse(fixture)
    assert p[50017750] == ("Москва, Ореховый Проезд 39а", ("10:00–22:00",) * 7) and p[8][0] == "Минск, Пушкина 1"
    assert p[7] == (None, ()) and p[50017750][1] is p[8][1]  # одинаковые недели — один объект
    assert hours(p[50017750][1]) == "ежедн. 10:00–22:00" and hours(p[7][1]) is None
    sat, sun, mon = (datetime(2026, 11, d, 12, tzinfo=MSK) for d in (14, 15, 16))
    assert (hours(p[172986][1], sat), hours(p[172986][1], sun), hours(p[172986][1], mon)) == \
        ("сегодня 10:00–18:00", "сегодня закрыт", "сегодня 09:00–21:00")

    async def main():
        global _tried_at, _ok, _offices
        dl = AsyncMock(return_value=fixture)
        with patch(f"{__name__}._download", dl):
            r = await lookup([50017750, 8, None])
            assert r == {50017750: ("Москва, Ореховый Проезд 39а", "ежедн. 10:00–22:00"),
                         8: ("Минск, Пушкина 1", "ежедн. 10:00–22:00")} and dl.await_count == 1
            assert await lookup([50017750]) and await lookup([999]) == {} and dl.await_count == 1  # свежий — не качаем
            await asyncio.gather(*(lookup([998]) for _ in range(3)))
            assert dl.await_count == 1
            _tried_at = 0  # прошли сутки — неизвестный ПВЗ обновляет справочник
            await lookup([999])
            assert dl.await_count == 2
        _tried_at, _ok = 0, False
        boom = AsyncMock(side_effect=TimeoutError("cdn"))
        with patch(f"{__name__}._download", boom):
            assert await lookup([999]) == {} and await lookup([997]) == {} and boom.await_count == 1  # сбой — раз в час
            assert await lookup([50017750]) == {50017750: ("Москва, Ореховый Проезд 39а", "ежедн. 10:00–22:00")}  # старый жив
        _tried_at, _offices = 0, {}
        with patch(f"{__name__}._download", AsyncMock(return_value=b"[{")):  # битый JSON — тоже не падаем
            assert await lookup([1]) == {}

    asyncio.run(main())
    print("ok")
