"""add task_history table

Revision ID: 99c121dd1980
Revises: 001_shared_download
Create Date: 2026-01-30 14:03:55.077737

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '99c121dd1980'
down_revision: Union[str, Sequence[str], None] = '001_shared_download'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'task_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('task_name', sa.String(), nullable=False),
        sa.Column('uri', sa.String(), nullable=True),
        sa.Column('total_length', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('result', sa.String(), nullable=False),
        sa.Column('reason', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('finished_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_task_history_owner_id', 'task_history', ['owner_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_task_history_owner_id', 'task_history')
    op.drop_table('task_history')
