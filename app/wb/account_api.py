"""Состояние аккаунта WB обычным HTTP по Bearer сайта (без браузера и антибота).

Bearer — localStorage www: `wbx__tokenData` или `wbid-oauth-sdk-access-token` (у разных
сессий по-разному; токен SSO id.wb.ru `wbIdAccessToken` — не тот, не берём). full-balance ещё хочет куку
wbx-validation-key, остальным хватает Bearer. Суммы WB — в копейках, наружу отдаём рубли.
401 → Unauthorized: решение (рефреш / «войдите заново») — у вызывающего.
"""
import re
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession

from app.wb.web_session import curl_proxy, web_proxy

BALANCE_URL = "https://wb-balance.wildberries.ru/wb-balance/api/v1/balance/full-balance"
GRADE_URL = "https://user-grade.wildberries.ru/api/v6/grade?curr=RUB&uf_loc=ru"
ORDERS_URL = ("https://wbxoofex.wildberries.ru/api/v2/orders"
              "?limit=100&exclude_delivery_types=777,778&exclude_b2b=true&offset=0")
STATUSES_URL = "https://status-tracker.wb.ru/api/v6/statuses?lang=ru"
TOKEN_KEYS = ("wbx__tokenData", "wbid-oauth-sdk-access-token")  # по порядку предпочтения
WWW = "https://www.wildberries.ru"
WALLET_WCTYPE = 1  # grade.payload.payments[]: способ оплаты «Кошелёк»
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
            "wallet_balance": _rub(d.get("wallet_balance_rub")), "bonus_balance": _rub(d.get("bonus_balance"))}


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
            if not r.get("uid"):
                continue
            out.append({"rid": r["uid"], "nm_id": r.get("nm_id"), "name": r.get("name"),
                        "brand": r.get("brand"), "price": _rub(r.get("price")),
                        "dst_office_id": r.get("dst_office_id") or order.get("dst_office_id"),
                        "ordered_at": _ts(order.get("order_dt")), "expiry_at": _ts(r.get("expiry_dt"))})
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


async def fetch(state: dict) -> dict:
    """Баланс, лимит, % Кошелька и активные заказы. Unauthorized на 401, прочие сбои — исключением."""
    token = bearer(state)
    if not token:  # не 401: сессия может быть жива (бизнес-цены идут по кукам) — просто нечем спросить
        raise NoBearer()
    auth = {**HEADERS, "Authorization": f"Bearer {token}"}
    vkey = next((c["value"] for c in state.get("cookies") or [] if c.get("name") == "wbx-validation-key"), None)
    proxy = curl_proxy(web_proxy())  # тот же выход, что у браузера аккаунта
    async with AsyncSession(impersonate="chrome", timeout=20,
                            proxies={"http": proxy, "https": proxy} if proxy else None) as s:
        async def call(method, url, **kw):
            r = await s.request(method, url, **kw)
            if r.status_code == 401:
                raise Unauthorized(url.split("/")[2])
            r.raise_for_status()
            return r.json()

        out = parse_balance(await call("GET", BALANCE_URL, headers={
            **auth, **({"Cookie": f"wbx-validation-key={vkey}"} if vkey else {})}))
        out["wallet_pct"] = parse_grade(await call("GET", GRADE_URL, headers=auth))
        orders = parse_orders(await call("GET", ORDERS_URL, headers=auth))
        if orders:  # одним запросом на все rid аккаунта
            apply_statuses(orders, await call("POST", STATUSES_URL, headers=auth,
                                              json={"ids": [o["rid"] for o in orders]}))
        out["orders"] = orders
        return out


if __name__ == "__main__":  # self-check на формах ответов из HAR (суммы в копейках)
    b = parse_balance({"wallet_balance_rub": 428500, "bonus_balance": 20100, "discount": {
        "expenses": 23355900, "monthly_limit": 100000000, "max_order_price": 100000000, "max_rid_price": 50000000}})
    assert b == {"limit_left": 766441, "monthly_limit": 1000000, "max_order": 1000000, "max_rid": 500000,
                 "wallet_balance": 4285, "bonus_balance": 201}, b
    assert parse_balance({})["limit_left"] is None  # бизнес-аккаунт без полей — не падаем
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
    print("ok")
