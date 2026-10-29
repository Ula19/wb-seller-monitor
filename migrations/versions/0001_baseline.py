"""базовая схема (как было до Alembic: create_all + ALTER в main.py)

Идемпотентная: на чистой базе создаёт таблицы, на существующей (копия боевой)
ничего не ломает и лишь добавляет колонки, которых могло не быть.

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _now():
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "sellers",
        sa.Column("supplier_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(256), nullable=True),
        sa.Column("brand", sa.String(256), nullable=True),
        sa.Column("added_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("b2b", sa.Boolean(), server_default="true", nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "products",
        sa.Column("supplier_id", sa.BigInteger(),
                  sa.ForeignKey("sellers.supplier_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("nm_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("brand", sa.String(256), nullable=True),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column("shelf_price", sa.Integer(), nullable=True),
        sa.Column("stock", sa.Integer(), nullable=True),
        sa.Column("pics", sa.Integer(), nullable=True),
        sa.Column("delivery_hours", sa.Integer(), nullable=True),
        sa.Column("from_seller", sa.Boolean(), nullable=True),
        sa.Column("subject_id", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.create_table(
        "notified",
        sa.Column("supplier_id", sa.BigInteger(), primary_key=True),
        sa.Column("nm_id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        if_not_exists=True,
    )
    # старые базы могли быть созданы до появления этих колонок — добиваем
    for col, typ in (("delivery_hours", "INTEGER"), ("from_seller", "BOOLEAN"),
                     ("subject_id", "INTEGER"), ("shelf_price", "INTEGER")):
        op.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col} {typ}")
    op.execute("ALTER TABLE sellers DROP COLUMN IF EXISTS is_fast")
    op.execute("ALTER TABLE sellers ADD COLUMN IF NOT EXISTS b2b BOOLEAN NOT NULL DEFAULT TRUE")


def downgrade() -> None:
    for name in ("notified", "app_settings", "products", "sellers", "users"):
        op.drop_table(name)
