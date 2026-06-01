"""Store MBA templates and saved signatures in the database.

Revision ID: f8c2d4e6a9b1
Revises: a26f9f092cc3
Create Date: 2026-05-31 00:00:00.000000
"""

from datetime import datetime
import hashlib
import mimetypes
from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

from migrations.schema_helpers import create_index_if_missing, create_table_if_missing, table_exists


revision = "f8c2d4e6a9b1"
down_revision = "a26f9f092cc3"
branch_labels = None
depends_on = None


DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _repo_root():
    return Path(__file__).resolve().parents[2]


def _signature_mime_type(path):
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _scalar(bind, sql, params=None):
    return bind.execute(sa.text(sql), params or {}).scalar()


def _execute(bind, sql, params=None):
    return bind.execute(sa.text(sql), params or {})


def _create_tables():
    create_table_if_missing(
        "mba_user_signatures",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("mba_users.id"), nullable=False),
        sa.Column("file_data", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    create_index_if_missing("ix_mba_user_signatures_user_id", "mba_user_signatures", ["user_id"])
    create_index_if_missing("ix_mba_user_signatures_sha256", "mba_user_signatures", ["sha256"])
    create_index_if_missing("ix_mba_user_signatures_is_active", "mba_user_signatures", ["is_active"])

    create_table_if_missing(
        "mba_document_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_key", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_data", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("uploaded_by_id", sa.Integer(), sa.ForeignKey("mba_users.id"), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.UniqueConstraint("template_key", "version", name="uq_mba_document_template_key_version"),
    )
    create_index_if_missing("ix_mba_document_templates_template_key", "mba_document_templates", ["template_key"])
    create_index_if_missing("ix_mba_document_templates_sha256", "mba_document_templates", ["sha256"])
    create_index_if_missing("ix_mba_document_templates_is_active", "mba_document_templates", ["is_active"])


def _backfill_document_templates(bind):
    template_dir = _repo_root() / "app" / "mba" / "docx_templates"
    if not template_dir.exists():
        return

    now = datetime.utcnow()
    for path in sorted(template_dir.glob("*.docx")):
        data = path.read_bytes()
        template_key = f"mba/docx_templates/{path.name}"
        exists = _scalar(
            bind,
            """
            select count(*)
            from mba_document_templates
            where template_key = :template_key and version = 1
            """,
            {"template_key": template_key},
        )
        if exists:
            continue
        _execute(
            bind,
            """
            insert into mba_document_templates
                (template_key, version, filename, file_data, mime_type, file_size, sha256, is_active, uploaded_at, notes)
            values
                (:template_key, 1, :filename, :file_data, :mime_type, :file_size, :sha256, true, :uploaded_at, :notes)
            """,
            {
                "template_key": template_key,
                "filename": path.name,
                "file_data": data,
                "mime_type": DOCX_MIME_TYPE,
                "file_size": len(data),
                "sha256": _sha256(data),
                "uploaded_at": now,
                "notes": "Imported from packaged Word template during migration.",
            },
        )


def _backfill_user_signatures(bind):
    signature_dir = _repo_root() / "uploads" / "mba_signatures"
    if not signature_dir.exists():
        return

    now = datetime.utcnow()
    for path in sorted(signature_dir.iterdir()):
        if not path.is_file():
            continue
        match = re.match(r"^user_(?P<user_id>\d+)\.(?:png|jpg|jpeg)$", path.name, flags=re.IGNORECASE)
        if not match:
            continue
        user_id = int(match.group("user_id"))
        user_exists = _scalar(bind, "select count(*) from mba_users where id = :user_id", {"user_id": user_id})
        if not user_exists:
            continue

        data = path.read_bytes()
        digest = _sha256(data)
        existing = _scalar(
            bind,
            """
            select count(*)
            from mba_user_signatures
            where user_id = :user_id and sha256 = :sha256
            """,
            {"user_id": user_id, "sha256": digest},
        )
        _execute(bind, "update mba_user_signatures set is_active = false where user_id = :user_id", {"user_id": user_id})
        if existing:
            _execute(
                bind,
                """
                update mba_user_signatures
                set is_active = true, updated_at = :updated_at
                where user_id = :user_id and sha256 = :sha256
                """,
                {"user_id": user_id, "sha256": digest, "updated_at": now},
            )
        else:
            _execute(
                bind,
                """
                insert into mba_user_signatures
                    (user_id, file_data, mime_type, file_size, sha256, source, is_active, created_at, updated_at)
                values
                    (:user_id, :file_data, :mime_type, :file_size, :sha256, :source, true, :created_at, :updated_at)
                """,
                {
                    "user_id": user_id,
                    "file_data": data,
                    "mime_type": _signature_mime_type(path),
                    "file_size": len(data),
                    "sha256": digest,
                    "source": "filesystem_import",
                    "created_at": now,
                    "updated_at": now,
                },
            )
        _execute(bind, "update mba_users set has_signature = true where id = :user_id", {"user_id": user_id})


def _backfill_project_documents(bind):
    forms_dir = _repo_root() / "uploads" / "mba_forms"
    if not forms_dir.exists() or not table_exists("mba_project_documents"):
        return

    rows = bind.execute(
        sa.text(
            """
            select id, project_id, stored_name, original_name, mime_type
            from mba_project_documents
            where file_data is null and stored_name is not null
            """
        )
    )
    for row in rows:
        path = forms_dir / str(row.project_id) / row.stored_name
        if not path.exists() or not path.is_file():
            continue
        data = path.read_bytes()
        mime_type = row.mime_type or mimetypes.guess_type(row.original_name or row.stored_name)[0] or "application/octet-stream"
        _execute(
            bind,
            """
            update mba_project_documents
            set file_data = :file_data, mime_type = :mime_type, file_size = :file_size
            where id = :id
            """,
            {
                "file_data": data,
                "mime_type": mime_type,
                "file_size": len(data),
                "id": row.id,
            },
        )


def upgrade():
    bind = op.get_bind()
    _create_tables()
    _backfill_document_templates(bind)
    _backfill_user_signatures(bind)
    _backfill_project_documents(bind)


def downgrade():
    if table_exists("mba_document_templates"):
        op.drop_index("ix_mba_document_templates_is_active", table_name="mba_document_templates")
        op.drop_index("ix_mba_document_templates_sha256", table_name="mba_document_templates")
        op.drop_index("ix_mba_document_templates_template_key", table_name="mba_document_templates")
        op.drop_table("mba_document_templates")
    if table_exists("mba_user_signatures"):
        op.drop_index("ix_mba_user_signatures_is_active", table_name="mba_user_signatures")
        op.drop_index("ix_mba_user_signatures_sha256", table_name="mba_user_signatures")
        op.drop_index("ix_mba_user_signatures_user_id", table_name="mba_user_signatures")
        op.drop_table("mba_user_signatures")
