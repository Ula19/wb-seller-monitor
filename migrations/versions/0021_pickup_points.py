"""справочник ПВЗ WB в БД (all-poo-fr): адрес, dest, координаты, подпись — для заказа товара

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wb_pickup_points',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column('country', sa.String(8), nullable=True),
        sa.Column('address', sa.String(512), nullable=True),
        sa.Column('dest', sa.BigInteger(), nullable=True),
        sa.Column('lat', sa.Float(), nullable=True),
        sa.Column('lon', sa.Float(), nullable=True),
        sa.Column('type_point', sa.Integer(), nullable=True),
        sa.Column('sign', sa.String(64), nullable=True),
        sa.Column('sign_v2', sa.String(64), nullable=True),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('week', postgresql.JSONB(), nullable=True),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('wb_pickup_points')
