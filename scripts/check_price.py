"""Что отдаёт catalog.wb.ru с кукой аккаунта и без неё — все поля товара.

Безопасно: всего 2 запроса с паузой. Запуск в контейнере (стабильный IP):
    docker compose cp scripts/check_price.py bot:/app/scripts/check_price.py
    docker compose exec bot env PYTHONPATH=/app python scripts/check_price.py
Куку берёт из WB_COOKIE или из settings.wb_cookie (та же, что у бота).
"""
import asyncio
import json
import os
import time

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.db import repo
from app.db.base import Session
from app.wb.client import HEADERS, IMPERSONATE, _parse_cookie

CATALOG = "https://catalog.wb.ru/sellers/v4/catalog"
COOKIE = os.environ.get("WB_COOKIE") or settings.wb_cookie


async def fetch(sess, supplier):
    params = {
        "appType": 1, "curr": "rub", "dest": settings.wb_dest,
        "sort": "popular", "spp": settings.wb_spp, "supplier": supplier, "page": 1,
    }
    return await sess.get(CATALOG, params=params)


def dump(tag, r):
    print(f"=== {tag}: {r.status_code} ===")
    if r.status_code != 200:
        print(r.text[:80]); return None
    p = r.json()["products"][0]
    print("nm", p["id"], "| ключи товара:", sorted(p.keys()))
    for s in p.get("sizes", []):
        if s.get("price"):
            print("price:", json.dumps(s["price"], ensure_ascii=False))
            print("size др.поля:", json.dumps({k: v for k, v in s.items() if k != "price"}, ensure_ascii=False)[:200])
            break
    return set(p.keys())


async def main():
    async with Session() as s:
        sellers = await repo.list_sellers(s)
    if not sellers:
        print("в БД нет продавцов"); return
    supplier = sellers[0].supplier_id
    print(f"продавец {supplier} ({sellers[0].name})")

    ck = _parse_cookie(COOKIE) if COOKIE else None
    async with AsyncSession(headers=HEADERS, cookies=ck, impersonate=IMPERSONATE, timeout=20) as s1:
        k1 = dump("С КУКОЙ" if ck else "С КУКОЙ (куки нет!)", await fetch(s1, supplier))
    time.sleep(5)  # пауза — не злим WAF
    async with AsyncSession(headers=HEADERS, impersonate=IMPERSONATE, timeout=20) as s2:
        k2 = dump("БЕЗ КУКИ", await fetch(s2, supplier))

    if k1 and k2:
        print("поля ТОЛЬКО с кукой:", (k1 - k2) or "нет — наборы одинаковые")


asyncio.run(main())
