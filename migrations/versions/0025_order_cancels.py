"""отмена позиций из группы ПВЗ (order_cancels); итог отмены — по факту: autobuy_orders.cancel_at, wb_accounts.orders_at

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_cancels',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('rids', postgresql.JSONB(), nullable=False),
        sa.Column('items', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('started', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('error', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_order_cancels_account_id', 'order_cancels', ['account_id'])
    op.create_index('ix_order_cancels_status', 'order_cancels', ['status'])
    op.add_column('autobuy_orders', sa.Column('cancel_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('wb_accounts', sa.Column('orders_at', sa.DateTime(timezone=True), nullable=True))
    # «отменён» раньше ставили по reject_initialized — это лишь «запрошена»: итог теперь считается по архиву и заказам
    op.execute("UPDATE autobuy_orders SET cancel_state = 'asked', cancel_at = coalesce(done_at, created_at) "
               "WHERE status = 'cancelled'")


def downgrade() -> None:
    op.execute("UPDATE autobuy_orders SET status = 'cancelled', cancel_state = NULL WHERE cancel_state = 'asked'")
    op.drop_column('wb_accounts', 'orders_at')
    op.drop_column('autobuy_orders', 'cancel_at')
    op.drop_index('ix_order_cancels_status', 'order_cancels')
    op.drop_index('ix_order_cancels_account_id', 'order_cancels')
    op.drop_table('order_cancels')
