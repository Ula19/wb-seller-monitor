"""сбои прокси аккаунта подряд (для пуша после 3 и строки на сайте)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('proxy_fails', sa.Integer(), server_default='0', nullable=False))
    op.add_column('wb_accounts', sa.Column('proxy_fail_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'proxy_fail_at')
    op.drop_column('wb_accounts', 'proxy_fails')
