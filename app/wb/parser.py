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
