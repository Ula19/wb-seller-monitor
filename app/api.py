"""API сайта (свой контейнер: `uvicorn app.api:app`). Ходит ТОЛЬКО в Postgres.

В WB ходит один worker (процесс бота): вход в аккаунт — заявкой в wb_login_requests,
имя и первая загрузка продавца — на ближайшем цикле мониторинга.
Вход на сайт — Telegram Login Widget (JS-колбэк → POST /api/auth/telegram) или
запасной пароль владельца. Сессия — подписанная cookie, пользователь сверяется
с белым списком users на каждом запросе (удалили — доступ закрыт сразу).
"""
import asyncio
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.crypto import encrypt_json
from app.db import models, repo
from app.db.base import Session
from app.wallet import best_offer, offer_text, wallet_price
from app.wb import media
from app.wb.proxy_check import is_parser_proxy, parse_proxy

COOKIE = "wbm_session"
SESSION_TTL = 30 * 86400
AUTH_MAX_AGE = 86400  # подпись виджета старше суток не принимаем
MIN_PASSWORD = 20  # запасной вход торчит в интернет: короткий пароль = вход выключен
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"

app = FastAPI(title="WB Монитор", docs_url=None, redoc_url=None, openapi_url=None)


# ---------- сессия и вход ----------
def _secret() -> bytes:
    if len(settings.session_secret) < 32:
        raise RuntimeError("SESSION_SECRET не задан (нужно ≥32 символов)")
    return settings.session_secret.encode()


def make_session(uid: int, now: float | None = None) -> str:
    body = f"{uid}.{int(now or time.time()) + SESSION_TTL}"
    return f"{body}.{hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()}"


def read_session(value: str | None, now: float | None = None) -> int | None:
    try:
        uid, exp, sig = (value or "").split(".")
        good = hmac.new(_secret(), f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, good) and int(exp) > (now or time.time()):
            return int(uid)
    except ValueError:
        pass
    return None


