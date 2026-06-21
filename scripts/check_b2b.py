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

NM = sys.argv[1] if len(sys.argv) > 1 else "1166248655"
URL = "https://www.wildberries.ru/__internal/card/cards/v4/detail"
# параметры точно как у браузера на бизнес-странице
PARAMS = {
    "appType": 1, "curr": "rub", "dest": -446112, "spp": 30,
    "hide_vflags": 4294967296, "hide_dflags": 131072, "hide_dtype": "11;13;14;15",
    "b2b": "true", "mdg": 3, "mtype": 257, "lang": "ru", "ab_testing": "false",
    "nm": NM,
}


async def main():
    print("кука активна:", bool(settings.wb_cookie), "| b2b=true")
    headers = {"Referer": "https://www.wildberries.ru/"}
    if os.environ.get("WB_AUTH"):
        headers["authorization"] = os.environ["WB_AUTH"]
    if os.environ.get("WB_POW"):
        headers["x-pow"] = os.environ["WB_POW"]
    r = await wb_client._session.get(URL, params=PARAMS, headers=headers)
    print("статус:", r.status_code)
    if r.status_code != 200:
        print(r.text[:150]); await wb_client.close(); return
    p = r.json()["products"][0]
    print("nm", p["id"], "|", p.get("name"))
    print("ключи товара:", sorted(p.keys()))
    for s in p.get("sizes", []):
        if s.get("price"):
            print("price:", json.dumps(s["price"], ensure_ascii=False))
            break
    await wb_client.close()


asyncio.run(main())
