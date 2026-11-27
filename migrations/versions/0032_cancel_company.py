"""отмена счёта компании (order_cancels.company_id) — бизнес-аккаунты, часть 4

Revision ID: 0032
Revises: 0031
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa

revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('order_cancels', sa.Column('company_id', sa.Integer(),
                                             sa.ForeignKey('wb_companies.id', ondelete='SET NULL'), nullable=True))


def downgrade() -> None:
    op.drop_column('order_cancels', 'company_id')
