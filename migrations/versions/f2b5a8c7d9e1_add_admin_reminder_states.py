"""add admin reminder states

Revision ID: f2b5a8c7d9e1
Revises: e7f4a2c9d1b6
Create Date: 2026-05-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import create_index_if_missing, create_table_if_missing


revision = "f2b5a8c7d9e1"
down_revision = "e7f4a2c9d1b6"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "mba_reminder_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("reminder_key", sa.String(length=255), nullable=False),
        sa.Column("last_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_sent_by_id", sa.Integer(), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(), nullable=True),
        sa.Column("dismissed_by_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dismissed_by_id"], ["mba_users.id"]),
        sa.ForeignKeyConstraint(["last_sent_by_id"], ["mba_users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reminder_key"),
    )
    create_index_if_missing("ix_mba_reminder_states_reminder_key", "mba_reminder_states", ["reminder_key"])


def downgrade():
    op.drop_index(op.f("ix_mba_reminder_states_reminder_key"), table_name="mba_reminder_states")
    op.drop_table("mba_reminder_states")
