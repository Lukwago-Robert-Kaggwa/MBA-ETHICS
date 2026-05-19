"""Add HDC-approved result mark snapshot.

Revision ID: c5d6e7f8a9b0
Revises: b774ef7dbbad
Create Date: 2026-05-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "c5d6e7f8a9b0"
down_revision = "b774ef7dbbad"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("results_hdc_approved_mark", sa.Float(), nullable=True))
    add_column_if_missing(
        "mba_projects",
        sa.Column("results_hdc_approved_classification", sa.String(length=40), nullable=True),
    )


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("results_hdc_approved_classification")
        batch_op.drop_column("results_hdc_approved_mark")
