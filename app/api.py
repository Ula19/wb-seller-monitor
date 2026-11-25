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
import uuid
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
from app.pickup import pickup_items
from app.wallet import best_offer, offer_text, wallet_price
from app.wb import account_api, media, pickups
from app.wb.web_session import VISIT_SESSION
from app.wb.proxy_check import is_parser_proxy, parse_proxy

COOKIE = "wbm_session"
SESSION_TTL = 30 * 86400
AUTH_MAX_AGE = 86400  # подпись виджета старше суток не принимаем
MIN_PASSWORD = 20  # запасной вход торчит в интернет: короткий пароль = вход выключен
PASSWORD_UID = 0  # вход по паролю — служебный ID: обычный пользователь, разработчиком (DEV_IDS) не бывает
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
    by_password = uid == PASSWORD_UID and len(settings.site_password) >= MIN_PASSWORD
    if uid is None or not (uid in settings.superadmins or by_password):
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
    return _login(resp, PASSWORD_UID)


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
            "role": "owner" if u.id == u.telegram_id else "admin", "is_dev": u.id in settings.devs}


# ---------- режим технических работ (SCOUT 2026-11-25) ----------
def guard(group: str):
    """Изменяющий эндпоинт группы: режим работ закрыл группу, а вошёл не разработчик → 503 с текстом баннера.
    Запрет — на сервере: старая открытая вкладка его не обойдёт."""
    async def check(u=Depends(user), s=Depends(db)):
        m = await repo.maintenance(s)
        if repo.closed(m, group) and u.id not in settings.devs:
            raise HTTPException(503, repo.maintenance_text(m))
    return [Depends(check)]


MONEY, PICKUP, SYNC, ACCOUNTS, SETTINGS = (guard(g) for g in repo.MAINTENANCE_GROUPS)


@app.get("/api/maintenance")
async def maintenance_get(u=Depends(user), s=Depends(db)):
    """Для баннера всем; разработчику ещё — сколько дел очереди выполняется (sending) и ждёт: перед деплоем ждать 0."""
    m = await repo.maintenance(s)
    sending = pending = 0
    for model in (models.Certificate, models.OrderPayment, models.AutobuyOrder):
        sending += await s.scalar(select(func.count()).select_from(model).where(model.status == "sending"))
        pending += await s.scalar(select(func.count()).select_from(model).where(model.status == "pending"))
    a = models.AutobuyOrder  # отмены и заборы «выполняется» не отмечают — считаем их ждущими
    pending += await s.scalar(select(func.count()).select_from(a).where(a.cancel_state == "pending"))
    for model in (models.OrderCancel, models.PickupSend):
        pending += await s.scalar(select(func.count()).select_from(model).where(model.status == "pending"))
    return {**m, "message": repo.maintenance_text(m), "groups": repo.MAINTENANCE_GROUPS, "sending": sending,
            "pending": pending}


class MaintenanceIn(BaseModel):
    on: bool
    text: str = Field("", max_length=200)
    until: str = Field("", pattern=r"^([01]\d|2[0-3]):[0-5]\d$|^$")  # "HH:MM" МСК или пусто
    blocked: list[str] = Field(default_factory=list, max_length=len(repo.MAINTENANCE_GROUPS))
    poll_paused: bool = False


@app.put("/api/maintenance")
async def maintenance_put(body: MaintenanceIn, u=Depends(user), s=Depends(db)):
    """Только разработчики. Включение/выключение — в журнал действий (кто, когда, что закрыто)."""
    if u.id not in settings.devs:
        raise HTTPException(403, "режим работ включают только разработчики (DEV_IDS)")
    if set(body.blocked) - set(repo.MAINTENANCE_GROUPS):
        raise HTTPException(422, "нет такой группы")
    old = await repo.maintenance(s)
    now = datetime.now(timezone.utc).isoformat()
    m = {**body.model_dump(), "text": body.text.strip(), "blocked": [g for g in repo.MAINTENANCE_GROUPS if g in body.blocked],
         "by": u.id if body.on != old["on"] else old["by"], "at": now if body.on != old["on"] else old["at"]}
    await repo.set_setting(s, repo.MAINTENANCE, json.dumps(m, ensure_ascii=False))
    if body.on != old["on"]:
        raw = await repo.get_setting(s, repo.MAINTENANCE_LOG)
        log_ = [*(json.loads(raw) if raw else []), {k: m[k] for k in ("on", "blocked", "poll_paused", "by", "at")}]
        await repo.set_setting(s, repo.MAINTENANCE_LOG, json.dumps(log_[-repo.MAINTENANCE_LOG_MAX:]))
    await s.commit()
    return await maintenance_get(u, s)


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
              "max_rid", "funds", "vb_points", "wallet_blocked", "wallet_arrested", "state_at", "proxy_fails", "proxy_fail_at", "polling", "visit_error")
