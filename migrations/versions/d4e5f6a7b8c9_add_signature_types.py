"""Add typed saved signatures for HDC signature roles.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-31 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, create_index_if_missing, table_exists


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    if not table_exists("mba_user_signatures"):
        return
    add_column_if_missing(
        "mba_user_signatures",
        sa.Column("signature_type", sa.String(length=40), nullable=False, server_default="primary"),
    )
    add_column_if_missing(
        "mba_user_signatures",
        sa.Column("printed_name", sa.String(length=255), nullable=True),
    )
    create_index_if_missing("ix_mba_user_signatures_signature_type", "mba_user_signatures", ["signature_type"])


def downgrade():
    if not table_exists("mba_user_signatures"):
        return
    try:
        op.drop_index("ix_mba_user_signatures_signature_type", table_name="mba_user_signatures")
    except Exception:
        pass
    try:
        op.drop_column("mba_user_signatures", "printed_name")
    except Exception:
        pass
    try:
        op.drop_column("mba_user_signatures", "signature_type")
    except Exception:
        pass
