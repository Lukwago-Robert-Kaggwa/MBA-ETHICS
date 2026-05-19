"""Backfill HDC-approved result mark snapshots.

Revision ID: b4e6f8a1c2d3
Revises: 8697bc60bdb4
Create Date: 2026-05-19 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "b4e6f8a1c2d3"
down_revision = "8697bc60bdb4"
branch_labels = None
depends_on = None


def _payload_dict(payload):
    if isinstance(payload, dict):
        return payload
    if not payload:
        return {}
    import json

    try:
        return json.loads(payload)
    except (TypeError, ValueError):
        return {}


def _classification(mark):
    if mark >= 75:
        return "Distinction"
    if mark >= 65:
        return "Merit"
    if mark >= 50:
        return "Pass"
    return "Fail"


def upgrade():
    bind = op.get_bind()
    metadata = sa.MetaData()
    projects = sa.Table(
        "mba_projects",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("project_status", sa.String),
        sa.Column("results_hdc_decision", sa.String),
        sa.Column("results_hdc_approved_mark", sa.Float),
        sa.Column("results_hdc_approved_classification", sa.String),
    )
    forms = sa.Table(
        "mba_forms",
        metadata,
        sa.Column("project_id", sa.Integer),
        sa.Column("form_type", sa.String),
        sa.Column("payload", sa.JSON),
    )
    approved_projects = bind.execute(
        sa.select(projects.c.id).where(
            projects.c.results_hdc_decision == "approved",
            projects.c.project_status.in_(("results_approved", "graduated")),
            projects.c.results_hdc_approved_mark.is_(None),
        )
    ).fetchall()
    for row in approved_projects:
        project_id = row.id
        grade_rows = bind.execute(
            sa.select(forms.c.payload).where(
                forms.c.project_id == project_id,
                forms.c.form_type.in_(
                    (
                        "assessment_result_assessor_1",
                        "assessment_result_assessor_2",
                        "assessment_result_assessor_3",
                    )
                ),
            )
        ).fetchall()
        grades = []
        for grade_row in grade_rows:
            payload = _payload_dict(grade_row.payload)
            try:
                grade = int(payload.get("grade", ""))
            except (TypeError, ValueError):
                continue
            if 0 <= grade <= 100:
                grades.append(grade)
        if not grades:
            continue
        mark = round(sum(grades) / len(grades), 1)
        bind.execute(
            projects.update()
            .where(projects.c.id == project_id)
            .values(
                results_hdc_approved_mark=mark,
                results_hdc_approved_classification=_classification(mark),
            )
        )


def downgrade():
    pass
