"""заказ товара на аккаунты: очередь заказов autobuy_orders и запросы сайта к worker'у order_quotes

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'autobuy_orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('uid', sa.String(40), nullable=False, unique=True),
        sa.Column('nm', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(512), nullable=True),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('pvz_id', sa.BigInteger(), nullable=False),
        sa.Column('pay_now', sa.Boolean(), nullable=False),
        sa.Column('price_ok', sa.Integer(), nullable=False),
        sa.Column('total', sa.Integer(), nullable=True),
        sa.Column('final', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('error', sa.String(512), nullable=True),
        sa.Column('rids', postgresql.JSONB(), server_default='[]', nullable=False),
        sa.Column('cancel_state', sa.String(16), nullable=True),
        sa.Column('cancel_error', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_autobuy_orders_account_id', 'autobuy_orders', ['account_id'])
    op.create_index('ix_autobuy_orders_status', 'autobuy_orders', ['status'])
    op.create_index('ix_autobuy_orders_cancel_state', 'autobuy_orders', ['cancel_state'])
    op.create_table(
        'order_quotes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('request', postgresql.JSONB(), nullable=False),
        sa.Column('result', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('error', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_order_quotes_status', 'order_quotes', ['status'])


def downgrade() -> None:
    op.drop_table('order_quotes')
    op.drop_table('autobuy_orders')
