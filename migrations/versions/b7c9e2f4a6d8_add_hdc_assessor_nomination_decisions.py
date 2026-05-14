"""add hdc assessor nomination decisions

Revision ID: b7c9e2f4a6d8
Revises: f2b5a8c7d9e1
Create Date: 2026-05-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "b7c9e2f4a6d8"
down_revision = "f2b5a8c7d9e1"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("assessor_1_hdc_decision", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_1_hdc_decision_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_1_hdc_decision_assessor_id", sa.Integer(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_2_hdc_decision", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_2_hdc_decision_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_2_hdc_decision_assessor_id", sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("assessor_2_hdc_decision_assessor_id")
        batch_op.drop_column("assessor_2_hdc_decision_at")
        batch_op.drop_column("assessor_2_hdc_decision")
        batch_op.drop_column("assessor_1_hdc_decision_assessor_id")
        batch_op.drop_column("assessor_1_hdc_decision_at")
        batch_op.drop_column("assessor_1_hdc_decision")
