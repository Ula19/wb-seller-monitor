"""кнопка ↻ «Обновить» у аккаунта: флаг polling для worker'а

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_accounts', sa.Column('polling', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'polling')
