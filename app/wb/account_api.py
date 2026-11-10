"""Состояние аккаунта WB обычным HTTP по Bearer сайта (без браузера и антибота).

Bearer — localStorage www: `wbx__tokenData` или `wbid-oauth-sdk-access-token` (у разных
сессий по-разному; токен SSO id.wb.ru `wbIdAccessToken` — не тот, не берём). full-balance ещё хочет куку
wbx-validation-key, остальным хватает Bearer. Суммы WB — в копейках, наружу отдаём рубли.
401 → Unauthorized: решение (рефреш / «войдите заново») — у вызывающего.
"""
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from app.wb.web_session import ProxyDown, curl_proxy, web_proxy

log = logging.getLogger(__name__)

BALANCE_URL = "https://wb-balance.wildberries.ru/wb-balance/api/v1/balance/full-balance"
VB_URL = "https://loyalty-balances.wildberries.ru/public/api/v1/bonuses/info"  # ВБ-баллы, авторизация как у full-balance
VB_HISTORY_URL = "https://loyalty-balances.wildberries.ru/public/api/v1/bonuses/history?limit=50&offset=0&currency_id=1"
# история заказов: только полученные (status=32), листаем offset; Bearer + кука wbx-validation-key
PURCHASES_URL = "https://astro.wildberries.ru/api/v1/shopper/api/v2/purchases/list?offset={offset}&status=32&limit={limit}"
PURCHASES_PAGE = 100
PURCHASES_MAX_PAGES = 200  # предохранитель: 20 000 покупок (у живых аккаунтов уже 3 000+)
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
VISIT_KEY = "wb_visit"  # так же — итог визита: {"pickup_code": str | None, "balance": ответ mywallet | None}
# JS в странице www: сайт сам Bearer к personalinfo не добавит — берём из localStorage, как сайт
PROFILE_JS = """async () => {
  const raw = localStorage.getItem('wbx__tokenData') || localStorage.getItem('wbid-oauth-sdk-access-token') || '';
  const m = raw.match(/eyJ[\\w-]+\\.[\\w-]+\\.[\\w-]+/);
  const h = {'x-requested-with': 'XMLHttpRequest'};
  if (m) h.authorization = 'Bearer ' + m[0];
  const r = await fetch('/webapi/personalinfo', {method: 'POST', headers: h, credentials: 'include'});
  return r.ok ? await r.json() : null;
}"""


# в браузере аккаунта (за антиботом, как personalinfo): история баланса — 20 последних операций, копим у себя.
# wb-balance/api/v2/balance/history НЕ трогаем — он требует СМС + PIN кошелька.
WALLET_JS = """async () => {
  const raw = localStorage.getItem('wbx__tokenData') || localStorage.getItem('wbid-oauth-sdk-access-token') || '';
  const m = raw.match(/eyJ[\\w-]+\\.[\\w-]+\\.[\\w-]+/);
  const h = {'x-requested-with': 'XMLHttpRequest', 'content-type': 'application/x-www-form-urlencoded; charset=UTF-8'};
  if (m) h.authorization = 'Bearer ' + m[0];
  const r = await fetch('/webapi/lk/mywallet/purchases/data', {method: 'POST', headers: h, credentials: 'include',
                                                               body: 'currency=RUB'});
  const text = await r.text();
  if (!r.ok) return {ok: false, status: r.status, text: text.slice(0, 200)};
  try { return {ok: true, data: JSON.parse(text)}; } catch (e) { return {ok: false, status: r.status, text: text.slice(0, 200)}; }
}"""  # → {ok, data} или {ok: false, status, text} — чтобы в логе было видно, почему не вышло
# код получения в ПВЗ: в API WB его нет — сайт считает в браузере (ежедневный, до 00:00 МСК); читаем текстом
# 6 цифр, между тройками бывает пробел/неразрывный; (?!\d) — не прихватить число со следующей строки («12 ноября»)
PICKUP_CODE_RE = re.compile(r"Код получения\s*(\d{3})[ \u00a0]?(\d{3})(?!\d)")


def parse_pickup_code(text: str) -> str | None:
    """«Код получения 112 396» → «112396»."""
    m = PICKUP_CODE_RE.search(text or "")
    return m.group(1) + m.group(2) if m else None


def _iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _purchase_items(d) -> list:
    return ((((d or {}).get("data") or {}).get("purchases") or {}).get("data") or {}).get("Items") or []


