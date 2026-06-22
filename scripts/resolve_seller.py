"""Найти эндпоинт slug -> supplier_id: собрать все URL-подобные строки из JS-бандла.

    docker compose cp scripts/resolve_seller.py bot:/app/scripts/resolve_seller.py
    docker compose exec bot env PYTHONPATH=/app python scripts/resolve_seller.py moderndevice
"""
import asyncio
import re
import sys

from app.wb.client import wb_client

SLUG = sys.argv[1] if len(sys.argv) > 1 else "moderndevice"
REF = {"Referer": "https://www.wildberries.ru/"}
KEYS = ("webapi", "seo", "slug", "supplier", "seller")


async def get(url):
    try:
        r = await wb_client._session.get(url, headers=REF)
        return r.status_code, (getattr(r, "text", "") or "")
    except Exception as e:
        return None, f"ERR {e}"


async def main():
    st, html = await get(f"https://www.wildberries.ru/seller/{SLUG}")
    print("HTML:", st, "| размер:", len(html))
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    scripts = [s if s.startswith("http") else "https:" + s for s in scripts]
    hits = set()
    for src in scripts:
        _, js = await get(src)
        if not isinstance(js, str):
            continue
        for s in re.findall(r"""["'`]([^"'`]{4,90})["'`]""", js):
            low = s.lower()
            if ("/" in s or "{" in s) and any(k in low for k in KEYS):
                hits.add(s)
    print("--- пути со словами webapi/seo/slug/supplier/seller ---")
    for h in sorted(hits):
        print(" ", h)
    await wb_client.close()


asyncio.run(main())
