"""add moodle manuscript request timestamp

Revision ID: c9f1a4d2e7b3
Revises: b7c9e2f4a6d8
Create Date: 2026-05-12 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "c9f1a4d2e7b3"
down_revision = "b7c9e2f4a6d8"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("dissertation_moodle_request_sent_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("dissertation_moodle_request_sent_at")
