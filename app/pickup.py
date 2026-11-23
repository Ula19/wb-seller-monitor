"""Текст «Общего забора» — Python-копия pickupText из web/src/accounts.tsx (буфер «Готовы забрать» на сайте):
адрес / 7XXXXXXXXXX Имя / Код : NNNNNN / товары «Имя - N шт» и «цена руб/шт» через пустую строку. Чистые функции —
их берут и api (снимок товаров), и worker (текст)."""


def pickup_items(orders) -> list[dict]:
    """Строки заказов (в ПВЗ, не скрытые) → [{name, n, price, sum}] по артикулу и цене (оплачен со скидкой / без —
    разные строки), в порядке появления; price — ₽ за шт, sum — за все штуки."""
    items: dict[tuple, dict] = {}
    for o in orders:
        k = (o.nm_id if o.nm_id is not None else o.rid, o.price)
        it = items.setdefault(k, {"name": o.name or f"арт. {o.nm_id}", "n": 0, "price": o.price or 0, "sum": 0})
        it["n"] += 1
        it["sum"] += o.price or 0
    return list(items.values())


def pickup_text(address: str | None, phone: str | None, title: str, code: str, items: list[dict]) -> str:
    digits = "".join(ch for ch in phone or "" if ch.isdigit())[-10:]
    head = [address or "", f"{'7' + digits if digits else ''} {title}".strip(), f"Код : {code}"]
    # старые снимки pickup_sends.items (до цены за шт) — без price
    return "\n\n".join(["\n".join(head), *(f"{it['name']} - {it['n']} шт\n{it.get('price', it['sum'] // it['n'])} руб/шт"
                                              for it in items)])


if __name__ == "__main__":  # self-check: тот же текст, что проверяет сайт (scratch_tests/pw_acc3.py, WANT)
    from types import SimpleNamespace as O
    want = """Москва, Озёрная улица 9
79001112234 Яна
Код : 123456

Смартфон Samsung Galaxy A17 - 2 шт
21543 руб/шт

Чехол - 1 шт
990 руб/шт"""
    orders = [O(rid="r1", nm_id=1, name="Смартфон Samsung Galaxy A17", price=21543), O(rid="r2", nm_id=2, name="Чехол", price=990),
              O(rid="r3", nm_id=1, name="Смартфон Samsung Galaxy A17", price=21543)]
    items = pickup_items(orders)
    assert items == [{"name": "Смартфон Samsung Galaxy A17", "n": 2, "price": 21543, "sum": 43086},
                     {"name": "Чехол", "n": 1, "price": 990, "sum": 990}], items
    assert pickup_text("Москва, Озёрная улица 9", "+7 900 111-22-34", "Яна", "123456", items) == want
    two = pickup_items([O(rid="a", nm_id=1, name="Т", price=100), O(rid="b", nm_id=1, name="Т", price=94),
                        O(rid="c", nm_id=1, name="Т", price=100)])  # один товар, две цены — две строки
    assert two == [{"name": "Т", "n": 2, "price": 100, "sum": 200}, {"name": "Т", "n": 1, "price": 94, "sum": 94}], two
    assert pickup_text("А", None, "Я", "1", [{"name": "Т", "n": 2, "sum": 200}]).endswith("Т - 2 шт\n100 руб/шт")  # старый снимок
    assert pickup_text(None, None, "Яна", "1", []) == "\nЯна\nКод : 1"  # без телефона — только имя, как на сайте
    assert pickup_items([O(rid="x", nm_id=None, name=None, price=None)]) == [{"name": "арт. None", "n": 1, "price": 0, "sum": 0}]
    print("ok")
