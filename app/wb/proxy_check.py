"""Прокси аккаунтов WB: разбор адреса (api, при добавлении) и проверка (worker, по кнопке).

Аккаунту нужен СВОЙ постоянный выход: не тот, что у мониторинга (прямой IP сервера и прокси
парсера из .env), и не меняющийся между запросами (сессия WB привязана к IP).
"""
import asyncio
from urllib.parse import urlparse

from app.config import settings

IP_URL = "https://api.ipify.org"
WB_URL = "https://www.wildberries.ru/"
IP_RECHECK_SEC = 10  # пауза между двумя замерами внешнего IP: ротация выдаёт другой
TIMEOUT = 20


def parse_proxy(url: str) -> tuple[str, str]:
    """Адрес прокси → (нормализованный url, host:port для показа). Плохой — ValueError с текстом для сайта."""
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https", "socks5", "socks5h"):
        raise ValueError("нужен адрес вида http://логин:пароль@хост:порт")
    try:
        port = u.port
    except ValueError:
        port = None
    if not u.hostname or not port:
        raise ValueError("нет хоста или порта: http://логин:пароль@хост:порт")
    socks = u.scheme.startswith("socks")
    if socks and u.username:  # Chromium (вход и рефреш идут в браузере) не умеет socks5 с паролем
        raise ValueError("браузер WB не умеет socks5 с паролем — нужен http://логин:пароль@хост:порт")
    auth = f"{u.username}:{u.password or ''}@" if u.username else ""
    server = f"{u.hostname.lower()}:{port}"
    return f"{'socks5' if socks else u.scheme}://{auth}{server}", server


def parser_proxies() -> list[str]:
    """Прокси мониторинга из .env (WB_PROXIES + WB_WEB_PROXY)."""
    raw = settings.wb_proxies.split(",") + [settings.wb_web_proxy]
    return [x.strip() for x in raw if x.strip()]


def is_parser_proxy(url: str) -> bool:
    norm = parse_proxy(url)[0]
    for p in parser_proxies():
        try:
            if parse_proxy(p)[0] == norm:
                return True
        except ValueError:  # прокси парсера бывает socks5 с паролем — для сравнения строкой
            if p.replace("socks5h://", "socks5://") == norm:
                return True
    return False


async def _get(url: str, proxy: str | None):
    from curl_cffi.requests import AsyncSession
    from app.wb.web_session import curl_proxy
    p = curl_proxy(proxy)
    async with AsyncSession(impersonate="chrome", timeout=TIMEOUT,
                            proxies={"http": p, "https": p} if p else None) as s:
        return await s.get(url)


async def exit_ip(proxy: str | None) -> str:
    r = await _get(IP_URL, proxy)
    r.raise_for_status()
    return r.text.strip()


async def monitoring_ips() -> set[str]:
    """Внешние IP мониторинга: прямой IP сервера + каждый прокси парсера (недоступный — пропускаем)."""
    got = await asyncio.gather(*(exit_ip(p) for p in [None, *parser_proxies()]), return_exceptions=True)
    return {ip for ip in got if isinstance(ip, str)}


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}"


async def check(url: str) -> dict:
    """→ {exit_ip, ok, error}. ok — IP постоянный, не мониторинга, WB отвечает (498 антибота — норма)."""
    try:
        ip = await exit_ip(url)
        await asyncio.sleep(IP_RECHECK_SEC)
        ip2 = await exit_ip(url)
    except Exception as e:
        return {"exit_ip": None, "ok": False, "error": f"прокси не отвечает ({_err(e)})"}
    if ip != ip2:
        return {"exit_ip": ip2, "ok": False, "error": f"IP меняется ({ip} → {ip2}) — для аккаунта не годится"}
    if ip in await monitoring_ips():
        return {"exit_ip": ip, "ok": False,
                "error": "тот же IP, что у мониторинга (прямой IP сервера или прокси парсера)"}
    try:
        r = await _get(WB_URL, url)
    except Exception as e:
        return {"exit_ip": ip, "ok": False, "error": f"WB недоступен через прокси ({_err(e)})"}
    if r.status_code in (403, 407):
        return {"exit_ip": ip, "ok": False, "error": f"WB ответил {r.status_code} — IP, похоже, заблокирован"}
    return {"exit_ip": ip, "ok": True, "error": None}


if __name__ == "__main__":  # self-check: разбор адреса, сравнение с парсером, проверка на заглушках
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace as R

    assert parse_proxy(" http://u:p@Gate.Example.com:8000 ") == ("http://u:p@gate.example.com:8000", "gate.example.com:8000")
    assert parse_proxy("socks5h://1.2.3.4:1080") == ("socks5://1.2.3.4:1080", "1.2.3.4:1080")
    for bad, why in [("socks5://u:p@h:1", "socks5 с паролем"), ("ftp://h:1", "вида"), ("http://h", "порта"),
                     ("http://h:99999", "порта"), ("h:1", "вида")]:
        try:
            parse_proxy(bad)
            raise AssertionError(bad)
        except ValueError as e:
            assert why in str(e), (bad, e)
    with patch.object(settings, "wb_proxies", "http://a:b@gate:8000, socks5://x:y@s:1"), \
            patch.object(settings, "wb_web_proxy", ""):
        assert is_parser_proxy("http://a:b@GATE:8000")  # тот же адрес — нельзя
        assert not is_parser_proxy("http://other:b@gate:8000")  # тот же шлюз, другой логин — можно

    def ips(*seq):  # заглушка ipify: по очереди для прокси аккаунта, мониторинг — 9.9.9.9
        it = iter(seq)

        async def fake(url, proxy):
            if url == IP_URL:
                return R(text=next(it) if proxy == "http://acc:1" else "9.9.9.9", raise_for_status=lambda: None)
            return R(status_code=498)
        return fake

    async def demo():
        with patch.object(asyncio, "sleep", AsyncMock()), patch.object(settings, "wb_proxies", ""), \
                patch.object(settings, "wb_web_proxy", ""):
            with patch(f"{__name__}._get", ips("1.1.1.1", "1.1.1.1")):
                assert await check("http://acc:1") == {"exit_ip": "1.1.1.1", "ok": True, "error": None}
            with patch(f"{__name__}._get", ips("1.1.1.1", "2.2.2.2")):
                assert "IP меняется" in (await check("http://acc:1"))["error"]
            with patch(f"{__name__}._get", ips("9.9.9.9", "9.9.9.9")):  # = прямой IP сервера
                assert "мониторинга" in (await check("http://acc:1"))["error"]
            with patch(f"{__name__}._get", AsyncMock(side_effect=TimeoutError("t"))):
                r = await check("http://acc:1")
                assert r["ok"] is False and "не отвечает" in r["error"]
        print("ok")
    asyncio.run(demo())
