"""Доступ к данным: запросы и CRUD-помощники (схема v2)."""

from datetime import datetime, timezone

from sqlalchemy import and_, delete, func, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import models
from app.wb.parser import NormProduct


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- пользователи ----------
# Владелец задаётся в .env (settings.owner_id); в users у него строка role=owner
# (на неё ссылаются его источники и аккаунты), остальные — выданные через /grant.
async def ensure_owner(s, uid: int) -> None:
    stmt = pg_insert(models.User).values(telegram_id=uid, role="owner")
    await s.execute(stmt.on_conflict_do_update(index_elements=[models.User.telegram_id],
                                               set_={"role": "owner"}))


async def list_user_ids(s) -> list[int]:
    res = await s.execute(select(models.User.telegram_id))
    return [r[0] for r in res.all()]


async def list_users(s) -> list[models.User]:
    """Whitelist для админки — без владельца."""
    res = await s.execute(
        select(models.User).where(models.User.role != "owner").order_by(models.User.created_at)
    )
    return list(res.scalars().all())


async def add_user(s, uid: int, username: str | None = None) -> bool:
    if await s.get(models.User, uid):
        return False
    s.add(models.User(telegram_id=uid, username=username, role="user"))
    return True


async def remove_user(s, uid: int) -> bool:
    u = await s.get(models.User, uid)
    if u and u.role != "owner":
        await s.delete(u)
        return True
    return False


# ---------- источники-продавцы ----------
async def add_source(s, uid: int, supplier_id: int, *, name=None, brand=None, b2b: bool) -> bool:
    """Пользователь начинает следить за продавцом. False — уже следит."""
    if await get_source(s, uid, supplier_id):
        return False
    stmt = pg_insert(models.Seller).values(supplier_id=supplier_id, name=name, brand=brand)
    await s.execute(stmt.on_conflict_do_nothing(index_elements=[models.Seller.supplier_id]))
    s.add(models.Source(user_id=uid, kind="seller", supplier_id=supplier_id, b2b=b2b))
    await s.flush()
    return True


async def remove_source(s, uid: int, supplier_id: int) -> bool:
    """Продавец и его товары остаются: за ними могут следить другие (и история цен)."""
    res = await s.execute(delete(models.Source).where(
        models.Source.user_id == uid, models.Source.kind == "seller",
        models.Source.supplier_id == supplier_id))
    return res.rowcount > 0


async def get_source(s, uid: int, supplier_id: int) -> models.Source | None:
    return await s.scalar(select(models.Source).where(
        models.Source.user_id == uid, models.Source.kind == "seller",
        models.Source.supplier_id == supplier_id))


async def list_sources(s, uid: int) -> list[models.Source]:
    res = await s.execute(select(models.Source).where(
        models.Source.user_id == uid, models.Source.kind == "seller",
    ).order_by(models.Source.created_at))
    return list(res.scalars().all())


async def set_source_b2b(s, uid: int, supplier_id: int, value: bool) -> bool:
    src = await get_source(s, uid, supplier_id)
    if src:
        src.b2b = value
        return True
    return False


async def monitored_supplier_ids(s) -> list[int]:
    """Продавцы, за которыми следит хоть один включённый источник."""
    res = await s.execute(select(models.Source.supplier_id).where(
        models.Source.kind == "seller", models.Source.enabled.is_(True),
    ).group_by(models.Source.supplier_id).order_by(func.min(models.Source.created_at)))
    return [r[0] for r in res.all()]


async def seller_sources(s, supplier_id: int) -> tuple[models.Seller | None, list[models.Source]]:
    res = await s.execute(select(models.Source).where(
        models.Source.kind == "seller", models.Source.supplier_id == supplier_id,
        models.Source.enabled.is_(True)))
    return await s.get(models.Seller, supplier_id), list(res.scalars().all())


async def mark_seeded(s, source_ids: list[int]) -> None:
    if source_ids:
        # now() транзакции = время событий первой загрузки → лента (at > seeded_at) их не покажет
        await s.execute(update(models.Source).where(models.Source.id.in_(source_ids))
                        .values(seeded_at=func.now()))


async def set_seller_info(s, supplier_id: int, name: str | None, brand: str | None) -> None:
    await s.execute(update(models.Seller).where(models.Seller.supplier_id == supplier_id)
                    .values(name=name, brand=brand))


