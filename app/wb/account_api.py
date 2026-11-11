"""Состояние аккаунта WB обычным HTTP по Bearer сайта (без браузера и антибота).

Bearer — localStorage www: `wbx__tokenData` или `wbid-oauth-sdk-access-token` (у разных
сессий по-разному; токен SSO id.wb.ru `wbIdAccessToken` — не тот, не берём). full-balance ещё хочет куку
wbx-validation-key, остальным хватает Bearer. Суммы WB — в копейках, наружу отдаём рубли.
401 → Unauthorized: решение (рефреш / «войдите заново») — у вызывающего.
"""
import asyncio
import html
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

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
# rids[].pay_state: 4 оплачен; 1 и 3 — не оплачен (3 — Katana 17 в ПВЗ, WB пишет «Не оплачен», проба paystate_probe); прочее — не знаем
PAY_STATE = {4: True, 1: False, 3: False}
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")
HEADERS = {"Origin": "https://www.wildberries.ru", "Referer": "https://www.wildberries.ru/",
           "Accept": "application/json"}


class CertUnknown(Exception):
    """Запрос активации мог дойти до WB, а ответа нет: сертификат мог активироваться — повторять нельзя."""


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
    avail = ((((d.get("balance_sources") or {}).get("RUB") or {}).get("wallet") or {}).get("details") or {}).get("availability")
    return {"limit_left": _rub(lim - exp) if isinstance(lim, int) and isinstance(exp, int) else None,
            "monthly_limit": _rub(lim),
            "max_order": _rub(disc.get("max_order_price")), "max_rid": _rub(disc.get("max_rid_price")),
            # деньги в Кошельке WB = сумма в шапке сайта (wallet_balance_rub + bonus_balance — оба в рублях)
            "funds": _rub(d.get("available_funds_rub")),
            # блокировка кошелька: availability у заблокированного "arrested" (wallet_status при этом "active" — не он)
            "wallet_blocked": None if avail is None else avail != "available",
            "wallet_arrested": _rub((d.get("not_authorized_orders") or {}).get("arrests_orders"))}


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
                    "source_type": str(it["sourceType"])[:32] if it.get("sourceType") else None,  # giftcards — сертификат
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


