"""Нормализация ответов внутреннего API WB + сборка ссылок на фото."""

from dataclasses import dataclass


@dataclass
class NormProduct:
    nm_id: int
    name: str
    brand: str | None
    price: int | None  # рубли
    stock: int
    pics: int
    supplier_id: int

    @property
    def url(self) -> str:
        return f"https://www.wildberries.ru/catalog/{self.nm_id}/detail.aspx"


def _price(p: dict) -> int | None:
    """Цена в рублях. В v4 лежит в sizes[].price.product (копейки), ÷100."""
    for size in p.get("sizes") or []:
        price = size.get("price") or {}
        val = price.get("product") or price.get("total") or price.get("basic")
        if val:
            return int(val) // 100
    # запасной вариант для старого формата
    for key in ("salePriceU", "priceU"):
        if p.get(key):
            return int(p[key]) // 100
    return None


def _stock(p: dict) -> int:
    if p.get("totalQuantity") is not None:
        return int(p["totalQuantity"])
    total = 0
    for size in p.get("sizes") or []:
        for st in size.get("stocks") or []:
            total += int(st.get("qty") or 0)
    return total


def normalize(p: dict, supplier_id: int) -> NormProduct:
    return NormProduct(
        nm_id=int(p["id"]),
        name=(p.get("name") or "").strip(),
        brand=p.get("brand"),
        price=_price(p),
        stock=_stock(p),
        pics=int(p.get("pics") or 0),
        supplier_id=supplier_id,
    )


# Диапазоны vol -> номер basket-хоста (актуально на июнь 2026).
# Границы со временем расширяются, поэтому в клиенте есть fallback-перебор.
_BASKET_RANGES = [
    (143, 1), (287, 2), (431, 3), (719, 4), (1007, 5), (1061, 6), (1115, 7),
    (1199, 8), (1396, 9), (1601, 10), (1655, 11), (1919, 12), (2045, 13),
    (2189, 14), (2405, 15), (2621, 16), (2848, 17), (3000, 18), (3200, 19),
    (3400, 20), (3700, 21), (3900, 22), (4100, 23), (4400, 24), (4500, 25),
    (4800, 26), (5100, 27), (5600, 28),
]


def basket_host(nm_id: int) -> int:
    vol = nm_id // 100000
    for ceil, host in _BASKET_RANGES:
        if vol <= ceil:
            return host
    return _BASKET_RANGES[-1][1]


def photo_url(nm_id: int, host: int | None = None, size: str = "big", idx: int = 1) -> str:
    vol = nm_id // 100000
    part = nm_id // 1000
    h = host if host is not None else basket_host(nm_id)
    return (
        f"https://basket-{h:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}"
        f"/images/{size}/{idx}.webp"
    )