async def touch_seller(s, supplier_id: int) -> None:
    await s.execute(update(models.Seller).where(models.Seller.supplier_id == supplier_id)
                    .values(last_check_at=_utcnow()))


# ---------- товары ----------
async def get_items(s, supplier_id: int) -> dict[int, models.Item]:
    res = await s.execute(select(models.Item).where(models.Item.supplier_id == supplier_id))
    return {it.nm_id: it for it in res.scalars().all()}


async def upsert_items(s, products: list[NormProduct]) -> None:
    """Снимок каталога в items. Цена — розница (витрина), бизнес-цены — в account_prices."""
    if not products:
        return
    rows = [dict(nm_id=p.nm_id, supplier_id=p.supplier_id, name=p.name, brand=p.brand,
                 subject_id=p.subject_id, pics=p.pics, price=p.shelf_price, stock=p.stock,
                 delivery_hours=p.delivery_hours, from_seller=p.from_seller,
                 is_active=True, last_seen_at=_utcnow()) for p in products]
    stmt = pg_insert(models.Item).values(rows)
    cols = ("supplier_id", "name", "brand", "subject_id", "pics", "price", "stock",
            "delivery_hours", "from_seller", "is_active", "last_seen_at")
    await s.execute(stmt.on_conflict_do_update(
        index_elements=[models.Item.nm_id], set_={c: stmt.excluded[c] for c in cols}))


async def add_history(s, products: list[NormProduct], event: str = "change") -> None:
    if products:
        await s.execute(pg_insert(models.PriceHistory).values(
            [dict(nm_id=p.nm_id, price=p.shelf_price, stock=p.stock, event=event) for p in products]))


async def deactivate_missing(s, supplier_id: int, seen_ids: set[int]) -> int:
    """Пропавшие из выдачи → is_active=False + событие gone. Пустая выдача гасит все (магазин распродан)."""
    res = await s.execute(update(models.Item).where(
        models.Item.supplier_id == supplier_id, models.Item.is_active.is_(True),
        models.Item.nm_id.not_in(seen_ids) if seen_ids else true(),
    ).values(is_active=False).returning(models.Item.nm_id, models.Item.price))
    gone = res.all()
    if gone:
        await s.execute(pg_insert(models.PriceHistory).values(
            [dict(nm_id=nm, price=price, stock=0, event="gone") for nm, price in gone]))
    return len(gone)


async def feed(s, uid: int, limit: int = 100) -> list:
    """Лента пользователя: события товаров его продавцов после первой загрузки источника.

    Строки: nm_id, event, price, stock, at, old_price, old_stock (прошлое событие), name, supplier_id.
    """
    # ponytail: lag по всей истории продавцов пользователя на каждый запрос; при росте — материализовать
    h = models.PriceHistory
    w = {"partition_by": h.nm_id, "order_by": (h.at, h.id)}
    inner = (select(h.id, h.nm_id, h.event, h.price, h.stock, h.at,
                    func.lag(h.price).over(**w).label("old_price"),
                    func.lag(h.stock).over(**w).label("old_stock"),
                    models.Item.name, models.Item.supplier_id, models.Source.seeded_at)
             .join(models.Item, models.Item.nm_id == h.nm_id)
             .join(models.Source, and_(models.Source.supplier_id == models.Item.supplier_id,
                                       models.Source.user_id == uid, models.Source.kind == "seller"))
             ).subquery()
    res = await s.execute(select(inner).where(inner.c.seeded_at.is_not(None), inner.c.at > inner.c.seeded_at)
                          .order_by(inner.c.at.desc(), inner.c.id.desc()).limit(limit))
    return list(res.all())


async def price_ago(s, nm_ids, at: datetime) -> dict[int, tuple[int | None, bool]]:
    """nm_id → (розничная цена на момент `at`, товар новее `at`). Новому Δ не считаем."""
    nm_ids = list(nm_ids)
    if not nm_ids:
        return {}
    h = models.PriceHistory
    last = (select(h.price).where(h.nm_id == models.Item.nm_id, h.at <= at)
            .order_by(h.at.desc(), h.id.desc()).limit(1).scalar_subquery())
    res = await s.execute(select(models.Item.nm_id, last, models.Item.first_seen_at > at)
                          .where(models.Item.nm_id.in_(nm_ids)))
    return {nm: (price, new) for nm, price, new in res.all()}


