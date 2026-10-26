"""Проверить B2B-цену товара тем же запросом, что бот, — через КАЖДЫЙ слот (direct + прокси).

Одна и та же кука на всех слотах: 200 где-то и 403 где-то → дело в IP; 403/498 везде →
дело в куке. Запуск в контейнере:
    docker compose exec bot env PYTHONPATH=/app python scripts/check_b2b.py [nm ...]
Куку берёт из БД (кнопка «🔑 Куки»), как бот.
"""
import asyncio
import json
import os
import sys

from app.db import repo
from app.db.base import Session, engine
from app.wb.client import B2B_DETAIL_URL, B2B_PARAMS, _browser_headers, wb_client

NMS = sys.argv[1:] or ["835510459"]


async def main():
    async with Session() as s:
        saved = await repo.get_setting(s, "wb_cookie")
    if saved:
        await wb_client.set_cookie(saved)
    print("кука:", "из БД" if saved else "из .env (может быть протухшей!)",
          "| полей:", len(wb_client._cookies or {}), "| URL:", B2B_DETAIL_URL)
    params = {**B2B_PARAMS, "nm": ";".join(NMS)}
    headers = _browser_headers(f"https://www.wildberries.ru/catalog/{NMS[0]}/detail.aspx")
    for slot in wb_client.slots:
        who = slot.proxy or "direct"
        try:
            r = await slot.session.get(B2B_DETAIL_URL, params=params, headers=headers,
                                       cookies=wb_client._cookies)
        except Exception as e:
            print(f"{who}: СЕТЕВАЯ ОШИБКА {type(e).__name__}")
            continue
        line = f"{who}: {r.status_code} server={r.headers.get('server')}"
        if r.status_code == 200:
            prods = r.json().get("products") or []
            prices = [((p.get("sizes") or [{}])[0].get("price") or {}).get("product") for p in prods]
            line += f" цены={prices}"
        else:
            line += f" тело={r.text[:80]!r}"
        print(line)
        await asyncio.sleep(2)
    await wb_client.close()
    await engine.dispose()


asyncio.run(main())
