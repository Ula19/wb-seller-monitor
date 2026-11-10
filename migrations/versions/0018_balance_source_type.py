"""операции баланса: sourceType WB (giftcards — пополнение сертификатом)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-10
"""
from alembic import op
import sqlalchemy as sa

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('account_balance_ops', sa.Column('source_type', sa.String(32), nullable=True))
    # накопленные до колонки: у сертификата operationName «Сертификат Wildberries»
    op.execute("UPDATE account_balance_ops SET source_type = 'giftcards' WHERE name LIKE 'Сертификат%'")


def downgrade() -> None:
    op.drop_column('account_balance_ops', 'source_type')
