"""Форматирование сообщений Telegram и Excel."""

import io
import re
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from openpyxl import Workbook

from app.config import settings
from app.emoji import esc, tge


def fmt_warehouse(from_seller) -> str | None:
    if from_seller is None:
        return None
    return "продавца" if from_seller else "WB"


def fmt_delivery(hours) -> str | None:
    """Дата доставки словом по часам (time1+time2). Сегодня/Завтра/Послезавтра/ДД.ММ."""
    if hours is None:
        return None
    # ponytail: now() в tz контейнера (TZ=Europe/Moscow в compose); сутки считаем по дате
    now = datetime.now()
    arrive = now + timedelta(hours=hours)
    days = (arrive.date() - now.date()).days
    return {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}.get(days) or arrive.strftime("%d.%m")


def _wh_delivery_lines(p) -> list[str]:
    lines = []
    wh = fmt_warehouse(getattr(p, "from_seller", None))
    if wh:
        lines.append(f"Склад: {wh}")
    dv = fmt_delivery(getattr(p, "delivery_hours", None))
    if dv:
        lines.append(f"Доставка: {dv}")
    return lines


def fmt_price(price: int | None) -> str:
    if price is None:
        return "—"
    return f"{price:,}".replace(",", " ") + " ₽"


def our_price(price: int | None) -> int | None:
    """Наша цена = цена ВБ минус our_discount_pct."""
    if price is None:
        return None
    return round(price * (1 - settings.our_discount_pct / 100))


def fmt_our(price: int | None) -> str:
    return fmt_price(our_price(price))


def wallet_price(price: int | None) -> int | None:
    """Цена с WB Кошельком = цена − WB_WALLET_DISCOUNT_PCT (WB округляет вниз)."""
    if price is None:
        return None
    return int(price * (1 - settings.wb_wallet_discount_pct / 100))


def _wallet_line(p, b2b: bool) -> list[str]:
    """Строка «С кошельком» — только для розницы и только если процент задан."""
    if b2b or not settings.wb_wallet_discount_pct or p.price is None:
        return []
    pct = settings.wb_wallet_discount_pct
    pct_s = f"{pct:g}"
    return [f"С кошельком (−{pct_s}%): {fmt_price(wallet_price(p.price))}"]


def _stale_line(p, b2b: bool) -> list[str]:
    """Guard: b2b-цена не подтверждена свежим detail (кука/бан/сбой) — честно помечаем.

    Причину не называем: пропуск бывает и при живой куке (упал один чанк, бан IP).
    getattr — в отчёты приходят и ORM-строки из БД, у них поля price_stale нет.
    """
    if b2b and getattr(p, "price_stale", False):
        return ["⚠️ Цена не подтверждена (нет свежих данных)"]
    return []


def fmt_stock(stock) -> str:
    if stock is None:
        return "—"
    return "нет в наличии" if stock == 0 else str(stock)


def mode_tag(b2b: bool) -> str:
    """Пометка режима цены для магазина."""
    return "🏢 бизнес" if b2b else "👤 розница"


def hourly_report_text(seller_name: str, products, b2b: bool = True) -> str:
    lines = [
        f"{tge('shop')} Магазин: {esc(seller_name)} ({mode_tag(b2b)})",
        "",
        f"Всего товаров: {len(products)}",
        "",
    ]
    for i, p in enumerate(products, 1):
        lines += [
            f"{i}. {esc(p.name)}",
            f"Артикул: {p.nm_id}",
            f"Цена ВБ: {fmt_price(p.price)}",
            *_wallet_line(p, b2b),
            f"Наша цена: {fmt_our(p.price)}",
            *_wh_delivery_lines(p),
            f"Остаток: {fmt_stock(p.stock)}",
            f"Ссылка: {p.url}",
            "",
        ]
    return "\n".join(lines)


def _price_delta(old: int, new: int) -> str:
    d = new - old
    arrow = "▲" if d > 0 else "▼"
    return f"{arrow} {abs(d):,}".replace(",", " ") + " ₽"


