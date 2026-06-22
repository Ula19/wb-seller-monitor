"""Нормализация ответов внутреннего API WB."""

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
    delivery_hours: int | None = None  # time1+time2, для даты доставки
    from_seller: bool | None = None  # True=склад продавца, False=склад WB

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


def _delivery(p: dict) -> tuple[int | None, bool | None]:
    """Часы доставки (time1+time2) и склад по dtype.

    Признак склада — младший разряд dtype: 1=продавца, 8=WB (по образцам b2b).
    """
    for size in p.get("sizes") or []:
        for src in [size, *(size.get("stocks") or [])]:
            t1, t2, dt = src.get("time1"), src.get("time2"), src.get("dtype")
            if t1 is None and t2 is None and dt is None:
                continue
            hours = (t1 or 0) + (t2 or 0) if (t1 is not None or t2 is not None) else None
            from_seller = None
            if dt is not None:
                # ponytail: 2 образца — продавец=1, WB=8; иной разряд всплывёт в выводе
                from_seller = (int(dt) & 0xF) == 1
            return hours, from_seller
    return None, None


def normalize(p: dict, supplier_id: int) -> NormProduct:
    hours, from_seller = _delivery(p)
    return NormProduct(
        nm_id=int(p["id"]),
        name=(p.get("name") or "").strip(),
        brand=p.get("brand"),
        price=_price(p),
        stock=_stock(p),
        pics=int(p.get("pics") or 0),
        supplier_id=supplier_id,
        delivery_hours=hours,
        from_seller=from_seller,
    )


if __name__ == "__main__":  # self-check признака склада по реальным образцам b2b
    wb = {"sizes": [{"time1": 2, "time2": 23, "dtype": 6597069766664}]}
    seller = {"sizes": [{"time1": 24, "time2": 18, "dtype": 6597069766657}]}
    assert _delivery(wb) == (25, False), _delivery(wb)
    assert _delivery(seller) == (42, True), _delivery(seller)
    print("ok")
