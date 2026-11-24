"""Заказ товара на аккаунт WB без сайта и без корзины (SCOUT 2026-11-19 «🛒 Заказать товар»).

stick → POST wbxoofex api/v10/order?sticker= → опрос v3/status. Только Bearer аккаунта через его прокси, без антибота.
Тело — как у сайта (JS toJsonModel, HAR wb_buy.har/orders_pay.har): товар из detail с dest ПВЗ, весь grade (его
подписал WB), подпись каждой позиции ed25519 ключом аккаунта (pvKey), подпись ПВЗ из справочника all-poo-fr.
Корзину аккаунта не трогаем: в заказ идёт только то, что перечислено в items.
Заказ повторно НЕ шлём никогда. Единственное исключение — WB сам ответил «result 2 + другой стикер»: тогда
пересобираем тело с новым стикером и тем же uid (так делает сайт).
"""
import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.wb.account_api import (GRADE_URL, ORDERS_URL, PRE_SEND, STATUSES_URL, WWW, Unauthorized, _client,
                                apply_statuses, bearer, parse_grade, www_storage)
from app.wb.web_session import ProxyDown

log = logging.getLogger(__name__)

X = "https://wbxoofex.wildberries.ru"
STICK_URL = X + "/api/v2/stick"
ORDER_URL = X + "/api/v10/order?sticker={}"
STATUS_URL = X + "/api/v3/status?id={}&sticker={}"
REJECT_URL = X + "/api/v3/rids/client/reject"
PAID_URL = "https://user-grade.wildberries.ru/api/v1/paid-services"  # цена и подпись «платной» отмены
PAY_LATER, PAY_NOW = 20, 10  # payment.pay_mode: «при получении» / Кошельком сразу
PAY_TYPE = 12
ORDER_FLAGS = 4268067  # верхний flags тела — у сайта всегда такой (3 заказа в HAR)
# причины отмены (JS сайта, SCOUT 2026-11-24): «оформлен» — бесплатная 12 (11, если can_reject_delayed); уже
# собирают / везут / в ПВЗ — «платная» 13 с подписанной ценой из paid-services (у наших аккаунтов 0 ₽)
REJECT_FREE, REJECT_DELAYED, REJECT_PAID = 12, 11, 13
GRADE_TTL_MS = 300_000  # user_grade.expire: сейчас + 5 мин
STATUS_TRIES, STATUS_SEC = 30, 1  # v3/status: оплата Кошельком — 6 × «ждать» (~7 с), даём до ~30 с
RESAVES = 2  # сколько раз WB может попросить пересохранить с новым стикером
MSK = timezone(timedelta(hours=3))
# v3/status: 0 готово, 1 ждать, 2 заказа не видно; эти — заказ мог создаться, но не оплачен
STATUS_FAIL = {3: "оплата не прошла", 4: "WB просит подтвердить оплату картой (3DS)", 7: "WB просит верификацию"}
NO_ANSWER = "WB не ответил — проверьте заказы аккаунта"
NOT_PAID = "заказ создан, но оплату WB не подтвердил — проверьте заказы аккаунта"


def parse_nm(text) -> int | None:
    """Артикул: число или ссылка …/catalog/<nm>/detail.aspx. Иначе None."""
    s = str(text or "").strip()
    m = re.fullmatch(r"\d{4,12}", s) or re.search(r"/catalog/(\d{4,12})(?:/|$|\?)", s)
    return int(m[1] if m.groups() else m[0]) if m else None


def device_id(state: dict) -> str:
    """Как у сайта: localStorage wbx__sessionID («site_…»); нет — кука device_id."""
    return www_storage(state).get("wbx__sessionID") or next(
        (c.get("value") or "" for c in state.get("cookies") or [] if c.get("name") == "device_id"), "")


def _jwt(token: str) -> dict:
    p = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def payment_signer(state: dict):
    """→ f(rid, total_kop, now=None) → payment_sign позиции, как у сайта; None — у аккаунта нет pvKey.
    Подпись: ed25519("<user>:<rid>:<total за шт>:RUB") ключом pvKey из wbx__tokenData (libsodium: первые 32 байта —
    seed). Перед ней — base64 JSON {device_id, session_id, user_id, user_shard_id из JWT; signed_at МСК с мс}."""
    try:
        pv = json.loads(www_storage(state).get("wbx__tokenData") or "{}").get("pvKey")
    except ValueError:
        pv = None
    token = bearer(state)
    if not pv or not token:
        return None
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(pv)[:32])
    jd, dev = _jwt(token), device_id(state)

    def sign(rid: str, total: int, now: datetime | None = None) -> str:
        now = now or datetime.now(MSK)
        head = json.dumps({"device_id": dev, "session_id": jd.get("session_id"), "user_id": str(jd["user"]),
                           "user_shard_id": str(jd.get("shard_key") or "0"),
                           "signed_at": now.astimezone(MSK).strftime("%Y-%m-%dT%H:%M:%S.")
                           + f"{now.microsecond // 1000:03d}+03:00"}, separators=(",", ":"))
        sig = key.sign(f"{jd['user']}:{rid}:{total}:RUB".encode())
        return base64.b64encode(head.encode()).decode() + "." + base64.b64encode(sig).decode()
    return sign


def _stocks(size: dict) -> list[dict]:
    """Склады как у сайта (HAR): склад карточки первым, дальше по priority убыв."""
    return sorted((dict(s) for s in size.get("stocks") or [] if s.get("wh")),
                  key=lambda s: (s["wh"] != size.get("wh"), -(s.get("priority") or 0)))


