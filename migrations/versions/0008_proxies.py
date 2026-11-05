"""прокси аккаунтов WB (с сайта) + своё имя аккаунта в label вместо маски номера

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06
"""
from alembic import op
import sqlalchemy as sa

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'proxies',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('label', sa.String(64), nullable=False),
        sa.Column('server', sa.String(128), nullable=False),
        sa.Column('url_enc', sa.LargeBinary(), nullable=False),
        sa.Column('exit_ip', sa.String(64), nullable=True),
        sa.Column('ok', sa.Boolean(), nullable=True),
        sa.Column('error', sa.String(256), nullable=True),
        sa.Column('checking', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column('wb_accounts', sa.Column('proxy_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_wb_accounts_proxy', 'wb_accounts', 'proxies', ['proxy_id'], ['id'], ondelete='RESTRICT')
    op.create_index('ix_wb_accounts_proxy_id', 'wb_accounts', ['proxy_id'])
    op.add_column('wb_login_requests', sa.Column('proxy_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_wb_login_requests_proxy', 'wb_login_requests', 'proxies', ['proxy_id'], ['id'],
                          ondelete='SET NULL')
    # label был маской номера по умолчанию — теперь это своё имя с сайта
    op.execute("UPDATE wb_accounts SET label = NULL WHERE label = phone_mask")


def downgrade() -> None:
    op.execute("UPDATE wb_accounts SET label = phone_mask WHERE label IS NULL")
    op.drop_constraint('fk_wb_login_requests_proxy', 'wb_login_requests', type_='foreignkey')
    op.drop_column('wb_login_requests', 'proxy_id')
    op.drop_index('ix_wb_accounts_proxy_id', 'wb_accounts')
    op.drop_constraint('fk_wb_accounts_proxy', 'wb_accounts', type_='foreignkey')
    op.drop_column('wb_accounts', 'proxy_id')
    op.drop_table('proxies')
