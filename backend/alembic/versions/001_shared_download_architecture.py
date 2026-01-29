"""Shared download architecture migration

Revision ID: 001_shared_download
Revises:
Create Date: 2026-01-29

This migration:
1. Creates new tables for shared download architecture:
   - download_tasks: Global download tasks (shared across users)
   - stored_files: Physical file storage with reference counting
   - user_files: User file references
   - user_task_subscriptions: User subscriptions to download tasks

2. The old 'tasks' table is kept for backward compatibility but will be
   deprecated. New downloads use the shared architecture.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001_shared_download'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create stored_files table
    op.create_table(
        'stored_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('content_hash', sa.String(), nullable=False),
        sa.Column('real_path', sa.String(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('is_directory', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('ref_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('original_name', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_stored_files_content_hash', 'stored_files', ['content_hash'], unique=True)

    # Create download_tasks table
    op.create_table(
        'download_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uri_hash', sa.String(), nullable=False),
        sa.Column('uri', sa.String(), nullable=False),
        sa.Column('gid', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False, server_default='queued'),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('total_length', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('completed_length', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('download_speed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('upload_speed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('error_display', sa.String(), nullable=True),
        sa.Column('stored_file_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.Column('completed_at', sa.String(), nullable=True),
        sa.Column('peak_download_speed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('peak_connections', sa.Integer(), nullable=False, server_default='0'),
        sa.ForeignKeyConstraint(['stored_file_id'], ['stored_files.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_download_tasks_uri_hash', 'download_tasks', ['uri_hash'], unique=True)
    op.create_index('ix_download_tasks_gid', 'download_tasks', ['gid'], unique=False)

    # Create user_files table
    op.create_table(
        'user_files',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('stored_file_id', sa.Integer(), nullable=False),
        sa.Column('display_name', sa.String(), nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['stored_file_id'], ['stored_files.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'stored_file_id'),
    )
    op.create_index('ix_user_files_owner_id', 'user_files', ['owner_id'], unique=False)
    op.create_index('ix_user_files_stored_file_id', 'user_files', ['stored_file_id'], unique=False)

    # Create user_task_subscriptions table
    op.create_table(
        'user_task_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('frozen_space', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('status', sa.String(), nullable=False, server_default='pending'),
        sa.Column('error_display', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['download_tasks.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'task_id'),
    )
    op.create_index('ix_user_task_subscriptions_owner_id', 'user_task_subscriptions', ['owner_id'], unique=False)
    op.create_index('ix_user_task_subscriptions_task_id', 'user_task_subscriptions', ['task_id'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order (respecting foreign key constraints)
    op.drop_index('ix_user_task_subscriptions_task_id', table_name='user_task_subscriptions')
    op.drop_index('ix_user_task_subscriptions_owner_id', table_name='user_task_subscriptions')
    op.drop_table('user_task_subscriptions')

    op.drop_index('ix_user_files_stored_file_id', table_name='user_files')
    op.drop_index('ix_user_files_owner_id', table_name='user_files')
    op.drop_table('user_files')

    op.drop_index('ix_download_tasks_gid', table_name='download_tasks')
    op.drop_index('ix_download_tasks_uri_hash', table_name='download_tasks')
    op.drop_table('download_tasks')

    op.drop_index('ix_stored_files_content_hash', table_name='stored_files')
    op.drop_table('stored_files')
