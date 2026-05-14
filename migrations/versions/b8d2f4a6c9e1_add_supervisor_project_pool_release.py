"""Add supervisor project pool release markers.

Revision ID: b8d2f4a6c9e1
Revises: a6c4d8e2f9b1
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, create_foreign_key_if_missing


revision = "b8d2f4a6c9e1"
down_revision = "a6c4d8e2f9b1"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("supervisor_pool_released_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("supervisor_pool_released_by_id", sa.Integer(), nullable=True))
    create_foreign_key_if_missing(
        "fk_mba_projects_supervisor_pool_released_by_id_mba_users",
        "mba_projects",
        "mba_users",
        ["supervisor_pool_released_by_id"],
        ["id"],
    )


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("supervisor_pool_released_by_id")
        batch_op.drop_column("supervisor_pool_released_at")
