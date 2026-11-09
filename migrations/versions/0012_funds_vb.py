"""аккаунт WB: деньги в Кошельке (available_funds_rub) и ВБ-баллы вместо «баланса» и «бонусов»

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('funds', sa.Integer(), nullable=True))
    op.add_column('wb_accounts', sa.Column('vb_points', sa.Integer(), nullable=True))
    # старые числа = сумма funds (баланс + «бонусы» в рублях) — переносим, чтобы до первого опроса не было «—»
    op.execute("UPDATE wb_accounts SET funds = coalesce(wallet_balance, 0) + coalesce(bonus_balance, 0) "
               "WHERE wallet_balance IS NOT NULL OR bonus_balance IS NOT NULL")
    op.drop_column('wb_accounts', 'wallet_balance')
    op.drop_column('wb_accounts', 'bonus_balance')


def downgrade() -> None:
    op.add_column('wb_accounts', sa.Column('wallet_balance', sa.Integer(), nullable=True))
    op.add_column('wb_accounts', sa.Column('bonus_balance', sa.Integer(), nullable=True))
    op.drop_column('wb_accounts', 'vb_points')
    op.drop_column('wb_accounts', 'funds')
