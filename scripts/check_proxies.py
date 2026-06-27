"""Проверка бесплатных прокси против каталога WB.

Дёргает каталог реального магазина через каждый прокси, печатает кто жив (200 + товары),
а в конце — готовую строку WB_PROXIES для .env (только живые).

Запуск на сервере в контейнере бота (там есть curl_cffi и рабочее окружение):

    docker compose exec -T bot python - < scripts/check_proxies.py

Список прокси правь в блоке PROXIES ниже (по одному IP:port на строку).
"""

import asyncio

from curl_cffi.requests import AsyncSession

# IP:port по одному на строку (#-комментарии и пустые строки игнорируются)
PROXIES = """
89.237.36.129:37647
176.99.134.183:8090
31.28.4.192:80
45.82.153.23:80
213.178.39.170:8080
79.111.13.155:50625
185.139.68.62:443
89.17.35.212:8080
45.87.140.155:8080
46.161.4.153:3333
89.237.36.193:37647
62.140.233.192:41258
78.109.34.192:8080
176.12.65.24:443
155.212.135.193:5050
89.17.35.213:8080
81.177.160.200:80
188.127.249.218:443
159.194.203.75:8118
5.101.5.160:2080
95.31.6.47:20173
46.229.187.39:80
147.45.215.249:8443
94.31.136.156:2080
45.12.73.202:10808
89.237.32.66:37647
94.198.218.123:3128
81.177.74.135:8081
185.178.44.115:10443
37.113.186.198:2080
"""

URL = "https://catalog.wb.ru/sellers/v4/catalog"
PARAMS = {
    "appType": 1, "curr": "rub", "dest": -1257786, "sort": "popular",
    "spp": 30, "supplier": 866740, "page": 1,  # МегаФон — точно есть товары
}
HEADERS = {"Accept-Language": "ru-RU,ru;q=0.9"}
TIMEOUT = 12
CONCURRENCY = 10


async def check(ip_port: str, sem: asyncio.Semaphore):
    proxy = f"http://{ip_port}"
    async with sem:
        s = AsyncSession(
            headers=HEADERS, impersonate="chrome",
            proxies={"http": proxy, "https": proxy}, timeout=TIMEOUT,
        )
        try:
            r = await s.get(URL, params=PARAMS)
        except Exception as e:
            return ip_port, None, type(e).__name__
        finally:
            await s.close()
    n = None
    if r.status_code == 200:
        try:
            d = r.json()
            n = len(d.get("products") or (d.get("data") or {}).get("products") or [])
        except Exception:
            n = -1
    return ip_port, r.status_code, n


async def main():
    proxies = [ln.split("#")[0].strip() for ln in PROXIES.splitlines()]
    proxies = [p for p in proxies if p]
    sem = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(check(p, sem) for p in proxies))

    good = []
    print("=== результаты ===")
    for ip_port, status, info in sorted(results, key=lambda x: (x[1] != 200, x[0])):
        if status == 200 and (info or 0) > 0:
            good.append(ip_port)
            mark = f"✅ ЖИВ (товаров {info})"
        elif status == 200:
            mark = "⚠️ 200, но 0 товаров (не годится)"
        elif status is None:
            mark = f"✖️ ошибка ({info})"
        else:
            mark = f"✖️ {status}"
        print(f"{ip_port:24} {mark}")

    print(f"\nЖивых: {len(good)}/{len(proxies)}")
    if good:
        print("\nВставь в .env:")
        print("WB_PROXIES=" + ",".join(f"http://{p}" for p in good))


asyncio.run(main())
