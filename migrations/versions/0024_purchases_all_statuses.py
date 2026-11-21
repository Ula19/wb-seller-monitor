"""архив заказов всех статусов (для журнала): status, status_id, created_at + один полный перегруз архива

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-21
"""
from alembic import op
import sqlalchemy as sa

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('account_purchases', sa.Column('status', sa.Integer(), nullable=True))
    op.add_column('account_purchases', sa.Column('status_id', sa.Integer(), nullable=True))
    op.add_column('account_purchases', sa.Column('created_at', sa.DateTime(timezone=True), nullable=True))
    # архив теперь без фильтра статуса — один раз грузим целиком (отмены, даты заказа); дальше снова догрузка
    op.execute("UPDATE wb_accounts SET purchases_full_at = NULL")


def downgrade() -> None:
    op.execute("DELETE FROM account_purchases WHERE status IS DISTINCT FROM 32 AND status IS NOT NULL")
    op.drop_column('account_purchases', 'created_at')
    op.drop_column('account_purchases', 'status_id')
    op.drop_column('account_purchases', 'status')
