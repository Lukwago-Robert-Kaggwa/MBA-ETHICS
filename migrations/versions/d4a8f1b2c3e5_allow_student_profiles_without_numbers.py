"""allow student profiles without numbers until completion

Revision ID: d4a8f1b2c3e5
Revises: c0f2a1d9b8e4
Create Date: 2026-05-11 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "d4a8f1b2c3e5"
down_revision = "c0f2a1d9b8e4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("mba_student_profiles") as batch_op:
        batch_op.alter_column(
            "student_number",
            existing_type=sa.String(length=40),
            nullable=True,
        )


def downgrade():
    with op.batch_alter_table("mba_student_profiles") as batch_op:
        batch_op.alter_column(
            "student_number",
            existing_type=sa.String(length=40),
            nullable=False,
        )
