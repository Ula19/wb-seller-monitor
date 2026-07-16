from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
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
    # режим цены: True — бизнес-цена (b2b, нужна кука), False — розничная
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
    """Key-value рантайм-настройки (напр. актуальная WB-кука)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class Notified(Base):
    """Глобальная защита от повторных уведомлений о новинке."""

    __tablename__ = "notified"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
