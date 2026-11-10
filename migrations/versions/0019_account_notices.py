"""уведомления колокольчика WB по аккаунтам + когда брали ленту

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('notices_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        'account_notices',
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('uuid', sa.String(64), primary_key=True),
        sa.Column('id', sa.Integer(), sa.Identity(), nullable=False, unique=True),
        sa.Column('topic_id', sa.String(64), nullable=True),
        sa.Column('title', sa.String(256), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('url', sa.String(1024), nullable=True),
        sa.Column('dt', sa.DateTime(timezone=True), nullable=True),
        sa.Column('wb_read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('read_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_account_notices_dt', 'account_notices', ['dt'])
    op.create_index('ix_account_notices_unread', 'account_notices', ['read_at'], postgresql_where=sa.text('read_at IS NULL'))


def downgrade() -> None:
    op.drop_table('account_notices')
    op.drop_column('wb_accounts', 'notices_at')
