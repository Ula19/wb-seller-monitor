"""цвет товара у позиции заказа (account_orders.color) — в тексте «Общего забора»

Revision ID: 0027
Revises: 0026
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('account_orders', sa.Column('color', sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column('account_orders', 'color')