def parse_purchases(d) -> list[dict]:
    """astro purchases/list → полученные позиции. Цена = Price − PaymentSaleAmount (скидка Кошелька), копейки → ₽;
    Dt — дата статуса, для «получен» это «забрали»."""
    out = []
    for it in _purchase_items(d):
        if not it.get("Rid"):
            continue
        price, sale = it.get("Price"), it.get("PaymentSaleAmount") or 0
        out.append({"rid": it["Rid"], "nm_id": it.get("NmId"), "name": it.get("GoodsName"), "brand": it.get("BrandName"),
                    "price": _rub(price - sale) if isinstance(price, int) and isinstance(sale, int) else None,
                    "dt": _iso(it.get("Dt")), "office_id": it.get("DstOfficeId")})
    return out


def parse_vb_history(d) -> list[dict]:
    """bonuses/history → операции ВБ-баллов: operation 1 начисление / 2 списание, amount — баллы."""
    out = []
    for h in ((d or {}).get("data") or {}).get("history") or []:
        if h.get("tx_id"):
            out.append({"tx_id": str(h["tx_id"])[:128], "title": h.get("title"), "description": h.get("description"),
                        "dt": _iso(h.get("dt")), "operation": h.get("operation"), "amount": h.get("amount")})
    return out


def parse_balance_history(d) -> list[dict]:
    """mywallet/purchases/data → операции баланса. Сумма = moneyIncome + bonusIncome: у «Оплата»/«Сертификат» деньги
    приходят в bonusIncome (тоже рубли — та же часть, что bonus_balance в «Кошельке WB»). dt — мс."""
    out = []
    groups = ((((d or {}).get("value") or {}).get("balanceHistory") or {}).get("groups")) or []
    for it in (i for g in groups for i in g.get("items") or []):
        if not it.get("txId"):
            continue
        num = lambda k: it.get(k) if isinstance(it.get(k), (int, float)) else 0
        out.append({"tx_id": str(it["txId"])[:128], "name": it.get("operationName"), "op_type": it.get("operationType"),
                    "amount": num("moneyIncome") + num("bonusIncome"), "balance": it.get("currentBalance"),
                    "dt": _ts(it["dt"] / 1000) if isinstance(it.get("dt"), (int, float)) else None})
    return out


def parse_profile(d) -> dict | None:
    """personalinfo → {name, phone, purchase_pct}. Аноним (нет Bearer/сессии) → None."""
    v = (d or {}).get("value") or {}
    if not v.get("isAuthenticated") and not v.get("firstName"):
        return None
    return {"name": (v.get("firstName") or "").strip() or None,
            "phone": v.get("formattedPhoneMobile") or None,
            "purchase_pct": v.get("purchasePercent") if isinstance(v.get("purchasePercent"), int) else None}


def _order_price(r: dict) -> int | None:
    """Цена позиции к оплате, ₽ = total_price − discount (скидка Кошелька), как на сайте WB и в «К оплате».
    price уже со скидкой только у оплаченных; у неоплаченных price = total_price (полная), скидка — в discount.
    Нет total_price — старое поведение (price)."""
    total, disc = r.get("total_price"), r.get("discount") or 0
    if isinstance(total, int) and isinstance(disc, int):
        return _rub(total - disc)
    return _rub(r.get("price"))


