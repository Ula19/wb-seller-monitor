"""Сравнить цену в catalog.wb.ru с кукой аккаунта и без неё.

Запуск в контейнере (там IP, который WB пускает):
    docker compose cp scripts/check_price.py bot:/app/scripts/check_price.py
    docker compose exec bot env PYTHONPATH=/app python scripts/check_price.py
"""
import asyncio
import json
import os

from app.config import settings
from app.db import repo
from app.db.base import Session
from app.wb.client import wb_client

# кука твоего аккаунта (для проверки кошелёк-цены). Секрет — не хардкодим:
#   WB_COOKIE='весь Cookie header' .venv/bin/python scripts/check_price.py
COOKIE = os.environ.get("WB_COOKIE", "")

CATALOG = "https://catalog.wb.ru/sellers/v4/catalog"


def parse_cookie(raw):
    out = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def first_price(r):
    try:
        p = r.json()["products"][0]
    except Exception:
        return "не JSON: " + r.text[:80]
    for s in p.get("sizes", []):
        if s.get("price"):
            return f"nm={p['id']} «{p.get('name')}» price={json.dumps(s['price'], ensure_ascii=False)}"
    return "цены нет"


async def fetch(supplier, cookies=None):
    params = {
        "appType": 1, "curr": "rub", "dest": settings.wb_dest,
        "sort": "popular", "spp": settings.wb_spp, "supplier": supplier, "page": 1,
    }
    return await wb_client._session.get(CATALOG, params=params, cookies=cookies)


async def main():
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        print("в БД нет продавцов"); return
    supplier = sellers[0].supplier_id
    print(f"продавец {supplier} ({sellers[0].name})")
    r1 = await fetch(supplier)
    print("без куки:", r1.status_code, first_price(r1) if r1.status_code == 200 else "")
    if COOKIE:
        r2 = await fetch(supplier, parse_cookie(COOKIE))
        print("с кукой :", r2.status_code, first_price(r2) if r2.status_code == 200 else "")
    else:
        print("WB_COOKIE не задан — пропустил авторизованный запрос")
    await wb_client.close()


asyncio.run(main())
