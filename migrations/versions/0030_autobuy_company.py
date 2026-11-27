"""заказ на компанию номера (autobuy_orders.company_id) — бизнес-аккаунты, часть 2

Revision ID: 0030
Revises: 0029
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa

revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('autobuy_orders', sa.Column('company_id', sa.Integer(),
                                              sa.ForeignKey('wb_companies.id', ondelete='SET NULL'), nullable=True))


def downgrade() -> None:
    op.drop_column('autobuy_orders', 'company_id')