def _positions(size, qty, uid, sticker, total, sign, now) -> list[dict]:
    """По позиции на штуку, со складов по очереди (остаток склада убывает); rid «<стикер>.<uid>.0.<n>»."""
    stocks = _stocks(size)
    store_ids = [s["wh"] for s in stocks]
    left = [s for s in stocks if (s.get("qty") or 0) > 0]
    if sum(s["qty"] for s in left) < qty:
        raise ValueError(f"на складах только {sum(s['qty'] for s in left)} шт")
    out = []
    for n in range(qty):
        s = left[0]
        rid = f"{sticker}.{uid}.0.{n}"
        eta = int(now.timestamp()) + 3600 * ((s.get("time1") or 0) + (s.get("time2") or 0))
        pos = {"rid": rid, "wh": s["wh"], "store_ids": store_ids, "return_fee": 0, "delivery_time": eta,
               "delivery_type": 6, "flags": 0, "first_ddate": eta,
               **({"payment_sign": sign(rid, total, now)} if sign else {}), "delivery_id": uid,
               **({"dtMax": s["dtMax"]} if s.get("dtMax") else {})}
        out.append(pos)
        s["qty"] -= 1
        if not s["qty"]:
            left.pop(0)
    return out


def prices(product: dict, wallet_pct: int) -> tuple[int, int]:
    """→ (total, final) за штуку в копейках: total = товар + логистика; final — со скидкой Кошелька, вниз до рубля
    (как у WB: 16 700 × 0,94 = 156,98 → 156 ₽)."""
    pr = (product["sizes"][0].get("price") or {})
    if not pr.get("product"):
        raise ValueError("у товара нет цены")
    total = pr["product"] + (pr.get("logistics") or 0)
    return total, total * (100 - wallet_pct) // 100 // 100 * 100


def _item(p: dict, size: dict, positions: list, total: int, final: int, now: datetime) -> dict:
    """Товар заказа — поля как у сайта, без аналитики (targetUrl, analytics, actualStimulusList, cashback_amount)."""
    pr = size.get("price") or {}
    stocks = _stocks(size)
    return {
        "chrt_id": size.get("optionId"), "nm_id": p["id"], "subject_id": p.get("subjectId"), "kind_id": p.get("kindId") or 0,
        "price": pr.get("basic"), "total_price": total, "brand": p.get("brand"), "name": p.get("name"),
        "size": size.get("origName") or "0", "positions": positions, "logistic_cost": pr.get("logistics") or 0,
        "basic_price": pr.get("basic"), "product_cost": total - (pr.get("logistics") or 0), "final_price": final,
        "flags": 0, "color": ((p.get("colors") or [{}])[0]).get("name") or "", "quantity": len(positions),
        "subjectParentId": p.get("subjectParentId"), "storeId": positions[0]["wh"], "timestamp": int(now.timestamp()),
        "dtype": size.get("dtype"), "payload": {"payload": size.get("payload")}, "reviewRating": p.get("reviewRating"),
        "feedbacks": p.get("feedbacks"), "isC2C": False, "viewFlags": p.get("viewFlags"), "isWeightProduct": False,
        "supplierId": p.get("supplierId"), "supplierName": p.get("supplier"), "volume": p.get("volume"),
        "return_cost": pr.get("return") or 0, "sale_conditions": size.get("saleConditions"),
        "seller_id": p.get("supplierId"), "comment": "",
        "minDeliveryHours": min((s.get("time1") or 0) + (s.get("time2") or 0) for s in stocks), "dist": size.get("dist"),
        "promoCodeDiscountAmount": 0, "isKgtResaleProduct": False, "picsUpdate": p.get("picsUpdate") or 0,
    }


def _delivery(pvz: dict, phone: str | None, name: str | None, uid: str) -> dict:
    """ПВЗ — всё из одной записи справочника (id, адрес, координаты, dest, подпись). Свободный текст не шлём."""
    if not pvz.get("sign") or not pvz.get("sign_v2"):
        raise ValueError(f"у ПВЗ {pvz.get('id')} нет подписи — WB не примет заказ")
    return {"dst_office_id": pvz["id"], "type_point": pvz["type_point"], "latitude": pvz["lat"],
            "longitude": pvz["lon"], "full_address": pvz["address"], "email": "",
            "phone": "".join(ch for ch in phone or "" if ch.isdigit()), "name": name or "", "tin": "0",
            "dest_id": int(pvz["dest"]), "sign": pvz["sign"], "sign_v2": pvz["sign_v2"], "locale": "ru",
            "phone_add": None, "items": {uid: {"delivery_type": 6, "cost": 0}}}


def build_order(*, product: dict, grade: dict, pvz: dict, phone: str | None, name: str | None, qty: int,
                pay_now: bool, uid: str, sticker: str, sign=None, now: datetime | None = None) -> tuple[dict, int, int]:
    """Тело api/v10/order: один товар (detail с dest этого ПВЗ, первый размер), qty штук. → (тело, total, final) за
    штуку в копейках. ValueError — тело не собрать (нет цены / остатка / подписи ПВЗ)."""
    if qty < 1:
        raise ValueError("количество должно быть больше нуля")
    now = now or datetime.now(MSK)
    size = (product.get("sizes") or [{}])[0]
    total, final = prices(product, parse_grade(grade) or 0)
    positions = _positions(size, qty, uid, sticker, total, sign, now)
    order = {"order_uid": uid, "lang": "ru", "locale": "ru", "app_type": 1,
             "items": [_item(product, size, positions, total, final, now)],
             "payment": {"pay_type": PAY_TYPE, "pay_mode": PAY_NOW if pay_now else PAY_LATER, "currency": "RUB",
                         "priority_order": None},
             "delivery": _delivery(pvz, phone, name, uid), "logistic_with": True, "return_fee_new": True,
             "pay_type": PAY_TYPE, "loyalty_card": None, "substitution_article_discounts": None, "promo_discounts": [],
             "promocode": None, "bypass_balance": True}
    wbpay = {"return_url": f"{WWW}/lk/orders/thankyou?orderId={uid}&ext=1&wbx=1",
             "token": {"card_id": "", "source_id": ""}, "additional": {"schedule_hash": "", "source": "", "product_type": 0}}
    # ponytail: сайт дописывает ещё wbClub, isPremium и т.п. — WB принял и без них (живые заказы 19.11)
    user_grade = {**grade["payload"], "sign": grade.get("sign"), "sign_version": grade.get("sign_version"),
                  "expire": int(now.timestamp() * 1000) + GRADE_TTL_MS}
    return {"order": order, "wbpay": wbpay, "user_grade": user_grade, "flags": ORDER_FLAGS}, total, final


