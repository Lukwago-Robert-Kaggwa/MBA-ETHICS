"""Store uploaded documents in the database.

Revision ID: a9d8c7b6e5f4
Revises: f3a9c6d1e8b2
Create Date: 2026-05-13 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, create_index_if_missing, create_table_if_missing


revision = "a9d8c7b6e5f4"
down_revision = "f3a9c6d1e8b2"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_project_documents", sa.Column("file_data", sa.LargeBinary(), nullable=True))
    add_column_if_missing("mba_project_documents", sa.Column("mime_type", sa.String(length=120), nullable=True))
    add_column_if_missing("mba_project_documents", sa.Column("file_size", sa.Integer(), nullable=True))

    create_table_if_missing(
        "ethcis_submission_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("submission_id", sa.Integer(), sa.ForeignKey("ethcis_form_submissions.id"), nullable=True),
        sa.Column("student_id", sa.Integer(), sa.ForeignKey("ethcis_users.id"), nullable=False),
        sa.Column("field_name", sa.String(length=120), nullable=True),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=255), nullable=False),
        sa.Column("file_data", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    create_index_if_missing("ix_ethcis_submission_files_submission_id", "ethcis_submission_files", ["submission_id"])
    create_index_if_missing("ix_ethcis_submission_files_student_id", "ethcis_submission_files", ["student_id"])
    create_index_if_missing("ix_ethcis_submission_files_stored_name", "ethcis_submission_files", ["stored_name"], unique=True)


def downgrade():
    op.drop_index("ix_ethcis_submission_files_stored_name", table_name="ethcis_submission_files")
    op.drop_index("ix_ethcis_submission_files_student_id", table_name="ethcis_submission_files")
    op.drop_index("ix_ethcis_submission_files_submission_id", table_name="ethcis_submission_files")
    op.drop_table("ethcis_submission_files")

    with op.batch_alter_table("mba_project_documents") as batch_op:
        batch_op.drop_column("file_size")
        batch_op.drop_column("mime_type")
        batch_op.drop_column("file_data")