def wb_button(url: str) -> InlineKeyboardMarkup:
    """Кнопка-ссылка на карточку товара под уведомлением."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Открыть на WB", url=url)]]
    )


def new_caption(seller_name: str, p, b2b: bool = True) -> str:
    """Текст уведомления о новом товаре в ассортименте магазина."""
    lines = [
        f"{tge('add')} Новый товар",
        "",
        f"Магазин: {esc(seller_name)} ({mode_tag(b2b)})",
        esc(p.name),
        f"Артикул: {p.nm_id}",
        "",
        f"{tge('price')} Цена: {fmt_price(p.price)}",
        *_wallet_line(p, b2b),
        *_stale_line(p, b2b),
        f"Наша цена: {fmt_our(p.price)}",
        f"Остаток: {fmt_stock(p.stock)}",
        *_wh_delivery_lines(p),
    ]
    return "\n".join(lines)


def change_caption(seller_name: str, p, events, b2b: bool = True) -> str:
    """events — список кортежей ('price', old, new) | ('availability', old, new)."""
    lines = [
        f"{tge('change')} Изменение товара",
        "",
        f"Магазин: {esc(seller_name)} ({mode_tag(b2b)})",
        esc(p.name),
        f"Артикул: {p.nm_id}",
        "",
    ]
    for kind, old, new in events:
        if kind == "price":
            lines.append(
                f"{tge('price')} Цена: {fmt_price(old)} → {fmt_price(new)} ({_price_delta(old, new)})"
            )
        elif kind == "availability":
            if new > 0:
                lines.append(f"{tge('stock')} Снова в наличии (остаток {new})")
            else:
                lines.append(f"{tge('stock')} Закончился (был остаток {old})")
    lines += [*_wallet_line(p, b2b), *_stale_line(p, b2b),
              f"Наша цена: {fmt_our(p.price)}",
              f"Остаток: {fmt_stock(p.stock)}",
              *_wh_delivery_lines(p)]
    return "\n".join(lines)


# --- грубый разбор названия для группировки/сортировки (модель · память · цвет) ---
# ponytail: эвристика под «модель <RAM> <ROM> <цвет>»; экзотику разложит хуже,
# апгрейд — словарь моделей по брендам, если понадобится.
_COLORS = [
    ("light violet", "violet"), ("light green", "green"),
    ("чёрн", "black"), ("черн", "black"), ("black", "black"),
    ("бел", "white"), ("white", "white"),
    ("зел", "green"), ("green", "green"),
    ("фиолет", "violet"), ("violet", "violet"), ("purple", "violet"),
    ("лаванд", "lavender"), ("lavender", "lavender"),
    ("голуб", "blue"), ("син", "blue"), ("blue", "blue"),
    ("золот", "gold"), ("gold", "gold"),
    ("серебр", "silver"), ("silver", "silver"),
    ("сер", "gray"), ("gray", "gray"), ("grey", "gray"),
    ("красн", "red"), ("red", "red"),
    ("розов", "pink"), ("pink", "pink"),
]
_RAM = {2, 3, 4, 6, 8, 12, 16}
_STO = {16, 32, 64, 128, 256, 512, 1024}
_NOISE = {"смартфон", "телефон", "phone", "мобильный", "light", "ds", "dual",
          "sim", "lte", "4g", "5g", "nfc", "android", "андроид", "ru", "eac",
          "global", "гб", "gb", "tb", "тб", "ram", "rom"}


def _color(s: str) -> str:
    for needle, canon in _COLORS:
        if needle in s:
            return canon
    return ""


def _memory(nums: list[int]) -> tuple[int, int]:
    ram = next((n for n in nums if n in _RAM), 0)
    sto = next((n for n in nums if n in _STO and n != ram), 0)
    return ram, sto


def _model(s: str) -> str:
    """Остаток названия без памяти, цвета, артикульных кодов и шума — это модель."""
    out = []
    for tok in re.split(r"[\s,()/]+", s):
        t = tok.strip(".\"'")
        if not t or t in _NOISE or _color(t):
            continue
        if t.replace("+", "").replace("gb", "").replace("гб", "").isdigit():
            continue
        if '"' in tok or re.fullmatch(r"\d+[.,]\d+", t):  # диагональ 6.7"
            continue
        has_d = any(c.isdigit() for c in t)
        has_a = any(c.isalpha() for c in t)
        if t.startswith("sm") or "-" in tok or (has_d and has_a and len(t) >= 5):
            continue  # артикульный код (SM-A075..., A075FZKDSKZ)
        out.append(t)
    return " ".join(out)


def _prod_key(name: str) -> tuple:
    """Ключ товара: (модель, RAM, ROM, цвет) — одинаковый у того же товара в разных магазинах."""
    s = (name or "").lower()
    nums = [int(x) for x in re.findall(r"\d+", s)]
    ram, sto = _memory(nums)
    return (_model(s), ram, sto, _color(s))


def _grouped_brand_rows(rows):
    """Сортирует (модель→память→цвет→цена) и вставляет None между разными товарами.

    rows — список (product, shop_name, b2b).
    """
    keyed = [(_prod_key(p.name), p, shop, b2b) for p, shop, b2b in rows]
    keyed.sort(key=lambda r: (r[0], r[1].price if r[1].price is not None else 10**12))
    out = []
    prev = None
    for key, p, shop, b2b in keyed:
        if prev is not None and key != prev:
            out.append(None)  # разделитель между разными товарами
        out.append((p, shop, b2b))
        prev = key
    return out


def brands_excel(rows) -> bytes:
    """Xlsx выборки по брендам: Название · Цена · С кошельком · Наша · Магазин · Артикул ·
    Склад · Доставка · Остаток · Ссылка.

    rows — список (product, shop_name, b2b). «С кошельком» — только у розничных строк.
    Сортировка модель→память→цвет→цена; пустая строка между разными товарами,
    один и тот же товар из разных магазинов идёт подряд.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(["Название", "Цена", "С кошельком", "Наша цена", "Магазин", "Артикул",
               "Склад", "Доставка", "Остаток", "Ссылка"])
    for row in _grouped_brand_rows(rows):
        if row is None:
            ws.append([])  # пустая строка-разделитель между товарами
            continue
        p, shop, b2b = row
        wallet = wallet_price(p.price) if (not b2b and settings.wb_wallet_discount_pct) else ""
        ws.append([
            p.name, p.price, wallet, our_price(p.price), shop, p.nm_id,
            fmt_warehouse(getattr(p, "from_seller", None)) or "",
            fmt_delivery(getattr(p, "delivery_hours", None)) or "",
            p.stock, p.url,
        ])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_excel(seller_name: str, products, b2b: bool = True) -> bytes:
    """Отчёт по магазину. Для розницы добавляется колонка «С кошельком»."""
    wallet = not b2b and settings.wb_wallet_discount_pct
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(
        ["Магазин", "Название", "Артикул", "Цена ВБ",
         *(["С кошельком"] if wallet else []),
         "Наша цена", "Склад", "Доставка", "Остаток", "Ссылка"]
    )
    for p in products:
        ws.append(
            [
                seller_name,
                p.name,
                p.nm_id,
                p.price,
                *([wallet_price(p.price)] if wallet else []),
                our_price(p.price),
                fmt_warehouse(getattr(p, "from_seller", None)) or "",
                fmt_delivery(getattr(p, "delivery_hours", None)) or "",
                p.stock,
                p.url,
            ]
        )
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def chunk_text(text: str, limit: int = 4000) -> list[str]:
    """Режет длинный отчёт под лимит Telegram (4096), по переносам строк."""
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


