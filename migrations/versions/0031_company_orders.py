"""заказы компаний (account_orders.company_id, invoice_url, pay_until) — бизнес-аккаунты, часть 3

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa

revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('account_orders', sa.Column('company_id', sa.Integer(),
                                              sa.ForeignKey('wb_companies.id', ondelete='SET NULL'), nullable=True))
    op.add_column('account_orders', sa.Column('invoice_url', sa.String(1024), nullable=True))
    op.add_column('account_orders', sa.Column('pay_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('account_orders', 'pay_until')
    op.drop_column('account_orders', 'invoice_url')
    op.drop_column('account_orders', 'company_id')