async def count_active_items(s, supplier_id: int) -> int:
    return await s.scalar(select(func.count()).select_from(models.Item).where(
        models.Item.supplier_id == supplier_id, models.Item.is_active.is_(True))) or 0


async def source_products(s, src: models.Source) -> list[NormProduct]:
    """Активные товары источника глазами его владельца (для отчётов).

    Бизнес-режим: цена — под бизнес-аккаунтом владельца, витрина — розница;
    нет бизнес-цены — price=None и price_stale (отчёт покажет «не подтверждена»).
    """
    res = await s.execute(select(models.Item).where(
        models.Item.supplier_id == src.supplier_id, models.Item.is_active.is_(True),
    ).order_by(func.lower(models.Item.name)))  # по названию: похожие товары рядом
    items = list(res.scalars().all())
    biz: dict[int, int] = {}
    if src.b2b:
        acc = await business_account(s, src.user_id)
        if acc:
            biz = await get_account_prices(s, acc.id, [it.nm_id for it in items])
    out = []
    for it in items:
        price = biz.get(it.nm_id) if src.b2b else it.price
        out.append(NormProduct(
            nm_id=it.nm_id, name=it.name or "", brand=it.brand, price=price, stock=it.stock or 0,
            pics=it.pics or 0, supplier_id=it.supplier_id, delivery_hours=it.delivery_hours,
            from_seller=it.from_seller, subject_id=it.subject_id, shelf_price=it.price,
            price_stale=src.b2b and price is None))
    return out


# ---------- аккаунты WB ----------
async def business_account(s, uid: int) -> models.WbAccount | None:
    """Бизнес-аккаунт пользователя для бизнес-цен (если их несколько — первый)."""
    # ponytail: первый по id; выбор «основного» аккаунта — когда появится сайт
    return await s.scalar(select(models.WbAccount).where(
        models.WbAccount.user_id == uid, models.WbAccount.kind == "business",
        models.WbAccount.status == "active").order_by(models.WbAccount.id).limit(1))


async def user_accounts(s, uid: int) -> list[models.WbAccount]:
    res = await s.execute(select(models.WbAccount).where(models.WbAccount.user_id == uid)
                          .order_by(models.WbAccount.id))
    return list(res.scalars().all())


async def account_orders(s, account_ids: list[int]) -> list[models.AccountOrder]:
    if not account_ids:
        return []
    res = await s.execute(select(models.AccountOrder).where(models.AccountOrder.account_id.in_(account_ids))
                          .order_by(models.AccountOrder.ordered_at.desc().nulls_last(), models.AccountOrder.rid))
    return list(res.scalars().all())


async def active_accounts(s) -> list[models.WbAccount]:
    res = await s.execute(select(models.WbAccount).where(models.WbAccount.status == "active")
                          .order_by(models.WbAccount.id))
    return list(res.scalars().all())


async def save_account(s, uid: int, phone_mask: str, kind: str, state_enc: bytes) -> models.WbAccount:
    """Вход в аккаунт: тот же номер у пользователя — обновляем, иначе новый аккаунт."""
    acc = await s.scalar(select(models.WbAccount).where(
        models.WbAccount.user_id == uid, models.WbAccount.phone_mask == phone_mask))
    if acc is None:
        acc = models.WbAccount(user_id=uid, phone_mask=phone_mask, label=phone_mask)
        s.add(acc)
    acc.kind, acc.state_enc, acc.status = kind, state_enc, "active"
    await s.flush()
    return acc


async def get_account_prices(s, account_id: int, nm_ids) -> dict[int, int]:
    nm_ids = list(nm_ids)
    if not nm_ids:
        return {}
    res = await s.execute(select(models.AccountPrice.nm_id, models.AccountPrice.price).where(
        models.AccountPrice.account_id == account_id, models.AccountPrice.nm_id.in_(nm_ids)))
    return {nm: price for nm, price in res.all() if price is not None}


