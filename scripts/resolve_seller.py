"""Найти эндпоинт slug -> supplier_id, выкопав URL-шаблоны из JS-бандла WB SPA.

В HTML страницы продавца числового ID нет — его грузит JS. Ищем в бандлах строки
с 'seller'/'supplier'/'slug'/'seo', чтобы понять, какой API даёт ID по slug.
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
    st, html = await get(f"https://www.wildberries.ru/seller/{SLUG}")
    print("HTML:", st, "| размер:", len(html))
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', html)
    scripts = [s if s.startswith("http") else "https:" + s for s in scripts]
    hits = set()
    for src in scripts:
        st, js = await get(src)
        if not isinstance(js, str):
            continue
        for pat in (r'["\'][^"\']{0,40}(?:seller|supplier)[^"\']{0,60}["\']',
                    r'[A-Za-z0-9_./{}-]*(?:bySlug|seo)[A-Za-z0-9_./{}-]*'):
            for mt in re.findall(pat, js):
                if "/" in mt or "Slug" in mt or "seo" in mt.lower():
                    hits.add(mt.strip("\"'"))
        print(f"  просмотрел {src.rsplit('/',1)[-1]} ({len(js)} б)")
    print("--- кандидаты путей ---")
    for h in sorted(hits):
        print(" ", h)
    await wb_client.close()


asyncio.run(main())
