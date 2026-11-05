"""месячный лимит скидки Кошелька у аккаунта (полоса на сайте)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('monthly_limit', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'monthly_limit')
