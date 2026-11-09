"""страница аккаунта: история заказов, ВБ-баллов и баланса, заметки к балансу, код получения, визит в браузере

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def _owner_fk():
    return sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='CASCADE'), primary_key=True)


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('browser_sync', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('wb_accounts', sa.Column('pickup_code', sa.String(16), nullable=True))
    op.add_column('wb_accounts', sa.Column('pickup_code_date', sa.Date(), nullable=True))
    op.create_table(
        'account_purchases', _owner_fk(),
        sa.Column('rid', sa.String(64), primary_key=True),
        sa.Column('nm_id', sa.BigInteger(), nullable=True),
        sa.Column('name', sa.String(512), nullable=True),
        sa.Column('brand', sa.String(256), nullable=True),
        sa.Column('price', sa.Integer(), nullable=True),
        sa.Column('dt', sa.DateTime(timezone=True), nullable=True),
        sa.Column('office_id', sa.BigInteger(), nullable=True),
        sa.Column('office_address', sa.String(256), nullable=True),
        sa.Column('office_hours', sa.String(64), nullable=True),
    )
    op.create_index('ix_account_purchases_dt', 'account_purchases', ['dt'])
    op.create_table(
        'account_vb_ops', _owner_fk(),
        sa.Column('tx_id', sa.String(128), primary_key=True),
        sa.Column('title', sa.String(256), nullable=True),
        sa.Column('description', sa.String(512), nullable=True),
        sa.Column('dt', sa.DateTime(timezone=True), nullable=True),
        sa.Column('operation', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Integer(), nullable=True),
    )
    op.create_table(
        'account_balance_ops', _owner_fk(),
        sa.Column('tx_id', sa.String(128), primary_key=True),
        sa.Column('name', sa.String(256), nullable=True),
        sa.Column('op_type', sa.Integer(), nullable=True),
        sa.Column('amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('balance', sa.Numeric(14, 2), nullable=True),
        sa.Column('dt', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        'balance_notes', _owner_fk(),
        sa.Column('tx_id', sa.String(128), primary_key=True),
        sa.Column('text', sa.String(1000), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('balance_notes')
    op.drop_table('account_balance_ops')
    op.drop_table('account_vb_ops')
    op.drop_index('ix_account_purchases_dt', table_name='account_purchases')
    op.drop_table('account_purchases')
    op.drop_column('wb_accounts', 'pickup_code_date')
    op.drop_column('wb_accounts', 'pickup_code')
    op.drop_column('wb_accounts', 'browser_sync')
