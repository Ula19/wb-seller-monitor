"""Сверка цен одного магазина по всем путям бота: розница и бизнес, веб и api-ios.

Розница: каталог анонимно (веб — антибот-куки, api-ios — guardium).
Бизнес: detail под бизнес-аккаунтом (веб — куки аккаунта, api-ios — Bearer).
Токены и веб-сессия берутся из БД, как у бота. ~4-6 запросов к WB.
Запуск в контейнере:
    docker compose cp scripts/check_price.py bot:/app/scripts/check_price.py
    docker compose exec bot env PYTHONPATH=/app python scripts/check_price.py [supplier_id] [сколько_товаров]
"""
import asyncio
import dataclasses
import sys

from app.db import repo
from app.db.base import Session, engine
from app.services import keeper
from app.wb.client import guardium_expires_at, wb_client
from app.wb.web_client import web_client

SUP = int(sys.argv[1]) if len(sys.argv) > 1 else 250110041
LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 20


async def load_tokens():
    async with Session() as s:
        guardium = await repo.get_setting(s, "wb_guardium")
        bearer = await repo.get_setting(s, "wb_bearer")
    if guardium:
        wb_client.set_guardium(guardium)
    if bearer:
        wb_client.set_bearer(bearer)
    await keeper.load_into_client()
    print("guardium истекает:", guardium_expires_at(wb_client.guardium) or "нет/не разобран")
    print("Bearer:", "есть" if wb_client.bearer else "НЕТ")
    print("веб (прокси):", "есть" if web_client.ready else "НЕТ",
          "| бизнес-аккаунт:", "есть" if web_client.account_ready else "НЕТ (/wblogin)")


async def b2b_prices(fetch, products) -> dict[int, int] | None:
    """Бизнес-цены копий товаров через заданный путь. None — путь не ответил."""
    copies = [dataclasses.replace(p, price=None) for p in products]
    got = await fetch(copies)
    if got is None:
        return None
    return {p.nm_id: p.price for p in copies if p.nm_id in got}


async def web_b2b(copies):
    items = await web_client.fetch_b2b_items([p.nm_id for p in copies])
    return None if items is None else wb_client._apply_detail(copies, items)


def cell(v) -> str:
    return "—" if v is None else str(v)


async def main():
    try:
        if SUP <= 0 or LIMIT <= 0:
            raise ValueError("ID продавца и число товаров должны быть положительными")
        await load_tokens()
        web_retail = await web_client.fetch_catalog(SUP)
        ios_retail = await wb_client._fetch_catalog_mobile(SUP, slot=wb_client._direct_slot)
        print(f"\nрозница: веб {cell(web_retail and len(web_retail))} товаров, "
              f"api-ios {len(ios_retail)} товаров")
        base = (web_retail or ios_retail)[:LIMIT]
        if not base:
            print("товаров нет ни в одном каталоге — сверять нечего")
            return
        web_r = {p.nm_id: p.price for p in web_retail or []}
        ios_r = {p.nm_id: p.price for p in ios_retail}
        web_b = await b2b_prices(web_b2b, base)
        ios_b = await b2b_prices(
            lambda c: wb_client._enrich_prices_mobile(c, wb_client._direct_slot), base)
        if web_b is None:
            print("веб бизнес-detail: НЕ ответил (нет аккаунта / ошибка — см. лог выше)")

        print(f"\n{'nm':>11} | {'розн веб':>8} | {'розн ios':>8} | {'биз веб':>8} | {'биз ios':>8} | товар")
        bad = 0
        for p in base:
            row = (web_r.get(p.nm_id), ios_r.get(p.nm_id),
                   (web_b or {}).get(p.nm_id), ios_b.get(p.nm_id))
            # расхождение одного пути с другим внутри розницы или бизнеса
            diff = (None not in row[:2] and row[0] != row[1]) or (None not in row[2:] and row[2] != row[3])
            bad += diff
            print(f"{p.nm_id:>11} | " + " | ".join(f"{cell(v):>8}" for v in row)
                  + f" | {p.name[:40]}" + ("  ⚠️ веб≠ios" if diff else ""))
        print(f"\nрасхождений веб/ios: {bad} из {len(base)}. "
              "Бизнес ≠ розница — норма, если у WB есть бизнес-скидка на товар.")
    except Exception as e:
        print("проверка не удалась:", type(e).__name__, str(e))
        raise
    finally:
        await wb_client.close()
        await engine.dispose()


asyncio.run(main())
