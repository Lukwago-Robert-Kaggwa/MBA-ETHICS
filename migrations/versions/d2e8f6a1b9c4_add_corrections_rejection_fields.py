"""Add corrections rejection fields.

Revision ID: d2e8f6a1b9c4
Revises: c9f1a4d2e7b3
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "d2e8f6a1b9c4"
down_revision = "c9f1a4d2e7b3"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("corrections_supervisor_rejected_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("corrections_supervisor_rejection_comments", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("corrections_supervisor_rejection_comments")
        batch_op.drop_column("corrections_supervisor_rejected_at")
