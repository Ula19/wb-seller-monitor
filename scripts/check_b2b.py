"""Проверить B2B-цену товара через www.wildberries.ru/__internal (b2b=true).

Безопасно: 1 запрос через сессию бота. Запуск в контейнере:
    docker compose cp scripts/check_b2b.py bot:/app/scripts/check_b2b.py
    docker compose exec bot env PYTHONPATH=/app WB_AUTH='Bearer ...' python scripts/check_b2b.py <nm>

Куку берёт из settings.wb_cookie (та же, что у бота). WB_AUTH — заголовок
authorization бизнес-аккаунта (если нужен), x-pow — через WB_POW.
"""
import asyncio
import json
import os
import sys

from app.config import settings
from app.wb.client import wb_client

NMS = sys.argv[1:] or ["1166248655"]
URL = "https://www.wildberries.ru/__internal/card/cards/v4/detail"
# параметры точно как у браузера на бизнес-странице
PARAMS = {
    "appType": 1, "curr": "rub", "dest": -446112, "spp": 30,
    "hide_vflags": 4294967296, "hide_dflags": 131072, "hide_dtype": "11;13;14;15",
    "b2b": "true", "mdg": 3, "mtype": 257, "lang": "ru", "ab_testing": "false",
    "nm": ";".join(NMS),
}


async def main():
    slot = wb_client._proxy_slots()[0]  # b2b ходит через прокси-слот, как в enrich_prices
    print("кука активна:", bool(settings.wb_cookie), "| прокси:", slot.proxy or "нет (напрямую)")
    # те же браузерные заголовки, что шлёт бот в _apply_detail_prices (без них WBAAS даёт 403)
    headers = {
        "Accept": "*/*",
        "Referer": f"https://www.wildberries.ru/catalog/{NMS[0]}/detail.aspx",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "x-requested-with": "XMLHttpRequest",
        "deviceid": settings.wb_device_id,
        "x-spa-version": settings.wb_spa_version,
    }
    try:
        r = await slot.session.get(URL, params=PARAMS, headers=headers)
    except Exception as e:
        print("СЕТЕВАЯ ОШИБКА (прокси/таймаут, НЕ кука):", e)
        await wb_client.close(); return
    print("статус:", r.status_code)
    if r.status_code != 200:
        print(r.text[:150]); await wb_client.close(); return
    for p in r.json().get("products") or []:
        sizes = p.get("sizes") or []
        size = sizes[0] if sizes else {}
        stock = (size.get("stocks") or [{}])[0]
        print("=" * 40)
        print("nm", p["id"], "|", p.get("name"))
        # верхний уровень + stock — где-то лежит признак склада
        for k in ("time1", "time2", "wh", "dist", "dtype"):
            print(f"  top.{k}:", p.get(k), "| stock.{}:".format(k), stock.get(k))
        print("  supplierFlags:", p.get("supplierFlags"))
    await wb_client.close()


asyncio.run(main())
