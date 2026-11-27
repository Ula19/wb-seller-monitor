"""компании номера (wb_companies) и wb_accounts.retail — бизнес-аккаунты, часть 1 (SCOUT 2026-11-24 «🏢»)

Revision ID: 0029
Revises: 0028
Create Date: 2026-11-27
"""
from alembic import op
import sqlalchemy as sa

revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'wb_companies',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('old_id', sa.BigInteger(), nullable=False),
        sa.Column('uuid', sa.String(64), nullable=True),
        sa.Column('name', sa.String(256), nullable=True),
        sa.Column('full_name', sa.String(512), nullable=True),
        sa.Column('inn', sa.String(16), nullable=True),
        sa.Column('kpp', sa.String(16), nullable=True),
        sa.Column('sign', sa.String(256), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('seen_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('account_id', 'old_id'),
    )
    op.create_index('ix_wb_companies_account_id', 'wb_companies', ['account_id'])
    op.add_column('wb_accounts', sa.Column('retail', sa.Boolean(), nullable=False, server_default='true'))


def downgrade() -> None:
    op.drop_column('wb_accounts', 'retail')
    op.drop_index('ix_wb_companies_account_id', 'wb_companies')
    op.drop_table('wb_companies')
