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
