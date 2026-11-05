"""состояние аккаунта: лимит, % Кошелька, балансы + активные заказы

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

STATE_COLS = ('wallet_pct', 'limit_left', 'max_order', 'max_rid', 'wallet_balance', 'bonus_balance')


def upgrade() -> None:
    for c in STATE_COLS:
        op.add_column('wb_accounts', sa.Column(c, sa.Integer(), nullable=True))
    op.add_column('wb_accounts', sa.Column('state_at', sa.DateTime(timezone=True), nullable=True))
    op.create_table('account_orders',
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('rid', sa.String(length=64), nullable=False),
    sa.Column('nm_id', sa.BigInteger(), nullable=True),
    sa.Column('name', sa.String(length=512), nullable=True),
    sa.Column('brand', sa.String(length=256), nullable=True),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('dst_office_id', sa.BigInteger(), nullable=True),
    sa.Column('status_name', sa.String(length=128), nullable=True),
    sa.Column('group_id', sa.Integer(), nullable=True),
    sa.Column('status_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('ordered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('expiry_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['account_id'], ['wb_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('account_id', 'rid')
    )


def downgrade() -> None:
    op.drop_table('account_orders')
    for c in (*STATE_COLS, 'state_at'):
        op.drop_column('wb_accounts', c)
