"""Add password reset token fields.

Revision ID: f1a2b3c4d5e6
Revises: e8a1c3f5b7d9
Create Date: 2026-06-19 00:00:01.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing, create_index_if_missing


revision = "f1a2b3c4d5e6"
down_revision = "e8a1c3f5b7d9"
branch_labels = None
depends_on = None


def upgrade():
    for table_name in ("mba_users", "ethcis_users"):
        add_column_if_missing(table_name, sa.Column("reset_token_hash", sa.String(64), nullable=True))
        add_column_if_missing(table_name, sa.Column("reset_token_expires_at", sa.DateTime(), nullable=True))
        create_index_if_missing(f"ix_{table_name}_reset_token_hash", table_name, ["reset_token_hash"])


def downgrade():
    for table_name in ("mba_users", "ethcis_users"):
        with op.batch_alter_table(table_name) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_reset_token_hash")
            batch_op.drop_column("reset_token_expires_at")
            batch_op.drop_column("reset_token_hash")
