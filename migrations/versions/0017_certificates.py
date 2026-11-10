"""сертификаты WB: очередь активации на аккаунты

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'certificates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id', ondelete='CASCADE'), nullable=False),
        sa.Column('code', sa.String(16), nullable=False),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('wb_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('nominal_entered', sa.Integer(), nullable=False),
        sa.Column('nominal', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(16), server_default='pending', nullable=False),
        sa.Column('wb_cert_id', sa.String(128), nullable=True),
        sa.Column('error_number', sa.Integer(), nullable=True),
        sa.Column('error_text', sa.String(256), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_certificates_user_id', 'certificates', ['user_id'])
    op.create_index('ix_certificates_account_id', 'certificates', ['account_id'])
    op.create_index('ix_certificates_wb_cert_id', 'certificates', ['wb_cert_id'])
    op.create_index('uq_cert_code', 'certificates', ['code'], unique=True, postgresql_where=sa.text("status <> 'error'"))


def downgrade() -> None:
    op.drop_table('certificates')
