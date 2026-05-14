"""add supervisor title change request

Revision ID: ab3f7d1e9c42
Revises: 7ad99b2f1c3e
Create Date: 2026-05-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "ab3f7d1e9c42"
down_revision = "7ad99b2f1c3e"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("supervisor_title_change_requested_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("supervisor_title_change_request", sa.Text(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("supervisor_title_change_resolved_at", sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column("mba_projects", "supervisor_title_change_resolved_at")
    op.drop_column("mba_projects", "supervisor_title_change_request")
    op.drop_column("mba_projects", "supervisor_title_change_requested_at")
