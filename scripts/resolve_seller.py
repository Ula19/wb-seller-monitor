"""Найти числовой supplier_id по slug-ссылке продавца (.../seller/<slug>).

Страница продавца — SPA-шелл, ID грузится JS-запросом. Скрипт ищет рабочий
API-эндпоинт slug -> id. Безопасно (несколько запросов через сессию бота).
    docker compose cp scripts/resolve_seller.py bot:/app/scripts/resolve_seller.py
    docker compose exec bot env PYTHONPATH=/app python scripts/resolve_seller.py moderndevice
"""
import asyncio
import re
import sys

from app.wb.client import wb_client

SLUG = sys.argv[1] if len(sys.argv) > 1 else "moderndevice"
REF = {"Referer": "https://www.wildberries.ru/"}

# кандидаты API, которые могут отдать данные продавца по slug
CANDIDATES = [
    f"https://www.wildberries.ru/webapi/seller/{SLUG}",
    f"https://www.wildberries.ru/webapi/seller/data/short/{SLUG}",
    f"https://www.wildberries.ru/webapi/seller/seo-data/{SLUG}",
    f"https://static-basket-01.wbbasket.ru/vol0/data/supplier-by-name/{SLUG}.json",
    f"https://seller-supplier.wildberries.ru/api/v1/seller/{SLUG}",
]


async def probe(url):
    try:
        r = await wb_client._session.get(url, headers=REF)
    except Exception as e:
        print(f"  {url}\n    ERR {e}")
        return
    body = (getattr(r, "text", "") or "")[:300]
    print(f"  {url}\n    {r.status_code} | {body!r}")


async def main():
    page = f"https://www.wildberries.ru/seller/{SLUG}"
    r = await wb_client._session.get(page, headers=REF)
    html = getattr(r, "text", "") or ""
    print("HTML страница:", r.status_code, "| размер:", len(html))
    print("script src:", re.findall(r'<script[^>]+src="([^"]+)"', html)[:10])
    print("ссылки api/webapi:", re.findall(r'https?://[^\s"\'<>]*?(?:api|webapi)[^\s"\'<>]*', html)[:10])
    print("длинные числа (>=5):", re.findall(r'\b\d{5,}\b', html)[:10])
    print("--- кандидаты эндпоинтов ---")
    for url in CANDIDATES:
        await probe(url)
    await wb_client.close()


asyncio.run(main())