POLL_FRESH_SEC = 30  # ↻ не чаще: только что обновлённый опрашивать незачем
ORDER_FIELDS = ("rid", "nm_id", "name", "brand", "color", "price", "dst_office_id", "status_name", "group_id",
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
             # куки есть, а ЛК WB не пускает: HTTP-опрос жив (не relogin), но бизнес-цены/код — нужен вход
             "site_lost": a.visit_error == VISIT_SESSION,
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
    count = lambda m, *w: s.scalar(select(func.count()).select_from(m).where(m.account_id == acc.id, *w))
    return {**(await _account_views(s, [acc]))[0], "browser_sync": acc.browser_sync,
            "pickup_code": acc.pickup_code if acc.pickup_code_date == repo.code_today() else None,
            "counts": {"purchases": await count(models.AccountPurchase, _got(models.AccountPurchase)),
                       "balance": await count(models.AccountBalanceOp),
                       "vb": await count(models.AccountVbOp)}}


PURCHASE_FIELDS = ("rid", "nm_id", "name", "brand", "price", "dt", "office_id", "office_address", "office_hours")


def _got(m):
    """«Покупки» — только полученные; null — строки архива до 0024 (тогда в нём были только полученные)."""
    return (m.status == account_api.PURCHASE_GOT) | m.status.is_(None)


def _msk_start(d: date) -> datetime:
    return datetime.combine(d, dtime.min, repo.MSK)


@app.get("/api/accounts/{account_id:int}/purchases")
async def purchases(account_id: int, d_from: date | None = Query(None, alias="from"),
                    d_to: date | None = Query(None, alias="to"), u=Depends(user), s=Depends(db)):
    """История заказов (полученные), новые сверху; from/to — дни по МСК включительно."""
    acc = await _my_account(s, u, account_id)
    m = models.AccountPurchase
    q = select(m).where(m.account_id == acc.id, _got(m))
    if d_from:
        q = q.where(m.dt >= _msk_start(d_from))
    if d_to:
        q = q.where(m.dt < _msk_start(d_to + timedelta(days=1)))
    return [{f: getattr(p, f) for f in PURCHASE_FIELDS} for p in await s.scalars(q.order_by(m.dt.desc().nulls_last()))]


@app.get("/api/accounts/{account_id:int}/balance")
async def balance_ops(account_id: int, u=Depends(user), s=Depends(db)):
    """Операции баланса (копим из визитов в браузере) + свои заметки."""
    acc = await _my_account(s, u, account_id)
    m, n, c = models.AccountBalanceOp, models.BalanceNote, models.Certificate
    rows = await s.execute(select(m, n.text, c.code)
                           .outerjoin(n, (n.account_id == m.account_id) & (n.tx_id == m.tx_id))
                           .outerjoin(c, (c.wb_cert_id == m.tx_id) & (c.status == "ok"))  # id активации = txId
                           .where(m.account_id == acc.id).order_by(m.dt.desc().nulls_last()))
    return [{"tx_id": o.tx_id, "name": o.name, "op_type": o.op_type, "amount": o.amount, "balance": o.balance,
             "dt": o.dt, "note": note, "cert_code": code, "source_type": o.source_type} for o, note, code in rows.all()]


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


@app.post("/api/accounts/{account_id:int}/sync", dependencies=SYNC)
async def browser_sync(account_id: int, u=Depends(user), s=Depends(db)):
    """Визит в браузере аккаунта (код получения + история баланса, ~30 с): worker откроет сайт WB под ним."""
    acc = await _my_account(s, u, account_id)
    if acc.status != "active":
        raise HTTPException(409, "аккаунт не активен — нужен вход")
    acc.browser_sync = True
    await s.commit()
    return {"ok": True}


@app.post("/api/accounts/poll-all", dependencies=SYNC)
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


@app.get("/api/settings/poll")
async def get_poll_settings(u=Depends(user), s=Depends(db)):
    """Частота опроса аккаунтов по типам (минуты) и ночной режим — общие для всех аккаунтов."""
    return await repo.poll_settings(s)


@app.put("/api/settings/poll", dependencies=SETTINGS)
async def put_poll_settings(body: dict, u=Depends(user), s=Depends(db)):
    """Сохранить частоту опроса; worker читает её каждый тик — без рестарта."""
    cfg = {}
    for k, hi in repo.POLL_MAX.items():
        v = body.get(k)
        if type(v) is not int or not 1 <= v <= hi:  # type — не isinstance: True/False тоже int
            raise HTTPException(422, f"{k}: нужно целое от 1 до {hi}")
        cfg[k] = v
    if type(body.get("night")) is not bool:
        raise HTTPException(422, "night: нужно да/нет")
    cfg["night"] = body["night"]
    for k in ("night_from", "night_to"):  # "HH:MM" по МСК
        v = body.get(k)
        if not (isinstance(v, str) and re.fullmatch(r"\d{2}:\d{2}", v) and int(v[:2]) < 24 and int(v[3:]) < 60):
            raise HTTPException(422, f"{k}: нужно время ЧЧ:ММ")
        cfg[k] = v
    if cfg["night_from"] == cfg["night_to"]:
        raise HTTPException(422, "Ночной режим: начало и конец совпадают")
    await repo.set_setting(s, repo.POLL_KEY, json.dumps(cfg))
    await s.commit()
    return cfg


@app.post("/api/accounts/{account_id:int}/poll", dependencies=SYNC)
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


@app.patch("/api/accounts/{account_id}", dependencies=ACCOUNTS)
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


@app.delete("/api/accounts/{account_id:int}", dependencies=ACCOUNTS)  # :int — иначе перехватит DELETE /api/accounts/login
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


@app.post("/api/accounts/login", status_code=201, dependencies=ACCOUNTS)
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


@app.delete("/api/accounts/login", dependencies=ACCOUNTS)
async def cancel_login(u=Depends(user), s=Depends(db)):
    """Отмена входа в любом активном состоянии; worker закроет браузер на ближайшей проверке."""
    if not await repo.cancel_login_request(s, u.telegram_id):
        raise HTTPException(404, "активного входа нет")
    await s.commit()
    return {"ok": True}


@app.post("/api/accounts/login/resend", dependencies=ACCOUNTS)
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


@app.post("/api/accounts/login/code", dependencies=ACCOUNTS)
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


@app.post("/api/proxies", status_code=201, dependencies=ACCOUNTS)
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


@app.post("/api/proxies/{proxy_id}/check", dependencies=ACCOUNTS)
async def check_proxy(proxy_id: int, u=Depends(user), s=Depends(db)):
    p = await s.get(models.Proxy, proxy_id)
    if p is None:
        raise HTTPException(404, "нет такого прокси")
    p.checking = True
    await s.commit()
    return {"ok": True}


@app.delete("/api/proxies/{proxy_id}", dependencies=ACCOUNTS)
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
# ---------- сертификаты WB ----------
CERT_FIELDS = ("id", "code", "account_id", "nominal_entered", "nominal", "status", "error_text", "created_at", "done_at")


class CertItem(BaseModel):
    code: str = Field(max_length=64)
    nominal: int = Field(gt=0, le=10_000_000)  # вне 500–100 000 — только предупреждение на сайте (бывают 5–80 ₽)
    account_id: int


class CertsIn(BaseModel):
    items: list[CertItem] = Field(min_length=1, max_length=500)


class CodesIn(BaseModel):
    codes: list[str] = Field(max_length=500)


def _ddmm(dt: datetime | None) -> str | None:
    return dt.astimezone(repo.MSK).strftime("%d.%m") if dt else None


async def _known_certs(s, codes) -> dict[str, dict]:
    """Коды, уже активированные или ждущие активации (status не error) → {status, title, done_at}."""
    m = models.Certificate
    rows = await s.execute(select(m, models.WbAccount).outerjoin(models.WbAccount, models.WbAccount.id == m.account_id)
                           .where(m.code.in_(list(codes)), m.status != "error"))
    return {c.code: {"status": c.status, "done_at": c.done_at or c.created_at,
                     "title": a and a.title}
            for c, a in rows.all()}


@app.post("/api/certificates/known")
async def known_certificates(body: CodesIn, u=Depends(user), s=Depends(db)):
    """Какие из кодов уже были (POST — код до активации = деньги, в URL и логи прокси не кладём)."""
    codes = {c for c in map(account_api.norm_cert, body.codes) if c}
    return await _known_certs(s, codes) if codes else {}


@app.post("/api/certificates", status_code=201, dependencies=MONEY)
async def add_certificates(body: CertsIn, u=Depends(user), s=Depends(db)):
    """Сертификаты в очередь активации (pending; активирует worker). Плохие строки → 422 со списком причин,
    ничего не создаётся."""
    accs = {a.id: a for a in await repo.user_accounts(s, u.telegram_id)}
    codes = [account_api.norm_cert(it.code) for it in body.items]
    known = await _known_certs(s, {c for c in codes if c})
    def why(it, code, seen) -> str | None:
        acc, k = accs.get(it.account_id), known.get(code)
        if not code:
            return "неверный код — нужно 16 латинских букв и цифр"
        if code in seen:
            return "код повторяется в списке"
        if k and k["status"] == "ok":
            parts = [x for x in (k["title"], _ddmm(k["done_at"])) if x]  # «уже активирован: Аня, 10.11»
            return "уже активирован" + (": " + ", ".join(parts) if parts else "")
        if k:
            return "уже добавлен — ждёт активации или проверки баланса"
        if acc is None:
            return "нет такого аккаунта"
        return "аккаунт не активен — нужен вход" if acc.status != "active" else None

    bad, seen = [], set()
    for i, (it, code) in enumerate(zip(body.items, codes)):
        if err := why(it, code, seen):
            bad.append({"row": i, "code": code or it.code[:64], "error": err})
        seen.add(code)
    if bad:
        raise HTTPException(422, {"code": "bad_items", "items": bad})
    rows = [models.Certificate(user_id=u.telegram_id, code=code, account_id=it.account_id, nominal_entered=it.nominal)
            for it, code in zip(body.items, codes)]
    s.add_all(rows)
    try:
        await s.commit()
    except IntegrityError:  # тот же код успели добавить параллельно
        raise HTTPException(409, "часть кодов уже добавлена — обновите страницу")
    return {"ids": [r.id for r in rows]}


class RetryItem(BaseModel):
    id: int
    account_id: int


class RetryIn(BaseModel):
    items: list[RetryItem] = Field(min_length=1, max_length=500)


@app.post("/api/certificates/retry", dependencies=MONEY)
async def retry_certificates(body: RetryIn, u=Depends(user), s=Depends(db)):
    """Неудачные (error | unknown) — снова в очередь, на тот же или другой аккаунт. Та же запись: история кода одна.
    Повтор unknown безопасен — WB дважды не зачислит (ответит ошибкой). Плохие строки → 422, ничего не меняется."""
    accs = {a.id: a for a in await repo.user_accounts(s, u.telegram_id)}
    m = models.Certificate
    certs = {c.id: c for c in await s.scalars(select(m).where(
        m.id.in_([it.id for it in body.items]), m.user_id == u.telegram_id))}
    bad = []
    for i, it in enumerate(body.items):
        c, acc = certs.get(it.id), accs.get(it.account_id)
        why = ("нет такого сертификата" if c is None
               else "уже активирован" if c.status == "ok"
               else "ещё активируется" if c.status not in ("error", "unknown")
               else "нет такого аккаунта" if acc is None
               else "аккаунт не активен — нужен вход" if acc.status != "active" else None)
        if why:
            bad.append({"row": i, "code": c.code if c else None, "error": why})
    if bad:
        raise HTTPException(422, {"code": "bad_items", "items": bad})
    for it in body.items:
        c = certs[it.id]
        c.account_id, c.status, c.done_at = it.account_id, "pending", None
        c.error_number = c.error_text = c.nominal = c.wb_cert_id = None
    try:
        await s.commit()
    except IntegrityError:  # этот код уже добавили заново и он ждёт/прошёл
        raise HTTPException(409, "код уже добавлен заново — обновите окно")
    return {"ids": [it.id for it in body.items]}


@app.get("/api/certificates")
async def certificates(ids: list[int] = Query(..., max_length=500), u=Depends(user), s=Depends(db)):
    """Статусы для мастера (опрос раз в 1–2 с) + свежий баланс аккаунта («баланс теперь»)."""
    m = models.Certificate
    rows = await s.execute(select(m, models.WbAccount).outerjoin(models.WbAccount, models.WbAccount.id == m.account_id)
                           .where(m.id.in_(ids), m.user_id == u.telegram_id).order_by(m.id))
    return [{**{f: getattr(c, f) for f in CERT_FIELDS}, "account_title": a and a.title, "funds": a and a.funds}
            for c, a in rows.all()]


@app.get("/api/certificates/history")
async def certificates_history(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                               account_id: int | None = None, u=Depends(user), s=Depends(db)):
    """История сертификатов: дни from–to по МСК включительно (нет — без границы), новые сверху."""
    m = models.Certificate
    q = (select(m, models.WbAccount).outerjoin(models.WbAccount, models.WbAccount.id == m.account_id)
         .where(m.user_id == u.telegram_id).order_by(m.created_at.desc(), m.id.desc()).limit(5000))
    if date_from:
        q = q.where(m.created_at >= datetime.combine(date_from, dtime(), repo.MSK))
    if date_to:
        q = q.where(m.created_at < datetime.combine(date_to + timedelta(days=1), dtime(), repo.MSK))
    if account_id is not None:
        q = q.where(m.account_id == account_id)
    return [{**{f: getattr(c, f) for f in CERT_FIELDS}, "account_title": a and a.title} for c, a in (await s.execute(q)).all()]


# ---------- оплата заказов с WB Кошелька ----------
PAY_FIELDS = ("id", "account_id", "rids", "amount", "status", "error_text", "funds_before", "funds_after",
              "created_at", "done_at")


class PayIn(BaseModel):
    account_id: int
    rids: list[str] = Field(min_length=1, max_length=200)
    confirm_amount: int  # сумма, которую человек подтвердил; не совпала с нашей — 409, ничего не платим


@app.post("/api/payments", status_code=201, dependencies=MONEY)
async def add_payment(body: PayIn, u=Depends(user), s=Depends(db)):
    """Оплата неоплаченных позиций аккаунта в очередь (платит worker). Сумма — по нашим ценам из account_orders.
    Строка аккаунта под FOR UPDATE: двойной клик / два окна не создадут вторую оплату тех же rids."""
    acc = await s.scalar(select(models.WbAccount).where(
        models.WbAccount.id == body.account_id, models.WbAccount.user_id == u.telegram_id).with_for_update())
    if acc is None:
        raise HTTPException(404, "нет такого аккаунта")
    if acc.status != "active":
        raise HTTPException(422, "аккаунт не активен — нужен вход")
    if acc.wallet_blocked:
        raise HTTPException(422, "Кошелёк заблокирован — оплата им недоступна")
    rids = list(dict.fromkeys(body.rids))
    orders = {o.rid: o for o in await s.scalars(select(models.AccountOrder).where(
        models.AccountOrder.account_id == acc.id, models.AccountOrder.rid.in_(rids)))}
    if any(orders.get(r) is None or orders[r].paid is not False for r in rids):
        raise HTTPException(422, "товар уже оплачен или пропал из заказов — обновите аккаунт")
    p = models.OrderPayment
    busy = await s.scalars(select(p.rids).where(p.account_id == acc.id, p.status.in_(("pending", "sending"))))
    if set(rids) & {r for x in busy for r in x}:
        raise HTTPException(409, "эти товары уже оплачиваются — дождитесь итога")
    amount = sum(orders[r].price or 0 for r in rids)
    if amount != body.confirm_amount:
        raise HTTPException(409, f"сумма изменилась: {amount:,} ₽ — обновите и подтвердите заново".replace(",", " "))
    row = p(user_id=u.telegram_id, account_id=acc.id, rids=rids, amount=amount)
    s.add(row)
    await s.commit()
    return {"id": row.id, "amount": amount}


@app.get("/api/payments")
async def payments(ids: list[int] = Query(..., max_length=500), u=Depends(user), s=Depends(db)):
    """Статусы оплат для окна (опрос раз в 1–2 с)."""
    p = models.OrderPayment
    rows = await s.scalars(select(p).where(p.id.in_(ids), p.user_id == u.telegram_id).order_by(p.id))
    return [{f: getattr(x, f) for f in PAY_FIELDS} for x in rows]


# ---------- «Заказать товар» (в WB ходит worker: order_quotes — карточка/предпросмотр, autobuy_orders — заказы) ----------
QUOTE_TTL = timedelta(hours=1)  # старые запросы к worker'у чистим при создании нового
PREVIEW_TTL = timedelta(minutes=10)  # заказ — только по свежему предпросмотру
ORDER_QTY_MAX = 100
AUTOBUY_FIELDS = ("id", "account_id", "nm", "name", "qty", "pvz_id", "pay_now", "price_ok", "total", "final", "status",
                "error", "rids", "cancel_state", "cancel_error", "cancel_at", "created_at", "done_at")
CANCEL_CHECK = timedelta(minutes=20)  # бесплатную WB проводит за ~40–50 с, платную (в пути / в ПВЗ) — дольше 10 мин (24.11)


def _rub(kop: int | None) -> str:
    return "? ₽" if kop is None else (f"{kop / 100:,.2f}" if kop % 100 else f"{kop // 100:,}").replace(",", " ") + " ₽"


async def _new_quote(s, u: Viewer, kind: str, req: dict) -> dict:
    q = models.OrderQuote
    await s.execute(delete(q).where(q.created_at < datetime.now(timezone.utc) - QUOTE_TTL))
    row = q(user_id=u.telegram_id, kind=kind, request=req)
    s.add(row)
    await s.commit()
    return {"id": row.id}


class ProductIn(BaseModel):
    q: str = Field(min_length=1, max_length=500)


@app.post("/api/order/product", status_code=201, dependencies=MONEY)
async def order_product(body: ProductIn, u=Depends(user), s=Depends(db)):
    """Карточка товара по артикулу или ссылке — запрос worker'у; итог — GET /api/order/quote/{id}."""
    return await _new_quote(s, u, "product", {"q": body.q.strip()})


class OrderLine(BaseModel):
    account_id: int
    nm: int = Field(gt=0)
    qty: int = Field(ge=1, le=ORDER_QTY_MAX)
    pvz_id: int = Field(gt=0)


class PreviewIn(BaseModel):
    lines: list[OrderLine] = Field(min_length=1, max_length=200)


def _dupes(lines) -> bool:
    keys = [(ln.account_id, ln.nm) for ln in lines]
    return len(keys) != len(set(keys))


@app.post("/api/order/preview", status_code=201, dependencies=MONEY)
async def order_preview(body: PreviewIn, u=Depends(user), s=Depends(db)):
    """Цены на ПВЗ, деньги и лимиты аккаунтов — запрос worker'у (одна строка = один заказ: артикул на аккаунт)."""
    mine = {a.id for a in await repo.user_accounts(s, u.telegram_id)}
    if any(ln.account_id not in mine for ln in body.lines):
        raise HTTPException(404, "нет такого аккаунта")
    if _dupes(body.lines):
        raise HTTPException(422, "товар повторяется у аккаунта — укажите количество в одной строке")
    return await _new_quote(s, u, "preview", {"lines": [ln.model_dump() for ln in body.lines]})


@app.get("/api/order/quote/{quote_id:int}")
async def order_quote(quote_id: int, u=Depends(user), s=Depends(db)):
    """Итог запроса (сайт опрашивает раз в ~0,5 с): status pending | working | done | error."""
    q = await s.scalar(select(models.OrderQuote).where(models.OrderQuote.id == quote_id,
                                                       models.OrderQuote.user_id == u.telegram_id))
    if q is None:
        raise HTTPException(404, "запрос не найден — начните заново")
    return {"id": q.id, "kind": q.kind, "status": q.status, "result": q.result, "error": q.error}


def _pvz_out(p: models.PickupPoint) -> dict:
    return {"id": p.id, "address": p.address, "hours": pickups.hours(p.week), "lat": p.lat, "lon": p.lon}


@app.get("/api/pickups/search")
async def pickups_search(q: str = Query(min_length=1, max_length=200), u=Depends(user), s=Depends(db)):
    """ПВЗ из справочника, только открытые: цифры — точный номер, иначе все слова адреса (ILIKE), до 20 строк.
    ponytail: без индекса (~200 мс на 97 тыс.); станет медленно — pg_trgm по address."""
    p, q = models.PickupPoint, q.strip()
    query = select(p).where(p.closed_at.is_(None))
    if q.isdigit():
        query = query.where(p.id == int(q))
    else:
        words = re.findall(r"\w+", q)[:8]
        if not words:
            return []
        for w in words:  # «_» в слове — буквально, не «любой символ»
            pat = "%" + w.replace("_", "\\_") + "%"
            query = query.where(p.address.ilike(pat, escape="\\"))
    return [_pvz_out(x) for x in await s.scalars(query.order_by(p.address).limit(20))]


@app.get("/api/order/pickups")
async def order_pickups(accounts: list[int] = Query(..., max_length=200), u=Depends(user), s=Depends(db)):
    """ПВЗ из активных заказов выбранных аккаунтов (без дублей, только открытые) + чьи они — для выбора по умолчанию."""
    mine = {a.id for a in await repo.user_accounts(s, u.telegram_id)} & set(accounts)
    o, p = models.AccountOrder, models.PickupPoint
    pairs = (await s.execute(select(o.dst_office_id, o.account_id).distinct().where(
        o.account_id.in_(mine), o.dst_office_id.is_not(None)))).all()
    whose: dict[int, list] = {}
    for pid, aid in pairs:
        whose.setdefault(pid, []).append(aid)
    rows = await s.scalars(select(p).where(p.id.in_(whose), p.closed_at.is_(None)).order_by(p.address))
    return [{**_pvz_out(x), "accounts": sorted(whose[x.id])} for x in rows]


class PlaceItem(OrderLine):
    pay_now: bool


class PlaceIn(BaseModel):
    quote_id: int
    items: list[PlaceItem] = Field(min_length=1, max_length=200)
    confirm_total: int  # коп.: «Заказать · сумма» — Σ (сейчас: now строки, при получении: total × qty)


async def _preview_for_place(s, u: Viewer, quote_id: int) -> dict:
    q = await s.scalar(select(models.OrderQuote).where(
        models.OrderQuote.id == quote_id, models.OrderQuote.user_id == u.telegram_id, models.OrderQuote.kind == "preview"))
    if q is None or q.status != "done":
        raise HTTPException(409, "предпросмотр не готов — обновите")
    if q.created_at < datetime.now(timezone.utc) - PREVIEW_TTL:
        raise HTTPException(409, "цены устарели — обновите предпросмотр")
    return q.result


def _place_errors(items, preview: dict, accs: dict, open_pvz: set) -> list[dict]:
    """Строки заказа против предпросмотра (цены — с сервера, клиенту не верим). → [{row, error}]."""
    by_acc = {a["account_id"]: a for a in preview.get("accounts") or []}
    bad, need = [], {}
    for i, it in enumerate(items):
        acc, pa = accs.get(it.account_id), by_acc.get(it.account_id) or {}
        line = next((ln for ln in pa.get("lines") or [] if (ln["nm"], ln["pvz_id"], ln["qty"]) == (it.nm, it.pvz_id, it.qty)), None)
        err = ("нет такого аккаунта" if acc is None else "аккаунт не активен — нужен вход" if acc.status != "active"
               else pa.get("error") or ("заказ отличается от предпросмотра — обновите" if line is None else line.get("error"))
               or ("ПВЗ закрыт — выберите другой" if it.pvz_id not in open_pvz else None)
               or ("только «Оплатить сейчас»: аккаунту доступна лишь предоплата" if not it.pay_now and pa.get("prepaid_only") else None)
               or ("Кошелёк заблокирован — оплата им недоступна" if it.pay_now and pa.get("wallet_blocked") else None))
        if err:
            bad.append({"row": i, "error": err})
            continue
        sums = need.setdefault(it.account_id, {"now": 0, "post": 0, "rows": []})
        sums["now" if it.pay_now else "post"] += line["now"] if it.pay_now else line["total"] * it.qty
        sums["rows"].append(i)
    for aid, sums in need.items():  # деньги и постоплата — на весь аккаунт
        pa = by_acc[aid]
        if sums["post"] > (pa.get("postpaid_left") or 0):
            bad += [{"row": i, "error": f"лимит постоплаты: доступно {_rub(pa.get('postpaid_left'))}, нужно {_rub(sums['post'])}"}
                    for i in sums["rows"] if not items[i].pay_now]
        if sums["now"] > (pa.get("funds") or 0):
            bad += [{"row": i, "error": f"на Кошельке {_rub(pa.get('funds'))}, нужно {_rub(sums['now'])}"}
                    for i in sums["rows"] if items[i].pay_now]
    return bad


@app.post("/api/order/place", status_code=201, dependencies=MONEY)
async def order_place(body: PlaceIn, u=Depends(user), s=Depends(db)):
    """Заказы в очередь (шлёт worker, заново проверив цену и деньги). Цены — из предпросмотра на сервере; сумма не
    совпала с confirm_total → 409. Строки аккаунтов под FOR UPDATE: двойной клик не создаст второй заказ."""
    if _dupes(body.items):
        raise HTTPException(422, "товар повторяется у аккаунта — укажите количество в одной строке")
    preview = await _preview_for_place(s, u, body.quote_id)
    need: dict[int, int] = {}
    for it in body.items:  # остаток — только по тому, что заказываем (неготовые строки сайт не шлёт)
        need[it.nm] = need.get(it.nm, 0) + it.qty
    if over := [x for x in preview.get("stock_over") or [] if need.get(x["nm"], 0) > x["stock"]]:
        raise HTTPException(422, "товара не хватает: " + ", ".join(f"{x['nm']} — нужно {need[x['nm']]}, есть {x['stock']}" for x in over))
    w = models.WbAccount
    accs = {a.id: a for a in await s.scalars(select(w).where(
        w.id.in_({it.account_id for it in body.items}), w.user_id == u.telegram_id).order_by(w.id).with_for_update())}
    open_pvz = set(await s.scalars(select(models.PickupPoint.id).where(
        models.PickupPoint.id.in_({it.pvz_id for it in body.items}), models.PickupPoint.closed_at.is_(None))))
    if bad := _place_errors(body.items, preview, accs, open_pvz):
        raise HTTPException(422, {"code": "bad_items", "items": bad})
    a = models.AutobuyOrder
    busy = set((await s.execute(select(a.account_id, a.nm).where(
        a.account_id.in_(accs), a.status.in_(("pending", "sending"))))).all())
    if any((it.account_id, it.nm) in busy for it in body.items):
        raise HTTPException(409, "этот товар уже заказывается на аккаунт — дождитесь итога")
    lines = {(ln["nm"], ln["pvz_id"], ln["qty"], x["account_id"]): ln
             for x in preview["accounts"] for ln in x.get("lines") or []}
    picked = [(it, lines[(it.nm, it.pvz_id, it.qty, it.account_id)]) for it in body.items]
    total = sum(ln["now"] if it.pay_now else ln["total"] * it.qty for it, ln in picked)
    if total != body.confirm_total:
        raise HTTPException(409, f"сумма изменилась: {_rub(total)} — обновите и подтвердите заново")
    rows = [a(user_id=u.telegram_id, account_id=it.account_id, uid="d" + uuid.uuid4().hex, nm=it.nm, name=ln.get("name"),
              qty=it.qty, pvz_id=it.pvz_id, pay_now=it.pay_now, price_ok=ln["total"]) for it, ln in picked]
    s.add_all(rows)
    await s.commit()
    return {"ids": [r.id for r in rows], "total": total}


def _can_cancel(o: models.AutobuyOrder) -> bool:
    """Заказ мог создаться (ok, unknown, error после status 3/4/7 — у них есть rids), отмену не просили или она не
    прошла (error). asked — повтор бесполезен: WB заявку принял, склад уже собирает."""
    return bool(o.rids) and o.status in ("ok", "error", "unknown") and o.cancel_state in (None, "error")


async def _cancel_outcome(s, account_id: int | None, rids: list[str], at: datetime | None) -> dict:
    """Итог запрошенной отмены по факту, без WB (опрос заказов и архива и так идёт): в архиве отменой — cancelled; в
    архиве иначе (получен) или всё ещё в заказах, перечитанных позже at + CANCEL_CHECK, — failed; иначе — asked."""
    p, o = models.AccountPurchase, models.AccountOrder
    arch = dict((await s.execute(select(p.rid, p.status).where(p.account_id == account_id, p.rid.in_(rids)))).all())
    live = set(await s.scalars(select(o.rid).where(o.account_id == account_id, o.rid.in_(rids))))
    seen = await s.scalar(select(models.WbAccount.orders_at).where(models.WbAccount.id == account_id))
    fresh = bool(seen and at and seen > at + CANCEL_CHECK)
    out = {"cancelled": 0, "failed": 0, "asked": 0}
    for r in rids:
        out["cancelled" if arch.get(r) in account_api.PURCHASE_CANCELLED else
            "failed" if r in arch or (r in live and fresh) else "asked"] += 1
    return out


@app.get("/api/order/status")
async def order_status(ids: list[int] = Query(..., max_length=500), u=Depends(user), s=Depends(db)):
    """Итог заказов для окна (опрос раз в 1–2 с) + можно ли отменить + итог запрошенной отмены."""
    a = models.AutobuyOrder
    rows = await s.scalars(select(a).where(a.id.in_(ids), a.user_id == u.telegram_id).order_by(a.id))
    return [{**{f: getattr(x, f) for f in AUTOBUY_FIELDS}, "can_cancel": _can_cancel(x),
             "cancel": await _cancel_outcome(s, x.account_id, x.rids, x.cancel_at) if x.cancel_state == "asked" else None}
            for x in rows]


class CancelIn(BaseModel):
    id: int


@app.post("/api/order/cancel", dependencies=MONEY)
async def order_cancel(body: CancelIn, u=Depends(user), s=Depends(db)):
    """Отмена заказа по его rids — в очередь (отменяет worker, раньше новых заказов)."""
    a = models.AutobuyOrder
    o = await s.scalar(select(a).where(a.id == body.id, a.user_id == u.telegram_id).with_for_update())
    if o is None:
        raise HTTPException(404, "заказ не найден")
    if not _can_cancel(o):
        raise HTTPException(409, "этот заказ отменить нельзя" if o.cancel_state != "pending" else "отмена уже идёт")
    o.cancel_state, o.cancel_error = "pending", None
    await s.commit()
    return {"id": o.id}


# ---------- отмена позиций из группы ПВЗ (в WB ходит worker: order_cancels) ----------
CANCEL_FIELDS = ("id", "account_id", "rids", "items", "status", "started", "error", "created_at", "done_at")


class OrderCancelIn(BaseModel):
    account_id: int
    rids: list[str] = Field(min_length=1, max_length=500)


@app.post("/api/order-cancels", status_code=201, dependencies=MONEY)
async def add_order_cancel(body: OrderCancelIn, u=Depends(user), s=Depends(db)):
    """Отмена позиций аккаунта в очередь. rids — только из его активных заказов; строка аккаунта под FOR UPDATE —
    двойной клик не создаст вторую отмену тех же rids."""
    acc = await s.scalar(select(models.WbAccount).where(
        models.WbAccount.id == body.account_id, models.WbAccount.user_id == u.telegram_id).with_for_update())
    if acc is None:
        raise HTTPException(404, "нет такого аккаунта")
    if acc.status != "active":
        raise HTTPException(422, "аккаунт не активен — нужен вход")
    rids = list(dict.fromkeys(body.rids))
    orders = {x.rid: x for x in await s.scalars(select(models.AccountOrder).where(
        models.AccountOrder.account_id == acc.id, models.AccountOrder.rid.in_(rids)))}
    if len(orders) != len(rids):
        raise HTTPException(409, "товар пропал из заказов — обновите аккаунт")
    c = models.OrderCancel
    busy = await s.scalars(select(c.rids).where(c.account_id == acc.id, c.status == "pending"))
    if set(rids) & {r for x in busy for r in x}:
        raise HTTPException(409, "отмена уже идёт — дождитесь итога")
    row = c(user_id=u.telegram_id, account_id=acc.id, rids=rids, items=pickup_items([orders[r] for r in rids]))
    s.add(row)
    await s.commit()
    return {"id": row.id}


@app.get("/api/order-cancels")
async def order_cancels(ids: list[int] = Query(..., max_length=100), u=Depends(user), s=Depends(db)):
    """Статус отмен для окна (опрос): asked — с итогом по факту (cancel), как у отмены заказа."""
    c = models.OrderCancel
    rows = await s.scalars(select(c).where(c.id.in_(ids), c.user_id == u.telegram_id).order_by(c.id))
    return [{**{f: getattr(x, f) for f in CANCEL_FIELDS},
             "cancel": await _cancel_outcome(s, x.account_id, x.started, x.done_at) if x.status == "asked" else None}
            for x in rows]


# ---------- «Общий забор» (в Telegram шлёт worker: pickup_sends) ----------
PICKUP_TO_MAX = 10


@app.get("/api/settings/pickup")
async def get_pickup_settings(u=Depends(user), s=Depends(db)):
    """Кому слать заборы в Telegram — список ID."""
    return {"to": json.loads(await repo.get_setting(s, repo.PICKUP_TO) or "[]")}


class PickupToIn(BaseModel):
    to: list[int] = Field(max_length=PICKUP_TO_MAX)


@app.put("/api/settings/pickup", dependencies=SETTINGS)
async def put_pickup_settings(body: PickupToIn, u=Depends(user), s=Depends(db)):
    to = list(dict.fromkeys(body.to))
    if any(x <= 0 for x in to):
        raise HTTPException(422, "Telegram ID — положительные числа")
    await repo.set_setting(s, repo.PICKUP_TO, json.dumps(to))
    await s.commit()
    return {"to": to}


async def _pickup_groups(s, u: Viewer, account_ids: set[int] | None = None) -> dict[tuple[int, int], dict]:
    """(аккаунт, ПВЗ) → {acc, address, orders}: в ПВЗ (группа 5), не скрытые, активные свои аккаунты, без rid'ов, которые
    ещё отправляются (pending). Отправленные (sent) остаются, пока товар не заберут: сам уйдёт из группы 5."""
    w, o, p = models.WbAccount, models.AccountOrder, models.PickupSend
    q = select(w).where(w.user_id == u.telegram_id, w.status == "active")
    accs = {a.id: a for a in await s.scalars(q if account_ids is None else q.where(w.id.in_(account_ids)))}
    busy = {r for rids in await s.scalars(select(p.rids).where(p.account_id.in_(accs), p.status == "pending")) for r in rids}
    out: dict[tuple[int, int], dict] = {}
    rows = await s.scalars(select(o).where(o.account_id.in_(accs), o.group_id == 5, o.hidden.is_(False))
                           .order_by(o.account_id, o.ordered_at.nulls_last(), o.rid))
    for x in rows:
        if x.rid in busy:
            continue
        oid = x.dst_office_id or 0
        g = out.setdefault((x.account_id, oid), {"acc": accs[x.account_id], "orders": [],
                                                  "address": x.office_address or (f"ПВЗ №{oid}" if oid else "ПВЗ не указан")})
        g["orders"].append(x)
    return out


@app.get("/api/pickups/board")
async def pickups_board(u=Depends(user), s=Depends(db)):
    """Окно «Общий забор»: ПВЗ → аккаунты (у аккаунта в разных ПВЗ — несколько строк), товары «имя × n», шт. и сумма ₽;
    sent_at — когда пару последний раз удачно отправляли (отправленное остаётся в окне до забора); code — код получения
    на сегодня (UTC, как у WB), code_at — когда снят, syncing — опрос за кодом ещё в очереди («Обновить коды»)."""
    groups = await _pickup_groups(s, u)
    today = repo.code_today()
    p = models.PickupSend
    last = dict(((a, o), at) for a, o, at in (await s.execute(
        select(p.account_id, p.office_id, func.max(p.sent_at)).where(p.account_id.in_({a for a, _ in groups}), p.status == "sent")
        .group_by(p.account_id, p.office_id))).all())
    offices: dict[int, dict] = {}
    for (aid, oid), g in groups.items():
        items = pickup_items(g["orders"])
        off = offices.setdefault(oid, {"office_id": oid, "address": g["address"], "accounts": []})
        a = g["acc"]
        code = a.pickup_code if a.pickup_code and a.pickup_code_date == today else None
        off["accounts"].append({"account_id": aid, "title": a.title, "n": sum(i["n"] for i in items),
                                "sum": sum(i["sum"] for i in items), "items": items, "sent_at": last.get((aid, oid)),
                                "code": code, "code_at": a.pickup_code_at if code else None, "syncing": a.polling})
    return sorted(offices.values(), key=lambda x: x["address"])


@app.post("/api/pickups/codes", dependencies=PICKUP)
async def pickups_codes(u=Depends(user), s=Depends(db)):
    """«Обновить коды»: всем аккаунтам окна — внеочередной HTTP-опрос (флаг polling, как ↻ «Обновить»): код берётся
    всегда, секунды на аккаунт."""
    ids = {aid for aid, _ in await _pickup_groups(s, u)}
    if not ids:
        raise HTTPException(409, "в окне нет аккаунтов — обновлять нечего")
    w = models.WbAccount
    await s.execute(update(w).where(w.id.in_(ids)).values(polling=True))
    await s.commit()
    return {"accounts": len(ids)}


class PickupPair(BaseModel):
    account_id: int
    office_id: int


class PickupSendIn(BaseModel):
    pairs: list[PickupPair] = Field(min_length=1, max_length=500)


@app.post("/api/pickups/send", status_code=201, dependencies=PICKUP)
async def pickups_send(body: PickupSendIn, u=Depends(user), s=Depends(db)):
    """Пары «аккаунт + ПВЗ» в очередь (шлёт worker). rids и товары — с сервера; строки аккаунтов под FOR UPDATE:
    двойной клик не отправит то же второй раз."""
    w = models.WbAccount
    ids = {x.account_id for x in body.pairs}
    await s.execute(select(w.id).where(w.id.in_(ids), w.user_id == u.telegram_id).order_by(w.id).with_for_update())
    groups = await _pickup_groups(s, u, ids)
    rows = [models.PickupSend(user_id=u.telegram_id, account_id=aid, office_id=oid, address=g["address"],
                              rids=[x.rid for x in g["orders"]], items=pickup_items(g["orders"]))
            for aid, oid in dict.fromkeys((x.account_id, x.office_id) for x in body.pairs) if (g := groups.get((aid, oid)))]
    if not rows:
        raise HTTPException(409, "нечего отправлять — товары уже забраны или ещё отправляются, обновите окно")
    s.add_all(rows)
    await s.commit()
    return {"ids": [x.id for x in rows]}


@app.get("/api/pickups/sends")
async def pickups_sends(ids: list[int] = Query(..., max_length=500), u=Depends(user), s=Depends(db)):
    """Экран после «Отправить»: пары (аккаунт, адрес, товары) и статус; need_code — кода на сегодня ещё нет (worker идёт
    за ним в браузере)."""
    p, w = models.PickupSend, models.WbAccount
    rows = await s.execute(select(p, w).outerjoin(w, w.id == p.account_id)
                           .where(p.id.in_(ids), p.user_id == u.telegram_id).order_by(p.id))
    today = repo.code_today()
    return [{"id": x.id, "account_id": x.account_id, "account_title": a and a.title, "office_id": x.office_id,
             "address": x.address, "items": x.items, "status": x.status,
             "error": x.error, "sent_at": x.sent_at, "need_code": not (a and a.pickup_code and a.pickup_code_date == today)}
            for x, a in rows.all()]


# ---------- «Журнал действий» (всё из БД; «Обновить» — флаг для worker'а) ----------
JOURNAL_MAX = 5000
CANCEL_WHY = {122: "отменили сами", 101: "не забрали вовремя", 666: "не оплачен"}  # 101 — гипотеза (expiredDg)


def _order_uid(rid: str) -> str:
    """rid «<стикер>.<заказ>.<товар>.<n>» → заказ (как OrderId архива)."""
    parts = rid.split(".")
    return parts[1] if len(parts) >= 3 else rid


async def _journal_ordered(s, ids, within) -> list[dict]:
    """«Заказали»: активные (ordered_at) ∪ архив (created_at) — по (аккаунт, заказ, артикул), штуки по rid без повторов."""
    o, p = models.AccountOrder, models.AccountPurchase
    groups: dict[tuple, dict] = {}
    rows = [*(await s.execute(select(o.account_id, o.rid, o.nm_id, o.name, o.office_address, o.ordered_at)
                                .where(o.account_id.in_(ids), *within(o.ordered_at)))).all(),
            *(await s.execute(select(p.account_id, p.rid, p.nm_id, p.name, p.office_address, p.created_at)
                                .where(p.account_id.in_(ids), *within(p.created_at)))).all()]
    for aid, rid, nm, name, addr, at in rows:
        g = groups.setdefault((aid, _order_uid(rid), nm), {"at": at, "rids": set(), "name": name, "address": addr})
        g["rids"].add(rid)
        g["at"], g["address"], g["name"] = min(g["at"], at), g["address"] or addr, g["name"] or name
    return [{"type": "ordered", "at": g["at"], "account_id": k[0],
             "text": f"Заказ: {g['name'] or f'арт. {k[2]}'} × {len(g['rids'])}" + (f" · {g['address']}" if g["address"] else "")}
            for k, g in groups.items()]


async def _journal_cancelled(s, ids, within) -> list[dict]:
    p = models.AccountPurchase
    groups: dict[tuple, dict] = {}
    for x in await s.scalars(select(p).where(p.account_id.in_(ids), p.status.in_(account_api.PURCHASE_CANCELLED),
                                              *within(p.dt))):
        g = groups.setdefault((x.account_id, _order_uid(x.rid), x.nm_id, x.status_id), {"at": x.dt, "n": 0, "name": x.name})
        g["n"] += 1
        g["at"] = max(g["at"], x.dt)
    return [{"type": "cancelled", "at": g["at"], "account_id": k[0],
             "text": f"Отмена: {g['name'] or f'арт. {k[2]}'} × {g['n']}" + (f" · {CANCEL_WHY[k[3]]}" if k[3] in CANCEL_WHY else "")}
            for k, g in groups.items()]


@app.get("/api/journal")
async def journal(date_from: date | None = Query(None, alias="from"), date_to: date | None = Query(None, alias="to"),
                  account_id: int | None = None, u=Depends(user), s=Depends(db)):
    """Журнал действий по всем аккаунтам из БД (WB не трогаем): заказали, отменили, оплатили, сертификаты, заборы.
    from/to — дни по МСК включительно (нет — без границы); новые сверху; counts — по типам за период."""
    accs = {a.id: a for a in await repo.user_accounts(s, u.telegram_id)}
    if account_id is not None and account_id not in accs:
        raise HTTPException(404, "нет такого аккаунта")
    ids = [account_id] if account_id is not None else list(accs)
    lo = _msk_start(date_from) if date_from else None
    hi = _msk_start(date_to + timedelta(days=1)) if date_to else None
    within = lambda col: [col.is_not(None), *([col >= lo] if lo else []), *([col < hi] if hi else [])]
    pay, cert, ps = models.OrderPayment, models.Certificate, models.PickupSend
    ev = [*await _journal_ordered(s, ids, within), *await _journal_cancelled(s, ids, within)]
    ev += [{"type": "paid", "at": x.done_at, "account_id": x.account_id,
            "text": f"Оплата товаров на {x.amount:,} ₽ · {len(x.rids)} шт.".replace(",", " ")}
           for x in await s.scalars(select(pay).where(pay.account_id.in_(ids), pay.status == "ok", *within(pay.done_at)))]
    ev += [{"type": "cert", "at": x.done_at, "account_id": x.account_id,
            "text": f"Сертификат добавлен: +{(x.nominal or x.nominal_entered):,} ₽".replace(",", " ")}
           for x in await s.scalars(select(cert).where(cert.account_id.in_(ids), cert.status == "ok", *within(cert.done_at)))]
    ev += [{"type": "pickup", "at": x.sent_at, "account_id": x.account_id,
            "text": f"Забор: {sum(i['n'] for i in x.items)} шт. · {x.address or f'ПВЗ №{x.office_id}'}"}
           for x in await s.scalars(select(ps).where(ps.account_id.in_(ids), ps.status == "sent", *within(ps.sent_at)))]
    if account_id is None:  # режим работ — не про аккаунт: только в общем списке
        raw = await repo.get_setting(s, repo.MAINTENANCE_LOG)
        for x in json.loads(raw) if raw else []:
            at = datetime.fromisoformat(x["at"])
            if (lo is None or at >= lo) and (hi is None or at < hi):
                names = (repo.MAINTENANCE_GROUPS[g].split(":")[0] for g in x["blocked"])
                what = ", ".join(t[:1].lower() + t[1:] for t in names) or "ничего"  # «обновление из WB»
                ev.append({"type": "maint", "at": at, "account_id": None,
                           "text": (f"Технические работы включены · закрыто: {what}"
                                    + (" · фоновый опрос WB на паузе" if x["poll_paused"] else "")
                                    if x["on"] else "Технические работы выключены") + f" · ID {x['by']}"})
    ev.sort(key=lambda e: e["at"], reverse=True)
    counts = {t: sum(e["type"] == t for e in ev) for t in ("ordered", "cancelled", "paid", "cert", "pickup")}
    raw = await repo.get_setting(s, repo.JOURNAL_CHECK)
    return {"events": [{**e, "account_title": accs[e["account_id"]].title if e["account_id"] else "—"} for e in ev[:JOURNAL_MAX]],
            "counts": {"all": len(ev), **counts}, "check": json.loads(raw) if raw else None}


@app.post("/api/journal/refresh", dependencies=SYNC)
async def journal_refresh(u=Depends(user), s=Depends(db)):
    """«Обновить»: флаг для worker'а — заказы всех аккаунтов (API в WB не ходит). Уже идёт — 409."""
    raw = await repo.get_setting(s, repo.JOURNAL_CHECK)
    if raw and json.loads(raw).get("state") in ("requested", "running"):
        raise HTTPException(409, "обновление уже идёт")
    await repo.set_setting(s, repo.JOURNAL_CHECK, json.dumps({"state": "requested", "at": datetime.now(timezone.utc).isoformat()}))
    await s.commit()
    return {"ok": True}


# ---------- уведомления WB (колокольчик) ----------
NOTICE_FIELDS = ("id", "account_id", "topic_id", "title", "text", "url", "dt", "read_at")


@app.get("/api/notices")
async def notices(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), u=Depends(user), s=Depends(db)):
    """Лента всех аккаунтов, новые сверху. ponytail: offset — при новых сверху «Показать ещё» может повторить строку
    (сайт убирает дубли по id); курсор по (dt, id) — если станет мешать."""
    m, a = models.AccountNotice, models.WbAccount
    rows = await s.execute(select(m, a).join(a, a.id == m.account_id).where(a.user_id == u.telegram_id)
                           .order_by(m.dt.desc().nulls_last(), m.id.desc()).limit(limit).offset(offset))
    return [{**{f: getattr(n, f) for f in NOTICE_FIELDS}, "account_title": acc.title} for n, acc in rows.all()]


