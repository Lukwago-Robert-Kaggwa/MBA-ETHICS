"""add capstone workflow gates

Revision ID: c0f2a1d9b8e4
Revises: ab3f7d1e9c42
Create Date: 2026-05-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, column_exists


revision = "c0f2a1d9b8e4"
down_revision = "ab3f7d1e9c42"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("assessment_results_forwarded_to_supervisor_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("corrections_released_to_student_at", sa.DateTime(), nullable=True))
    add_column_if_missing(
        "mba_projects",
        sa.Column("module_completion_status", sa.String(length=60), nullable=False, server_default="not_checked"),
    )
    add_column_if_missing("mba_projects", sa.Column("jbs5_hdc_approved_at", sa.DateTime(), nullable=True))
    if column_exists("mba_projects", "module_completion_status"):
        op.alter_column("mba_projects", "module_completion_status", server_default=None)


def downgrade():
    op.drop_column("mba_projects", "jbs5_hdc_approved_at")
    op.drop_column("mba_projects", "module_completion_status")
    op.drop_column("mba_projects", "corrections_released_to_student_at")
    op.drop_column("mba_projects", "assessment_results_forwarded_to_supervisor_at")
