"""время операций «Баланса»: WB отдаёт МСК как epoch, мы писали как UTC — старые строки на 3 ч назад

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-27
"""
from alembic import op

revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE account_balance_ops SET dt = dt - interval '3 hours' WHERE dt IS NOT NULL")


def downgrade() -> None:
    op.execute("UPDATE account_balance_ops SET dt = dt + interval '3 hours' WHERE dt IS NOT NULL")