async def _check_state(s) -> dict | None:
    raw = await repo.get_setting(s, repo.NOTICES_CHECK)
    return json.loads(raw) if raw else None


@app.get("/api/notices/unread")
async def notices_unread(u=Depends(user), s=Depends(db)):
    """Бейдж колокольчика + состояние «Проверить сейчас» — одним запросом (сайт опрашивает раз в 30 с)."""
    m, a = models.AccountNotice, models.WbAccount
    n = await s.scalar(select(func.count()).select_from(m).join(a, a.id == m.account_id)
                       .where(a.user_id == u.telegram_id, m.read_at.is_(None)))
    return {"count": n, "check": await _check_state(s)}


class ReadIn(BaseModel):
    ids: list[int] | None = Field(None, max_length=1000)  # None — все


@app.post("/api/notices/read")
async def notices_read(body: ReadIn, u=Depends(user), s=Depends(db)):
    m = models.AccountNotice
    mine = select(models.WbAccount.id).where(models.WbAccount.user_id == u.telegram_id)
    q = update(m).where(m.account_id.in_(mine), m.read_at.is_(None))
    if body.ids is not None:
        q = q.where(m.id.in_(body.ids))
    res = await s.execute(q.values(read_at=func.now()))
    await s.commit()
    return {"count": res.rowcount}


@app.post("/api/notices/check", dependencies=SYNC)
async def notices_check(u=Depends(user), s=Depends(db)):
    """«↻ Проверить сейчас»: флаг для worker'а (API в WB не ходит). Уже идёт — 409."""
    st = await _check_state(s)
    if st and st.get("state") in ("requested", "running"):
        raise HTTPException(409, "проверка уже идёт")
    await repo.set_setting(s, repo.NOTICES_CHECK, json.dumps({"state": "requested", "at": datetime.now(timezone.utc).isoformat()}))
    await s.commit()
    return {"ok": True}


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
