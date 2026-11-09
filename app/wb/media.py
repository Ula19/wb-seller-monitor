"""Картинки товаров WB: какой basket-хост отдаёт том (vol = nm // 100000).

Карта — публичный cdn.wbbasket.ru/api/v3/upstreams → origin.mediabasket_route_map[0].hosts[]
{vol_range_from, vol_range_to, host}. Worker берёт её раз в сутки и кладёт в app_settings (API в сеть не ходит),
сайт собирает URL сам: https://{host}/vol{vol}/part{nm // 1000}/{nm}/images/c246x328/1.webp
(проверено 2026-11-09 на трёх товарах — 200 image/webp).
"""
from curl_cffi.requests import AsyncSession

UPSTREAMS_URL = "https://cdn.wbbasket.ru/api/v3/upstreams"
SETTING_KEY = "img_hosts"  # app_settings: JSON [[vol_from, vol_to, host], …]


def parse_upstreams(d) -> list[list]:
    """upstreams → [[vol_from, vol_to, host], …]; не тот формат — пусто (сайт покажет заглушки)."""
    try:
        hosts = d["origin"]["mediabasket_route_map"][0]["hosts"]
    except (KeyError, IndexError, TypeError):
        return []
    return [[h["vol_range_from"], h["vol_range_to"], h["host"]] for h in hosts
            if isinstance(h.get("vol_range_from"), int) and isinstance(h.get("vol_range_to"), int) and h.get("host")]


def image_url(hosts: list[list], nm: int) -> str | None:
    """То же, что делает сайт (здесь — для проверки формулы)."""
    vol = nm // 100000
    host = next((h for a, b, h in hosts if a <= vol <= b), None)
    return host and f"https://{host}/vol{vol}/part{nm // 1000}/{nm}/images/c246x328/1.webp"


async def fetch_hosts() -> list[list]:
    async with AsyncSession(impersonate="chrome", timeout=20) as s:  # публичный CDN — без прокси
        r = await s.get(UPSTREAMS_URL)
    r.raise_for_status()
    return parse_upstreams(r.json())


if __name__ == "__main__":  # self-check на форме ответа upstreams (2026-11-09)
    d = {"origin": {"mediabasket_route_map": [{"method": "range", "hosts": [
        {"vol_range_from": 0, "vol_range_to": 143, "host": "basket-01.wbbasket.ru"},
        {"vol_range_from": 2400, "vol_range_to": 2600, "host": "basket-16.wbbasket.ru"},
        {"vol_range_from": "x", "vol_range_to": 1, "host": "битый"}]}]}}
    hosts = parse_upstreams(d)
    assert hosts == [[0, 143, "basket-01.wbbasket.ru"], [2400, 2600, "basket-16.wbbasket.ru"]], hosts
    assert image_url(hosts, 254385484) == \
        "https://basket-16.wbbasket.ru/vol2543/part254385/254385484/images/c246x328/1.webp"  # 200 вживую
    assert image_url(hosts, 999999999) is None and parse_upstreams({}) == [] and parse_upstreams(None) == []
    print("ok")
