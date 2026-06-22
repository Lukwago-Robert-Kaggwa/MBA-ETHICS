"""add co-supervisor invitation fields

Revision ID: c2d4e6f8a0b2
Revises: f1a2b3c4d5e6
Create Date: 2026-06-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "c2d4e6f8a0b2"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_id", sa.Integer(), sa.ForeignKey("mba_users.id"), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_invitation_status", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_invited_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_reminder_sent_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_accepted_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("co_supervisor_required", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    op.drop_column("mba_projects", "co_supervisor_required")
    op.drop_column("mba_projects", "co_supervisor_accepted_at")
    op.drop_column("mba_projects", "co_supervisor_reminder_sent_at")
    op.drop_column("mba_projects", "co_supervisor_invited_at")
    op.drop_column("mba_projects", "co_supervisor_invitation_status")
    op.drop_column("mba_projects", "co_supervisor_id")
