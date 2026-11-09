"""заказы аккаунтов: плановая доставка, оплата, адрес и часы ПВЗ, скрытие на сайте

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None

COLS = ('delivery_at', 'paid', 'office_address', 'office_hours', 'hidden')


def upgrade() -> None:
    op.add_column('account_orders', sa.Column('delivery_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('account_orders', sa.Column('paid', sa.Boolean(), nullable=True))
    op.add_column('account_orders', sa.Column('office_address', sa.String(256), nullable=True))
    op.add_column('account_orders', sa.Column('office_hours', sa.String(64), nullable=True))
    op.add_column('account_orders', sa.Column('hidden', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    for c in reversed(COLS):
        op.drop_column('account_orders', c)
