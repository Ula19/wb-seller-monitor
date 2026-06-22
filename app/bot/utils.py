"""Мелкие помощники, общие для хендлеров."""

import re


def parse_supplier_id(text: str | None) -> int | None:
    """Извлекает supplier_id из текста: число или ссылка wildberries.ru/seller/<ID>."""
    if not text:
        return None
    m = re.search(r"seller/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,})", text)
    return int(m.group(1)) if m else None


def parse_seller_slug(text: str | None) -> str | None:
    """Slug продавца из ссылки .../seller/<slug> или одиночного слова (не число)."""
    if not text:
        return None
    m = re.search(r"seller/([A-Za-z0-9][A-Za-z0-9_-]*)", text)
    tok = (m.group(1) if m else text).strip()
    if tok and not tok.isdigit() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]+", tok):
        return tok.lower()
    return None


if __name__ == "__main__":  # self-check парсеров
    assert parse_supplier_id("https://www.wildberries.ru/seller/250110041") == 250110041
    assert parse_seller_slug("https://www.wildberries.ru/seller/moderndevice") == "moderndevice"
    assert parse_seller_slug("https://www.wildberries.ru/seller/250110041") is None  # число → не slug
    assert parse_seller_slug("ModernDevice") == "moderndevice"
    assert parse_seller_slug("добавь магазин сюда") is None
    print("ok")
