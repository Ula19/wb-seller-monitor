"""схема v2 с нуля: items глобально, sources, wb_accounts, account_prices, price_history

Revision ID: 0001
Revises:
Create Date: 2026-11-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('app_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('items',
    sa.Column('nm_id', sa.BigInteger(), nullable=False),
    sa.Column('supplier_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=512), nullable=True),
    sa.Column('brand', sa.String(length=256), nullable=True),
    sa.Column('subject_id', sa.Integer(), nullable=True),
    sa.Column('pics', sa.Integer(), nullable=True),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('stock', sa.Integer(), nullable=True),
    sa.Column('delivery_hours', sa.Integer(), nullable=True),
    sa.Column('from_seller', sa.Boolean(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('nm_id')
    )
    op.create_index(op.f('ix_items_supplier_id'), 'items', ['supplier_id'], unique=False)
    op.create_table('sellers',
    sa.Column('supplier_id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=256), nullable=True),
    sa.Column('brand', sa.String(length=256), nullable=True),
    sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_check_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('supplier_id')
    )
    op.create_table('users',
    sa.Column('telegram_id', sa.BigInteger(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('telegram_id')
    )
    op.create_table('price_history',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('nm_id', sa.BigInteger(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('stock', sa.Integer(), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['nm_id'], ['items.nm_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_price_history_nm_id'), 'price_history', ['nm_id'], unique=False)
    op.create_table('sources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=16), server_default='seller', nullable=False),
    sa.Column('supplier_id', sa.BigInteger(), nullable=True),
    sa.Column('b2b', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('seeded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['supplier_id'], ['sellers.supplier_id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'kind', 'supplier_id')
    )
    op.create_index(op.f('ix_sources_user_id'), 'sources', ['user_id'], unique=False)
    op.create_table('wb_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.BigInteger(), nullable=False),
    sa.Column('label', sa.String(length=64), nullable=True),
    sa.Column('phone_mask', sa.String(length=32), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('state_enc', sa.LargeBinary(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='active', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.telegram_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_wb_accounts_user_id'), 'wb_accounts', ['user_id'], unique=False)
    op.create_table('account_prices',
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('nm_id', sa.BigInteger(), nullable=False),
    sa.Column('price', sa.Integer(), nullable=True),
    sa.Column('priced_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['account_id'], ['wb_accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['nm_id'], ['items.nm_id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('account_id', 'nm_id')
    )


def downgrade() -> None:
    op.drop_table('account_prices')
    op.drop_index(op.f('ix_wb_accounts_user_id'), table_name='wb_accounts')
    op.drop_table('wb_accounts')
    op.drop_index(op.f('ix_sources_user_id'), table_name='sources')
    op.drop_table('sources')
    op.drop_index(op.f('ix_price_history_nm_id'), table_name='price_history')
    op.drop_table('price_history')
    op.drop_table('users')
    op.drop_table('sellers')
    op.drop_index(op.f('ix_items_supplier_id'), table_name='items')
    op.drop_table('items')
    op.drop_table('app_settings')