if __name__ == "__main__":  # self-check уведомлений и группировки по брендам
    from types import SimpleNamespace
    p = SimpleNamespace(name="Тест", nm_id=123, price=1000, url="http://x",
                        stock=5, from_seller=None, delivery_hours=None)
    # кошелёк: WB режет вниз (46306 × 0.94 = 43527.64 → 43527, сверено с сайтом)
    settings.wb_wallet_discount_pct = 6.0
    assert wallet_price(46306) == 43527, wallet_price(46306)
    nc = new_caption("Магазин", p, b2b=False)
    assert "123" in nc and "С кошельком" in nc, nc
    assert "Остаток: 5" in nc, nc  # остаток в уведомлении о новинке
    assert "Остаток: 5" in change_caption("М", p, [("price", 1000, 900)], b2b=False)
    assert "С кошельком" not in new_caption("Магазин", p, b2b=True)  # b2b — без кошелька
    ch = change_caption("Магазин", p, [("price", 1000, 1200)], b2b=False)
    assert "1 000 ₽ → 1 200 ₽" in ch and "▲ 200 ₽" in ch, ch
    # guard: b2b + price_stale → пометка; розница/не stale/ORM-строка без поля → нет
    ps = SimpleNamespace(name="Т", nm_id=1, price=1000, url="http://x",
                         stock=5, from_seller=None, delivery_hours=None, price_stale=True)
    assert "не подтверждена" in new_caption("М", ps, b2b=True)
    assert "не подтверждена" in change_caption("М", ps, [("availability", 0, 5)], b2b=True)
    assert "не подтверждена" not in new_caption("М", ps, b2b=False)  # розница — никогда
    assert "не подтверждена" not in new_caption("М", p, b2b=True)    # поля нет (ORM) — нет
    prod = lambda name, price: SimpleNamespace(
        name=name, price=price, nm_id=1, url="http://x", stock=1,
        from_seller=None, delivery_hours=None)
    sample = [
        (prod("Смартфон Galaxy A07 4 128GB Black SM-A075FZKDSKZ", 8000), "b", True),
        (prod("Смартфон Galaxy A07 4 128 ГБ, чёрный", 7900), "a", False),
        (prod("Смартфон Galaxy A07 6 128GB Green SM-A075FZGHSKZ", 9000), "a", False),
        (prod("Смартфон Galaxy A26 6/128 Black", 14000), "a", False),
    ]
    grouped = _grouped_brand_rows(sample)
    # A07 4/128 black (7900, 8000) | None | A07 6/128 green | None | A26
    assert grouped[0][0].price == 7900 and grouped[1][0].price == 8000, grouped  # внутри по цене
    assert grouped.count(None) == 2, grouped  # два разделителя = три товара
    assert _prod_key("Galaxy A07 4 128GB Black") == _prod_key("Galaxy A07 A075F 4 128Gb чёрный"), \
        "разные названия одного товара должны совпасть"
    assert isinstance(brands_excel(sample), bytes)
    print("ok")
