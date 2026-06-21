from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
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
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)  # рубли
    stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pics: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


class Notified(Base):
    """Глобальная защита от повторных уведомлений о новинке."""

    __tablename__ = "notified"

    supplier_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nm_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
