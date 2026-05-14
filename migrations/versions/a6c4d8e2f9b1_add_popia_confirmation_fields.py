"""Add POPIA confirmation fields to user tables.

Revision ID: a6c4d8e2f9b1
Revises: e4b7c2d9a1f3
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "a6c4d8e2f9b1"
down_revision = "e4b7c2d9a1f3"
branch_labels = None
depends_on = None


def _add_popia_columns(table_name):
    add_column_if_missing(table_name, sa.Column("popia_confirmed_at", sa.DateTime(), nullable=True))
    add_column_if_missing(table_name, sa.Column("popia_notice_version", sa.String(length=40), nullable=True))
    add_column_if_missing(table_name, sa.Column("popia_confirmed_ip", sa.String(length=64), nullable=True))
    add_column_if_missing(table_name, sa.Column("popia_confirmed_user_agent", sa.String(length=255), nullable=True))


def _drop_popia_columns(table_name):
    with op.batch_alter_table(table_name) as batch_op:
        batch_op.drop_column("popia_confirmed_user_agent")
        batch_op.drop_column("popia_confirmed_ip")
        batch_op.drop_column("popia_notice_version")
        batch_op.drop_column("popia_confirmed_at")


def upgrade():
    _add_popia_columns("mba_users")
    _add_popia_columns("ethcis_users")


def downgrade():
    _drop_popia_columns("ethcis_users")
    _drop_popia_columns("mba_users")
