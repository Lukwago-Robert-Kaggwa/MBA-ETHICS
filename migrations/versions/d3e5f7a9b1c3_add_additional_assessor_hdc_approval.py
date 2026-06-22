"""add additional assessor hdc decision + global assessor approval status

Revision ID: d3e5f7a9b1c3
Revises: c2d4e6f8a0b2
Create Date: 2026-06-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import add_column_if_missing


revision = "d3e5f7a9b1c3"
down_revision = "c2d4e6f8a0b2"
branch_labels = None
depends_on = None


def upgrade():
    add_column_if_missing("mba_projects", sa.Column("assessor_3_hdc_decision", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_3_hdc_decision_at", sa.DateTime(), nullable=True))
    add_column_if_missing("mba_projects", sa.Column("assessor_3_hdc_decision_assessor_id", sa.Integer(), nullable=True))
    add_column_if_missing("mba_users", sa.Column("hdc_assessor_approval_status", sa.String(length=20), nullable=True))
    add_column_if_missing("mba_users", sa.Column("hdc_assessor_approval_at", sa.DateTime(), nullable=True))
    add_column_if_missing(
        "mba_users", sa.Column("hdc_assessor_approval_set_by_id", sa.Integer(), nullable=True)
    )

    # Backfill: anyone already HDC-approved as assessor_1/assessor_2 on any
    # existing project should be treated as "previously approved" going
    # forward, not just people approved after this feature ships.
    bind = op.get_bind()
    metadata = sa.MetaData()
    projects = sa.Table(
        "mba_projects",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("assessor_1_id", sa.Integer),
        sa.Column("assessor_1_hdc_decision", sa.String),
        sa.Column("assessor_1_hdc_decision_at", sa.DateTime),
        sa.Column("assessor_2_id", sa.Integer),
        sa.Column("assessor_2_hdc_decision", sa.String),
        sa.Column("assessor_2_hdc_decision_at", sa.DateTime),
    )
    users = sa.Table(
        "mba_users",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("hdc_assessor_approval_status", sa.String),
        sa.Column("hdc_assessor_approval_at", sa.DateTime),
    )

    latest_approval_at = {}
    for slot in ("assessor_1", "assessor_2"):
        rows = bind.execute(
            sa.select(
                projects.c[f"{slot}_id"].label("user_id"),
                projects.c[f"{slot}_hdc_decision_at"].label("decided_at"),
            ).where(
                projects.c[f"{slot}_hdc_decision"] == "approved",
                projects.c[f"{slot}_id"].isnot(None),
            )
        ).fetchall()
        for row in rows:
            existing = latest_approval_at.get(row.user_id)
            if existing is None or (row.decided_at and row.decided_at > existing):
                latest_approval_at[row.user_id] = row.decided_at

    for user_id, decided_at in latest_approval_at.items():
        bind.execute(
            users.update()
            .where(users.c.id == user_id)
            .values(hdc_assessor_approval_status="approved", hdc_assessor_approval_at=decided_at)
        )


def downgrade():
    with op.batch_alter_table("mba_projects") as batch_op:
        batch_op.drop_column("assessor_3_hdc_decision_assessor_id")
        batch_op.drop_column("assessor_3_hdc_decision_at")
        batch_op.drop_column("assessor_3_hdc_decision")
    with op.batch_alter_table("mba_users") as batch_op:
        batch_op.drop_column("hdc_assessor_approval_set_by_id")
        batch_op.drop_column("hdc_assessor_approval_at")
        batch_op.drop_column("hdc_assessor_approval_status")
