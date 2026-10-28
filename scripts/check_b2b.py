"""Проверить бизнес-цену товара тем же запросом, что бот (мобильный detail + Bearer).

Сравнивает с Bearer и без: совпадают — у товара сейчас нет бизнес-скидки (или Bearer
не бизнес-аккаунта); 401/403 — WB не принял Bearer; 498 — протух guardium. Запуск:
    docker compose exec bot env PYTHONPATH=/app python scripts/check_b2b.py [nm ...]
Токены берёт из БД (кнопка «🔑 Токены»), как бот.
"""
import asyncio
import sys

from app.config import settings
from app.db import repo
from app.db.base import Session, engine
from app.wb.client import B2B_DETAIL_URL, B2B_PARAMS, _mobile_headers, bearer_user, wb_client

NMS = sys.argv[1:] or ["326308531"]


async def main():
    async with Session() as s:
        guardium = await repo.get_setting(s, "wb_guardium")
        bearer = await repo.get_setting(s, "wb_bearer")
    if guardium:
        wb_client.set_guardium(guardium)
    if bearer:
        wb_client.set_bearer(bearer)
    print("guardium:", "из БД" if guardium else "из .env",
          "| Bearer:", f"аккаунт {bearer_user(wb_client.bearer)}" if wb_client.bearer else "НЕТ")
    params = {**B2B_PARAMS, "dest": settings.wb_dest, "udg": settings.wb_b2b_udg, "nm": ";".join(NMS)}
    base = _mobile_headers(wb_client.guardium)
    for name, headers in (("с Bearer", {**base, "authorization": wb_client.bearer}), ("без Bearer", base)):
        r = await wb_client._get(B2B_DETAIL_URL, params=params, headers=headers, slot=wb_client._direct_slot)
        if r is None or r.status_code != 200:
            print(f"{name}: {'сеть' if r is None else r.status_code}")
            continue
        for p in r.json().get("products") or []:
            price = next((sz["price"]["product"] // 100 for sz in p.get("sizes") or []
                          if (sz.get("price") or {}).get("product")), None)
            print(f"{name}: {p['id']} | {price} ₽ | {p.get('name', '')[:40]}")
    await wb_client.close()
    await engine.dispose()


asyncio.run(main())
