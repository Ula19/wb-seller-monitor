"""мультиаккаунт: wb_sessions, user_sellers, user_product_prices

Перенос данных, чтобы после обновления всё работало как раньше:
- владелец (OWNER_ID) получает строку в users — на неё ссылаются его сессия и магазины;
- каждый юзер (владелец + whitelist) подписан на все текущие магазины с их режимом b2b
  (раньше уведомления и так получали все);
- мобильный Bearer из app_settings становится сессией владельца.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from app.config import settings

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _user_fk():
    return sa.ForeignKey("users.telegram_id", ondelete="CASCADE")


def upgrade() -> None:
    op.create_table(
        "wb_sessions",
        sa.Column("telegram_id", sa.BigInteger(), _user_fk(), primary_key=True),
        sa.Column("wb_user_id", sa.BigInteger(), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("storage_state", JSONB(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "user_sellers",
        sa.Column("telegram_id", sa.BigInteger(), _user_fk(), primary_key=True),
        sa.Column("supplier_id", sa.BigInteger(),
                  sa.ForeignKey("sellers.supplier_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("b2b", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_table(
        "user_product_prices",
        sa.Column("telegram_id", sa.BigInteger(), _user_fk(), primary_key=True),
        sa.Column("supplier_id", sa.BigInteger(), primary_key=True),
        sa.Column("nm_id", sa.BigInteger(), primary_key=True),
        sa.Column("b2b_price", sa.Integer(), nullable=True),
        sa.Column("last_priced_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["supplier_id", "nm_id"],
                                ["products.supplier_id", "products.nm_id"], ondelete="CASCADE"),
    )

    op.execute(sa.text(
        "INSERT INTO users (telegram_id, role) VALUES (:owner, 'owner') "
        "ON CONFLICT (telegram_id) DO UPDATE SET role = 'owner'"
    ).bindparams(owner=settings.owner_id))
    op.execute(
        "INSERT INTO user_sellers (telegram_id, supplier_id, b2b) "
        "SELECT u.telegram_id, s.supplier_id, s.b2b FROM users u CROSS JOIN sellers s"
    )
    op.execute(sa.text(
        "INSERT INTO wb_sessions (telegram_id, access_token) "
        "SELECT :owner, value FROM app_settings WHERE key = 'wb_bearer' AND value <> ''"
    ).bindparams(owner=settings.owner_id))


def downgrade() -> None:
    for name in ("user_product_prices", "user_sellers", "wb_sessions"):
        op.drop_table(name)
    # строку владельца в users не трогаем: она безвредна для старой схемы