# ---------- уведомления (колокольчик WB) ----------
BELL = "https://wbx-bell-v3.wildberries.ru/shard-proxy/api/v3/notice/"
NOTICE_UNREAD_URL = BELL + "unread?app_type=web&screen=main"  # счётчик, ничего не меняет
NOTICE_GET_URL = BELL + "get?app_type=web&screen=main"  # POST без тела: вся лента; ПОМЕЧАЕТ ВСЁ ПРОЧИТАННЫМ на WB
HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def _ns(v) -> datetime | None:
    """Наносекунды WB (числом или строкой) → datetime; 0/мусор → None."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(n / 1e9, timezone.utc) if n > 0 else None


def wb_url(u) -> str | None:
    """Ссылка — только https на wildberries.ru и его поддомены; прочее (чужой домен, javascript:) — None."""
    if not isinstance(u, str):
        return None
    p = urlparse(html.unescape(u.strip()))
    host = (p.hostname or "").lower()
    return p.geturl() if p.scheme == "https" and (host == "wildberries.ru" or host.endswith(".wildberries.ru")) else None


def _plain(s) -> str:
    """HTML WB → чистый текст (на сайт — только текстом)."""
    s = re.sub(r"<br\s*/?>", "\n", str(s or ""), flags=re.I)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"[ \t\u00a0]+", " ", s).strip()


def parse_notices(d) -> list[dict]:
    """notice/get → уведомления. url — из <a href> текста, иначе payload.apns_custom_data.data.url (только *.wildberries.ru).
    wb_read_at None — на WB не прочитано (до нашего get: get отдаёт состояние до пометки)."""
    out = []
    for it in ((d or {}).get("payload") if isinstance(d, dict) else None) or []:
        if not isinstance(it, dict) or not it.get("uuid") or str(it.get("is_deleted")).lower() == "true":
            continue
        try:
            extra = json.loads(it.get("payload") or "{}")
        except (TypeError, ValueError):
            extra = {}
        href = HREF.search(str(it.get("text") or ""))
        data = ((extra.get("apns_custom_data") or {}).get("data") or {}) if isinstance(extra, dict) else {}
        out.append({"uuid": str(it["uuid"])[:64], "topic_id": str(it["topic_id"])[:64] if it.get("topic_id") else None,
                    "title": _plain(it.get("title"))[:256] or None, "text": _plain(it.get("text")) or None,
                    "url": wb_url(href.group(1) if href else None) or wb_url(data.get("url") if isinstance(data, dict) else None),
                    "dt": _ns(it.get("dt")), "wb_read_at": _ns(it.get("read_date"))})
    return out


async def fetch_notices(state: dict, proxy: str | None = None, force: bool = False) -> tuple[int | None, list[dict] | None]:
    """→ (непрочитанных на WB, лента | None). Ленту берём, только если есть непрочитанные или force (раз в час / «Проверить
    сейчас»): get помечает всё прочитанным на WB. Ошибки — исключением (Unauthorized/ProxyDown, как у fetch)."""
    async with _client(state, proxy) as (call, auth, _):
        headers = {**auth, "Referer": "https://www.wildberries.ru/lk"}
        d = await call("GET", NOTICE_UNREAD_URL, headers=headers)
        n = ((d or {}).get("payload") or {}).get("count") if isinstance(d, dict) else None
        unread = n if isinstance(n, int) else None
        if not force and not unread:
            return unread, None
        return unread, parse_notices(await call("POST", NOTICE_GET_URL, headers=headers))


# ---------- подарочные сертификаты ----------
CERT_URL = "https://astro.wildberries.ru/api/v1/giftcards/certificates/activation"
CERT_RE = re.compile(r"[A-Z0-9]{16}")
CERT_BUSY = 5  # errorNumber «От вас слишком много запросов. Попробуйте через 2 сек.»
CERT_RETRY_SEC, CERT_RETRIES = 3, 3
# сбой curl ДО отправки запроса (до WB не дошли — можно считать ошибкой); всё прочее (Operation timed out, Empty
# reply, Recv failure…) — после: запрос мог дойти. Голое «(28)» не годится — бывает обоих видов
PRE_SEND = re.compile(r"\(28\) Connection timed out|\(56\) CONNECT tunnel failed|\(35\) TLS connect error|"
                      r"\(7\) |\b407\b")


def norm_cert(s) -> str | None:
    """Код сертификата → 16 символов [A-Z0-9] (как храним и показываем); с дефисами, пробелами, префиксом wbcert-,
    в любом регистре. Не похоже на код — None."""
    s = re.sub(r"[\s\-\u2010-\u2015]", "", str(s or "")).upper()
    if len(s) == 22 and s.startswith("WBCERT"):
        s = s[6:]
    return s if CERT_RE.fullmatch(s) else None


def cert_key(code: str) -> str:
    """Как ждёт WB: wbcert-XXXX-XXXX-XXXX-XXXX (без дефисов — HTTP 400)."""
    return "wbcert-" + "-".join(code[i:i + 4] for i in range(0, 16, 4))


def parse_cert(d) -> dict:
    """Ответ активации → {"ok": True, "id", "nominal" ₽} | {"ok": False, "error_number", "error_text"}.
    Непонятный ответ — CertUnknown (мог активироваться)."""
    x = ((((d or {}).get("data") or {}).get("certificatesActivation") or {}).get("data") or {}) if isinstance(d, dict) else {}
    if x.get("id") and x.get("status") == "processed":
        return {"ok": True, "id": str(x["id"])[:128], "nominal": _rub(x.get("nominal"))}
    if isinstance(x.get("errorNumber"), int):
        return {"ok": False, "error_number": x["errorNumber"], "error_text": str(x.get("errorText") or "")[:256]}
    raise CertUnknown(f"непонятный ответ WB: {str(d)[:200]}")


async def activate_cert(state: dict, proxy: str | None, code: str, sleep=None) -> dict:
    """Активация на аккаунт (зачисление в баланс). → parse_cert; на «слишком много запросов» ждём и повторяем.
    ProxyDown — до WB не дошли (ошибка, повторять можно); CertUnknown — запрос мог дойти, ответа нет;
    Unauthorized/NoBearer — нужен вход."""
    sleep = sleep or asyncio.sleep
    body = json.dumps({"certificatesActivationRequest": {"activationKey": cert_key(code)}})
    async with _client(state, proxy) as (call, _, with_vkey):
        headers = {**with_vkey, "Content-Type": "text/plain;charset=UTF-8",
                   "Referer": "https://www.wildberries.ru/gift/certificates/activate"}
        for attempt in range(CERT_RETRIES + 1):
            try:
                res = parse_cert(await call("POST", CERT_URL, headers=headers, data=body))
            except ProxyDown as e:
                if PRE_SEND.search(str(e)):
                    raise
                raise CertUnknown(str(e)) from e
            except (Unauthorized, CertUnknown):
                raise
            except Exception as e:
                code_ = getattr(getattr(e, "response", None), "status_code", None)
                if isinstance(code_, int) and 400 <= code_ < 500:  # WB отклонил запрос — не активирован
                    return {"ok": False, "error_number": None, "error_text": f"WB отклонил запрос: HTTP {code_}"}
                raise CertUnknown(f"{type(e).__name__}: {str(e)[:200]}") from e
            if res["ok"] or res["error_number"] != CERT_BUSY or attempt == CERT_RETRIES:
                return res
            await sleep(CERT_RETRY_SEC)
    return res


PAY_URL = "https://wbxoofex.wildberries.ru/api/v3/orders_pay"
# ponytail: у всех наших позиций pay_type 12 (HAR + проба 2026-11-11); если появится другой — хранить у позиции
PAY_TYPE = 12


class PayUnknown(Exception):
    """Запрос оплаты мог дойти до WB, а ответа нет — оплата могла пройти."""


def pay_body(rids: list[str]) -> str:
    return json.dumps({"rids": [{"rid": r, "flags": 1} for r in rids],
                       "return_url": WWW + "/lk/myorders/delivery?paymentSuccess=true",
                       "pay_type": PAY_TYPE, "bypass_balance": True, "app_type": 1})


async def pay_orders(state: dict, proxy: str | None, rids: list[str]) -> str | None:
    """Оплата позиций с WB Кошелька (без СМС/PIN). → None — WB принял ({}); текст — WB отказал.
    ProxyDown — до WB не дошли; PayUnknown — мог дойти, ответа нет; Unauthorized/NoBearer — нужен вход."""
    cookies = {c.get("name"): c.get("value") for c in state.get("cookies") or []}
    async with _client(state, proxy) as (call, auth, _):
        headers = {**auth, "Content-Type": "text/plain;charset=UTF-8", "Referer": WWW + "/lk/myorders/delivery",
                   **{h: cookies[c] for h, c in (("wbauid", "_wbauid"), ("deviceid", "device_id")) if cookies.get(c)}}
        try:
            d = await call("POST", PAY_URL, headers=headers, data=pay_body(rids))
        except ProxyDown as e:
            if PRE_SEND.search(str(e)):
                raise
            raise PayUnknown(str(e)) from e
        except Unauthorized:
            raise
        except Exception as e:
            r = getattr(e, "response", None)
            if isinstance(getattr(r, "status_code", None), int) and 400 <= r.status_code < 500:  # WB отказал
                return f"WB отказал: HTTP {r.status_code} {(getattr(r, 'text', '') or '')[:200]}".strip()
            raise PayUnknown(f"{type(e).__name__}: {str(e)[:200]}") from e
    return None if d == {} else f"WB ответил: {json.dumps(d, ensure_ascii=False)[:200]}"


async def fetch_balance(state: dict, proxy: str | None = None) -> dict:
    """Только full-balance (после активации — свежий funds)."""
    async with _client(state, proxy) as (call, _, with_vkey):
        return parse_balance(await call("GET", BALANCE_URL, headers=with_vkey))


if __name__ == "__main__":  # self-check на формах ответов из HAR (суммы в копейках)
    b = parse_balance({"wallet_balance_rub": 428500, "bonus_balance": 20100, "available_funds_rub": 448600, "discount": {
        "expenses": 23355900, "monthly_limit": 100000000, "max_order_price": 100000000, "max_rid_price": 50000000}})
    assert b == {"limit_left": 766441, "monthly_limit": 1000000, "max_order": 1000000, "max_rid": 500000,
                 "funds": 4486, "wallet_blocked": None, "wallet_arrested": None}, b
    # блокировка кошелька (HAR block_wallet — Павел; certificate — Дмитрий)
    w = lambda a: {"balance_sources": {"RUB": {"wallet": {"details": {"availability": a, "internal_available": 0}}}},
                   "wallet_status": "active", "not_authorized_orders": {"arrests_orders": 13517451, "income_orders": 0}}
    assert (parse_balance(w("arrested"))["wallet_blocked"], parse_balance(w("arrested"))["wallet_arrested"]) == (True, 135174)
    assert parse_balance(w("available"))["wallet_blocked"] is False
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
        {"uid": "katana", "pay_state": 3, "total_price": 9546900, "price": 9546900, "discount": 572900},  # 3 — «Не оплачен»
        {"uid": "honor", "pay_state": 4, "total_price": 2760800, "price": 2595100, "discount": 165700},
        {"uid": "old", "price": 100000},  # без total_price — как раньше
        {"uid": "nodisc", "total_price": 50000, "price": 50000, "discount": None}]}]})
    assert [(x["rid"], x["price"]) for x in o] == [("rtx", 52065), ("katana", 89740), ("honor", 25951),
                                                   ("old", 1000), ("nodisc", 500)], o
    assert [x["paid"] for x in o] == [False, False, True, None, None], o  # pay_state 3 — не оплачен, нет кода — не знаем
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
    assert [x["source_type"] for x in bal] == [None, None]
    gift = parse_balance_history({"value": {"balanceHistory": {"groups": [{"items": [  # HAR certificate.har
        {"operationName": "Сертификат Wildberries", "sourceType": "giftcards", "txId": "d32a.101126.0", "bonusIncome": 500,
         "moneyIncome": 0, "operationType": 0, "dt": 1794231734716}]}]}}})
    assert (gift[0]["source_type"], gift[0]["amount"]) == ("giftcards", 500), gift
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

    # сертификаты: код в любом виде → 16 символов; в WB — с дефисами
    for raw in ("UYPMJUAFGZEVR89S", "uypm-juaf-gzev-r89s", "wbcert-UYPM-JUAF-GZEV-R89S", " WBCERT-uypmjuafgzevr89s ",
                "UYPM JUAF GZEV R89S", "UYPM\u2013JUAF\u2013GZEV\u2013R89S"):
        assert norm_cert(raw) == "UYPMJUAFGZEVR89S", raw
    for bad in ("", None, "UYPMJUAFGZEVR89", "UYPMJUAFGZEVR89SS", "UYPM_JUAF_GZEV_R89S", "ЮYPMJUAFGZEVR89S", "wbcert-UYPM"):
        assert norm_cert(bad) is None, bad
    assert norm_cert("WBCERTAAAAAAAAAA") == "WBCERTAAAAAAAAAA"  # код, который сам начинается с WBCERT, — не режем
    assert cert_key("UYPMJUAFGZEVR89S") == "wbcert-UYPM-JUAF-GZEV-R89S"
    ok = {"data": {"certificatesActivation": {"data": {"id": "d32a.101126.0", "status": "processed", "nominal": 50000,
                                                        "currency": "RUB"}}}}
    busy = {"data": {"certificatesActivation": {"data": {"errorText": "От вас слишком много запросов.", "errorNumber": 5}}}}
    nf = {"data": {"certificatesActivation": {"data": {"errorText": "Ключ активации не найден.", "errorNumber": 21}}}}
    assert parse_cert(ok) == {"ok": True, "id": "d32a.101126.0", "nominal": 500}
    assert parse_cert(nf) == {"ok": False, "error_number": 21, "error_text": "Ключ активации не найден."}
    for weird in ({}, None, {"data": {"certificatesActivation": {"data": {"id": "x", "status": "pending"}}}}):
        try:
            parse_cert(weird)
            raise AssertionError(weird)
        except CertUnknown:
            pass

    class HTTP400(Exception):
        response = R(400, None)

    def cert_session(script):
        """script — ответы по очереди: dict (200 с телом) | исключение (бросит request)."""
        sent = []

        class S:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def request(self, method, url, **kw):
                sent.append((method, url, kw))
                x = script.pop(0)
                if isinstance(x, BaseException):
                    raise x
                if x == 400:
                    r = R(400, None)
                    r.raise_for_status = lambda: (_ for _ in ()).throw(HTTP400())
                    return r
                return R(200, x)
        return S, sent

    curl = lambda t: RequestException(f"Failed to perform, curl: {t}. See https://curl.se/libcurl/c/libcurl-errors.html")
    pre = ("(28) Connection timed out after 20002 milliseconds", "(56) CONNECT tunnel failed, response 502",
           "(35) TLS connect error: error:00000000:invalid library (0):OPENSSL_internal:invalid library (0)",
           "(7) Failed to connect to 51.194.246.76 port 44347 after 3 ms: Couldn't connect to server")
    post = ("(28) Operation timed out after 20001 milliseconds with 0 bytes received", "(52) Empty reply from server",
            "(56) Recv failure: Connection reset by peer", "(92) HTTP/2 stream 1 was not closed cleanly")
    async def certs():
        naps = []

        async def nap(sec):
            naps.append(sec)

        async def run(script):
            S, sent = cert_session(script)
            with patch(f"{__name__}.AsyncSession", S), patch(f"{__name__}.web_proxy", lambda: None):
                return await activate_cert({**st, "cookies": [{"name": "wbx-validation-key", "value": "vk"}]}, None,
                                           "UYPMJUAFGZEVR89S", sleep=nap), sent
        res, sent = await run([ok])
        assert res["ok"] and res["nominal"] == 500 and len(sent) == 1
        m, url, kw = sent[0]
        assert (m, url) == ("POST", CERT_URL) and kw["data"] == \
            '{"certificatesActivationRequest": {"activationKey": "wbcert-UYPM-JUAF-GZEV-R89S"}}', kw
        assert kw["headers"]["Content-Type"] == "text/plain;charset=UTF-8" and "wbx-validation-key=vk" in kw["headers"]["Cookie"]
        assert (await run([nf]))[0]["error_number"] == 21 and not naps
        res, sent = await run([busy, busy, ok])  # «слишком много запросов» → пауза 3 с и повтор
        assert res["ok"] and len(sent) == 3 and naps == [3, 3], naps
        naps.clear()
        res, sent = await run([busy] * 4)  # повторов не больше трёх
        assert res["error_number"] == 5 and len(sent) == 4 and naps == [3, 3, 3]
        assert (await run([400]))[0] == {"ok": False, "error_number": None, "error_text": "WB отклонил запрос: HTTP 400"}
        for t in pre:
            try:
                await run([curl(t)])
                raise AssertionError(t)
            except ProxyDown:
                pass
        for t in post:
            try:
                await run([curl(t)])
                raise AssertionError(t)
            except CertUnknown:
                pass
        S, _ = cert_session([])

        class S407(S):
            async def request(self, method, url, **kw):
                return R(407, None)
        with patch(f"{__name__}.AsyncSession", S407), patch(f"{__name__}.web_proxy", lambda: None):
            try:
                await activate_cert(st, None, "UYPMJUAFGZEVR89S", sleep=nap)
                raise AssertionError("407")
            except ProxyDown:
                pass
        for weird in ([{}], [RuntimeError("json")]):  # 200 с непонятным телом, сбой разбора — мог активироваться
            try:
                await run(weird)
                raise AssertionError(weird)
            except CertUnknown:
                pass
    asyncio.run(certs())

    # оплата (HAR pay_order.har): тело, заголовки, {} → принято, прочее → текст ошибки, ProxyDown до/после отправки
    assert json.loads(pay_body(["r1", "r2"])) == {
        "rids": [{"rid": "r1", "flags": 1}, {"rid": "r2", "flags": 1}],
        "return_url": "https://www.wildberries.ru/lk/myorders/delivery?paymentSuccess=true",
        "pay_type": 12, "bypass_balance": True, "app_type": 1}

    async def pays():
        async def run(script, cookies=()):
            S, sent = cert_session(script)
            with patch(f"{__name__}.AsyncSession", S), patch(f"{__name__}.web_proxy", lambda: None):
                return await pay_orders({**st, "cookies": list(cookies)}, None, ["r1"]), sent
        res, sent = await run([{}], [{"name": "_wbauid", "value": "123"}, {"name": "device_id", "value": "site_x"}])
        m, url, kw = sent[0]
        assert res is None and (m, url) == ("POST", PAY_URL) and json.loads(kw["data"])["rids"] == [{"rid": "r1", "flags": 1}]
        h = kw["headers"]
        assert (h["wbauid"], h["deviceid"], h["Content-Type"]) == ("123", "site_x", "text/plain;charset=UTF-8"), h
        assert h["Authorization"].startswith("Bearer ") and h["Referer"].endswith("/lk/myorders/delivery")
        assert "wbauid" not in (await run([{}]))[1][0][2]["headers"]  # кук нет — заголовков нет
        assert (await run([{"error": "no money"}]))[0] == 'WB ответил: {"error": "no money"}'
        assert (await run([400]))[0].startswith("WB отказал: HTTP 400")
        for t in pre:
            try:
                await run([curl(t)])
                raise AssertionError(t)
            except ProxyDown:
                pass
        for x in [curl(t) for t in post] + [RuntimeError("json")]:
            try:
                await run([x])
                raise AssertionError(x)
            except PayUnknown:
                pass
    asyncio.run(pays())

    # уведомления (HAR notification.har): числа строкой, HTML в тексте, url из href или payload, только *.wildberries.ru
    pay = lambda url: json.dumps({"push_text": "x", "apns_custom_data": {"data": {"url": url, "urlType": "web"}}})
    ns = parse_notices({"result": 0, "payload": [
        {"uuid": "cc31", "title": "Вход в ваш профиль", "topic_id": "new_device_login", "dt": "1793722322000000000",
         "read_date": "1793746957000000000", "is_deleted": "False",
         "text": "В профиль на WB ID зашли: 51.194.246.76. Если это были не вы, смените пароль", "payload": '{"client_ids":["wb"]}'},
        {"uuid": "d1", "title": "Ваш заказ ждёт вас", "topic_id": "cdp-info-notice-24h", "dt": 1793722322000000000, "read_date": 0,
         "text": 'Заберите <a href="https://www.wildberries.ru/lk/myorders/delivery?a=1&amp;b=2">заказ</a>&nbsp;до&nbsp;12.11<br>Спасибо',
         "payload": pay("https://evil.example/x")},
        {"uuid": "p1", "title": "Сертификат", "dt": "1793722322000000000", "read_date": "0", "text": "Получатель активировал",
         "payload": pay("https://wildberries.ru/gift/certificates")},
        {"uuid": "e1", "title": "чужая ссылка", "text": '<a href="https://wildberries.ru.evil.com/">x</a>', "payload": pay("javascript:alert(1)")},
        {"uuid": "bad-json", "title": "t", "text": "t", "payload": "{не json"},
        {"uuid": "del", "title": "удалено", "is_deleted": True}, {"title": "без uuid"}]})
    assert [x["uuid"] for x in ns] == ["cc31", "d1", "p1", "e1", "bad-json"], ns
    assert ns[0]["wb_read_at"] == datetime(2026, 11, 3, 23, 2, 37, tzinfo=timezone.utc) and ns[0]["url"] is None
    assert "51.194.246.76" in ns[0]["text"] and ns[0]["topic_id"] == "new_device_login"
    assert ns[1]["text"] == "Заберите заказ до 12.11\nСпасибо" and ns[1]["wb_read_at"] is None, ns[1]
    assert ns[1]["url"] == "https://www.wildberries.ru/lk/myorders/delivery?a=1&b=2", ns[1]["url"]  # href главнее payload
    assert (ns[2]["url"], ns[2]["wb_read_at"], ns[2]["topic_id"]) == ("https://wildberries.ru/gift/certificates", None, None)
    assert ns[3]["url"] is None and ns[4]["url"] is None and ns[1]["dt"] == ns[0]["dt"]  # число и строка — одно и то же
    assert parse_notices(None) == [] and parse_notices({"payload": None}) == []
    assert wb_url("http://www.wildberries.ru/") is None and wb_url("https://WWW.Wildberries.RU/lk") == "https://WWW.Wildberries.RU/lk"

    async def bell():
        calls = []

        class S:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def request(self, method, url, **kw):
                calls.append((method, url, kw["headers"].get("Referer")))
                return R(200, {"result": 0, "payload": {"count": unread}} if "unread" in url else
                         {"result": 0, "payload": [{"uuid": "u1", "title": "t", "text": "x", "dt": 1, "read_date": 0}]})
        with patch(f"{__name__}.AsyncSession", S), patch(f"{__name__}.web_proxy", lambda: None):
            unread = 0
            assert await fetch_notices(st) == (0, None) and len(calls) == 1  # непрочитанных нет — get не зовём
            n, items = await fetch_notices(st, force=True)
            assert n == 0 and [i["uuid"] for i in items] == ["u1"] and calls[-1][:2] == ("POST", NOTICE_GET_URL)
            unread = 2
            calls.clear()
            assert (await fetch_notices(st))[0] == 2 and [c[0] for c in calls] == ["GET", "POST"]
            assert calls[0][2] == "https://www.wildberries.ru/lk"
    asyncio.run(bell())
    print("ok")
