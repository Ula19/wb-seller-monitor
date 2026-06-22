"""Доступ к данным: запросы и CRUD-помощники."""

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import models
from app.wb.parser import NormProduct


# ---------- пользователи ----------
# Владелец живёт только в .env (settings.owner_id) и в БД не пишется —
# в БД лежат лишь выданные через /grant пользователи (whitelist).
async def list_user_ids(s) -> list[int]:
    res = await s.execute(select(models.User.telegram_id))
    return [r[0] for r in res.all()]


async def list_users(s) -> list[models.User]:
    res = await s.execute(select(models.User).order_by(models.User.created_at))
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


# ---------- магазины ----------
async def add_seller(s, supplier_id: int, name=None, brand=None) -> bool:
    if await s.get(models.Seller, supplier_id):
        return False
    s.add(models.Seller(supplier_id=supplier_id, name=name, brand=brand))
    return True


async def remove_seller(s, supplier_id: int) -> bool:
    sl = await s.get(models.Seller, supplier_id)
    if sl:
        await s.delete(sl)
        return True
    return False


async def get_seller(s, supplier_id: int) -> models.Seller | None:
    return await s.get(models.Seller, supplier_id)


async def list_sellers(s) -> list[models.Seller]:
    res = await s.execute(select(models.Seller).order_by(models.Seller.added_at))
    return list(res.scalars().all())


# ---------- товары ----------
async def get_product_ids(s, supplier_id: int) -> set[int]:
    res = await s.execute(
        select(models.Product.nm_id).where(models.Product.supplier_id == supplier_id)
    )
    return {r[0] for r in res.all()}


async def get_products(s, supplier_id: int) -> list[models.Product]:
    res = await s.execute(
        select(models.Product).where(models.Product.supplier_id == supplier_id)
    )
    return list(res.scalars().all())


async def upsert_product(s, p: NormProduct) -> None:
    stmt = pg_insert(models.Product).values(
        supplier_id=p.supplier_id,
        nm_id=p.nm_id,
        name=p.name,
        brand=p.brand,
        price=p.price,
        stock=p.stock,
        pics=p.pics,
        delivery_hours=p.delivery_hours,
        from_seller=p.from_seller,
        is_active=True,
        last_seen_at=func.now(),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[models.Product.supplier_id, models.Product.nm_id],
        set_=dict(
            name=stmt.excluded.name,
            brand=stmt.excluded.brand,
            price=stmt.excluded.price,
            stock=stmt.excluded.stock,
            pics=stmt.excluded.pics,
            delivery_hours=stmt.excluded.delivery_hours,
            from_seller=stmt.excluded.from_seller,
            is_active=True,
            last_seen_at=func.now(),
        ),
    )
    await s.execute(stmt)


async def deactivate_missing(s, supplier_id: int, seen_ids: set[int]) -> None:
    """Помечает is_active=False товары, которых нет в текущей выдаче."""
    if not seen_ids:
        return
    await s.execute(
        update(models.Product)
        .where(
            models.Product.supplier_id == supplier_id,
            models.Product.nm_id.not_in(seen_ids),
        )
        .values(is_active=False)
    )


async def count_active_products(s, supplier_id: int) -> int:
    return await s.scalar(
        select(func.count()).select_from(models.Product).where(
            models.Product.supplier_id == supplier_id,
            models.Product.is_active.is_(True),
        )
    ) or 0


async def get_active_products(s, supplier_id: int) -> list[models.Product]:
    res = await s.execute(
        select(models.Product)
        .where(
            models.Product.supplier_id == supplier_id,
            models.Product.is_active.is_(True),
        )
        .order_by(func.lower(models.Product.name))  # по названию: похожие товары рядом
    )
    return list(res.scalars().all())


# ---------- защита от дублей ----------
async def is_notified(s, supplier_id: int, nm_id: int) -> bool:
    return await s.get(models.Notified, (supplier_id, nm_id)) is not None


async def mark_notified(s, supplier_id: int, nm_id: int) -> None:
    if not await is_notified(s, supplier_id, nm_id):
        s.add(models.Notified(supplier_id=supplier_id, nm_id=nm_id))


# ---------- статистика ----------
async def stats(s) -> tuple[int, int, int]:
    sellers = await s.scalar(select(func.count()).select_from(models.Seller))
    products = await s.scalar(select(func.count()).select_from(models.Product))
    users = await s.scalar(select(func.count()).select_from(models.User))
    return sellers or 0, products or 0, users or 0