def check_telegram(data: dict, bot_token: str, now: float | None = None) -> int | None:
    """Подпись Telegram Login Widget: HMAC-SHA256(sha256(BOT_TOKEN), data_check_string) == hash."""
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    good = hmac.new(hashlib.sha256(bot_token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    try:
        fresh = (now or time.time()) - int(fields.get("auth_date", 0)) <= AUTH_MAX_AGE
        return int(fields["id"]) if fresh and hmac.compare_digest(str(data.get("hash", "")), good) else None
    except (KeyError, ValueError):
        return None


def _login(resp: Response, uid: int) -> dict:
    # Lax, не Strict: ссылки из пушей бота — переход с чужого сайта, Strict-cookie там не придёт
    resp.set_cookie(COOKIE, make_session(uid), max_age=SESSION_TTL, httponly=True,
                    secure=bool(settings.site_domain), samesite="lax")
    return {"id": uid}


async def db():
    async with Session() as s:
        yield s


@dataclass
class Viewer:
    id: int  # кто вошёл
    telegram_id: int  # чьи данные: владелец пространства (первый из SUPERADMIN_IDS)


async def user(request: Request) -> Viewer:
    """Одно общее пространство: любой из SUPERADMIN_IDS работает с данными владельца.
    Список читается на каждом запросе — убрали ID из .env и перезапустили api, доступ закрыт."""
    # ponytail: одно пространство, команды — при продаже доступа
    uid = read_session(request.cookies.get(COOKIE))
    if uid is None or uid not in settings.superadmins:
        raise HTTPException(401, "нужен вход")
    return Viewer(id=uid, telegram_id=settings.owner_id)


class TelegramAuth(BaseModel):
    id: int
    auth_date: int
    hash: str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


@app.post("/api/auth/telegram")
async def auth_telegram(body: TelegramAuth, resp: Response):
    uid = check_telegram(body.model_dump(), settings.bot_token)
    if uid is None:
        raise HTTPException(401, "подпись Telegram не прошла")
    if uid not in settings.superadmins:
        raise HTTPException(403, f"доступа нет — передайте владельцу ваш Telegram ID: {uid}")
    return _login(resp, uid)


class PasswordAuth(BaseModel):
    password: str


@app.post("/api/auth/password")
async def auth_password(body: PasswordAuth, resp: Response):
    pw = settings.site_password
    if len(pw) < MIN_PASSWORD:
        raise HTTPException(404, "вход по паролю выключен")
    if not hmac.compare_digest(body.password.encode(), pw.encode()):
        await asyncio.sleep(1)  # против перебора
        raise HTTPException(401, "неверный пароль")
    return _login(resp, settings.owner_id)


@app.get("/api/config")
async def config(s=Depends(db)):
    """Без входа: странице входа нужны имя бота для виджета и есть ли запасной пароль."""
    return {"bot_username": await repo.get_setting(s, repo.BOT_USERNAME_KEY),
            "password_enabled": len(settings.site_password) >= MIN_PASSWORD}


@app.post("/api/logout")
async def logout(resp: Response):
    resp.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/me")
async def me(u=Depends(user)):
    return {"id": u.id, "owner_id": u.telegram_id, "username": None,
            "role": "owner" if u.id == u.telegram_id else "admin"}


# ---------- продавцы ----------
class SellerIn(BaseModel):
    supplier_id: int = Field(gt=0)
    b2b: bool = False


class SellerPatch(BaseModel):
    b2b: bool | None = None
    enabled: bool | None = None


async def _my_source(s, u, supplier_id: int) -> models.Source:
    src = await repo.get_source(s, u.telegram_id, supplier_id)
    if src is None:
        raise HTTPException(404, "нет такого продавца")
    return src


@app.get("/api/sellers")
async def sellers(u=Depends(user), s=Depends(db)):
    srcs = await repo.list_sources(s, u.telegram_id)
    counts = dict((await s.execute(select(models.Item.supplier_id, func.count()).where(
        models.Item.supplier_id.in_([x.supplier_id for x in srcs]), models.Item.is_active.is_(True),
    ).group_by(models.Item.supplier_id))).all())
    return [{"supplier_id": x.supplier_id, "name": x.name, "brand": x.seller.brand if x.seller else None,
             "b2b": x.b2b, "enabled": x.enabled, "loaded": x.seeded_at is not None,
             "last_check_at": x.seller.last_check_at if x.seller else None,
             "items": counts.get(x.supplier_id, 0)} for x in srcs]


@app.post("/api/sellers", status_code=201)
async def add_seller(body: SellerIn, u=Depends(user), s=Depends(db)):
    # имя и первую загрузку (без уведомлений) сделает worker на ближайшем цикле
    if not await repo.add_source(s, u.telegram_id, body.supplier_id, b2b=body.b2b):
        raise HTTPException(409, "этот продавец уже добавлен")
    await s.commit()
    return {"supplier_id": body.supplier_id}


@app.patch("/api/sellers/{supplier_id}")
async def patch_seller(supplier_id: int, body: SellerPatch, u=Depends(user), s=Depends(db)):
    src = await _my_source(s, u, supplier_id)
    if body.b2b is not None:
        src.b2b = body.b2b
    if body.enabled is not None:
        src.enabled = body.enabled
    await s.commit()
    return {"ok": True}


@app.delete("/api/sellers/{supplier_id}")
async def delete_seller(supplier_id: int, u=Depends(user), s=Depends(db)):
    if not await repo.remove_source(s, u.telegram_id, supplier_id):
        raise HTTPException(404, "нет такого продавца")
    await s.commit()
    return {"ok": True}


# ---------- товары и лента ----------
@app.get("/api/items")
async def items(seller_id: int | None = None, q: str = "", in_stock: bool = False,
                u=Depends(user), s=Depends(db)):
    srcs = [x for x in await repo.list_sources(s, u.telegram_id) if seller_id in (None, x.supplier_id)]
    accounts = [a for a in await repo.user_accounts(s, u.telegram_id) if a.status == "active"]
    q = q.strip().lower()
    rows = [(src, p) for src in srcs for p in await repo.source_products(s, src)
            if not (q and q not in f"{p.name} {p.brand or ''} {p.nm_id}".lower()) and not (in_stock and not p.stock)]
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    ago = await repo.price_ago(s, [p.nm_id for _, p in rows], day_ago)
    targets = await repo.user_targets(s, u.telegram_id)
    out = []
    for src, p in rows:
        w = wallet_price(p.shelf_price, accounts)
        p24, seen = ago.get(p.nm_id, (None, None))
        # «новый» = появился ПОСЛЕ первой загрузки продавца и моложе суток (ассортимент при загрузке — не новинки)
        new = bool(seen and src.seeded_at and seen > src.seeded_at and seen > day_ago)
        # лучшая цена — как в пуше «ниже цели» (неподтверждённая бизнес-цена не в счёт)
        best = best_offer(None if p.price_stale else p.price, src.b2b, p.shelf_price, accounts)
        t = targets.get(p.nm_id)
        out.append({"nm_id": p.nm_id, "name": p.name, "brand": p.brand, "url": p.url,
                    "supplier_id": src.supplier_id, "seller": src.name, "b2b": src.b2b,
                    "price": p.price, "price_stale": p.price_stale, "shelf_price": p.shelf_price,
                    "wallet_price": w[0] if w else None, "wallet_account": acc_title(w[1]) if w else None,
                    "price_24h": None if new else p24, "is_new": new,
                    "best_price": best[0] if best else None, "best_how": offer_text(best) if best else None,
                    "target": t.price if t else None,
                    "below_target": bool(t and best and p.stock > 0 and best[0] <= t.price),
                    "stock": p.stock, "delivery_hours": p.delivery_hours})
    return out


def acc_title(a) -> str:
    """Как называть аккаунт на сайте: своё имя, иначе имя из WB, иначе маска номера."""
    return a.title


@app.get("/api/feed")
async def feed(limit: int = Query(100, ge=1, le=500), u=Depends(user), s=Depends(db)):
    """new — новинка, change — цена/остаток (old_* — прошлое значение), gone — пропал. Розница.
    Цена с Кошельком — от цены события с ТЕКУЩИМИ % и лимитом аккаунтов (% меняется редко)."""
    accounts = [a for a in await repo.user_accounts(s, u.telegram_id) if a.status == "active"]
    names = {x.supplier_id: x.name for x in await repo.list_sources(s, u.telegram_id)}
    out = []
    for r in await repo.feed(s, u.telegram_id, limit):
        w = wallet_price(r.price, accounts) if r.event != "gone" else None
        out.append({"nm_id": r.nm_id, "name": r.name, "supplier_id": r.supplier_id, "seller": names.get(r.supplier_id),
                    "event": r.event, "price": r.price, "stock": r.stock, "old_price": r.old_price,
                    "old_stock": r.old_stock, "at": r.at,
                    "wallet_price": w[0] if w else None, "wallet_account": acc_title(w[1]) if w else None})
    return out


# ---------- цели ₽ ----------
class TargetIn(BaseModel):
    price: int = Field(gt=0, le=100_000_000)


@app.put("/api/targets/{nm_id}")
async def put_target(nm_id: int, body: TargetIn, u=Depends(user), s=Depends(db)):
    item = await s.get(models.Item, nm_id)
    if item is None or not await repo.get_source(s, u.telegram_id, item.supplier_id):
        raise HTTPException(404, "товара нет среди ваших продавцов")
    await repo.set_target(s, u.telegram_id, nm_id, body.price)
    await s.commit()
    return {"nm_id": nm_id, "target": body.price}


@app.get("/api/targets")
async def targets_list(u=Depends(user), s=Depends(db)):
    """Мои цели: лучшая цена сейчас (как в пуше) и сколько до цели."""
    accounts = [a for a in await repo.user_accounts(s, u.telegram_id) if a.status == "active"]
    targets = await repo.user_targets(s, u.telegram_id)
    out = []
    for src in await repo.list_sources(s, u.telegram_id):
        for p in await repo.source_products(s, src):
            t = targets.get(p.nm_id)
            if t is None:
                continue
            best = best_offer(None if p.price_stale else p.price, src.b2b, p.shelf_price, accounts)
            gap = best[0] - t.price if best else None
            out.append({"nm_id": p.nm_id, "name": p.name, "url": p.url, "supplier_id": src.supplier_id,
                        "seller": src.name, "target": t.price, "best_price": best[0] if best else None,
                        "best_how": offer_text(best) if best else None, "gap": gap,
                        "gap_pct": round(gap / t.price * 100, 1) if gap is not None else None,
                        "stock": p.stock, "below": bool(best and p.stock > 0 and gap <= 0),
                        "below_at": t.below_at, "created_at": t.created_at})
    # цели на товары, пропавшие из выдачи: source_products их не вернёт — показываем, чтобы можно было снять
    seen = {x["nm_id"] for x in out}
    for nm, t in targets.items():
        if nm not in seen:
            it = await s.get(models.Item, nm)
            out.append({"nm_id": nm, "name": it.name if it else None, "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
                        "supplier_id": it.supplier_id if it else None, "seller": None, "target": t.price,
                        "best_price": None, "best_how": "нет в продаже", "gap": None, "gap_pct": None, "stock": 0,
                        "below": False, "below_at": t.below_at, "created_at": t.created_at})
    return out


@app.delete("/api/targets/{nm_id}")
async def delete_target(nm_id: int, u=Depends(user), s=Depends(db)):
    if not await repo.delete_target(s, u.telegram_id, nm_id):
        raise HTTPException(404, "цели нет")
    await s.commit()
    return {"ok": True}


# ---------- аккаунты WB ----------
ACC_FIELDS = ("id", "label", "phone_mask", "name", "phone", "purchase_pct", "kind", "status", "wallet_pct", "limit_left", "monthly_limit", "max_order",
              "max_rid", "funds", "vb_points", "state_at", "proxy_fails", "proxy_fail_at", "polling")
POLL_FRESH_SEC = 30  # ↻ не чаще: только что обновлённый опрашивать незачем
ORDER_FIELDS = ("rid", "nm_id", "name", "brand", "price", "dst_office_id", "status_name", "group_id",
                "status_at", "ordered_at", "expiry_at", "delivery_at", "paid", "office_address", "office_hours", "hidden")


async def _my_account(s, u, account_id: int) -> models.WbAccount:
    acc = await s.get(models.WbAccount, account_id)
    if acc is None or acc.user_id != u.telegram_id:
        raise HTTPException(404, "нет такого аккаунта")
    return acc


async def _account_views(s, accs: list[models.WbAccount]) -> list[dict]:
    """Карточки аккаунтов: поля + активные заказы + прокси."""
    orders: dict[int, list] = {}
    for o in await repo.account_orders(s, [a.id for a in accs]):
        orders.setdefault(o.account_id, []).append({f: getattr(o, f) for f in ORDER_FIELDS})
    proxies = {p.id: p for p in await s.scalars(select(models.Proxy))}
    return [{**{f: getattr(a, f) for f in ACC_FIELDS}, "title": a.title, "orders": orders.get(a.id, []),
             "proxy_down": a.proxy_fails >= repo.PROXY_ALERT_AFTER,  # порог тот же, что у пуша
             "proxy": (p := proxies.get(a.proxy_id)) and {"id": p.id, "label": p.label, "server": p.server,
                                                         "exit_ip": p.exit_ip}} for a in accs]


@app.get("/api/accounts")
async def accounts(u=Depends(user), s=Depends(db)):
    return await _account_views(s, await repo.user_accounts(s, u.telegram_id))


@app.get("/api/accounts/{account_id:int}")
async def account(account_id: int, u=Depends(user), s=Depends(db)):
    """Страница аккаунта: карточка + заказы + код получения (только сегодняшний — он действует до 00:00 МСК)
    + счётчики вкладок."""
    acc = await _my_account(s, u, account_id)
    count = lambda m: s.scalar(select(func.count()).select_from(m).where(m.account_id == acc.id))
    return {**(await _account_views(s, [acc]))[0], "browser_sync": acc.browser_sync,
            "pickup_code": acc.pickup_code if acc.pickup_code_date == repo.msk_today() else None,
            "counts": {"purchases": await count(models.AccountPurchase), "balance": await count(models.AccountBalanceOp),
                       "vb": await count(models.AccountVbOp)}}


PURCHASE_FIELDS = ("rid", "nm_id", "name", "brand", "price", "dt", "office_id", "office_address", "office_hours")


def _msk_start(d: date) -> datetime:
    return datetime.combine(d, dtime.min, repo.MSK)


@app.get("/api/accounts/{account_id:int}/purchases")
async def purchases(account_id: int, d_from: date | None = Query(None, alias="from"),
                    d_to: date | None = Query(None, alias="to"), u=Depends(user), s=Depends(db)):
    """История заказов (полученные), новые сверху; from/to — дни по МСК включительно."""
    acc = await _my_account(s, u, account_id)
    m = models.AccountPurchase
    q = select(m).where(m.account_id == acc.id)
    if d_from:
        q = q.where(m.dt >= _msk_start(d_from))
    if d_to:
        q = q.where(m.dt < _msk_start(d_to + timedelta(days=1)))
    return [{f: getattr(p, f) for f in PURCHASE_FIELDS} for p in await s.scalars(q.order_by(m.dt.desc().nulls_last()))]


@app.get("/api/accounts/{account_id:int}/balance")
async def balance_ops(account_id: int, u=Depends(user), s=Depends(db)):
    """Операции баланса (копим из визитов в браузере) + свои заметки."""
    acc = await _my_account(s, u, account_id)
    m, n = models.AccountBalanceOp, models.BalanceNote
    rows = await s.execute(select(m, n.text).outerjoin(n, (n.account_id == m.account_id) & (n.tx_id == m.tx_id))
                           .where(m.account_id == acc.id).order_by(m.dt.desc().nulls_last()))
    return [{"tx_id": o.tx_id, "name": o.name, "op_type": o.op_type, "amount": o.amount, "balance": o.balance,
             "dt": o.dt, "note": note} for o, note in rows.all()]


class NoteIn(BaseModel):
    text: str = Field(max_length=1000)


@app.put("/api/accounts/{account_id:int}/balance/{tx_id}/note")
async def balance_note(account_id: int, tx_id: str, body: NoteIn, u=Depends(user), s=Depends(db)):
    """Заметка к операции баланса; пустая — удалить."""
    acc = await _my_account(s, u, account_id)
    if await s.get(models.AccountBalanceOp, (acc.id, tx_id)) is None:
        raise HTTPException(404, "нет такой операции")
    text = body.text.strip()
    if text:
        stmt = pg_insert(models.BalanceNote).values(account_id=acc.id, tx_id=tx_id, text=text)
        await s.execute(stmt.on_conflict_do_update(index_elements=["account_id", "tx_id"],
                                                   set_={"text": text, "updated_at": func.now()}))
    else:
        await s.execute(delete(models.BalanceNote).where(models.BalanceNote.account_id == acc.id,
                                                         models.BalanceNote.tx_id == tx_id))
    await s.commit()
    return {"ok": True}


@app.get("/api/accounts/{account_id:int}/vb")
async def vb_ops(account_id: int, u=Depends(user), s=Depends(db)):
    acc = await _my_account(s, u, account_id)
    m = models.AccountVbOp
    return [{"tx_id": o.tx_id, "title": o.title, "description": o.description, "dt": o.dt, "operation": o.operation,
             "amount": o.amount}
            for o in await s.scalars(select(m).where(m.account_id == acc.id).order_by(m.dt.desc().nulls_last()))]


@app.post("/api/accounts/{account_id:int}/sync")
async def browser_sync(account_id: int, u=Depends(user), s=Depends(db)):
    """Визит в браузере аккаунта (код получения + история баланса, ~30 с): worker откроет сайт WB под ним."""
    acc = await _my_account(s, u, account_id)
    if acc.status != "active":
        raise HTTPException(409, "аккаунт не активен — нужен вход")
    acc.browser_sync = True
    await s.commit()
    return {"ok": True}


@app.post("/api/accounts/poll-all")
async def poll_all(u=Depends(user), s=Depends(db)):
    """«Обновить все»: ↻ для всех активных, кроме только что обновлённых."""
    fresh = datetime.now(timezone.utc) - timedelta(seconds=POLL_FRESH_SEC)
    m = models.WbAccount
    res = await s.execute(update(m).where(
        m.user_id == u.telegram_id, m.status == "active", m.polling.is_(False),
        (m.state_at.is_(None)) | (m.state_at < fresh)).values(polling=True))
    await s.commit()
    return {"count": res.rowcount}


@app.get("/api/img-hosts")
async def img_hosts(u=Depends(user), s=Depends(db)):
    """Карта basket-хостов картинок [[vol_from, vol_to, host], …] — кладёт worker раз в сутки."""
    raw = await repo.get_setting(s, media.SETTING_KEY)
    return json.loads(raw) if raw else []


@app.post("/api/accounts/{account_id:int}/poll")
async def poll_account(account_id: int, u=Depends(user), s=Depends(db)):
    """↻ «Обновить»: worker опросит аккаунт вне расписания (polling — как checking у прокси)."""
    acc = await _my_account(s, u, account_id)
    if acc.status != "active":
        raise HTTPException(409, "аккаунт не активен — нужен вход")
    if acc.polling:
        return {"ok": True}
    if acc.state_at and (datetime.now(timezone.utc) - acc.state_at).total_seconds() < POLL_FRESH_SEC:
        raise HTTPException(409, "только что обновлён")
    acc.polling = True
    await s.commit()
    return {"ok": True}


class AccountIn(BaseModel):
    label: str | None = Field(None, max_length=64)


@app.patch("/api/accounts/{account_id}")
async def rename_account(account_id: int, body: AccountIn, u=Depends(user), s=Depends(db)):
    """Своё имя аккаунта (главнее имени из WB). Пусто — вернуть имя из WB."""
    acc = await s.get(models.WbAccount, account_id)
    if acc is None or acc.user_id != u.telegram_id:
        raise HTTPException(404, "нет такого аккаунта")
    acc.label = (body.label or "").strip() or None
    await s.commit()
    return {"ok": True, "title": acc.title}


class OrderIn(BaseModel):
    hidden: bool


@app.patch("/api/accounts/{account_id}/orders/{rid}")
async def hide_order(account_id: int, rid: str, body: OrderIn, u=Depends(user), s=Depends(db)):
    """Скрыть/вернуть позицию заказа на сайте; опрос keeper'а hidden не трогает."""
    acc = await s.get(models.WbAccount, account_id)
    o = await s.get(models.AccountOrder, (account_id, rid))
    if acc is None or acc.user_id != u.telegram_id or o is None:
        raise HTTPException(404, "нет такого заказа")
    o.hidden = body.hidden
    await s.commit()
    return {"ok": True}


@app.delete("/api/accounts/{account_id:int}")  # :int — иначе перехватит DELETE /api/accounts/login
async def delete_account(account_id: int, u=Depends(user), s=Depends(db)):
    acc = await s.get(models.WbAccount, account_id)
    if acc is None or acc.user_id != u.telegram_id:
        raise HTTPException(404, "нет такого аккаунта")
    await s.delete(acc)
    await s.commit()
    return {"ok": True}


class LoginIn(BaseModel):
    phone: str
    kind: Literal["business", "retail"]
    proxy_id: int | None = None  # None — вход через общий прокси (IP парсера)
    relogin: bool = False  # номер уже подключён — сайт спросил и пользователь подтвердил повторный вход


class CodeIn(BaseModel):
    code: str


def _request_view(r: models.WbLoginRequest | None):
    return r and {"id": r.id, "status": r.status, "kind": r.kind, "error": r.error,
                  "account_id": r.account_id, "created_at": r.created_at,
                  "sent_via": r.sent_via, "resend_after": r.resend_after, "resend": r.resend}


class ResendIn(BaseModel):
    via: Literal["push", "sms"]


@app.post("/api/accounts/login", status_code=201)
async def start_login(body: LoginIn, u=Depends(user), s=Depends(db)):
    phone = re.sub(r"\D", "", body.phone)
    phone = phone[1:] if len(phone) == 11 and phone[0] in "78" else phone
    if len(phone) != 10:
        raise HTTPException(422, "нужен номер РФ: 10 цифр после +7")
    if not body.relogin and (acc := await repo.find_account(s, u.telegram_id, phone)):
        # не входим молча поверх: сайт спросит «Войти заново?» и повторит с relogin
        raise HTTPException(409, {"code": "already_connected",
                                  "account": {"id": acc.id, "title": acc.title, "status": acc.status}})
    if body.proxy_id is not None:  # один прокси на несколько аккаунтов — норма, непроверенный — нет
        p = await s.get(models.Proxy, body.proxy_id)
        if p is None:
            raise HTTPException(404, "нет такого прокси")
        if p.ok is not True:
            raise HTTPException(409, "прокси не прошёл проверку — нажмите «Проверить» в разделе «Прокси»")
    s.add(models.WbLoginRequest(user_id=u.telegram_id, phone=phone, kind=body.kind, proxy_id=body.proxy_id))
    try:
        await s.commit()
    except IntegrityError:  # частичный уникальный индекс: одна активная заявка на пользователя
        raise HTTPException(409, "вход уже идёт — дождитесь его или введите код")
    return _request_view(await repo.last_login_request(s, u.telegram_id))


@app.get("/api/accounts/login")
async def login_status(u=Depends(user), s=Depends(db)):
    """Последняя заявка: new (в очереди) → running → wait_code (введите код) → done | error."""
    return _request_view(await repo.last_login_request(s, u.telegram_id))


@app.delete("/api/accounts/login")
async def cancel_login(u=Depends(user), s=Depends(db)):
    """Отмена входа в любом активном состоянии; worker закроет браузер на ближайшей проверке."""
    if not await repo.cancel_login_request(s, u.telegram_id):
        raise HTTPException(404, "активного входа нет")
    await s.commit()
    return {"ok": True}


@app.post("/api/accounts/login/resend")
async def login_resend(body: ResendIn, u=Depends(user), s=Depends(db)):
    """Прислать код заново (пуш или СМС): worker нажмёт «Получить новый код» на странице WB ID."""
    r = await repo.last_login_request(s, u.telegram_id)
    if r is None or r.status != "wait_code":
        raise HTTPException(409, "код сейчас не ждём")
    if r.resend_after and (left := (r.resend_after - datetime.now(timezone.utc)).total_seconds()) > 0:
        raise HTTPException(409, f"можно через {int(left) + 1} с")
    r.resend = body.via
    await s.commit()
    return {"ok": True}


@app.post("/api/accounts/login/code")
async def login_code(body: CodeIn, u=Depends(user), s=Depends(db)):
    code = body.code.strip()
    if not re.fullmatch(r"\d{4,8}", code):
        raise HTTPException(422, "код — 4–8 цифр")
    r = await repo.last_login_request(s, u.telegram_id)
    if r is None or r.status != "wait_code":
        raise HTTPException(409, "код сейчас не ждём")
    r.code = code
    await s.commit()
    return {"ok": True}


# ---------- прокси аккаунтов (проверяет worker: api в сеть не ходит) ----------
PROXY_FIELDS = ("id", "label", "server", "exit_ip", "ok", "error", "checking", "checked_at", "created_at")


class ProxyIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)
    url: str = Field(max_length=512)


@app.get("/api/proxies")
async def proxies(u=Depends(user), s=Depends(db)):
    """Список; used_by — кто на нём (один прокси на несколько аккаунтов — норма)."""
    used: dict[int, list[str]] = {}
    for a in await repo.user_accounts(s, u.telegram_id):
        if a.proxy_id:
            used.setdefault(a.proxy_id, []).append(a.title)
    rows = await s.scalars(select(models.Proxy).order_by(models.Proxy.id))
    return [{**{f: getattr(p, f) for f in PROXY_FIELDS}, "used_by": used.get(p.id, [])} for p in rows]


@app.post("/api/proxies", status_code=201)
async def add_proxy(body: ProxyIn, u=Depends(user), s=Depends(db)):
    try:
        url, server = parse_proxy(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if is_parser_proxy(url):
        raise HTTPException(422, "это прокси мониторинга (WB_PROXIES/WB_WEB_PROXY) — аккаунту нужен свой")
    p = models.Proxy(label=body.label.strip(), server=server, url_enc=encrypt_json(url), checking=True)
    s.add(p)
    await s.commit()  # worker проверит сразу
    return {f: getattr(p, f) for f in PROXY_FIELDS} | {"used_by": []}


@app.post("/api/proxies/{proxy_id}/check")
async def check_proxy(proxy_id: int, u=Depends(user), s=Depends(db)):
    p = await s.get(models.Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "нет такого прокси")
    p.checking = True
    await s.commit()
    return {"ok": True}


@app.delete("/api/proxies/{proxy_id}")
async def delete_proxy(proxy_id: int, u=Depends(user), s=Depends(db)):
    p = await s.get(models.Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "нет такого прокси")
    if busy := [a.title for a in await s.scalars(select(models.WbAccount).where(models.WbAccount.proxy_id == p.id))]:
        raise HTTPException(409, f"на прокси аккаунты: {', '.join(busy)} — сначала перелогиньте их на другой прокси")
    await s.delete(p)
    await s.commit()
    return {"ok": True}


# ---------- статус worker'а ----------
@app.get("/api/status")
async def status(u=Depends(user), s=Depends(db)):
    raw = await repo.get_setting(s, repo.HEARTBEAT_KEY)
    last = json.loads(raw) if raw else None
    alive = bool(last) and time.time() - last["at"] < max(3 * settings.monitor_interval_seconds, 300)
    sellers_n, items_n, _ = await repo.stats(s)
    return {"worker_alive": alive, "last_pass": last, "sellers": sellers_n, "items": items_n}


@app.get("/{path:path}", include_in_schema=False)
async def site(path: str):
    """Собранный сайт с того же origin (без CORS). Любой не-/api путь без файла → index.html:
    маршруты React (ссылки из пушей, F5 на /items) обрабатывает сам сайт."""
    if path == "api" or path.startswith("api/") or not WEB_DIST.is_dir():
        raise HTTPException(404)
    f = (WEB_DIST / path).resolve()
    if path and f.is_file() and f.is_relative_to(WEB_DIST.resolve()):
        return FileResponse(f)
    return FileResponse(WEB_DIST / "index.html")
