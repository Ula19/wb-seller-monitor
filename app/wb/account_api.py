"""Состояние аккаунта WB обычным HTTP по Bearer сайта (без браузера и антибота).

Bearer — localStorage www: `wbx__tokenData` или `wbid-oauth-sdk-access-token` (у разных
сессий по-разному; токен SSO id.wb.ru `wbIdAccessToken` — не тот, не берём). full-balance ещё хочет куку
wbx-validation-key, остальным хватает Bearer. Суммы WB — в копейках, наружу отдаём рубли.
401 → Unauthorized: решение (рефреш / «войдите заново») — у вызывающего.
"""
import logging
import re
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from app.wb.web_session import ProxyDown, curl_proxy, web_proxy

log = logging.getLogger(__name__)

BALANCE_URL = "https://wb-balance.wildberries.ru/wb-balance/api/v1/balance/full-balance"
VB_URL = "https://loyalty-balances.wildberries.ru/public/api/v1/bonuses/info"  # ВБ-баллы, авторизация как у full-balance
GRADE_URL = "https://user-grade.wildberries.ru/api/v6/grade?curr=RUB&uf_loc=ru"
ORDERS_URL = ("https://wbxoofex.wildberries.ru/api/v2/orders"
              "?limit=100&exclude_delivery_types=777,778&exclude_b2b=true&offset=0")
STATUSES_URL = "https://status-tracker.wb.ru/api/v6/statuses?lang=ru"
TOKEN_KEYS = ("wbx__tokenData", "wbid-oauth-sdk-access-token")  # по порядку предпочтения
WWW = "https://www.wildberries.ru"
WALLET_WCTYPE = 1  # grade.payload.payments[]: способ оплаты «Кошелёк»
PAY_STATE = {4: True, 1: False}  # rids[].pay_state: 4 оплачен, 1 не оплачен, прочее — не знаем
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
HEADERS = {"Origin": "https://www.wildberries.ru", "Referer": "https://www.wildberries.ru/",
           "Accept": "application/json"}


class Unauthorized(Exception):
    """WB ответил 401: access протух или сессию отозвали."""


class NoBearer(Exception):
    """В localStorage сессии нет Bearer сайта — браузерный рефреш может его дописать."""


def bearer(state: dict) -> str | None:
    www = {it.get("name"): it.get("value") or "" for o in state.get("origins") or [] if o.get("origin") == WWW
           for it in o.get("localStorage") or []}
    for key in TOKEN_KEYS:
        if m := JWT.search(www.get(key, "")):
            return m[0]
    return None


def _rub(kop) -> int | None:
    return kop // 100 if isinstance(kop, int) else None


def _ts(sec) -> datetime | None:
    return datetime.fromtimestamp(sec, timezone.utc) if isinstance(sec, (int, float)) and sec > 0 else None


def parse_balance(d: dict) -> dict:
    disc = d.get("discount") or {}
    lim, exp = disc.get("monthly_limit"), disc.get("expenses")
    return {"limit_left": _rub(lim - exp) if isinstance(lim, int) and isinstance(exp, int) else None,
            "monthly_limit": _rub(lim),
            "max_order": _rub(disc.get("max_order_price")), "max_rid": _rub(disc.get("max_rid_price")),
            # деньги в Кошельке WB = сумма в шапке сайта (wallet_balance_rub + bonus_balance — оба в рублях)
            "funds": _rub(d.get("available_funds_rub"))}


def parse_vb(d) -> int | None:
    """bonuses/info → ВБ-баллы (это баллы, не рубли)."""
    v = ((d or {}).get("data") or {}).get("total_bonuses") if isinstance(d, dict) else None
    return v if isinstance(v, int) else None


def parse_grade(d: dict) -> int | None:
    """% Кошелька: payments[wctype_id=1].discount_value (база 3 + Клуб 3). У b2b может не быть."""
    for p in (d.get("payload") or {}).get("payments") or []:
        if p.get("wctype_id") == WALLET_WCTYPE:
            return p.get("discount_value")
    return None


PROFILE_KEY = "wb_profile"  # web_session кладёт профиль в возвращаемый state; keeper забирает его оттуда
# JS в странице www: сайт сам Bearer к personalinfo не добавит — берём из localStorage, как сайт
PROFILE_JS = """async () => {
  const raw = localStorage.getItem('wbx__tokenData') || localStorage.getItem('wbid-oauth-sdk-access-token') || '';
  const m = raw.match(/eyJ[\\w-]+\\.[\\w-]+\\.[\\w-]+/);
  const h = {'x-requested-with': 'XMLHttpRequest'};
  if (m) h.authorization = 'Bearer ' + m[0];
  const r = await fetch('/webapi/personalinfo', {method: 'POST', headers: h, credentials: 'include'});
  return r.ok ? await r.json() : null;
}"""


