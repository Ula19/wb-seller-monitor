"""Показать цены товаров продавца так, как их отдаёт API боту (с кукой).

Безопасно: 1 запрос через рабочую сессию бота. Запуск в контейнере:
    docker compose cp scripts/check_price.py bot:/app/scripts/check_price.py
    docker compose exec bot env PYTHONPATH=/app python scripts/check_price.py [supplier_id]
"""
import asyncio
import json
import sys

from app.config import settings
from app.wb.client import CATALOG_URL, wb_client

SUP = int(sys.argv[1]) if len(sys.argv) > 1 else 250110041


async def main():
    print("кука активна:", bool(settings.wb_cookie))
    params = {
        "appType": 1, "curr": "rub", "dest": settings.wb_dest,
        "sort": "popular", "spp": settings.wb_spp, "supplier": SUP, "page": 1,
    }
    r = await wb_client._session.get(CATALOG_URL, params=params)
    print("статус:", r.status_code)
    if r.status_code == 200:
        for p in r.json().get("products", []):
            price = next((s["price"] for s in p.get("sizes", []) if s.get("price")), None)
            print(p["id"], "|", p.get("name"), "|", json.dumps(price, ensure_ascii=False))
    else:
        print(r.text[:100])
    await wb_client.close()


asyncio.run(main())
