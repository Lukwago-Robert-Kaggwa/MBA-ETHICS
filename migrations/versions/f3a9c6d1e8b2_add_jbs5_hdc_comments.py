"""Add dedicated JBS5 HDC comments.

Revision ID: f3a9c6d1e8b2
Revises: b8d2f4a6c9e1
Create Date: 2026-05-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "f3a9c6d1e8b2"
down_revision = "b8d2f4a6c9e1"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("jbs5_hdc_comments", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE mba_projects
        SET jbs5_hdc_comments = hdc_comments
        WHERE jbs5_hdc_comments IS NULL
          AND hdc_comments IS NOT NULL
          AND project_status IN ('jbs5_submitted_to_hdc', 'jbs5_hdc_approved', 'jbs5_hdc_declined')
        """
    )


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("jbs5_hdc_comments")