def _order_headers(state: dict, with_vkey: dict) -> dict:
    cookies = {c.get("name"): c.get("value") for c in state.get("cookies") or []}
    return {**with_vkey, "Content-Type": "text/plain;charset=UTF-8", "Referer": WWW + "/lk/basket",
            **({"wbauid": cookies["_wbauid"]} if cookies.get("_wbauid") else {}), "deviceid": device_id(state)}


async def fetch_grade(state: dict, proxy: str | None) -> dict:
    """Ответ grade целиком — он идёт в тело заказа (подписан WB) и даёт % Кошелька и остаток постоплаты."""
    async with _client(state, proxy) as (call, auth, _):
        return await call("GET", GRADE_URL, headers=auth)


def _rids(body: dict) -> list[str]:
    return [p["rid"] for it in body["order"]["items"] for p in it["positions"]]


async def _send(call, headers, sticker, body):
    """POST заказа → ответ WB (dict) | ("refused", текст) | None — ответа нет, заказ мог создаться.
    ProxyDown до отправки и Unauthorized — наверх: заказа точно нет."""
    try:
        return await call("POST", ORDER_URL.format(sticker), headers=headers,
                          data=json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
    except ProxyDown as e:
        if PRE_SEND.search(str(e)):
            raise
        return None
    except Unauthorized:
        raise
    except Exception as e:
        r = getattr(e, "response", None)
        if isinstance(getattr(r, "status_code", None), int) and 400 <= r.status_code < 500:  # WB отказал
            return "refused", f"WB отказал: HTTP {r.status_code} {(getattr(r, 'text', '') or '')[:300]}".strip()
        return None


async def place_order(state: dict, proxy: str | None, uid: str, make_body, sleep=None) -> dict:
    """Заказ: make_body(стикер) → тело (uid в нём тот же). → {"status": ok | error | unknown, "error": текст, "rids"}.
    error — точный текст WB (поле error) или наш; unknown — заказ мог создаться, не повторять, проверить заказы.
    ProxyDown — до WB не дошли (заказа нет); Unauthorized/NoBearer — нужен вход; ValueError из make_body — тело не
    собрать (ничего не отправлено)."""
    sleep = sleep or asyncio.sleep
    async with _client(state, proxy) as (call, auth, with_vkey):
        sticker = (await call("GET", STICK_URL, headers=auth))["sticker"]
        headers = _order_headers(state, with_vkey)
        for _ in range(RESAVES + 1):
            body = make_body(sticker)
            r = await _send(call, headers, sticker, body)
            if isinstance(r, tuple):
                return {"status": "error", "error": r[1], "rids": _rids(body)}
            if isinstance(r, dict) and r.get("error") and r.get("result") != 0:  # отказ: точный текст WB
                return {"status": "error", "error": str(r["error"]), "rids": _rids(body)}
            if not isinstance(r, dict) or r.get("result") in (0, None):
                break  # принят, или ответ непонятный / его нет — дальше только спрашиваем, не шлём
            new = r.get("sticker")
            if r.get("result") == 2 and new and new != sticker:
                sticker = new  # WB просит пересохранить с новым стикером: тот же uid, новые rid и подписи
                continue
            return {"status": "error", "rids": _rids(body),
                    "error": r.get("error") or f"WB не принял заказ: {json.dumps(r, ensure_ascii=False)[:300]}"}
        else:
            return {"status": "error", "rids": _rids(body), "error": "WB слишком много раз попросил пересохранить заказ"}
        pay_now = body["order"]["payment"]["pay_mode"] == PAY_NOW
        return await _confirm(call, auth, uid, sticker, _rids(body), pay_now, sleep)


async def _confirm(call, auth, uid, sticker, rids, pay_now, sleep) -> dict:
    """Опрос v3/status до ~30 с; нет ясного ответа — ищем uid в списке заказов. Ничего не шлём повторно."""
    for i in range(STATUS_TRIES):
        if i:
            await sleep(STATUS_SEC)
        try:
            d = await call("GET", STATUS_URL.format(uid, sticker), headers=auth) or {}
        except Exception:  # не ответил — спросим ещё
            continue
        if d.get("result") == 0:
            return {"status": "ok", "error": None, "rids": rids}
        if d.get("result") in STATUS_FAIL:
            text = STATUS_FAIL[d["result"]] + (f": {d['error']}" if d.get("error") else "")
            return {"status": "error", "error": text, "rids": rids}
        # 1 — ждать; 2 «order not found» — заказа пока не видно (мог ещё не дойти) — спрашиваем дальше
    try:
        orders = (await call("GET", ORDERS_URL, headers=auth) or {}).get("data") or []
    except Exception:
        orders = []
    mine = next((o for o in orders if o.get("id") == uid), None)
    if not mine:
        return {"status": "unknown", "error": NO_ANSWER, "rids": rids}
    wb = mine.get("rids") or []
    if pay_now and not (wb and all(r.get("pay_state") == 4 for r in wb)):
        return {"status": "unknown", "error": NOT_PAID, "rids": [r.get("uid") for r in wb] or rids}
    return {"status": "ok", "error": None, "rids": [r.get("uid") for r in wb] or rids}


async def _reasons(call, auth, rids: list[str]) -> dict[str, int]:
    """Причина отмены каждого rid, как у сайта: свежие v2/orders + status-tracker. «Оформлен» (group 1, записи
    статуса ещё нет) — 12/11; со сроком хранения (в ПВЗ) или дальше «оформлен» — 13. Нет в заказах — только что
    оформлен и ещё не виден: 12, как раньше."""
    # ponytail: v2/orders — первые 100 заказов (как в опросе); у аккаунта больше — дальние rid уйдут как «не видны» (12)
    wb = {r.get("uid"): r for o in (await call("GET", ORDERS_URL, headers=auth) or {}).get("data") or []
          for r in o.get("rids") or []}
    ask = [{"rid": r} for r in rids if r in wb and not wb[r].get("expiry_dt")]
    if ask:
        apply_statuses(ask, await call("POST", STATUSES_URL, headers=auth, json={"ids": [o["rid"] for o in ask]}))
    fresh = {o["rid"] for o in ask if o["group_id"] in (1, None)}
    return {r: REJECT_PAID if r in wb and r not in fresh else
            REJECT_DELAYED if (wb.get(r) or {}).get("can_reject_delayed") else REJECT_FREE for r in rids}


def _paid_ug(d: dict) -> tuple[dict | None, str | None]:
    """paid-services → (paid_cancellation_ug для тела reject | None, почему 13 не шлём). Шлём только при цене 0 и
    подписи WB; поля — как пришли (подпись WB, своё не подставляем)."""
    pc = d.get("paid_cancellation") or {}
    rub = (pc.get("currencies") or {}).get("RUB")
    if isinstance(rub, int) and rub > 0:
        return None, f"отмена платная: {rub / 100:g} ₽ — отмените на сайте WB"
    if rub != 0 or not (pc.get("sign") and d.get("uuid") and d.get("expires_at")):
        return None, "WB не дал цену и подпись платной отмены — отмените на сайте WB"
    return {"currencies": pc["currencies"], "sign": pc["sign"], "expires_at": d["expires_at"], "uuid": d["uuid"]}, None


async def cancel_order(state: dict, proxy: str | None, rids: list[str]) -> tuple[list[str], str | None]:
    """Отмена позиций: «оформленные» — бесплатной (12/11), остальные — «платной» 13 (только при цене 0 ₽; деньги
    Кошелька возвращаются ≤ 30 с, лимит скидки — нет). Два вида — два запроса. → (rid'ы, отмену которых WB начал
    (reject_initialized — заявку приняли, итог по факту), пояснение для окна: ответ WB или почему 13 не слали)."""
    started, notes = [], []
    async with _client(state, proxy) as (call, auth, with_vkey):
        reasons = await _reasons(call, auth, rids)
        paid = [r for r in rids if reasons[r] == REJECT_PAID]
        parts = [([r for r in rids if reasons[r] != REJECT_PAID], {})]
        if paid:
            ug, why = _paid_ug(await call("GET", PAID_URL, headers=auth) or {})
            parts.append((paid, {"paid_cancellation_ug": ug}) if ug else ([], {}))
            notes += [why] if why else []
        for part, extra in parts:
            if not part:
                continue
            d = await call("POST", REJECT_URL, headers=_order_headers(state, with_vkey), data=json.dumps(
                {"rejects": [{"reason": reasons[r], "rid": r} for r in part], **extra})) or {}
            started += d.get("reject_initialized") or []
            # в лог всегда: какая причина ушла и что WB ответил
            log.info("отмена reason %s: начато %d из %d; не начато %s, details %s, error %s",
                     "/".join(sorted({str(reasons[r]) for r in part})), len(d.get("reject_initialized") or []), len(part),
                     d.get("reject_not_initialized"), str(d.get("reject_details"))[:300], d.get("error"))
            notes += [str(x)[:150] for x in (d.get("error"), d.get("reject_details")) if x]
    return started, "; ".join(notes)[:200] or None


if __name__ == "__main__":  # self-check: тело как у сайта (HAR Яны 14.11, Кошельком сразу), подпись, ответы WB
    from unittest.mock import patch

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from curl_cffi.requests.exceptions import HTTPError, RequestException

    from app.wb import account_api

    assert [parse_nm(x) for x in ("526937417", " 526937417 ", "https://www.wildberries.ru/catalog/526937417/detail.aspx",
                                  "wildberries.ru/catalog/526937417?size=1", "абв", "", None, "12", "52693741712345")] == \
        [526937417] * 4 + [None] * 5

    # товар: поля detail (HAR 11:54:10), цена/цвет/payload — как в корзине у сайта в момент заказа
    P = {"id": 526937417, "kindId": 0, "brand": "Мобимания", "colors": [{"name": "прозрачный"}], "subjectId": 390,
         "subjectParentId": 8995, "name": "Чехол на iPhone 17 Pro Max прозрачный силиконовый защита",
         "supplier": "Мобимания", "supplierId": 978936, "picsUpdate": 0, "reviewRating": 4.9, "feedbacks": 1025,
         "volume": 3, "viewFlags": 135028761, "sizes": [{
             "origName": "0", "optionId": 726650443, "wh": 8321084434, "dtype": 6614249635848, "dist": 872,
             "price": {"basic": 22500, "product": 16700, "logistics": 0, "return": 0}, "saleConditions": 2302672896,
             "payload": "PAYLOAD", "stocks": [
                 {"wh": 8248341716, "qty": 1, "priority": 50197, "time1": 48, "time2": 55},
                 {"wh": 8184288095, "qty": 1, "priority": 45736, "time1": 48, "time2": 55},
                 {"wh": 6858463757, "qty": 41, "priority": 48479, "time1": 3, "time2": 53},
                 {"wh": 8321084434, "qty": 41, "priority": 69356, "time1": 2, "time2": 35}]}]}
    G = {"state": 0, "sign_version": 6, "sign": "GSIGN", "payload": {"user_id": "1", "remaining_postpaid_limit": 15367300,
         "payments": [{"wctype_id": 5, "discount_value": 9}, {"wctype_id": 1, "discount_value": 6}]}}
    PVZ = {"id": 50017750, "address": "Москва, Ореховый Проезд 39а", "dest": -531264, "lat": 55.616392,
           "lon": 37.733716, "type_point": 8, "sign": "9yoinOGROqo=", "sign_v2": "79nDjen0sJQ="}
    UID, NOW = "d8d3f45881c4644e8a40c0f9f28babf77", datetime.fromtimestamp(1794657250.556, MSK)
    WH = [8321084434, 8248341716, 6858463757, 8184288095]
    SITE_ITEM = {  # HAR: тело сайта без аналитики и payment_sign; timestamp у сайта — время корзины, у нас — сейчас
        "chrt_id": 726650443, "nm_id": 526937417, "subject_id": 390, "kind_id": 0, "price": 22500, "total_price": 16700,
        "brand": "Мобимания", "name": "Чехол на iPhone 17 Pro Max прозрачный силиконовый защита", "size": "0",
        "positions": [{"rid": "ebA.d8d3f45881c4644e8a40c0f9f28babf77.0.0", "wh": 8321084434, "store_ids": WH,
                       "return_fee": 0, "delivery_time": 1794790450, "delivery_type": 6, "flags": 0,
                       "first_ddate": 1794790450, "delivery_id": UID}],
        "logistic_cost": 0, "basic_price": 22500, "product_cost": 16700, "final_price": 15600, "flags": 0,
        "color": "прозрачный", "quantity": 1, "subjectParentId": 8995, "storeId": 8321084434, "timestamp": 1794657250,
        "dtype": 6614249635848, "payload": {"payload": "PAYLOAD"}, "reviewRating": 4.9, "feedbacks": 1025,
        "isC2C": False, "viewFlags": 135028761, "isWeightProduct": False, "supplierId": 978936,
        "supplierName": "Мобимания", "volume": 3, "return_cost": 0, "sale_conditions": 2302672896, "seller_id": 978936,
        "comment": "", "minDeliveryHours": 37, "dist": 872, "promoCodeDiscountAmount": 0, "isKgtResaleProduct": False,
        "picsUpdate": 0}
    SITE_DELIVERY = {  # телефон/имя/email — выдуманные (у сайта там данные Яны; email мы не шлём)
        "dst_office_id": 50017750, "type_point": 8, "latitude": 55.616392, "longitude": 37.733716,
        "full_address": "Москва, Ореховый Проезд 39а", "email": "", "phone": "79990001122", "name": "Тест", "tin": "0",
        "dest_id": -531264, "sign": "9yoinOGROqo=", "sign_v2": "79nDjen0sJQ=", "locale": "ru", "phone_add": None,
        "items": {UID: {"delivery_type": 6, "cost": 0}}}
    kw = dict(product=P, grade=G, pvz=PVZ, phone="+7 999 000-11-22", name="Тест", uid=UID, sticker="ebA", now=NOW)
    body, total, final = build_order(**kw, qty=1, pay_now=True)
    o = body["order"]
    assert (total, final) == (16700, 15600) and o["items"] == [SITE_ITEM], o["items"]
    assert list(o["items"][0]) == list(SITE_ITEM) and list(o["delivery"]) == list(SITE_DELIVERY)  # порядок как у сайта
    assert o["delivery"] == SITE_DELIVERY, o["delivery"]
    assert {k: v for k, v in o.items() if k not in ("items", "delivery")} == {
        "order_uid": UID, "lang": "ru", "locale": "ru", "app_type": 1,
        "payment": {"pay_type": 12, "pay_mode": 10, "currency": "RUB", "priority_order": None}, "logistic_with": True,
        "return_fee_new": True, "pay_type": 12, "loyalty_card": None, "substitution_article_discounts": None,
        "promo_discounts": [], "promocode": None, "bypass_balance": True}
    assert body["wbpay"] == {"return_url": f"https://www.wildberries.ru/lk/orders/thankyou?orderId={UID}&ext=1&wbx=1",
                             "token": {"card_id": "", "source_id": ""},
                             "additional": {"schedule_hash": "", "source": "", "product_type": 0}}
    assert body["flags"] == 4268067 and list(body) == ["order", "wbpay", "user_grade", "flags"]
    assert body["user_grade"] == {**G["payload"], "sign": "GSIGN", "sign_version": 6, "expire": 1794657250556 + 300000}
    assert build_order(**kw, qty=1, pay_now=False)[0]["order"]["payment"]["pay_mode"] == 20
    # 3 шт: склад карточки (41) — все три; а если там 1 — дальше по priority, срок у позиции — свой
    assert [p["wh"] for p in build_order(**kw, qty=3, pay_now=False)[0]["order"]["items"][0]["positions"]] == [WH[0]] * 3
    P1 = json.loads(json.dumps(P))
    P1["sizes"][0]["stocks"][3]["qty"] = 1
    items = build_order(**{**kw, "product": P1}, qty=3, pay_now=False)[0]["order"]["items"][0]
    assert [(p["wh"], p["rid"], p["delivery_time"] - 1794657250) for p in items["positions"]] == [
        (WH[0], f"ebA.{UID}.0.0", 37 * 3600), (WH[1], f"ebA.{UID}.0.1", 103 * 3600), (WH[2], f"ebA.{UID}.0.2", 56 * 3600)]
    assert items["quantity"] == 3 and items["total_price"] == 16700 and items["storeId"] == WH[0]  # цены — за штуку
    for bad, msg in (({"qty": 85}, "только 84 шт"), ({"qty": 0}, "больше нуля"),
                     ({"qty": 1, "pvz": {**PVZ, "sign": None}}, "нет подписи")):
        try:
            build_order(**{**kw, "pay_now": False, **bad})
            raise AssertionError(bad)
        except ValueError as e:
            assert msg in str(e), e
    P2 = json.loads(json.dumps(P))
    P2["sizes"][0]["price"]["logistics"] = 1500  # логистика входит в total, product_cost — без неё
    it2 = build_order(**{**kw, "product": P2}, qty=1, pay_now=False)[0]["order"]["items"][0]
    assert (it2["total_price"], it2["product_cost"], it2["logistic_cost"], it2["final_price"]) == (18200, 16700, 1500, 17100)

    # подпись: тестовый ключ (настоящий pvKey — секрет аккаунта, в код не кладём)
    sk = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    pub = sk.public_key()
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    pv = (bytes(range(32)) + pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).hex()
    claims = base64.urlsafe_b64encode(json.dumps({"user": 4755886, "session_id": "sess", "shard_key": "5"}).encode())
    tok = "eyJhbGciOiJSUzI1NiJ9." + claims.decode().rstrip("=") + ".sig"
    st = {"cookies": [{"name": "device_id", "value": "cookie_dev"}, {"name": "_wbauid", "value": "777"}],
          "origins": [{"origin": WWW, "localStorage": [
              {"name": "wbx__tokenData", "value": json.dumps({"token": tok, "pvKey": pv})},
              {"name": "wbx__sessionID", "value": "site_dev"}]}]}
    assert device_id(st) == "site_dev" and device_id({**st, "origins": []}) == "cookie_dev" and device_id({}) == ""
    sig = payment_signer(st)("ebA.d1.0.0", 16700, NOW)
    head, s = sig.split(".")
    assert json.loads(base64.b64decode(head)) == {"device_id": "site_dev", "session_id": "sess", "user_id": "4755886",
                                                  "user_shard_id": "5", "signed_at": "2026-11-14T14:54:10.556+03:00"}
    assert list(json.loads(base64.b64decode(head))) == ["device_id", "session_id", "user_id", "user_shard_id", "signed_at"]
    Ed25519PublicKey.from_public_bytes(pub.public_bytes(Encoding.Raw, PublicFormat.Raw)).verify(
        base64.b64decode(s), b"4755886:ebA.d1.0.0:16700:RUB")  # бросит InvalidSignature, если строка не та
    assert payment_signer({"origins": []}) is None
    signed = build_order(**kw, qty=2, pay_now=False, sign=payment_signer(st))[0]["order"]["items"][0]["positions"]
    for p in signed:
        pub.verify(base64.b64decode(p["payment_sign"].split(".")[1]), f"4755886:{p['rid']}:16700:RUB".encode())
    assert list(signed[0]) == ["rid", "wh", "store_ids", "return_fee", "delivery_time", "delivery_type", "flags",
                               "first_ddate", "payment_sign", "delivery_id"]  # как у сайта

    # ответы WB (живые 19.11, Таня): v10/order и v3/status — result значит разное
    class R:
        def __init__(self, code, data):
            self.status_code, self._d = code, data
            self.content = b"" if data is None else json.dumps(data).encode()
            self.text = self.content.decode()

        def json(self):
            return self._d

        def raise_for_status(self):
            if self.status_code >= 400:
                raise HTTPError(f"HTTP {self.status_code}", response=self)

    def session(**script):
        """script: stick/order/status/orders/reject — ответы по очереди: dict | код | исключение."""
        sent = []

        class S:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def request(self, method, url, **kw):
                key = next(k for k, u in (("stick", STICK_URL), ("order", X + "/api/v10/order"),
                                          ("status", X + "/api/v3/status"), ("orders", ORDERS_URL), ("reject", REJECT_URL),
                                          ("statuses", STATUSES_URL), ("paid", PAID_URL))
                           if url.startswith(u))
                sent.append((key, url, kw))
                q = script.get(key) or []
                x = q.pop(0) if len(q) > 1 else (q[0] if q else {})  # последний ответ повторяется
                if isinstance(x, BaseException):
                    raise x
                return R(x, {"error": "bad"}) if isinstance(x, int) else R(200, x)
        return S, sent

    curl = lambda t: RequestException(f"Failed to perform, curl: {t}")
    naps = []

    async def nap(sec):
        naps.append(sec)

    async def order(pay_now=False, **script):
        S, sent = session(**script)
        stickers = []

        def make(stk):
            stickers.append(stk)
            return build_order(**{**kw, "sticker": stk}, qty=2, pay_now=pay_now, sign=payment_signer(st))[0]
        naps.clear()
        with patch.object(account_api, "AsyncSession", S), patch.object(account_api, "web_proxy", lambda: None), \
                patch.object(account_api, "RETRY_SEC", (0, 0)):
            res = await place_order(st, None, UID, make, sleep=nap)
        return res, [k for k, *_ in sent], sent, stickers

    async def main():
        rids = [f"ebA.{UID}.0.0", f"ebA.{UID}.0.1"]
        # принят → статус «ждать» ×2 → готово
        res, keys, sent, _ = await order(stick=[{"sticker": "ebA"}], order=[{"result": 0, "sticker": "ebA"}],
                                         status=[{"result": 1}, {"result": 1}, {"result": 0}])
        assert res == {"status": "ok", "error": None, "rids": rids} and keys == ["stick", "order"] + ["status"] * 3
        assert naps == [1, 1], naps
        _, url, k = sent[1]
        assert url.endswith("/api/v10/order?sticker=ebA") and json.loads(k["data"])["order"]["order_uid"] == UID
        h = k["headers"]
        assert (h["deviceid"], h["wbauid"], h["Content-Type"]) == ("site_dev", "777", "text/plain;charset=UTF-8"), h
        assert h["Authorization"] == f"Bearer {tok}" and sent[2][1] == STATUS_URL.format(UID, "ebA")
        # отказ: result 1 + error — это НЕ «ждать»: точный текст WB, статус не спрашиваем
        res, keys, *_ = await order(stick=[{"sticker": "eI"}], order=[{"result": 1, "error": "empty delivery sign",
                                                                       "sticker": "eI"}])
        assert res["status"] == "error" and res["error"] == "empty delivery sign" and keys == ["stick", "order"], res
        res, keys, *_ = await order(stick=[{"sticker": "eI"}], order=[{"result": 2, "error": "order not found"}])
        assert (res["error"], keys) == ("order not found", ["stick", "order"])
        for r in ({"result": 2, "sticker": "eJ", "error": "limit"}, {"error": "limit"}):  # error важнее нового стикера
            res, keys, *_ = await order(stick=[{"sticker": "eI"}], order=[r, {"result": 0}])
            assert (res["status"], res["error"], keys) == ("error", "limit", ["stick", "order"]), (r, res, keys)
        # result 2 + ДРУГОЙ стикер → пересобрать (тот же uid, новые rid и подписи); тот же стикер — отказ, не шлём
        res, keys, sent, stickers = await order(stick=[{"sticker": "eI"}], order=[{"result": 2, "sticker": "eJ"},
                                                                                  {"result": 0}], status=[{"result": 0}])
        b2 = json.loads(sent[2][2]["data"])
        assert res["status"] == "ok" and stickers == ["eI", "eJ"] and keys == ["stick", "order", "order", "status"]
        assert sent[2][1].endswith("sticker=eJ") and b2["order"]["order_uid"] == UID and sent[3][1].endswith("sticker=eJ")
        assert [p["rid"] for p in b2["order"]["items"][0]["positions"]] == res["rids"] == [f"eJ.{UID}.0.0", f"eJ.{UID}.0.1"]
        pub.verify(base64.b64decode(b2["order"]["items"][0]["positions"][0]["payment_sign"].split(".")[1]),
                   f"4755886:eJ.{UID}.0.0:16700:RUB".encode())
        res, keys, *_ = await order(stick=[{"sticker": "eI"}], order=[{"result": 2, "sticker": "eI"}])
        assert res["status"] == "error" and keys == ["stick", "order"] and "WB не принял заказ" in res["error"], res
        res, keys, *_ = await order(stick=[{"sticker": "e1"}], order=[{"result": 2, "sticker": f"e{i}"} for i in range(2, 9)])
        assert res["status"] == "error" and keys.count("order") == 3, keys  # пересохранений не больше двух
        res, keys, *_ = await order(stick=[{"sticker": "e1"}], order=[400])
        assert res["status"] == "error" and res["error"].startswith("WB отказал: HTTP 400") and keys == ["stick", "order"]
        # до отправки не подключились (все три захода) → ProxyDown, заказа нет
        try:
            await order(stick=[{"sticker": "e1"}], order=[curl("(28) Connection timed out after 5000 ms")] * 3)
            raise AssertionError("ProxyDown")
        except ProxyDown:
            pass
        try:
            await order(stick=[{"sticker": "e1"}], order=[401])
            raise AssertionError("401")
        except Unauthorized:
            pass
        # ответа нет после отправки: НЕ шлём повторно; статус до ~30 с, потом список заказов
        post = curl("(28) Operation timed out after 20001 milliseconds with 0 bytes received")
        res, keys, *_ = await order(stick=[{"sticker": "e1"}], order=[post], status=[{"result": 2, "error": "order not found"}],
                                    orders=[{"data": [{"id": "other", "rids": []}]}])
        assert res == {"status": "unknown", "error": NO_ANSWER, "rids": [f"e1.{UID}.0.0", f"e1.{UID}.0.1"]}, res
        assert keys == ["stick", "order"] + ["status"] * 30 + ["orders"] and len(naps) == 29, keys
        res, keys, *_ = await order(stick=[{"sticker": "e1"}], order=[post], status=[{"result": 2}, {"result": 1}, {"result": 0}])
        assert res["status"] == "ok" and keys.count("order") == 1 and keys.count("status") == 3  # дошёл — статус сказал
        res, keys, *_ = await order(stick=[{"sticker": "e1"}], order=[post], status=[curl("(52) Empty reply")],
                                    orders=[{"data": [{"id": UID, "rids": [{"uid": "w.1", "pay_state": 1},
                                                                           {"uid": "w.2", "pay_state": 1}]}]}])
        assert res == {"status": "ok", "error": None, "rids": ["w.1", "w.2"]} and keys.count("order") == 1, res
        res, *_ = await order(stick=[{"sticker": "e1"}], order=[post], status=[{"result": 1}], orders=[500])
        assert res["status"] == "unknown" and res["error"] == NO_ANSWER  # и список заказов не пришёл
        res, *_ = await order(stick=[{"sticker": "e1"}], order=[{}], status=[{"result": 1}], orders=[{"data": []}])
        assert res["status"] == "unknown"  # непонятный ответ на заказ — тоже «мог создаться»
        # Кошельком сразу: оплата не прошла (status 3) — текст WB; не дождались оплаты — unknown
        res, *_ = await order(True, stick=[{"sticker": "e1"}], order=[{"result": 0}],
                              status=[{"result": 1}, {"result": 3, "error": "insufficient funds"}])
        assert res["status"] == "error" and res["error"] == "оплата не прошла: insufficient funds" and res["rids"], res
        res, *_ = await order(True, stick=[{"sticker": "e1"}], order=[{"result": 0}], status=[{"result": 4}])
        assert res["error"] == "WB просит подтвердить оплату картой (3DS)"
        res, *_ = await order(True, stick=[{"sticker": "e1"}], order=[{"result": 0}], status=[{"result": 1}],
                              orders=[{"data": [{"id": UID, "rids": [{"uid": "w.1", "pay_state": 4},
                                                                     {"uid": "w.2", "pay_state": 1}]}]}])
        assert res == {"status": "unknown", "error": NOT_PAID, "rids": ["w.1", "w.2"]}, res
        res, *_ = await order(True, stick=[{"sticker": "e1"}], order=[{"result": 0}], status=[{"result": 1}],
                              orders=[{"data": [{"id": UID, "rids": [{"uid": "w.1", "pay_state": 4}]}]}])
        assert res["status"] == "ok"
        # тело не собрать — ничего не отправляем
        S, sent = session(stick=[{"sticker": "e1"}])
        with patch.object(account_api, "AsyncSession", S), patch.object(account_api, "web_proxy", lambda: None):
            try:
                await place_order(st, None, UID, lambda stk: build_order(**{**kw, "sticker": stk}, qty=99, pay_now=False))
                raise AssertionError("ValueError")
            except ValueError:
                assert [k for k, *_ in sent] == ["stick"]
            # отмена: «оформлен» (group 1 / нет записи / нет в заказах) — 12 (can_reject_delayed — 11), дальше — 13
            r = [f"r{i}" for i in range(6)]
            ORD = {"data": [{"rids": [{"uid": r[0]}, {"uid": r[1], "can_reject_delayed": True}, {"uid": r[2]},
                                      {"uid": r[3]}, {"uid": r[4], "expiry_dt": 1795000000}]}]}
            ST = [[{"rid": r[0], "group_id": "1", "date": 1}], [{"rid": r[1], "group_id": "1", "date": 1}],
                  [{"rid": r[2], "group_id": "1", "date": 1}, {"rid": r[2], "group_id": "2", "date": 2}]]  # r3 — записи нет
            PAID = {"uuid": "fb1a", "user_id": "47345296", "expires_at": 1794331115,
                    "paid_cancellation": {"currencies": {"RUB": 0, "KZT": 0}, "sign": "56a1"}}
            UG = {"currencies": {"RUB": 0, "KZT": 0}, "sign": "56a1", "expires_at": 1794331115, "uuid": "fb1a"}

            async def cancel(**script):
                S, sent = session(orders=[ORD], statuses=[ST], **script)
                with patch.object(account_api, "AsyncSession", S):
                    res = await cancel_order(st, None, r)
                return res, sent, [json.loads(k["data"]) for key, _, k in sent if key == "reject"]
            res, sent, bodies = await cancel(paid=[PAID], reject=[{"reject_initialized": r[:2]},
                                                                   {"reject_initialized": r[2:3], "error": "x"}])
            assert res == (r[:3], "x"), res
            assert bodies == [{"rejects": [{"reason": 12, "rid": r[0]}, {"reason": 11, "rid": r[1]},
                                           {"reason": 12, "rid": r[3]}, {"reason": 12, "rid": r[5]}]},
                              {"rejects": [{"reason": 13, "rid": r[2]}, {"reason": 13, "rid": r[4]}],
                               "paid_cancellation_ug": UG}], bodies
            assert [x[2]["json"] for x in sent if x[0] == "statuses"] == [{"ids": r[:4]}]  # в ПВЗ (срок) — без трекера
            assert sent[-1][2]["headers"]["deviceid"] == "site_dev" and "Bearer" in sent[-1][2]["headers"]["Authorization"]
            # «платная» не бесплатна или без подписи — 13 не шлём, бесплатные шлём, причина — в пояснении
            for p, why in (({**PAID, "paid_cancellation": {"currencies": {"RUB": 15000}, "sign": "s"}}, "отмена платная: 150 ₽"),
                           ({**PAID, "paid_cancellation": {"currencies": {"RUB": 0}}}, "WB не дал цену"),
                           ({**PAID, "paid_cancellation": {"currencies": {"KZT": 0}, "sign": "s"}}, "WB не дал цену"),
                           ({**PAID, "uuid": None}, "WB не дал цену"), ({}, "WB не дал цену")):
                res, sent, bodies = await cancel(paid=[p], reject=[{"reject_initialized": [r[0]], "reject_details": {"a": 1}}])
                assert res[0] == [r[0]] and res[1].startswith(why) and res[1].endswith("{'a': 1}"), (p, res)
                assert len(bodies) == 1 and {x["reason"] for x in bodies[0]["rejects"]} == {11, 12}, bodies
            # только «оформленные» — paid-services не спрашиваем
            S, sent = session(orders=[ORD], statuses=[ST], reject=[{"reject_initialized": [r[0]]}])
            with patch.object(account_api, "AsyncSession", S):
                assert await cancel_order(st, None, [r[0]]) == ([r[0]], None)
            assert [k for k, *_ in sent] == ["orders", "statuses", "reject"]

    asyncio.run(main())
    print("ok")
