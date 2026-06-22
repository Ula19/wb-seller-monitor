"""Форматирование сообщений Telegram и Excel."""

import io
from datetime import datetime, timedelta

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


def fmt_stock(stock) -> str:
    if stock is None:
        return "—"
    return "нет в наличии" if stock == 0 else str(stock)


def hourly_report_text(seller_name: str, products) -> str:
    lines = [f"{tge('shop')} Магазин: {esc(seller_name)}", "", f"Всего товаров: {len(products)}", ""]
    for i, p in enumerate(products, 1):
        lines += [
            f"{i}. {esc(p.name)}",
            f"Артикул: {p.nm_id}",
            f"Цена ВБ: {fmt_price(p.price)}",
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


def change_caption(seller_name: str, p, events) -> str:
    """events — список кортежей ('price', old, new) | ('availability', old, new)."""
    lines = [
        f"{tge('change')} Изменение товара",
        "",
        f"Магазин: {esc(seller_name)}",
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
    lines += [f"Наша цена: {fmt_our(p.price)}", *_wh_delivery_lines(p), "", "Ссылка:", p.url]
    return "\n".join(lines)


def new_item_caption(seller_name: str, p) -> str:
    wh = "".join(f"{line}\n" for line in _wh_delivery_lines(p))
    return (
        f"{tge('new')} Новый товар обнаружен\n\n"
        f"Магазин: {esc(seller_name)}\n\n"
        f"Название: {esc(p.name)}\n\n"
        f"Артикул: {p.nm_id}\n\n"
        f"Цена ВБ: {fmt_price(p.price)}\n"
        f"Наша цена: {fmt_our(p.price)}\n"
        f"{wh}\n"
        f"Ссылка:\n{p.url}"
    )


def build_excel(seller_name: str, products) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Товары"
    ws.append(
        ["Магазин", "Название", "Артикул", "Цена ВБ", "Наша цена", "Склад", "Доставка",
         "Остаток", "Ссылка", "Дата обнаружения"]
    )
    for p in products:
        fs = getattr(p, "first_seen_at", None)
        ws.append(
            [
                seller_name,
                p.name,
                p.nm_id,
                p.price,
                our_price(p.price),
                fmt_warehouse(getattr(p, "from_seller", None)) or "",
                fmt_delivery(getattr(p, "delivery_hours", None)) or "",
                p.stock,
                p.url,
                fs.strftime("%Y-%m-%d %H:%M") if fs else "",
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
