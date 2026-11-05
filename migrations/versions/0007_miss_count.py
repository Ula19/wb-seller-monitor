"""промахи товара подряд: «пропал» только после 3 циклов без него в выдаче

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('items', sa.Column('miss_count', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('items', 'miss_count')