def parse_profile(d) -> dict | None:
    """personalinfo → {name, phone, purchase_pct}. Аноним (нет Bearer/сессии) → None."""
    v = (d or {}).get("value") or {}
    if not v.get("isAuthenticated") and not v.get("firstName"):
        return None
    return {"name": (v.get("firstName") or "").strip() or None,
            "phone": v.get("formattedPhoneMobile") or None,
            "purchase_pct": v.get("purchasePercent") if isinstance(v.get("purchasePercent"), int) else None}


def parse_orders(d: dict) -> list[dict]:
    """Позиции активных заказов: в заказе (data[]) несколько rid, ключ — uid."""
    out = []
    for order in d.get("data") or []:
        for r in order.get("rids") or []:
            if not r.get("uid") or r.get("pay_type") == 0:  # pay_type 0 — служебный «Пакет», не товар
                continue
            out.append({"rid": r["uid"], "nm_id": r.get("nm_id"), "name": r.get("name"),
                        "brand": r.get("brand"), "price": _rub(r.get("price")),
                        "dst_office_id": r.get("dst_office_id") or order.get("dst_office_id"),
                        "ordered_at": _ts(order.get("order_dt")), "expiry_at": _ts(r.get("expiry_dt")),
                        "delivery_at": _ts(r.get("delivery_time")),  # плановая доставка (верхняя граница срока)
                        "paid": PAY_STATE.get(r.get("pay_state"))})
    return out


def apply_statuses(orders: list[dict], d) -> None:
    """status-tracker: на каждый rid история; текущий статус — запись с максимальной date (нс)."""
    last = {}
    for hist in d or []:
        for st in hist or []:
            rid = st.get("rid")
            if rid and (rid not in last or (st.get("date") or 0) > (last[rid].get("date") or 0)):
                last[rid] = st
    for o in orders:
        st = last.get(o["rid"]) or {}
        o["status_name"] = st.get("status_name")
        o["group_id"] = int(st["group_id"]) if str(st.get("group_id", "")).isdigit() else None
        o["status_at"] = _ts((st.get("date") or 0) / 1e9)


async def fetch(state: dict, proxy: str | None = None) -> dict:
    """Баланс, лимит, % Кошелька и активные заказы. Unauthorized на 401, ProxyDown — сеть через прокси
    не работает (таймаут, обрыв, 407), прочие сбои — исключением. proxy — прокси аккаунта."""
    token = bearer(state)
    if not token:  # не 401: сессия может быть жива (бизнес-цены идут по кукам) — просто нечем спросить
        raise NoBearer()
    auth = {**HEADERS, "Authorization": f"Bearer {token}"}
    vkey = next((c["value"] for c in state.get("cookies") or [] if c.get("name") == "wbx-validation-key"), None)
    proxy = curl_proxy(proxy or web_proxy())  # тот же выход, что у браузера аккаунта
    async with AsyncSession(impersonate="chrome", timeout=20,
                            proxies={"http": proxy, "https": proxy} if proxy else None) as s:
        async def call(method, url, **kw):
            try:
                r = await s.request(method, url, **kw)
            except RequestException as e:  # до ответа WB не дошли: таймаут, обрыв, CONNECT 407
                raise ProxyDown(f"{type(e).__name__}: {str(e)[:160]}") from e
            if r.status_code == 407:
                raise ProxyDown("407 Proxy Authentication Required")
            if r.status_code == 401:
                raise Unauthorized(url.split("/")[2])
            r.raise_for_status()
            return r.json()

        with_vkey = {**auth, **({"Cookie": f"wbx-validation-key={vkey}"} if vkey else {})}
        out = parse_balance(await call("GET", BALANCE_URL, headers=with_vkey))
        try:  # баллы — не главное: их сбой не ломает опрос
            out["vb_points"] = parse_vb(await call("GET", VB_URL, headers=with_vkey))
        except Exception as e:
            out["vb_points"] = None
            log.warning("ВБ-баллы не получены: %s: %s", type(e).__name__, str(e)[:200])
        out["wallet_pct"] = parse_grade(await call("GET", GRADE_URL, headers=auth))
        orders = parse_orders(await call("GET", ORDERS_URL, headers=auth))
        if orders:  # одним запросом на все rid аккаунта
            apply_statuses(orders, await call("POST", STATUSES_URL, headers=auth,
                                              json={"ids": [o["rid"] for o in orders]}))
        out["orders"] = orders
        return out


