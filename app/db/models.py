"""Схема v2: товар глобален (по nm_id), пользователь следит за ним через источники.

Один скан WB обслуживает всех: items/price_history общие, sources — чьё и в каком
режиме цены, бизнес-цена — у каждого бизнес-аккаунта своя (account_prices).
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _now():
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """Пользователи с доступом. role: owner | user."""

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="user")
    created_at: Mapped[datetime] = _now()


class Seller(Base):
    """Продавец WB (данные с WB, общие для всех)."""

    __tablename__ = "sellers"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    added_at: Mapped[datetime] = _now()
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Item(Base):
    """Товар WB глобально + его текущий снимок (розница = витрина каталога).

    Не удаляем: пропал из выдачи — is_active=False. Поэтому «новинка» = nm_id,
    которого ещё нет в items.
    """

    __tablename__ = "items"

    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ponytail: без FK на sellers — поиск принесёт товары неотслеживаемых продавцов
    supplier_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pics: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # розница, ₽
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    from_seller: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    first_seen_at: Mapped[datetime] = _now()
    last_seen_at: Mapped[datetime] = _now()


class PriceHistory(Base):
    """Только изменения розничной цены/остатка (для графиков)."""

    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.nm_id", ondelete="CASCADE"), index=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    at: Mapped[datetime] = _now()


class Source(Base):
    """Что отслеживает пользователь. Пока kind=seller (поиск и товар — позже).

    seeded_at — когда прошла первая загрузка: до неё новинки не шлём, иначе весь
    ассортимент магазина пришёл бы «новинками».
    """

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("user_id", "kind", "supplier_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="seller", server_default="seller")
    supplier_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("sellers.supplier_id", ondelete="CASCADE"), nullable=True)
    b2b: Mapped[bool] = mapped_column(Boolean, default=False)  # режим цены: бизнес / розница
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    seeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _now()

    seller: Mapped[Seller | None] = relationship(lazy="joined")

    @property
    def name(self) -> str | None:
        return self.seller.name if self.seller else None


class WbAccount(Base):
    """Аккаунт WB пользователя (1–10 на пользователя).

    state_enc — storageState браузера ЦЕЛИКОМ (куки + localStorage с Bearer
    wbx__tokenData), зашифрован app.crypto. status: active | relogin | off.
    """

    __tablename__ = "wb_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    phone_mask: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16), default="business")  # business | retail
    state_enc: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = _now()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # состояние (account_api, раз в 10 мин), рубли; у бизнес-аккаунта может быть NULL
    wallet_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)  # % Кошелька
    limit_left: Mapped[int | None] = mapped_column(Integer, nullable=True)  # остаток месячного лимита скидки
    max_order: Mapped[int | None] = mapped_column(Integer, nullable=True)  # разовый лимит заказа
    max_rid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # лимит позиции
    wallet_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bonus_balance: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountOrder(Base):
    """Активная позиция заказа аккаунта (rid из wbxoofex). Ушла в архив — строку удаляем."""

    __tablename__ = "account_orders"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    rid: Mapped[str] = mapped_column(String(64), primary_key=True)
    nm_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(256), nullable=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # ₽
    dst_office_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # ПВЗ, адрес — потом
    status_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1 оформлен … 3 в пути, 5 в ПВЗ
    status_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expiry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AccountPrice(Base):
    """Бизнес-цена товара под конкретным аккаунтом."""

    __tablename__ = "account_prices"

    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("wb_accounts.id", ondelete="CASCADE"), primary_key=True)
    nm_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("items.nm_id", ondelete="CASCADE"), primary_key=True)
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priced_at: Mapped[datetime] = _now()


class AppSetting(Base):
    """Key-value рантайм-настройки (guardium, часы отчётов, рабочее окно)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
