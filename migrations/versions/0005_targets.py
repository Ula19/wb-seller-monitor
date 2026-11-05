"""цели ₽ на товары (событие «ниже цели»)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('targets',
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('nm_id', sa.BigInteger(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=False),
    sa.Column('below', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['nm_id'], ['items.nm_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'nm_id')
    )


def downgrade() -> None:
    op.drop_table('targets')
