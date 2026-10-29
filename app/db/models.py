from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class User(Base):
    """Пользователи с доступом к боту. role: owner | user."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Seller(Base):
    """Общий список отслеживаемых магазинов WB."""

    __tablename__ = "sellers"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # режим цены: True — бизнес-цена (b2b, нужен Bearer), False — розничная
    b2b: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")


class Product(Base):
    """Снапшот товара магазина (общий, не по пользователю)."""

    __tablename__ = "products"

    supplier_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sellers.supplier_id", ondelete="CASCADE"),
        primary_key=True,
    )
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # «наша» цена (detail)
    shelf_price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # витрина (каталог)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pics: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_seller: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # предмет WB
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def url(self) -> str:
        return f"https://www.wildberries.ru/catalog/{self.nm_id}/detail.aspx"


class AppSetting(Base):
    """Key-value рантайм-настройки (напр. актуальные токены WB)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class WbSession(Base):
    """Залогиненный аккаунт WB юзера бота (один аккаунт на юзера).

    shard_key/session_id не храним — они есть внутри JWT access_token.
    """

    __tablename__ = "wb_sessions"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    wb_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)  # Bearer
    # куки браузера (Playwright storageState) — по wbid-refresh обновляем токен без SMS
    storage_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # active | expired | revoked
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserSeller(Base):
    """Какие магазины отслеживает конкретный юзер."""

    __tablename__ = "user_sellers"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    supplier_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sellers.supplier_id", ondelete="CASCADE"),
        primary_key=True,
    )
    # режим цены: True — бизнес-цена под аккаунтом юзера, False — розничная
    b2b: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserProductPrice(Base):
    """Бизнес-цена товара под аккаунтом юзера (у каждого аккаунта своя)."""

    __tablename__ = "user_product_prices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["supplier_id", "nm_id"],
            ["products.supplier_id", "products.nm_id"],
            ondelete="CASCADE",
        ),
    )

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        primary_key=True,
    )
    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    b2b_price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = цены ещё нет
    last_priced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Notified(Base):
    """Глобальная защита от повторных уведомлений о новинке."""

    __tablename__ = "notified"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
