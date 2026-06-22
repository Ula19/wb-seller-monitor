"""Найти supplier_id по slug: голый /seller/<slug> редиректит на /seller/<id>/<slug>.

Проверяем итоговый URL после редиректа и заголовок Location.
    docker compose cp scripts/resolve_seller.py bot:/app/scripts/resolve_seller.py
    docker compose exec bot env PYTHONPATH=/app python scripts/resolve_seller.py moderndevice
"""
import asyncio
import sys

from app.wb.client import wb_client

SLUG = sys.argv[1] if len(sys.argv) > 1 else "moderndevice"
URL = f"https://www.wildberries.ru/seller/{SLUG}"
REF = {"Referer": "https://www.wildberries.ru/"}


async def main():
    # с редиректами — смотрим финальный URL
    r = await wb_client._session.get(URL, headers=REF)
    print("итоговый URL:", getattr(r, "url", None))
    print("статус:", r.status_code)
    print("история:", [getattr(h, "url", h) for h in (getattr(r, "history", None) or [])])
    # без редиректов — смотрим Location
    r2 = await wb_client._session.get(URL, headers=REF, allow_redirects=False)
    print("без редиректа статус:", r2.status_code)
    print("Location:", r2.headers.get("Location") or r2.headers.get("location"))
    await wb_client.close()


asyncio.run(main())
