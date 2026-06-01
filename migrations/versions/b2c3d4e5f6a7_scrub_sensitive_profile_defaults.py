"""Scrub sensitive banking and tax fields from reusable profile defaults.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-31 00:00:00.000000
"""

import json

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import column_exists, table_exists


revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


SENSITIVE_DEFAULT_FIELDS = {
    "income_tax_number",
    "bank_changed",
    "bank_account_holder",
    "bank_name",
    "bank_branch_name",
    "bank_branch_code",
    "bank_account_number",
    "bank_account_type",
    "bank_account_ownership",
}


def _as_dict(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _scrub_table(bind, table_name):
    if not table_exists(table_name) or not column_exists(table_name, "form_defaults"):
        return

    profile_table = sa.table(
        table_name,
        sa.column("id", sa.Integer),
        sa.column("form_defaults", sa.JSON),
    )
    rows = bind.execute(sa.select(profile_table.c.id, profile_table.c.form_defaults))
    for row in rows:
        defaults = _as_dict(row.form_defaults)
        if not defaults:
            continue
        cleaned = {
            key: value
            for key, value in defaults.items()
            if key not in SENSITIVE_DEFAULT_FIELDS
        }
        if cleaned == defaults:
            continue
        bind.execute(
            profile_table.update()
            .where(profile_table.c.id == row.id)
            .values(form_defaults=cleaned or None)
        )


def upgrade():
    bind = op.get_bind()
    _scrub_table(bind, "mba_student_profiles")
    _scrub_table(bind, "mba_scholar_profiles")


def downgrade():
    # Removed sensitive defaults cannot be safely reconstructed.
    pass