async def upsert_account_prices(s, account_id: int, prices: dict[int, int]) -> None:
    if not prices:
        return
    stmt = pg_insert(models.AccountPrice).values(
        [dict(account_id=account_id, nm_id=nm, price=pr, priced_at=_utcnow()) for nm, pr in prices.items()])
    await s.execute(stmt.on_conflict_do_update(
        index_elements=[models.AccountPrice.account_id, models.AccountPrice.nm_id],
        set_={"price": stmt.excluded.price, "priced_at": stmt.excluded.priced_at}))


ACCOUNT_STATE_FIELDS = ("wallet_pct", "limit_left", "monthly_limit", "max_order", "max_rid", "wallet_balance", "bonus_balance")


async def save_account_state(s, account_id: int, st: dict) -> None:
    """Состояние из account_api.fetch: поля аккаунта + активные заказы (пропавшие rid — в архив, удаляем)."""
    await s.execute(update(models.WbAccount).where(models.WbAccount.id == account_id).values(
        state_at=_utcnow(), **{k: st.get(k) for k in ACCOUNT_STATE_FIELDS}))
    orders = st.get("orders") or []
    await s.execute(delete(models.AccountOrder).where(
        models.AccountOrder.account_id == account_id,
        models.AccountOrder.rid.not_in([o["rid"] for o in orders]) if orders else true()))
    if orders:
        cols = ("nm_id", "name", "brand", "price", "dst_office_id", "status_name", "group_id",
                "status_at", "ordered_at", "expiry_at")
        stmt = pg_insert(models.AccountOrder).values(
            [{"account_id": account_id, "rid": o["rid"], **{c: o.get(c) for c in cols}} for o in orders])
        await s.execute(stmt.on_conflict_do_update(
            index_elements=[models.AccountOrder.account_id, models.AccountOrder.rid],
            set_={c: stmt.excluded[c] for c in cols}))


# ---------- заявки на вход в аккаунт WB (сайт → worker) ----------
LOGIN_ACTIVE = ("new", "running", "wait_code")


async def last_login_request(s, uid: int) -> models.WbLoginRequest | None:
    return await s.scalar(select(models.WbLoginRequest).where(models.WbLoginRequest.user_id == uid)
                          .order_by(models.WbLoginRequest.id.desc()).limit(1))


async def next_login_request(s) -> models.WbLoginRequest | None:
    return await s.scalar(select(models.WbLoginRequest).where(models.WbLoginRequest.status == "new")
                          .order_by(models.WbLoginRequest.id).limit(1))


async def finish_login_request(s, req_id: int, status: str, *, error=None, account_id=None) -> None:
    """Итог заявки; номер и код больше не нужны — стираем."""
    await s.execute(update(models.WbLoginRequest).where(models.WbLoginRequest.id == req_id).values(
        status=status, error=error, account_id=account_id, phone=None, code=None))


async def fail_stale_logins(s) -> int:
    """Старт worker'а: заявки, которые вёл прошлый процесс, уже не доведёт никто."""
    res = await s.execute(update(models.WbLoginRequest).where(
        models.WbLoginRequest.status.in_(("running", "wait_code"))).values(
        status="error", error="вход прерван перезапуском, повторите", phone=None, code=None))
    return res.rowcount


# ---------- настройки (key-value) ----------
HEARTBEAT_KEY = "monitor_last_pass"  # пульс worker'а: пишет мониторинг, читает /api/status
BOT_USERNAME_KEY = "bot_username"  # для Telegram-виджета на сайте: пишет worker при старте

async def get_setting(s, key: str) -> str | None:
    row = await s.get(models.AppSetting, key)
    return row.value if row else None


async def set_setting(s, key: str, value: str) -> None:
    row = await s.get(models.AppSetting, key)
    if row:
        row.value = value
    else:
        s.add(models.AppSetting(key=key, value=value))


# ---------- статистика ----------
async def stats(s) -> tuple[int, int, int]:
    """Продавцов в мониторинге, активных товаров, пользователей (без владельца)."""
    sellers = len(await monitored_supplier_ids(s))
    items = await s.scalar(select(func.count()).select_from(models.Item)
                           .where(models.Item.is_active.is_(True)))
    users = await s.scalar(
        select(func.count()).select_from(models.User).where(models.User.role != "owner"))
    return sellers, items or 0, users or 0
