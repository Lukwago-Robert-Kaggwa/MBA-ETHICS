import os
import shutil
import mimetypes
import secrets

import click
from sqlalchemy import text

from .extensions import db
from .models import EthicsUser, MbaUser
from .seeds import seed_mba_disciplines


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create all MBA and Ethics tables in the configured database."""
        db.create_all()
        seed_mba_disciplines()
        db.session.commit()
        print("Created database tables.")

    @app.cli.command("create-admins")
    def create_admins():
        """Create starter admin users for both systems."""
        from .models import EthicsRole, MbaRole

        created_accounts = []

        def generated_password():
            return secrets.token_urlsafe(18)

        mba_admin = MbaUser.find_by_email("mba.admin@uj.ac.za")
        if not mba_admin:
            mba_admin = MbaUser(email="mba.admin@uj.ac.za", role=MbaRole.MAIN_ADMIN.value)
            password = generated_password()
            mba_admin.set_password(password)
            db.session.add(mba_admin)
            created_accounts.append((mba_admin.email, password))

        ethics_admin = EthicsUser.find_by_email("ethics.admin@uj.ac.za")
        if not ethics_admin:
            ethics_admin = EthicsUser(email="ethics.admin@uj.ac.za", role=EthicsRole.SUPER_ADMIN.value)
            password = generated_password()
            ethics_admin.set_password(password)
            db.session.add(ethics_admin)
            created_accounts.append((ethics_admin.email, password))

        starter_users = [
            ("ethics.supervisor@uj.ac.za", EthicsRole.SUPERVISOR.value),
            ("ethics.reviewer@uj.ac.za", EthicsRole.REVIEWER.value),
            ("ethics.rec@uj.ac.za", EthicsRole.REC.value),
        ]
        for email, role in starter_users:
            user = EthicsUser.find_by_email(email)
            if not user:
                user = EthicsUser(email=email, role=role, first_name=role.replace("_", " ").title())
                password = generated_password()
                user.set_password(password)
                db.session.add(user)
                created_accounts.append((user.email, password))

        db.session.commit()
        if created_accounts:
            click.echo("Starter accounts created with generated temporary passwords:")
            for email, password in created_accounts:
                click.echo(f"  {email}: {password}")
            click.echo("Store these values securely. Existing accounts were not reset.")
        else:
            click.echo("Starter accounts already exist. No passwords were changed.")

    @app.cli.command("sync-db")
    def sync_db():
        """Create new tables and add columns needed by the integrated workflow."""
        db.create_all()
        statements = [
            """
            create table if not exists ethcis_form_drafts (
                id serial primary key,
                student_id integer not null references ethcis_users(id),
                form_type varchar(1) not null,
                payload json not null default '{}'::json,
                created_at timestamp without time zone not null default now(),
                updated_at timestamp without time zone not null default now(),
                constraint uq_ethcis_student_form_draft unique (student_id, form_type)
            )
            """,
            "alter table ethcis_users drop constraint if exists ethcis_user_role_check",
            """
            alter table ethcis_users add constraint ethcis_user_role_check
            check (role in ('super_admin','admin','reviewer','supervisor','student','rec','dean'))
            """,
            "alter table ethcis_users add column if not exists student_number varchar(40)",
            "alter table ethcis_users add column if not exists supervisor_id integer",
            "alter table ethcis_users add column if not exists staff_number varchar(80)",
            "alter table ethcis_users add column if not exists specialisation varchar(180)",
            "alter table ethcis_users add column if not exists authenticated_student boolean not null default false",
            "alter table ethcis_users add column if not exists watched_demo boolean not null default false",
            "alter table ethcis_users add column if not exists popia_confirmed_at timestamp without time zone",
            "alter table ethcis_users add column if not exists popia_notice_version varchar(40)",
            "alter table ethcis_users add column if not exists popia_confirmed_ip varchar(64)",
            "alter table ethcis_users add column if not exists popia_confirmed_user_agent varchar(255)",
            "alter table mba_users add column if not exists popia_confirmed_at timestamp without time zone",
            "alter table mba_users add column if not exists popia_notice_version varchar(40)",
            "alter table mba_users add column if not exists popia_confirmed_ip varchar(64)",
            "alter table mba_users add column if not exists popia_confirmed_user_agent varchar(255)",
            "alter table mba_student_profiles alter column student_number drop not null",
            "alter table mba_projects add column if not exists primary_supervisor_invitation_status varchar(20)",
            "alter table mba_projects add column if not exists assessor_1_invitation_status varchar(20)",
            "alter table mba_projects add column if not exists assessor_2_invitation_status varchar(20)",
            "alter table mba_projects add column if not exists assessor_3_invitation_status varchar(20)",
            "alter table mba_projects add column if not exists assessor_1_hdc_decision varchar(20)",
            "alter table mba_projects add column if not exists assessor_2_hdc_decision varchar(20)",
            "alter table mba_projects add column if not exists assessor_1_hdc_decision_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_2_hdc_decision_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_1_hdc_decision_assessor_id integer",
            "alter table mba_projects add column if not exists assessor_2_hdc_decision_assessor_id integer",
            "alter table mba_projects add column if not exists assessor_1_invited_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_1_reminder_sent_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_2_invited_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_2_reminder_sent_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_3_invited_at timestamp without time zone",
            "alter table mba_projects add column if not exists assessor_3_reminder_sent_at timestamp without time zone",
            """
            update mba_projects
            set assessor_1_invited_at = invitations_sent_at
            where assessor_1_invitation_status = 'pending'
              and assessor_1_id is not null
              and assessor_1_invited_at is null
              and invitations_sent_at is not null
            """,
            """
            update mba_projects
            set assessor_2_invited_at = invitations_sent_at
            where assessor_2_invitation_status = 'pending'
              and assessor_2_id is not null
              and assessor_2_invited_at is null
              and invitations_sent_at is not null
            """,
            """
            update mba_projects
            set assessor_3_invited_at = invitations_sent_at
            where assessor_3_invitation_status = 'pending'
              and assessor_3_id is not null
              and assessor_3_invited_at is null
              and invitations_sent_at is not null
            """,
            "alter table mba_projects add column if not exists assignment_confirmed boolean not null default false",
            "alter table mba_projects add column if not exists invitations_sent_at timestamp without time zone",
            "alter table mba_projects add column if not exists supervisor_confirmed boolean not null default false",
            "alter table mba_projects add column if not exists assessors_confirmed boolean not null default false",
            "alter table mba_projects add column if not exists assessors_nominated_at timestamp without time zone",
            "alter table mba_projects add column if not exists dissertation_released_to_assessors boolean not null default false",
            "alter table mba_projects add column if not exists dissertation_released_at timestamp without time zone",
            "alter table mba_projects add column if not exists supervisor_pool_released_at timestamp without time zone",
            "alter table mba_projects add column if not exists supervisor_pool_released_by_id integer",
            "alter table mba_projects add column if not exists dissertation_moodle_request_sent_at timestamp without time zone",
            "alter table mba_projects add column if not exists dissertation_resubmission_requested_at timestamp without time zone",
            "alter table mba_projects add column if not exists dissertation_resubmission_open boolean not null default false",
            "alter table mba_projects add column if not exists dissertation_resubmission_opened_at timestamp without time zone",
            "alter table mba_projects add column if not exists additional_assessment_requested_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_requested_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_student_resubmitted_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_supervisor_approved_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_supervisor_comments text",
            "alter table mba_projects add column if not exists corrections_supervisor_rejected_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_supervisor_rejection_comments text",
            "alter table mba_projects add column if not exists assessment_results_forwarded_to_supervisor_at timestamp without time zone",
            "alter table mba_projects add column if not exists corrections_released_to_student_at timestamp without time zone",
            "alter table mba_projects add column if not exists module_completion_status varchar(60) not null default 'not_checked'",
            "alter table mba_projects add column if not exists module_completion_marks_email varchar(255)",
            "alter table mba_projects add column if not exists module_completion_verification_token varchar(128)",
            "alter table mba_projects add column if not exists module_completion_requested_at timestamp without time zone",
            "alter table mba_projects add column if not exists module_completion_responded_at timestamp without time zone",
            "alter table mba_projects add column if not exists module_completion_response varchar(10)",
            """
            create unique index if not exists uq_mba_projects_module_completion_token
            on mba_projects (module_completion_verification_token)
            where module_completion_verification_token is not null
            """,
            "alter table mba_projects add column if not exists jbs5_hdc_approved_at timestamp without time zone",
            "alter table mba_projects add column if not exists results_submitted_to_hdc_at timestamp without time zone",
            "alter table mba_projects add column if not exists results_hdc_decision varchar(20)",
            "alter table mba_projects add column if not exists results_hdc_reviewed_at timestamp without time zone",
            "alter table mba_projects add column if not exists results_hdc_comments text",
            "alter table mba_projects add column if not exists jbs5_hdc_comments text",
            """
            update mba_projects
            set jbs5_hdc_comments = hdc_comments
            where jbs5_hdc_comments is null
                and hdc_comments is not null
                and project_status in ('jbs5_submitted_to_hdc', 'jbs5_hdc_approved', 'jbs5_hdc_declined')
            """,
            "alter table mba_projects add column if not exists results_released_to_supervisor_at timestamp without time zone",
            "alter table mba_projects add column if not exists supervisor_title_change_requested_at timestamp without time zone",
            "alter table mba_projects add column if not exists supervisor_title_change_request text",
            "alter table mba_projects add column if not exists supervisor_title_change_resolved_at timestamp without time zone",
            "alter table mba_forms add column if not exists student_signed boolean not null default false",
            "alter table mba_forms add column if not exists supervisor_signed boolean not null default false",
            """
            create table if not exists mba_project_documents (
                id serial primary key,
                project_id integer not null references mba_projects(id),
                doc_type varchar(60) not null,
                original_name varchar(255) not null,
                stored_name varchar(255) not null,
                uploaded_by_id integer not null references mba_users(id),
                uploaded_at timestamp without time zone not null default now()
            )
            """,
            "alter table mba_project_documents add column if not exists file_data bytea",
            "alter table mba_project_documents add column if not exists mime_type varchar(120)",
            "alter table mba_project_documents add column if not exists file_size integer",
            """
            delete from mba_project_documents older
            using mba_project_documents newer
            where older.project_id = newer.project_id
                and older.doc_type = newer.doc_type
                and older.id < newer.id
            """,
            """
            create unique index if not exists uq_mba_project_documents_project_doc_type
            on mba_project_documents (project_id, doc_type)
            """,
            """
            create table if not exists ethcis_submission_files (
                id serial primary key,
                submission_id integer references ethcis_form_submissions(id),
                student_id integer not null references ethcis_users(id),
                field_name varchar(120),
                original_name varchar(255) not null,
                stored_name varchar(255) not null unique,
                file_data bytea not null,
                mime_type varchar(120),
                file_size integer,
                uploaded_at timestamp without time zone not null default now()
            )
            """,
            "create index if not exists ix_ethcis_submission_files_submission_id on ethcis_submission_files (submission_id)",
            "create index if not exists ix_ethcis_submission_files_student_id on ethcis_submission_files (student_id)",
            "create unique index if not exists ix_ethcis_submission_files_stored_name on ethcis_submission_files (stored_name)",
            """
            create table if not exists mba_project_supervisor_invitations (
                id serial primary key,
                project_id integer not null references mba_projects(id),
                supervisor_id integer not null references mba_users(id),
                status varchar(20) not null default 'pending',
                invited_at timestamp without time zone not null default now(),
                reminder_sent_at timestamp without time zone,
                responded_at timestamp without time zone
            )
            """,
            "alter table mba_project_supervisor_invitations add column if not exists reminder_sent_at timestamp without time zone",
            """
            create table if not exists mba_reminder_states (
                id serial primary key,
                reminder_key varchar(255) not null unique,
                last_sent_at timestamp without time zone,
                last_sent_by_id integer references mba_users(id),
                dismissed_at timestamp without time zone,
                dismissed_by_id integer references mba_users(id),
                created_at timestamp without time zone not null default now(),
                updated_at timestamp without time zone not null default now()
            )
            """,
            """
            create table if not exists mba_disciplines (
                id serial primary key,
                name varchar(160) not null unique,
                is_active boolean not null default true,
                sort_order integer not null default 0,
                created_at timestamp without time zone not null default now()
            )
            """,
            "alter table mba_projects add column if not exists discipline_id integer",
            "alter table mba_scholar_profiles add column if not exists research_themes text",
            "alter table mba_scholar_profiles add column if not exists research_interests text",
            "alter table mba_scholar_profiles add column if not exists research_disciplines text",
            "alter table mba_scholar_profiles add column if not exists students_supervised_total integer not null default 0",
            "alter table mba_scholar_profiles add column if not exists students_assessed_total integer not null default 0",
            "alter table mba_scholar_profiles add column if not exists publication_count integer not null default 0",
            "alter table mba_scholar_profiles add column if not exists selected_publications text",
            "alter table mba_scholar_profiles add column if not exists scholarly_profile_links text",
        ]
        for statement in statements:
            db.session.execute(text(statement))

        seed_mba_disciplines()
        db.session.commit()
        print("Database schema synced.")

    @app.cli.command("secure-ethics-uploads")
    def secure_ethics_uploads():
        """Move legacy Ethics uploads out of the public static directory."""
        legacy_dir = os.path.join(app.root_path, "static", "uploads", "ethics")
        secure_dir = os.path.abspath(os.path.join(app.root_path, "..", "uploads", "ethics"))
        os.makedirs(secure_dir, exist_ok=True)

        moved = 0
        skipped = 0
        missing = 0

        if not os.path.isdir(legacy_dir):
            print("No legacy public Ethics upload directory was found.")
            return

        for name in os.listdir(legacy_dir):
            legacy_path = os.path.join(legacy_dir, name)
            secure_path = os.path.join(secure_dir, name)
            if not os.path.isfile(legacy_path):
                continue
            if os.path.exists(secure_path):
                os.remove(legacy_path)
                skipped += 1
                continue
            shutil.move(legacy_path, secure_path)
            moved += 1

        try:
            missing = len(
                [
                    name
                    for name in os.listdir(legacy_dir)
                    if os.path.isfile(os.path.join(legacy_dir, name))
                ]
            )
        except OSError:
            missing = 0

        print(
            f"Ethics uploads secured. moved={moved}, skipped_existing={skipped}, remaining_in_legacy_dir={missing}"
        )

    @app.cli.command("backfill-document-bytes")
    def backfill_document_bytes():
        """Copy legacy filesystem documents into database byte columns."""
        from .models import EthicsFormSubmission, EthicsSubmissionFile, MbaProjectDocument

        def mime_for(filename):
            guessed, _encoding = mimetypes.guess_type(filename or "")
            return guessed or "application/octet-stream"

        mba_uploads_root = os.path.abspath(os.path.join(app.root_path, "..", "uploads", "mba_forms"))
        ethics_upload_roots = [
            os.path.abspath(os.path.join(app.root_path, "..", "uploads", "ethics")),
            os.path.abspath(os.path.join(app.root_path, "static", "uploads", "ethics")),
        ]

        mba_backfilled = 0
        mba_missing = 0
        for doc in MbaProjectDocument.query.all():
            if doc.file_data:
                continue
            path = os.path.join(mba_uploads_root, str(doc.project_id), doc.stored_name or "")
            if not doc.stored_name or not os.path.isfile(path):
                mba_missing += 1
                continue
            with open(path, "rb") as fh:
                data = fh.read()
            doc.file_data = data
            doc.mime_type = doc.mime_type or mime_for(doc.original_name or doc.stored_name)
            doc.file_size = len(data)
            mba_backfilled += 1

        def payload_file_items(submission):
            for section in (submission.payload or {}).get("sections", []):
                for answer in section.get("answers", []):
                    value = answer.get("value")
                    if not isinstance(value, list):
                        continue
                    for item in value:
                        if isinstance(item, dict) and item.get("stored_name"):
                            yield answer.get("name"), item

        ethics_backfilled = 0
        ethics_missing = 0
        for submission in EthicsFormSubmission.query.all():
            for field_name, item in payload_file_items(submission):
                stored_name = item.get("stored_name")
                if not stored_name or os.path.basename(stored_name) != stored_name:
                    continue
                record = EthicsSubmissionFile.query.filter_by(stored_name=stored_name).first()
                if record and record.file_data:
                    if record.submission_id is None:
                        record.submission_id = submission.id
                    continue
                file_path = None
                for root in ethics_upload_roots:
                    candidate = os.path.join(root, stored_name)
                    if os.path.isfile(candidate):
                        file_path = candidate
                        break
                if not file_path:
                    ethics_missing += 1
                    continue
                with open(file_path, "rb") as fh:
                    data = fh.read()
                if not record:
                    record = EthicsSubmissionFile(
                        submission_id=submission.id,
                        student_id=submission.student_id,
                        field_name=field_name,
                        original_name=item.get("filename") or stored_name,
                        stored_name=stored_name,
                        file_data=data,
                        mime_type=mime_for(item.get("filename") or stored_name),
                        file_size=len(data),
                    )
                    db.session.add(record)
                else:
                    record.submission_id = record.submission_id or submission.id
                    record.student_id = record.student_id or submission.student_id
                    record.field_name = record.field_name or field_name
                    record.file_data = data
                    record.mime_type = record.mime_type or mime_for(item.get("filename") or stored_name)
                    record.file_size = len(data)
                ethics_backfilled += 1

        db.session.commit()
        print(
            "Document bytes backfilled. "
            f"mba={mba_backfilled}, mba_missing={mba_missing}, "
            f"ethics={ethics_backfilled}, ethics_missing={ethics_missing}"
        )

    @app.cli.command("create-mba-staff")
    @click.option("--email", required=True, help="Staff email address")
    @click.option("--password", required=True, help="Initial password")
    @click.option("--role", required=True, type=click.Choice(["scholar", "examiner", "hdc", "admin", "main_admin"]))
    @click.option(
        "--scholar-role",
        "scholar_role",
        type=click.Choice(["supervisor", "examiner", "both"]),
        default=None,
        help="Required when role is scholar and ignored for other roles.",
    )
    @click.option("--first-name", default="", help="Optional first name")
    @click.option("--last-name", default="", help="Optional last name")
    def create_mba_staff(email, password, role, scholar_role, first_name, last_name):
        """Create or update an MBA staff account for supervisor/examiner assignment."""
        from .models import MbaRole, MbaScholarRole, normalize_email

        clean_email = normalize_email(email)
        allowed_roles = {
            "scholar": MbaRole.SCHOLAR.value,
            "examiner": MbaRole.EXAMINER.value,
            "hdc": MbaRole.HDC.value,
            "admin": MbaRole.ADMIN.value,
            "main_admin": MbaRole.MAIN_ADMIN.value,
        }

        if role == MbaRole.SCHOLAR.value and not scholar_role:
            raise click.UsageError("--scholar-role is required when --role scholar is used.")
        if role != MbaRole.SCHOLAR.value and scholar_role:
            raise click.UsageError("--scholar-role can only be used when --role scholar is selected.")

        if scholar_role and scholar_role not in {
            MbaScholarRole.SUPERVISOR.value,
            MbaScholarRole.EXAMINER.value,
            MbaScholarRole.BOTH.value,
        }:
            raise click.UsageError("Invalid --scholar-role value.")

        user = MbaUser.find_by_email(clean_email)
        created = False
        if not user:
            user = MbaUser(email=clean_email)
            created = True
            db.session.add(user)

        user.role = allowed_roles[role]
        user.scholar_role = scholar_role if role == MbaRole.SCHOLAR.value else None
        user.first_name = first_name or user.first_name
        user.last_name = last_name or user.last_name
        user.is_active = True
        user.set_password(password)

        db.session.commit()
        action = "created" if created else "updated"
        print(f"MBA staff user {action}: {user.email} ({user.role}, scholar_role={user.scholar_role})")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Inject dummy students, scholar profiles, and projects at every workflow stage for demo purposes."""
        import uuid
        from datetime import datetime, timedelta

        from .models import (
            MbaDiscipline,
            MbaForm,
            MbaProject,
            MbaProjectDocument,
            MbaRole,
            MbaScholarProfile,
            MbaStudentProfile,
            MbaUser,
            ProjectStatus,
        )

        # ── Stub PDF bytes (minimal valid PDF, not a real document) ───────────
        STUB_PDF = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj "
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj "
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R>>endobj "
            b"xref\n0 4\n0000000000 65535 f \n"
            b"trailer<</Size 4/Root 1 0 R>>\nstartxref\n9\n%%EOF\n"
        )
        demo_password = os.getenv("MBA_DEMO_SEED_PASSWORD") or secrets.token_urlsafe(18)

        uploads_root = os.path.join(app.root_path, "..", "uploads", "mba_forms")

        def write_stub(project_id, stored_name):
            folder = os.path.join(uploads_root, str(project_id))
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, stored_name)
            if not os.path.exists(path):
                with open(path, "wb") as fh:
                    fh.write(STUB_PDF)

        def upsert_doc(project, doc_type, label, uploader_id):
            stored = f"demo_{uuid.uuid4().hex[:12]}.pdf"
            doc = MbaProjectDocument.query.filter_by(project_id=project.id, doc_type=doc_type).first()
            if not doc:
                doc = MbaProjectDocument(
                    project_id=project.id,
                    doc_type=doc_type,
                    original_name=f"{label}.pdf",
                    stored_name=stored,
                    file_data=STUB_PDF,
                    mime_type="application/pdf",
                    file_size=len(STUB_PDF),
                    uploaded_by_id=uploader_id,
                    uploaded_at=datetime.utcnow() - timedelta(days=5),
                )
                db.session.add(doc)
            else:
                doc.file_data = doc.file_data or STUB_PDF
                doc.mime_type = doc.mime_type or "application/pdf"
                doc.file_size = doc.file_size or len(STUB_PDF)
            write_stub(project.id, doc.stored_name)

        def upsert_form(project_id, form_type, payload):
            frm = MbaForm.query.filter_by(project_id=project_id, form_type=form_type).first()
            if not frm:
                frm = MbaForm(
                    project_id=project_id,
                    form_type=form_type,
                    payload=payload,
                    student_signed=True,
                    submitted_at=datetime.utcnow() - timedelta(days=5),
                )
                db.session.add(frm)
            else:
                frm.payload = payload

        # ── Disciplines ───────────────────────────────────────────────────────
        disc_names = [
            "Finance",
            "Marketing",
            "Strategy and Leadership",
            "Operations Management",
            "Human Resource Management",
        ]
        disciplines = {}
        for name in disc_names:
            d = MbaDiscipline.query.filter_by(name=name).first()
            if not d:
                d = MbaDiscipline(name=name, is_active=True, sort_order=0)
                db.session.add(d)
                db.session.flush()
            disciplines[name] = d
        db.session.flush()
        print("Disciplines ready.")

        # ── Scholar profiles ──────────────────────────────────────────────────
        scholar_data = [
            (
                "mba.supervisor@uj.ac.za",
                "Jane", "Supervisor",
                {
                    "name": "Jane", "surname": "Supervisor", "title": "Prof",
                    "department": "Finance and Investment Management",
                    "position": "Associate Professor",
                    "affiliation": "University of Johannesburg",
                    "qualification": "PhD Finance",
                    "skills": "financial modelling corporate governance portfolio management",
                    "research_themes": "corporate finance investment banking financial inclusion",
                    "research_interests": "banking finance corporate governance JSE strategy leadership",
                    "research_disciplines": "finance strategy leadership general management",
                    "academic_experience": 15,
                    "approved_before": True,
                },
            ),
            (
                "mba.examiner1@uj.ac.za",
                "Tom", "ExaminerOne",
                {
                    "name": "Tom", "surname": "ExaminerOne", "title": "Dr",
                    "department": "Finance",
                    "position": "Senior Lecturer",
                    "affiliation": "University of Johannesburg",
                    "qualification": "PhD Banking and Finance",
                    "skills": "investment analysis banking financial risk",
                    "research_themes": "digital banking financial inclusion banking sector corporate governance",
                    "research_interests": "finance banking investment JSE digital banking",
                    "research_disciplines": "finance banking investment",
                    "academic_experience": 10,
                    "approved_before": True,
                },
            ),
            (
                "mba.examiner2@uj.ac.za",
                "Sara", "ExaminerTwo",
                {
                    "name": "Sara", "surname": "ExaminerTwo", "title": "Dr",
                    "department": "Marketing and Retail Management",
                    "position": "Senior Lecturer",
                    "affiliation": "University of Johannesburg",
                    "qualification": "PhD Marketing",
                    "skills": "digital marketing consumer behaviour brand management social media",
                    "research_themes": "digital marketing SME marketing strategy social media brand",
                    "research_interests": "marketing digital social media consumer behaviour SME",
                    "research_disciplines": "marketing business strategy",
                    "academic_experience": 8,
                    "approved_before": True,
                },
            ),
            (
                "mba.examiner3@uj.ac.za",
                "Mark", "ExaminerThree",
                {
                    "name": "Mark", "surname": "ExaminerThree", "title": "Dr",
                    "department": "Industrial Psychology and People Management",
                    "position": "Associate Professor",
                    "affiliation": "University of Johannesburg",
                    "qualification": "PhD Human Resource Management",
                    "skills": "talent management employee engagement organisational behaviour",
                    "research_themes": "employee engagement retention human resource management talent",
                    "research_interests": "HR people management engagement retention banking talent",
                    "research_disciplines": "human resource management operations management",
                    "academic_experience": 12,
                    "approved_before": False,
                },
            ),
            (
                "mba.both@uj.ac.za",
                "Alex", "BothRoles",
                {
                    "name": "Alex", "surname": "BothRoles", "title": "Prof",
                    "department": "Business Management",
                    "position": "Professor",
                    "affiliation": "University of Johannesburg",
                    "qualification": "PhD Strategic Management",
                    "skills": "strategic planning leadership organisational performance change management",
                    "research_themes": "strategic leadership organisational performance corporate strategy",
                    "research_interests": "strategy leadership performance corporate governance management",
                    "research_disciplines": "strategy leadership general management",
                    "academic_experience": 20,
                    "approved_before": True,
                },
            ),
        ]

        scholars = {}
        for email, first, last, profile_data in scholar_data:
            user = MbaUser.find_by_email(email)
            if not user:
                print(f"  SKIP (not found): {email} — run flask create-mba-staff first")
                continue
            user.first_name = first
            user.last_name = last
            user.has_profile = True
            profile = user.scholar_profile
            if not profile:
                profile = MbaScholarProfile(user_id=user.id)
                db.session.add(profile)
                db.session.flush()
            for k, v in profile_data.items():
                setattr(profile, k, v)
            scholars[email] = user
        db.session.flush()
        print("Scholar profiles updated.")

        # ── Students ──────────────────────────────────────────────────────────
        student_data = [
            ("219001001", "Thabo", "Molefe", "219001001@student.uj.ac.za", "MBA General", "Block A"),
            ("219001002", "Nomsa", "Dlamini", "219001002@student.uj.ac.za", "MBA Marketing", "Block B"),
            ("219001003", "Sipho", "Nkosi",   "219001003@student.uj.ac.za", "MBA Strategy", "Block A"),
            ("219001004", "Lerato", "Sithole", "219001004@student.uj.ac.za", "MBA Finance", "Block C"),
        ]

        students = {}
        for student_number, first, last, email, module, block_id in student_data:
            user = MbaUser.find_by_email(email)
            if not user:
                user = MbaUser(email=email, role=MbaRole.STUDENT.value, first_name=first, last_name=last, is_active=True)
                user.set_password(demo_password)
                db.session.add(user)
                db.session.flush()
            user.first_name = first
            user.last_name = last
            user.has_profile = True
            profile = user.student_profile
            if not profile:
                profile = MbaStudentProfile(
                    user_id=user.id,
                    student_number=student_number,
                    name=first,
                    surname=last,
                    contact=f"07{student_number[-8:]}",
                    module=module,
                    block_id=block_id,
                    degree="MBA",
                )
                db.session.add(profile)
            students[student_number] = user
        db.session.flush()
        print("Student accounts ready.")

        admin = MbaUser.find_by_email("mba.admin@uj.ac.za")
        admin_id = admin.id if admin else list(students.values())[0].id
        supervisor = scholars.get("mba.supervisor@uj.ac.za")
        examiner1 = scholars.get("mba.examiner1@uj.ac.za")
        examiner2 = scholars.get("mba.examiner2@uj.ac.za")
        examiner3 = scholars.get("mba.examiner3@uj.ac.za")

        # ── Project factory ───────────────────────────────────────────────────
        def get_or_create_project(student_id, title):
            p = MbaProject.query.filter_by(student_id=student_id, project_title=title).first()
            if not p:
                general_discipline = disciplines.get("General")
                p = MbaProject(
                    student_id=student_id,
                    project_title=title,
                    project_description="Demo project.",
                    discipline=general_discipline.name if general_discipline else "General",
                    discipline_id=general_discipline.id if general_discipline else None,
                )
                db.session.add(p)
                db.session.flush()
            return p

        now = datetime.utcnow()

        # ── PROJECT 1: CREATED ────────────────────────────────────────────────
        # Thabo — Finance project, just created and JBS5 filled. Shows on admin queue once submitted.
        s1 = students["219001001"]
        d_finance = disciplines["Finance"]
        p1 = get_or_create_project(s1.id, "Impact of Digital Banking on Financial Inclusion in South Africa")
        p1.project_description = (
            "This study investigates how digital banking innovations affect financial inclusion "
            "among unbanked populations in South Africa, with focus on mobile banking adoption."
        )
        p1.discipline = d_finance.name
        p1.discipline_id = d_finance.id
        p1.project_status = ProjectStatus.CREATED.value
        p1.created_at = now - timedelta(days=2)
        upsert_form(p1.id, "jbs5", {
            "full_name": "Thabo Molefe", "student_number": "219001001",
            "email": s1.email, "contact": "0712345001",
            "programme": "MBA", "block_id": "Block A",
            "research_title": p1.project_title,
            "discipline": d_finance.name,
            "abstract": "Digital banking innovations and their impact on financial inclusion in South Africa.",
            "research_approach": "Mixed methods — survey and case study",
            "research_objectives": "1. Assess adoption rate. 2. Measure impact on unbanked. 3. Identify barriers.",
            "expected_outcomes": "Recommendations for banking policy and product design.",
        })
        upsert_doc(p1, "jbs5", "JBS5_Research_Proposal", s1.id)
        print("Project 1 (CREATED) done.")

        # ── PROJECT 2: ADMIN_SUBMITTED ────────────────────────────────────────
        # Nomsa — Marketing project submitted to admin. Admin sees it in queue with auto-suggest.
        s2 = students["219001002"]
        d_marketing = disciplines["Marketing"]
        p2 = get_or_create_project(s2.id, "Social Media Marketing Strategies for SMEs in Gauteng")
        p2.project_description = (
            "An analysis of social media marketing effectiveness for small and medium enterprises "
            "in Gauteng, examining platform selection, content strategy, and consumer engagement."
        )
        p2.discipline = d_marketing.name
        p2.discipline_id = d_marketing.id
        p2.project_status = ProjectStatus.ADMIN_SUBMITTED.value
        p2.created_at = now - timedelta(days=7)
        upsert_form(p2.id, "jbs5", {
            "full_name": "Nomsa Dlamini", "student_number": "219001002",
            "email": s2.email, "contact": "0712345002",
            "programme": "MBA Marketing", "block_id": "Block B",
            "research_title": p2.project_title,
            "discipline": d_marketing.name,
            "abstract": "Social media strategies and SME growth in Gauteng.",
            "research_approach": "Quantitative survey of 150 SMEs",
            "research_objectives": "1. Identify top platforms. 2. Measure ROI. 3. Compare strategies.",
            "expected_outcomes": "A practical social media playbook for Gauteng SMEs.",
        })
        upsert_doc(p2, "jbs5", "JBS5_Research_Proposal", s2.id)
        print("Project 2 (ADMIN_SUBMITTED) done.")

        # ── PROJECT 3: ADMIN_APPROVED ─────────────────────────────────────────
        # Sipho — Strategy project approved to HDC. Supervisor accepted, assessors nominated, waiting HDC.
        s3 = students["219001003"]
        d_strategy = disciplines["Strategy and Leadership"]
        p3 = get_or_create_project(s3.id, "Strategic Leadership and Organisational Performance in South African Retail")
        p3.project_description = (
            "This research examines the relationship between strategic leadership styles and "
            "organisational performance in South African retail companies listed on the JSE."
        )
        p3.discipline = d_strategy.name
        p3.discipline_id = d_strategy.id
        p3.project_status = ProjectStatus.ADMIN_APPROVED.value
        p3.created_at = now - timedelta(days=21)
        if supervisor:
            p3.primary_supervisor_id = supervisor.id
            p3.primary_supervisor_invitation_status = "accepted"
            p3.supervisor_confirmed = True
            p3.supervisor_accepted_at = now - timedelta(days=14)
        if examiner1:
            p3.assessor_1_id = examiner1.id
            p3.assessor_1_invitation_status = None
        if examiner2:
            p3.assessor_2_id = examiner2.id
            p3.assessor_2_invitation_status = None
        if examiner3:
            p3.assessor_3_id = examiner3.id
            p3.assessor_3_invitation_status = None
        p3.assessors_confirmed = False
        for form_type, label, payload in [
            ("jbs5", "JBS5_Research_Proposal", {
                "full_name": "Sipho Nkosi", "student_number": "219001003",
                "email": s3.email, "research_title": p3.project_title,
                "discipline": d_strategy.name,
                "abstract": "Strategic leadership styles and firm performance in JSE-listed retail.",
            }),
            ("supervisor_agreement", "Supervisor_Agreement", {
                "supervisor_full_name": "Jane Supervisor", "position": "Associate Professor",
                "department": "Finance and Investment Management",
                "student_name": "Sipho Nkosi", "student_number": "219001003",
                "research_title": p3.project_title,
                "capacity_statement": "I confirm I have capacity to supervise this project.",
                "agreement_declaration": True,
            }),
            ("jbs10", "JBS10_Submission_Form", {
                "full_name": "Sipho Nkosi", "student_number": "219001003",
                "email": s3.email, "research_title": p3.project_title,
                "supervisor_name": "Jane Supervisor",
                "submission_date": (now - timedelta(days=10)).strftime("%Y-%m-%d"),
                "declaration": True,
            }),
            ("intent_to_submit", "Intent_to_Submit", {
                "full_name": "Sipho Nkosi", "student_number": "219001003",
                "email": s3.email, "research_title": p3.project_title,
                "supervisor_name": "Jane Supervisor",
                "intended_date": (now - timedelta(days=8)).strftime("%Y-%m-%d"),
            }),
        ]:
            upsert_form(p3.id, form_type, payload)
            upsert_doc(p3, form_type, label, s3.id)
        print("Project 3 (ADMIN_APPROVED) done.")

        # ── PROJECT 4: HDC_VERIFIED ───────────────────────────────────────────
        # Lerato — HR project. HDC verified assessors. Assessor 1 accepted, 2 & 3 still pending.
        s4 = students["219001004"]
        d_hr = disciplines["Human Resource Management"]
        p4 = get_or_create_project(s4.id, "Employee Engagement and Retention in South African Banking Sector")
        p4.project_description = (
            "This study explores employee engagement drivers and their influence on staff retention "
            "within major South African commercial banks, with focus on talent management practices."
        )
        p4.discipline = d_hr.name
        p4.discipline_id = d_hr.id
        p4.project_status = ProjectStatus.HDC_VERIFIED.value
        p4.created_at = now - timedelta(days=45)
        if supervisor:
            p4.primary_supervisor_id = supervisor.id
            p4.primary_supervisor_invitation_status = "accepted"
            p4.supervisor_confirmed = True
            p4.supervisor_accepted_at = now - timedelta(days=38)
        if examiner1:
            p4.assessor_1_id = examiner1.id
            p4.assessor_1_invitation_status = "accepted"
        if examiner2:
            p4.assessor_2_id = examiner2.id
            p4.assessor_2_invitation_status = "pending"
        if examiner3:
            p4.assessor_3_id = examiner3.id
            p4.assessor_3_invitation_status = "pending"
        p4.assessors_confirmed = True
        p4.assessors_nominated_at = now - timedelta(days=20)
        for form_type, label, payload in [
            ("jbs5", "JBS5_Research_Proposal", {
                "full_name": "Lerato Sithole", "student_number": "219001004",
                "email": s4.email, "research_title": p4.project_title,
                "discipline": d_hr.name,
                "abstract": "Employee engagement and retention in South African banking.",
            }),
            ("supervisor_agreement", "Supervisor_Agreement", {
                "supervisor_full_name": "Jane Supervisor",
                "student_name": "Lerato Sithole", "student_number": "219001004",
                "research_title": p4.project_title, "agreement_declaration": True,
            }),
            ("jbs10", "JBS10_Submission_Form", {
                "full_name": "Lerato Sithole", "student_number": "219001004",
                "email": s4.email, "research_title": p4.project_title,
                "supervisor_name": "Jane Supervisor",
                "submission_date": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                "declaration": True,
            }),
            ("intent_to_submit", "Intent_to_Submit", {
                "full_name": "Lerato Sithole", "student_number": "219001004",
                "email": s4.email, "research_title": p4.project_title,
                "intended_date": (now - timedelta(days=28)).strftime("%Y-%m-%d"),
            }),
        ]:
            upsert_form(p4.id, form_type, payload)
            upsert_doc(p4, form_type, label, s4.id)
        upsert_doc(p4, "dissertation", "Dissertation_Final", s4.id)
        print("Project 4 (HDC_VERIFIED) done.")

        # ── PROJECT 5: RESULTS_SUBMITTED_TO_HDC ──────────────────────────────
        # Thabo — second project. All 3 assessors submitted grades. HDC sees grade table.
        p5 = get_or_create_project(s1.id, "Corporate Governance and Firm Performance in JSE-listed Companies")
        p5.project_description = (
            "An empirical analysis of the relationship between corporate governance mechanisms "
            "and financial performance of companies listed on the Johannesburg Stock Exchange."
        )
        d_finance2 = disciplines["Finance"]
        p5.discipline = d_finance2.name
        p5.discipline_id = d_finance2.id
        p5.project_status = ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value
        p5.created_at = now - timedelta(days=90)
        if supervisor:
            p5.primary_supervisor_id = supervisor.id
            p5.primary_supervisor_invitation_status = "accepted"
            p5.supervisor_confirmed = True
            p5.supervisor_accepted_at = now - timedelta(days=80)
        if examiner1:
            p5.assessor_1_id = examiner1.id
            p5.assessor_1_invitation_status = "accepted"
        if examiner2:
            p5.assessor_2_id = examiner2.id
            p5.assessor_2_invitation_status = "accepted"
        if examiner3:
            p5.assessor_3_id = examiner3.id
            p5.assessor_3_invitation_status = "accepted"
        p5.assessors_confirmed = True
        p5.assessors_nominated_at = now - timedelta(days=60)
        p5.results_submitted_to_hdc_at = now - timedelta(days=2)
        for form_type, label, payload in [
            ("jbs5", "JBS5_Research_Proposal", {
                "full_name": "Thabo Molefe", "student_number": "219001001",
                "email": s1.email, "research_title": p5.project_title,
                "discipline": d_finance2.name,
                "abstract": "Corporate governance and firm performance on the JSE.",
            }),
            ("supervisor_agreement", "Supervisor_Agreement", {
                "supervisor_full_name": "Jane Supervisor",
                "student_name": "Thabo Molefe", "student_number": "219001001",
                "research_title": p5.project_title, "agreement_declaration": True,
            }),
            ("jbs10", "JBS10_Submission_Form", {
                "full_name": "Thabo Molefe", "student_number": "219001001",
                "email": s1.email, "research_title": p5.project_title,
                "supervisor_name": "Jane Supervisor",
                "submission_date": (now - timedelta(days=50)).strftime("%Y-%m-%d"),
                "declaration": True,
            }),
            ("intent_to_submit", "Intent_to_Submit", {
                "full_name": "Thabo Molefe", "student_number": "219001001",
                "email": s1.email, "research_title": p5.project_title,
                "intended_date": (now - timedelta(days=48)).strftime("%Y-%m-%d"),
            }),
        ]:
            upsert_form(p5.id, form_type, payload)
            upsert_doc(p5, form_type, label, s1.id)
        upsert_doc(p5, "dissertation", "Dissertation_Final", s1.id)
        # Assessor grade forms
        grade_data = [
            ("assessor_1", examiner1, 78, "Pass with Distinction",
             "The candidate demonstrates strong command of corporate governance theory. "
             "Literature review is comprehensive. Methodology is sound. Minor gaps in data interpretation."),
            ("assessor_2", examiner2, 72, "Pass with Merit",
             "Good understanding of the JSE context. The empirical analysis is well-structured. "
             "Recommendations could be strengthened with more industry-specific insights."),
            ("assessor_3", examiner3, 81, "Pass with Distinction",
             "Excellent research design. The thesis makes an original contribution to governance literature. "
             "Writing is clear and well-referenced. A strong piece of work overall."),
        ]
        for slot, assessor, grade, recommendation, written in grade_data:
            upsert_form(p5.id, slot, {
                "assessor_name": f"{assessor.first_name} {assessor.last_name}" if assessor else slot,
                "department": assessor.scholar_profile.department if assessor and assessor.scholar_profile else "",
                "affiliation": "University of Johannesburg",
                "student_name": "Thabo Molefe",
                "student_number": "219001001",
                "research_title": p5.project_title,
                "grade": grade,
                "recommendation": recommendation,
                "written_assessment": written,
                "declaration": True,
            })
            upsert_doc(p5, slot, f"Assessor_Grade_{slot}", assessor.id if assessor else admin_id)
        print("Project 5 (RESULTS_SUBMITTED_TO_HDC) done.")

        db.session.commit()
        print("\nDemo data seeded successfully.")
        print("  Demo student password:", demo_password)
        print("  Set MBA_DEMO_SEED_PASSWORD before running this command if you need a stable local demo password.")
        print("  Projects: 5 across CREATED, ADMIN_SUBMITTED, ADMIN_APPROVED, HDC_VERIFIED, RESULTS_SUBMITTED_TO_HDC")
