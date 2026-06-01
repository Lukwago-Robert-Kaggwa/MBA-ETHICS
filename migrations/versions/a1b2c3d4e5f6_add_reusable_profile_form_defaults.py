"""Add reusable MBA profile defaults for repeated form data.

Revision ID: a1b2c3d4e5f6
Revises: f8c2d4e6a9b1
Create Date: 2026-05-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "a1b2c3d4e5f6"
down_revision = "f8c2d4e6a9b1"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_student_profiles", sa.Column("postal_code", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_student_profiles", sa.Column("id_passport_number", sa.String(length=80), nullable=True))
    add_column_if_missing("mba_student_profiles", sa.Column("default_signing_location", sa.String(length=255), nullable=True))
    add_column_if_missing("mba_student_profiles", sa.Column("form_defaults", sa.JSON(), nullable=True))

    add_column_if_missing("mba_scholar_profiles", sa.Column("staff_number", sa.String(length=80), nullable=True))
    add_column_if_missing("mba_scholar_profiles", sa.Column("id_passport_number", sa.String(length=80), nullable=True))
    add_column_if_missing("mba_scholar_profiles", sa.Column("postal_code", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_scholar_profiles", sa.Column("default_signing_location", sa.String(length=255), nullable=True))
    add_column_if_missing("mba_scholar_profiles", sa.Column("form_defaults", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_scholar_profiles") as batch_op:
        batch_op.drop_column("form_defaults")
        batch_op.drop_column("default_signing_location")
        batch_op.drop_column("postal_code")
        batch_op.drop_column("id_passport_number")
        batch_op.drop_column("staff_number")

    with op.batch_alter_table("mba_student_profiles") as batch_op:
        batch_op.drop_column("form_defaults")
        batch_op.drop_column("default_signing_location")
        batch_op.drop_column("id_passport_number")
        batch_op.drop_column("postal_code")
