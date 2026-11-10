"""аккаунт: кошелёк заблокирован и сумма ареста (из full-balance)

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('wallet_blocked', sa.Boolean(), nullable=True))
    op.add_column('wb_accounts', sa.Column('wallet_arrested', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'wallet_arrested')
    op.drop_column('wb_accounts', 'wallet_blocked')