if __name__ == "__main__":  # self-check на формах ответов из HAR (суммы в копейках)
    b = parse_balance({"wallet_balance_rub": 428500, "bonus_balance": 20100, "available_funds_rub": 448600, "discount": {
        "expenses": 23355900, "monthly_limit": 100000000, "max_order_price": 100000000, "max_rid_price": 50000000}})
    assert b == {"limit_left": 766441, "monthly_limit": 1000000, "max_order": 1000000, "max_rid": 500000,
                 "funds": 4486}, b
    assert parse_balance({})["limit_left"] is None and parse_balance({})["funds"] is None  # бизнес без полей — не падаем
    yana = parse_balance({"wallet_balance_rub": 594100, "bonus_balance": 19194700, "available_funds_rub": 19788800})
    assert yana["funds"] == 197888 == 5941 + 191947, yana  # = сумма в шапке WB у Яны
    assert parse_vb({"data": {"total_bonuses": 9094}, "state": 0}) == 9094
    assert parse_vb({"state": 1}) is None and parse_vb(None) is None and parse_vb({"data": {"total_bonuses": "x"}}) is None
    assert parse_grade({"payload": {"payments": [{"wctype_id": 2}, {"wctype_id": 1, "discount_value": 6,
                                                                    "anonymous_discount": 3}]}}) == 6
    assert parse_grade({"state": 0}) is None
    o = parse_orders({"data": [{"order_dt": 1792950454, "dst_office_id": 172986, "rids": [
        {"uid": "eAe.a.0.0", "nm_id": 1, "price": 100000, "expiry_dt": 1794603599},
        {"uid": "eAe.b.0.0", "nm_id": 2, "price": 5050, "dst_office_id": 5}]}]})
    assert [(x["rid"], x["price"], x["dst_office_id"]) for x in o] == [("eAe.a.0.0", 1000, 172986), ("eAe.b.0.0", 50, 5)]
    apply_statuses(o, [[{"rid": "eAe.a.0.0", "status_name": "Вы оформили заказ", "group_id": "1", "date": 1 * 10**18},
                        {"rid": "eAe.a.0.0", "status_name": "Едет в пункт выдачи", "group_id": "3", "date": 2 * 10**18}]])
    assert (o[0]["status_name"], o[0]["group_id"]) == ("Едет в пункт выдачи", 3) and o[1]["status_name"] is None
    o = parse_orders({"data": [{"rids": [
        {"uid": "p4", "pay_state": 4, "pay_type": 1, "delivery_time": 1794653000},
        {"uid": "p1", "pay_state": 1, "pay_type": 1}, {"uid": "p2", "pay_state": 2},
        {"uid": "bag", "pay_type": 0, "name": "Пакет", "pay_state": 4}]}]})
    assert [(x["rid"], x["paid"]) for x in o] == [("p4", True), ("p1", False), ("p2", None)], o
    assert o[0]["delivery_at"] == datetime(2026, 11, 14, 10, 43, 20, tzinfo=timezone.utc) and o[1]["delivery_at"] is None
    assert parse_profile({"value": {"isAuthenticated": True, "firstName": " Анна ", "purchasePercent": 94,
                                    "formattedPhoneMobile": "+7 916 123-45-67"}}) == \
        {"name": "Анна", "phone": "+7 916 123-45-67", "purchase_pct": 94}
    assert parse_profile({"value": {"isAuthenticated": False, "firstName": ""}}) is None and parse_profile(None) is None
    st = {"cookies": [], "origins": [{"origin": WWW, "localStorage": [
        {"name": "wbx__tokenData", "value": '{"token":"eyJa.eyJb.sig-1","exp":1}'}]}]}
    assert bearer(st) == "eyJa.eyJb.sig-1" and bearer({"origins": []}) is None
    sso = {"origin": "https://id.wb.ru", "localStorage": [{"name": "wbIdAccessToken", "value": "eyJs.eyJs.s"}]}
    oauth = {"origin": WWW, "localStorage": [{"name": "wbid-oauth-sdk-access-token", "value": "eyJo.eyJo.o"}]}
    assert bearer({"origins": [sso, oauth]}) == "eyJo.eyJo.o"  # сессия с сервера 2026-11-05: только этот ключ
    assert bearer({"origins": [sso]}) is None  # SSO-токен id.wb.ru не подходит

    # fetch на подменённой HTTP-сессии: баллы приходят; сбой именно их запроса — None, опрос идёт дальше
    import asyncio
    from unittest.mock import patch

    class R:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def json(self):
            return self.body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def session(vb):
        bodies = {BALANCE_URL: R(200, {"available_funds_rub": 19788800}), VB_URL: vb,
                  GRADE_URL: R(200, {"state": 0}), ORDERS_URL: R(200, {"data": []})}
        seen = []

        class S:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def request(self, method, url, **kw):
                seen.append((url, kw.get("headers", {}).get("Cookie")))
                return bodies[url]
        return S, seen

    async def demo():
        for vb, want in ((R(200, {"data": {"total_bonuses": 9094}, "state": 0}), 9094), (R(500, {}), None),
                         (R(401, {}), None)):
            S, seen = session(vb)
            with patch(f"{__name__}.AsyncSession", S), patch(f"{__name__}.web_proxy", lambda: None):
                out = await fetch({**st, "cookies": [{"name": "wbx-validation-key", "value": "vk"}]})
            assert (out["funds"], out["vb_points"], out["orders"]) == (197888, want, []), out
            assert (VB_URL, "wbx-validation-key=vk") in seen  # заголовки как у full-balance
    with patch.object(log, "warning"):
        asyncio.run(demo())
    print("ok")
