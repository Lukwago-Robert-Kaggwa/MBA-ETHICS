"""Add Moodle upload timestamp fields.

Revision ID: e8a1c3f5b7d9
Revises: d4e5f6a7b8c9
Create Date: 2026-06-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "e8a1c3f5b7d9"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("capstone_project_moodle_uploaded_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("manuscript_moodle_uploaded_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("manuscript_moodle_uploaded_at")
        batch_op.drop_column("capstone_project_moodle_uploaded_at")
