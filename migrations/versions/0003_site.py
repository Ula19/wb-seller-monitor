"""сайт: заявки на вход в аккаунт WB, события в истории цен (лента)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-05
"""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('price_history', sa.Column('event', sa.String(length=8), server_default='change', nullable=False))
    op.create_table('wb_login_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('phone', sa.String(length=16), nullable=True),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='new', nullable=False),
    sa.Column('code', sa.String(length=16), nullable=True),
    sa.Column('error', sa.String(length=256), nullable=True),
    sa.Column('account_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['wb_accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_wb_login_requests_user_id', 'wb_login_requests', ['user_id'])
    op.create_index('uq_login_active', 'wb_login_requests', ['user_id'], unique=True,
                    postgresql_where=sa.text("status IN ('new', 'running', 'wait_code')"))


def downgrade() -> None:
    op.drop_table('wb_login_requests')
    op.drop_column('price_history', 'event')
