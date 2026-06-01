"""Encrypt assessor banking and tax data at rest.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-05-31 00:00:00.000000
"""

import base64
import hashlib
import json
import os

from alembic import op
from cryptography.fernet import Fernet
from flask import current_app
import sqlalchemy as sa

from migrations.schema_helpers import column_exists, table_exists


revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


SENSITIVE_FORM_FIELD_NAMES = {
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
SENSITIVE_DOCUMENT_TYPE_PREFIXES = (
    "assessor_banking_",
    "assessor_temp_appointment_",
    "assessor_temp_claim_",
)
ENCRYPTED_PAYLOAD_MARKER = "mba_sensitive_v1"
ENCRYPTED_DOCUMENT_PREFIX = b"MBAENC1:"


def _config_value(name):
    try:
        return current_app.config.get(name) or ""
    except RuntimeError:
        return ""


def _sensitive_data_key_material():
    configured = _config_value("MBA_DATA_ENCRYPTION_KEY") or os.getenv("MBA_DATA_ENCRYPTION_KEY") or ""
    if configured:
        return str(configured).strip(), "mba_data_encryption_key"
    fallback = os.getenv("SECRET_KEY") or _config_value("SECRET_KEY") or ""
    if fallback:
        derived = base64.urlsafe_b64encode(hashlib.sha256(str(fallback).encode("utf-8")).digest()).decode("ascii")
        return derived, "secret_key_fallback"
    raise RuntimeError("MBA_DATA_ENCRYPTION_KEY must be configured before encrypting sensitive banking details.")


def _sensitive_data_fernet():
    key, _source = _sensitive_data_key_material()
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
        return Fernet(derived)


def _sensitive_key_version():
    _key, source = _sensitive_data_key_material()
    return source


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


def _is_encrypted_sensitive_value(value):
    return isinstance(value, dict) and value.get("__encrypted__") == ENCRYPTED_PAYLOAD_MARKER and bool(value.get("ciphertext"))


def _encrypt_sensitive_value(value):
    if _is_encrypted_sensitive_value(value):
        return value
    if value is None or str(value).strip() == "":
        return value
    ciphertext = _sensitive_data_fernet().encrypt(str(value).encode("utf-8")).decode("ascii")
    return {
        "__encrypted__": ENCRYPTED_PAYLOAD_MARKER,
        "alg": "fernet",
        "key_version": _sensitive_key_version(),
        "ciphertext": ciphertext,
    }


def _encrypt_payload(payload):
    payload = _as_dict(payload)
    changed = False
    for field_name in SENSITIVE_FORM_FIELD_NAMES:
        if field_name not in payload or str(payload.get(field_name) or "").strip() == "":
            continue
        encrypted_value = _encrypt_sensitive_value(payload[field_name])
        if encrypted_value != payload[field_name]:
            payload[field_name] = encrypted_value
            changed = True
    return payload, changed


def _sensitive_document_type(doc_type):
    return str(doc_type or "").startswith(SENSITIVE_DOCUMENT_TYPE_PREFIXES)


def _encrypted_document_bytes(data):
    return bool(data and bytes(data[: len(ENCRYPTED_DOCUMENT_PREFIX)]) == ENCRYPTED_DOCUMENT_PREFIX)


def _encrypt_document_bytes(data):
    if not data or _encrypted_document_bytes(data):
        return data
    return ENCRYPTED_DOCUMENT_PREFIX + _sensitive_data_fernet().encrypt(bytes(data))


def _encrypt_existing_form_payloads(bind):
    if not table_exists("mba_forms") or not column_exists("mba_forms", "payload"):
        return

    forms = sa.table(
        "mba_forms",
        sa.column("id", sa.Integer),
        sa.column("form_type", sa.String),
        sa.column("payload", sa.JSON),
    )
    rows = bind.execute(
        sa.select(forms.c.id, forms.c.form_type, forms.c.payload).where(
            sa.or_(
                forms.c.form_type.like("assessor_banking_%"),
                forms.c.form_type.like("assessor_temp_appointment_%"),
                forms.c.form_type.like("assessor_temp_claim_%"),
            )
        )
    )
    for row in rows:
        payload, changed = _encrypt_payload(row.payload)
        if not changed:
            continue
        bind.execute(
            forms.update()
            .where(forms.c.id == row.id)
            .values(payload=payload)
        )


def _encrypt_existing_generated_documents(bind):
    if not table_exists("mba_project_documents") or not column_exists("mba_project_documents", "file_data"):
        return

    documents = sa.table(
        "mba_project_documents",
        sa.column("id", sa.Integer),
        sa.column("doc_type", sa.String),
        sa.column("file_data", sa.LargeBinary),
    )
    rows = bind.execute(
        sa.select(documents.c.id, documents.c.doc_type, documents.c.file_data).where(
            sa.or_(
                documents.c.doc_type.like("assessor_banking_%"),
                documents.c.doc_type.like("assessor_temp_appointment_%"),
                documents.c.doc_type.like("assessor_temp_claim_%"),
            )
        )
    )
    for row in rows:
        if not _sensitive_document_type(row.doc_type):
            continue
        encrypted_file_data = _encrypt_document_bytes(row.file_data)
        if encrypted_file_data == row.file_data:
            continue
        bind.execute(
            documents.update()
            .where(documents.c.id == row.id)
            .values(file_data=encrypted_file_data)
        )


def upgrade():
    bind = op.get_bind()
    _encrypt_existing_form_payloads(bind)
    _encrypt_existing_generated_documents(bind)


def downgrade():
    # Deliberately keep sensitive data encrypted on downgrade.
    pass
