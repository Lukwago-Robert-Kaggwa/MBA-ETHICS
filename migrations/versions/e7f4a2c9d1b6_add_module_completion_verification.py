"""add module completion verification fields

Revision ID: e7f4a2c9d1b6
Revises: d4a8f1b2c3e5
Create Date: 2026-05-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, create_unique_constraint_if_missing


revision = "e7f4a2c9d1b6"
down_revision = "d4a8f1b2c3e5"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("module_completion_marks_email", sa.String(length=255), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("module_completion_verification_token", sa.String(length=128), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("module_completion_requested_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("module_completion_responded_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("module_completion_response", sa.String(length=10), nullable=True))
    create_unique_constraint_if_missing(
        "uq_mba_projects_module_completion_token",
        "mba_projects",
        ["module_completion_verification_token"],
    )


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_constraint("uq_mba_projects_module_completion_token", type_="unique")
        batch_op.drop_column("module_completion_response")
        batch_op.drop_column("module_completion_responded_at")
        batch_op.drop_column("module_completion_requested_at")
        batch_op.drop_column("module_completion_verification_token")
        batch_op.drop_column("module_completion_marks_email")
