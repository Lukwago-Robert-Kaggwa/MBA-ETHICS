"""Add results release to supervisor timestamp.

Revision ID: e4b7c2d9a1f3
Revises: d2e8f6a1b9c4
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "e4b7c2d9a1f3"
down_revision = "d2e8f6a1b9c4"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("results_released_to_supervisor_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("results_released_to_supervisor_at")
