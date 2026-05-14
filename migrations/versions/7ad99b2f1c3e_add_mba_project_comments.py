"""add mba project comments

Revision ID: 7ad99b2f1c3e
Revises: 40c8fdf09260
Create Date: 2026-05-04 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import create_index_if_missing, create_table_if_missing


revision = "7ad99b2f1c3e"
down_revision = "40c8fdf09260"
branch_labels = None
depends_on = None


def upgrade():
    create_table_if_missing(
        "mba_project_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["mba_users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["mba_projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    create_index_if_missing("ix_mba_project_comments_author_id", "mba_project_comments", ["author_id"])
    create_index_if_missing("ix_mba_project_comments_project_id", "mba_project_comments", ["project_id"])


def downgrade():
    op.drop_index(op.f("ix_mba_project_comments_project_id"), table_name="mba_project_comments")
    op.drop_index(op.f("ix_mba_project_comments_author_id"), table_name="mba_project_comments")
    op.drop_table("mba_project_comments")
