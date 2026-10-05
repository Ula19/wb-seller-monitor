"""Проверить одну страницу каталога тем же запросом, что использует бот.

Безопасно: 1 запрос через рабочую сессию бота. Запуск в контейнере:
    docker compose cp scripts/check_price.py bot:/app/scripts/check_price.py
    docker compose exec bot env PYTHONPATH=/app python scripts/check_price.py [supplier_id]
"""
import asyncio
import json
import sys

from app.db import repo
from app.db.base import Session, engine
from app.wb.client import CATALOG_URL, wb_client

SUP = int(sys.argv[1]) if len(sys.argv) > 1 else 250110041


async def load_saved_cookie():
    async with Session() as s:
        saved = await repo.get_setting(s, "wb_cookie")
    if saved:
        await wb_client.set_cookie(saved)
    token = (wb_client._cookies or {}).get("x_wbaas_token")
    print("кука:", "из БД" if saved else "из .env")
    print("токен WBAAS:", "есть" if token else "нет")


def show_response(r):
    if r is None:
        print("ответ не получен: ретраи исчерпаны")
        return
    print("статус:", r.status_code)
    print("server:", r.headers.get("server"), "байт:", len(r.content))
    if r.status_code == 200:
        data = r.json()
        products = data.get("products") or (data.get("data") or {}).get("products") or []
        print("товаров на странице:", len(products))
        for p in products:
            price = next((s["price"] for s in p.get("sizes", []) if s.get("price")), None)
            print(p["id"], "|", p.get("name"), "|", json.dumps(price, ensure_ascii=False))
    else:
        if r.status_code == 498:
            print("WBAAS отклонил запрос; проверь свежесть токена и доступ с IP сервера.")
        print("тело:", repr(r.text[:300]))


async def main():
    try:
        if SUP <= 0:
            raise ValueError("ID продавца должен быть положительным")
        await load_saved_cookie()
        print("URL:", CATALOG_URL)
        response = await wb_client.fetch_catalog_page(SUP, slot=wb_client._direct_slot)
        show_response(response)
    except Exception as e:
        print("проверка не удалась:", type(e).__name__, str(e))
        raise
    finally:
        await wb_client.close()
        await engine.dispose()


asyncio.run(main())
