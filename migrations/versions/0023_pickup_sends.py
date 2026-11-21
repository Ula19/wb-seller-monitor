"""«Общий забор»: отправки кода получения и товаров по паре «аккаунт + ПВЗ» в Telegram

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'pickup_sends',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('office_id', sa.BigInteger(), nullable=False),
        sa.Column('address', sa.String(256), nullable=True),
        sa.Column('rids', postgresql.JSONB(), nullable=False),
        sa.Column('items', postgresql.JSONB(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('error', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_pickup_sends_account_id', 'pickup_sends', ['account_id'])
    op.create_index('ix_pickup_sends_status', 'pickup_sends', ['status'])


def downgrade() -> None:
    op.drop_table('pickup_sends')
