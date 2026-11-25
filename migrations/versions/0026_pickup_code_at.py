"""когда снят код получения (wb_accounts.pickup_code_at) — показать в «Общем заборе» рядом с кодом

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-25
"""
from alembic import op
import sqlalchemy as sa

revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('pickup_code_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'pickup_code_at')
