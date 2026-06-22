"""Найти числовой supplier_id по slug-ссылке продавца (.../seller/<slug>).

Безопасно: 1 запрос HTML-страницы через сессию бота (с кукой). Запуск:
    docker compose cp scripts/resolve_seller.py bot:/app/scripts/resolve_seller.py
    docker compose exec bot env PYTHONPATH=/app python scripts/resolve_seller.py moderndevice
"""
import asyncio
import re
import sys

from app.wb.client import wb_client

SLUG = sys.argv[1] if len(sys.argv) > 1 else "moderndevice"
URL = f"https://www.wildberries.ru/seller/{SLUG}"


async def main():
    r = await wb_client._session.get(
        URL, headers={"Referer": "https://www.wildberries.ru/"}
    )
    html = getattr(r, "text", "") or ""
    print("URL:", URL)
    print("статус:", r.status_code, "| размер:", len(html))
    # кандидаты на supplier_id в HTML/встроенном JSON
    patterns = [
        r'supplierId["\':= ]+(\d+)',
        r'supplier[_-]?id["\':= ]+(\d+)',
        r'"id"\s*:\s*(\d{4,})',
        r'/seller/(\d+)',
        r'supplier=(\d+)',
    ]
    for pat in patterns:
        found = re.findall(pat, html)
        print(f"{pat} -> {found[:8]}")
    i = html.lower().find("supplier")
    print("контекст 'supplier':", repr(html[max(0, i - 40):i + 140]) if i >= 0 else "нет")
    await wb_client.close()


asyncio.run(main())
