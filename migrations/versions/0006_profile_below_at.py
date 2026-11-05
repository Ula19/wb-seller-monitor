"""профиль аккаунта из WB (имя, телефон, процент выкупа) и время срабатывания цели

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('name', sa.String(length=64), nullable=True))
    op.add_column('wb_accounts', sa.Column('phone', sa.String(length=32), nullable=True))
    op.add_column('wb_accounts', sa.Column('purchase_pct', sa.Integer(), nullable=True))
    op.add_column('targets', sa.Column('below_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('targets', 'below_at')
    for c in ('purchase_pct', 'phone', 'name'):
        op.drop_column('wb_accounts', c)
