"""оплата заказов с WB Кошелька: очередь оплат с сайта

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('rids', postgresql.JSONB(), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('error_text', sa.String(256), nullable=True),
        sa.Column('funds_before', sa.Integer(), nullable=True),
        sa.Column('funds_after', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_order_payments_account_id', 'order_payments', ['account_id'])
    op.create_index('ix_order_payments_status', 'order_payments', ['status'])


def downgrade() -> None:
    op.drop_table('order_payments')
