"""Найти числовой supplier_id по slug-ссылке продавца (.../seller/<slug>).

Страница продавца — SPA-шелл, но в HTML встречается 9-значный supplier_id.
Скрипт показывает контекст длинных чисел и проверяет кандидата по id-эндпоинтам.
    docker compose cp scripts/resolve_seller.py bot:/app/scripts/resolve_seller.py
    docker compose exec bot env PYTHONPATH=/app python scripts/resolve_seller.py moderndevice
"""
import asyncio
import re
import sys

from app.wb.client import wb_client

SLUG = sys.argv[1] if len(sys.argv) > 1 else "moderndevice"
REF = {"Referer": "https://www.wildberries.ru/"}


async def get(url):
    try:
        r = await wb_client._session.get(url, headers=REF)
        return r.status_code, (getattr(r, "text", "") or "")
    except Exception as e:
        return None, f"ERR {e}"


async def main():
    page = f"https://www.wildberries.ru/seller/{SLUG}"
    st, html = await get(page)
    print("HTML:", st, "| размер:", len(html))
    # контекст всех чисел 6+ знаков — где лежит supplier_id
    for n in dict.fromkeys(re.findall(r"\d{6,}", html)):
        i = html.find(n)
        print(f"  {n}: ...{html[max(0,i-45):i+len(n)+15]}...")
    # кандидаты supplier_id: 8-9 значные числа
    cands = [n for n in dict.fromkeys(re.findall(r"\b\d{8,9}\b", html))]
    print("кандидаты supplier_id:", cands)
    for sid in cands:
        print(f"--- проверяю {sid} ---")
        st, body = await get(
            f"https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-id/{sid}.json"
        )
        print(f"  supplier-by-id: {st} | {body[:200]!r}")
        st, body = await get(
            f"https://catalog.wb.ru/sellers/v4/catalog?appType=1&curr=rub&dest=-1257786&page=1&supplier={sid}"
        )
        cnt = body.count('"id":') if isinstance(body, str) else 0
        print(f"  catalog: {st} | товаров на стр1≈{cnt}")
    await wb_client.close()


asyncio.run(main())
