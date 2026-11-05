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
    print("ok")
