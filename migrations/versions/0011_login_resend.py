"""заявка на вход: повторная отправка кода (просьба сайта, куда ушёл код, когда можно повторить); статус cancelled

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-09
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('wb_login_requests', sa.Column('resend', sa.String(8), nullable=True))
    op.add_column('wb_login_requests', sa.Column('sent_via', sa.String(8), nullable=True))
    op.add_column('wb_login_requests', sa.Column('resend_after', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE wb_login_requests SET status = 'error', error = 'отменено' WHERE status = 'cancelled'")
    op.drop_column('wb_login_requests', 'resend_after')
    op.drop_column('wb_login_requests', 'sent_via')
    op.drop_column('wb_login_requests', 'resend')
