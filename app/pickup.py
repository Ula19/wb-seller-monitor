"""Текст «Общего забора» — Python-копия pickupText из web/src/accounts.tsx (буфер «Готовы забрать» на сайте):
адрес / 7XXXXXXXXXX Имя / Код : NNNNNN / товары «Имя, цвет - N шт» и «цена руб/шт» через пустую строку. Чистые функции —
их берут и api (снимок товаров), и worker (текст)."""


def pickup_items(orders) -> list[dict]:
    """Строки заказов (в ПВЗ, не скрытые) → [{name, color, n, price, sum}] по артикулу, цвету и цене (оплачен со скидкой /
    без — разные строки), в порядке появления; price — ₽ за шт, sum — за все штуки."""
    items: dict[tuple, dict] = {}
    for o in orders:
        k = (o.nm_id if o.nm_id is not None else o.rid, o.color, o.price)
        it = items.setdefault(k, {"name": o.name or f"арт. {o.nm_id}", "color": o.color, "n": 0, "price": o.price or 0, "sum": 0})
        it["n"] += 1
        it["sum"] += o.price or 0
    return list(items.values())


def pickup_text(address: str | None, phone: str | None, title: str, code: str, items: list[dict]) -> str:
    digits = "".join(ch for ch in phone or "" if ch.isdigit())[-10:]
    head = [address or "", f"{'7' + digits if digits else ''} {title}".strip(), f"Код : {code}"]
    # старые снимки pickup_sends.items — без price (до цены за шт) и без color (до цвета)
    return "\n\n".join(["\n".join(head), *(f"{item_name(it)} - {it['n']} шт\n{it.get('price', it['sum'] // it['n'])} руб/шт"
                                              for it in items)])


def item_name(it: dict) -> str:
    """«Имя, цвет»; цвета нет (старый снимок, WB не дал) — только имя."""
    return f"{it['name']}, {it['color']}" if it.get("color") else it["name"]


if __name__ == "__main__":  # self-check: тот же текст, что проверяет сайт (scratch_tests/pw_acc3.py, WANT)
    from types import SimpleNamespace

    O = lambda color=None, **kw: SimpleNamespace(color=color, **kw)
    want = """Москва, Озёрная улица 9
79001112234 Яна
Код : 123456

Смартфон Samsung Galaxy A17, черный - 2 шт
21543 руб/шт

Чехол - 1 шт
990 руб/шт"""
    orders = [O(rid="r1", nm_id=1, name="Смартфон Samsung Galaxy A17", price=21543, color="черный"),
              O(rid="r2", nm_id=2, name="Чехол", price=990),  # WB цвет не дал — только имя
              O(rid="r3", nm_id=1, name="Смартфон Samsung Galaxy A17", price=21543, color="черный")]
    items = pickup_items(orders)
    assert items == [{"name": "Смартфон Samsung Galaxy A17", "color": "черный", "n": 2, "price": 21543, "sum": 43086},
                     {"name": "Чехол", "color": None, "n": 1, "price": 990, "sum": 990}], items
    assert pickup_text("Москва, Озёрная улица 9", "+7 900 111-22-34", "Яна", "123456", items) == want
    two = pickup_items([O(rid="a", nm_id=1, name="Т", price=100), O(rid="b", nm_id=1, name="Т", price=94),
                        O(rid="c", nm_id=1, name="Т", price=100)])  # один товар, две цены — две строки
    assert [(x["n"], x["price"]) for x in two] == [(2, 100), (1, 94)], two
    colors = pickup_items([O("белый", rid="a", nm_id=1, name="Т", price=100), O("черный", rid="b", nm_id=1, name="Т", price=100)])
    assert [(x["color"], x["n"]) for x in colors] == [("белый", 1), ("черный", 1)], colors  # один артикул, два цвета — две строки
    assert pickup_text("А", None, "Я", "1", colors).endswith("Т, белый - 1 шт\n100 руб/шт\n\nТ, черный - 1 шт\n100 руб/шт")
    assert pickup_text("А", None, "Я", "1", [{"name": "Т", "n": 2, "sum": 200}]).endswith("Т - 2 шт\n100 руб/шт")  # старый снимок
    assert pickup_text(None, None, "Яна", "1", []) == "\nЯна\nКод : 1"  # без телефона — только имя, как на сайте
    assert pickup_items([O(rid="x", nm_id=None, name=None, price=None)]) == [
        {"name": "арт. None", "color": None, "n": 1, "price": 0, "sum": 0}]
    print("ok")