def parse_orders(d: dict) -> list[dict]:
    """Позиции активных заказов: в заказе (data[]) несколько rid, ключ — uid."""
    out = []
    for order in d.get("data") or []:
        for r in order.get("rids") or []:
            if not r.get("uid") or r.get("pay_type") == 0:  # pay_type 0 — служебный «Пакет», не товар
                continue
            out.append({"rid": r["uid"], "nm_id": r.get("nm_id"), "name": r.get("name"),
                        "brand": r.get("brand"), "price": _order_price(r),
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


@asynccontextmanager
async def _client(state: dict, proxy: str | None):
    """HTTP под личностью аккаунта: call(method, url, **kw) → json; заголовки auth (Bearer) и with_vkey (+ кука
    wbx-validation-key — её хотят full-balance, bonuses, astro). Unauthorized на 401, ProxyDown — сеть через прокси."""
    token = bearer(state)
    if not token:  # не 401: сессия может быть жива (бизнес-цены идут по кукам) — просто нечем спросить
        raise NoBearer()
    auth = {**HEADERS, "Authorization": f"Bearer {token}"}
    vkey = next((c["value"] for c in state.get("cookies") or [] if c.get("name") == "wbx-validation-key"), None)
    with_vkey = {**auth, **({"Cookie": f"wbx-validation-key={vkey}"} if vkey else {})}
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
            return r.json() if r.content else None  # 2xx с пустым телом (у bonuses/history без истории)
        yield call, auth, with_vkey


async def fetch(state: dict, proxy: str | None = None) -> dict:
    """Баланс, лимит, % Кошелька, ВБ-баллы (+ их история) и активные заказы. Unauthorized на 401, ProxyDown — сеть
    через прокси не работает (таймаут, обрыв, 407), прочие сбои — исключением. proxy — прокси аккаунта."""
    async with _client(state, proxy) as (call, auth, with_vkey):
        out = parse_balance(await call("GET", BALANCE_URL, headers=with_vkey))
        try:  # баллы — не главное: их сбой не ломает опрос
            out["vb_points"] = parse_vb(await call("GET", VB_URL, headers=with_vkey))
        except Exception as e:
            out["vb_points"] = None
            log.warning("ВБ-баллы не получены: %s: %s", type(e).__name__, str(e)[:200])
        try:  # история баллов — тоже не главное; None = не трогать накопленное
            out["vb_ops"] = parse_vb_history(await call("GET", VB_HISTORY_URL, headers=with_vkey))
        except Exception as e:
            out["vb_ops"] = None
            log.warning("история ВБ-баллов не получена: %s: %s", type(e).__name__, str(e)[:200])
        out["wallet_pct"] = parse_grade(await call("GET", GRADE_URL, headers=auth))
        orders = parse_orders(await call("GET", ORDERS_URL, headers=auth))
        if orders:  # одним запросом на все rid аккаунта
            apply_statuses(orders, await call("POST", STATUSES_URL, headers=auth,
                                              json={"ids": [o["rid"] for o in orders]}))
        out["orders"] = orders
        return out


async def fetch_purchases(state: dict, proxy: str | None = None,
                          known: set[str] | None = None) -> tuple[list[dict], bool]:
    """История полученных заказов, новые сверху, по PURCHASES_PAGE. → (позиции, дошли ли до конца).
    known=None — полная загрузка до последней (неполной) страницы; known — догрузка: стоп на первой странице,
    где все rid уже известны."""
    out: list[dict] = []
    async with _client(state, proxy) as (call, _, with_vkey):
        for page in range(PURCHASES_MAX_PAGES):
            d = await call("GET", PURCHASES_URL.format(offset=page * PURCHASES_PAGE, limit=PURCHASES_PAGE),
                           headers=with_vkey)
            got = parse_purchases(d)
            out += got
            if len(_purchase_items(d)) < PURCHASES_PAGE:
                return out, True
            if known is not None and all(p["rid"] in known for p in got):
                return out, False
    return out, False


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
    # цена = total_price − discount (Аня, 2026-11-10): у неоплаченных price полная, скидка Кошелька — в discount
    o = parse_orders({"data": [{"rids": [
        {"uid": "rtx", "pay_state": 1, "total_price": 5538900, "price": 5538900, "discount": 332400},
        {"uid": "katana", "pay_state": 1, "total_price": 9546900, "price": 9546900, "discount": 572900},
        {"uid": "honor", "pay_state": 4, "total_price": 2760800, "price": 2595100, "discount": 165700},
        {"uid": "old", "price": 100000},  # без total_price — как раньше
        {"uid": "nodisc", "total_price": 50000, "price": 50000, "discount": None}]}]})
    assert [(x["rid"], x["price"]) for x in o] == [("rtx", 52065), ("katana", 89740), ("honor", 25951),
                                                   ("old", 1000), ("nodisc", 500)], o
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
            self.content = b"" if body is None else b"{}"

        def json(self):
            return self.body

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    def session(vb):
        bodies = {BALANCE_URL: R(200, {"available_funds_rub": 19788800}), VB_URL: vb, VB_HISTORY_URL: R(200, None),
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
            assert (out["funds"], out["vb_points"], out["orders"], out["vb_ops"]) == (197888, want, [], []), out
            assert (VB_URL, "wbx-validation-key=vk") in seen  # заголовки как у full-balance
    with patch.object(log, "warning") as warn:
        asyncio.run(demo())
    assert not any("истори" in str(c) for c in warn.call_args_list), warn.call_args_list  # пустая история — не сбой

    # история заказов (HAR боевого): цена = Price − PaymentSaleAmount — как в активных (A17: 17 094 − 1 026 = 16 068)
    item = lambda rid, **kw: {"Rid": rid, "NmId": 1656607451, "GoodsName": "Смартфон A17", "BrandName": "Samsung",
                              "Price": 1709400, "PaymentSaleAmount": 102600, "Dt": "2026-11-09T10:35:38Z",
                              "DstOfficeId": 50044805, "Status": 32, **kw}
    pur = parse_purchases({"data": {"purchases": {"data": {"Items": [item("eA5.a.0.0"), item(None), item(
        "eA5.b.0.0", PaymentSaleAmount=None)]}}}})
    assert [(x["rid"], x["price"]) for x in pur] == [("eA5.a.0.0", 16068), ("eA5.b.0.0", 17094)], pur
    assert pur[0]["dt"] == datetime(2026, 11, 9, 10, 35, 38, tzinfo=timezone.utc) and pur[0]["office_id"] == 50044805
    assert parse_purchases({}) == [] and parse_purchases(None) == []
    # история ВБ-баллов
    vb = parse_vb_history({"data": {"icons": {}, "history": [
        {"tx_id": "01a162d7", "description": "Вы выполнили задание", "title": "Баллы за задание",
         "dt": "2026-10-22T09:00:58Z", "operation": 1, "amount": 350}, {"title": "без tx_id"}]}})
    assert vb == [{"tx_id": "01a162d7", "title": "Баллы за задание", "description": "Вы выполнили задание",
                   "dt": datetime(2026, 10, 22, 9, 0, 58, tzinfo=timezone.utc), "operation": 1, "amount": 350}], vb
    # история баланса: «Оплата»/«Сертификат» — деньги в bonusIncome (moneyIncome 0)
    bal = parse_balance_history({"resultState": 0, "value": {"balanceHistory": {"totalItems": 3, "groups": [
        {"groupName": "09 ноября", "items": [
            {"txId": "ub_dd_47345296_15_1_-1_t_balance", "operationName": "Списано с карты", "operationType": 0,
             "moneyIncome": 99, "bonusIncome": 0, "currentBalance": 2390, "dt": 1794231734716},
            {"txId": "01a1bfe0", "operationName": "Оплата", "operationType": 1, "moneyIncome": 0, "bonusIncome": -52065,
             "currentBalance": 2291, "dt": 1794231348052}]},
        {"groupName": "07 ноября", "items": [{"operationName": "без txId"}]}]}}})
    assert [(x["tx_id"], x["amount"], x["op_type"]) for x in bal] == [
        ("ub_dd_47345296_15_1_-1_t_balance", 99, 0), ("01a1bfe0", -52065, 1)], bal
    assert bal[0]["dt"] == datetime(2026, 11, 9, 13, 42, 14, 716000, tzinfo=timezone.utc) and bal[0]["balance"] == 2390
    assert parse_balance_history(None) == []
    # код получения: строго 6 цифр, неразрывный пробел, без числа со следующей строки
    assert parse_pickup_code("Код получения\n112\u00a0396\n12 ноября") == "112396"
    assert parse_pickup_code("Код получения 112396") == "112396" and parse_pickup_code("Код получения 1123") is None
    assert parse_pickup_code("") is None and parse_pickup_code(None) is None

    async def pages():  # листаем по 100, пока страница полная; догрузка — до страницы без новых rid
        page = lambda k: {"data": {"purchases": {"data": {"Items": [item(f"r{k}_{i}") for i in range(PURCHASES_PAGE)]}}}}
        last = {"data": {"purchases": {"data": {"Items": [item("last")]}}}}
        urls = []

        class S:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def request(self, method, url, **kw):
                urls.append(url)
                off = int(url.split("offset=")[1].split("&")[0]) // PURCHASES_PAGE
                return R(200, page(off) if off < 2 else last)  # 2 полные страницы + неполная
        with patch(f"{__name__}.AsyncSession", S), patch(f"{__name__}.web_proxy", lambda: None):
            got, complete = await fetch_purchases(st)
            assert len(got) == 2 * PURCHASES_PAGE + 1 and complete and len(urls) == 3, urls
            assert "offset=100&status=32&limit=100" in urls[1]
            urls.clear()
            known = {p["rid"] for p in got if p["rid"].startswith("r0_")}  # первая страница уже есть
            got, complete = await fetch_purchases(st, known={"r0_1"})  # на 1-й странице есть новые — листаем дальше
            assert len(urls) == 3 and complete
            urls.clear()
            got, complete = await fetch_purchases(st, known=known)
            assert len(urls) == 1 and not complete and len(got) == PURCHASES_PAGE  # всё известно — стоп
    asyncio.run(pages())
    print("ok")
