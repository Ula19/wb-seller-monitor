"""Цена с WB Кошельком: витрина × (1 − %/100), если покупка влезает в лимиты аккаунта.

Правило (SCOUT П1/П3): скидка Кошелька персональная (3 или 6%) и пропадает ЦЕЛИКОМ, если
заказ больше остатка месячного лимита или разового лимита, а позиция — больше лимита позиции.
С лимитами сравниваем цену ДО скидки: у WB «до/после» не выяснено, «до» — осторожнее
(не пообещаем скидку, которой не будет). Одна позиция = один заказ.
"""


def wallet_price(price: int | None, accounts) -> tuple[int, object] | None:
    """Лучшая цена с Кошельком среди аккаунтов → (цена, аккаунт) или None.

    accounts — объекты с wallet_pct, limit_left, max_order, max_rid (None = неизвестно:
    такой аккаунт пропускаем, кроме max_order/max_rid — их WB отдаёт не всегда).
    """
    best = None
    for a in accounts:
        if not price or not a.wallet_pct or a.limit_left is None:
            continue
        if price > a.limit_left or (a.max_order and price > a.max_order) or (a.max_rid and price > a.max_rid):
            continue
        p = int(price * (100 - a.wallet_pct) / 100)
        if best is None or p < best[0]:
            best = (p, a)
    return best


def best_offer(own: int | None, b2b: bool, shelf: int | None, accounts) -> tuple[int, str, object] | None:
    """Лучшая цена, по которой можно купить → (цена, откуда, аккаунт|None). Её сравнивают с целью.

    own — цена источника, ПОДТВЕРЖДЁННАЯ: розница = витрина каталога, бизнес = бизнес-цена этого
    цикла (неподтверждённую не передавать — цель по ней не срабатывает). Кошелёк — от той же витрины.
    откуда: business | retail | wallet.
    """
    offers = []
    if own:
        offers.append((own, "business" if b2b else "retail", None))
    if w := wallet_price(shelf, accounts):
        offers.append((w[0], "wallet", w[1]))
    return min(offers, key=lambda o: o[0]) if offers else None


def offer_text(offer) -> str:
    """Подпись к цене: «с Кошельком −6% · аккаунт …23-82» / «бизнес-цена» / «витрина»."""
    price, kind, acc = offer
    if kind == "wallet":
        label = acc.label or ""
        return f"с Кошельком −{acc.wallet_pct}% · аккаунт …{label[-5:]}"
    return "бизнес-цена" if kind == "business" else "витрина"


if __name__ == "__main__":
    from types import SimpleNamespace as A

    six = A(label="6%", wallet_pct=6, limit_left=641625, max_order=1000000, max_rid=500000)
    three = A(label="3%", wallet_pct=3, limit_left=2000000, max_order=1000000, max_rid=500000)
    assert wallet_price(121581, [three, six]) == (114286, six)  # корзина 2026-11-04: ровно −6%
    assert wallet_price(700000, [six]) is None  # > остатка лимита 641 625 и > позиции 500 000
    assert wallet_price(450000, [six, three])[1] is six
    assert wallet_price(600000, [six, three]) is None  # оба: позиция > 500 000
    lim = A(label="мало", wallet_pct=6, limit_left=1000, max_order=None, max_rid=None)
    assert wallet_price(1000, [lim]) == (940, lim) and wallet_price(1001, [lim]) is None  # граница — до скидки
    b2b = A(label="бизнес", wallet_pct=None, limit_left=None, max_order=None, max_rid=None)
    assert wallet_price(1000, [b2b]) is None and wallet_price(None, [six]) is None
    # лучшая цена для цели: кошелёк дешевле витрины; бизнес дешевле кошелька; без своей цены — только кошелёк
    six.label = "+7 ••• •••-23-82"
    assert best_offer(121581, False, 121581, [six]) == (114286, "wallet", six)
    assert offer_text(best_offer(121581, False, 121581, [six])) == "с Кошельком −6% · аккаунт …23-82"
    assert best_offer(100000, True, 121581, [six]) == (100000, "business", None)
    assert best_offer(None, True, 121581, []) is None  # бизнес не подтверждён, кошелька нет — сравнивать не с чем
    assert best_offer(700000, False, 700000, [six]) == (700000, "retail", None)  # не влезает в лимит
    assert offer_text((1, "business", None)) == "бизнес-цена" and offer_text((1, "retail", None)) == "витрина"
    print("ok")
