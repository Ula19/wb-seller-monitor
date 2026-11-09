"""аккаунт: история заказов загружена целиком (дальше догрузка) и причина неудачного визита в браузере

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('purchases_full_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('wb_accounts', sa.Column('visit_error', sa.String(128), nullable=True))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'visit_error')
    op.drop_column('wb_accounts', 'purchases_full_at')
