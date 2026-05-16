import csv
from html import escape as html_escape
from io import BytesIO, TextIOWrapper
import secrets
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

from datetime import datetime

from flask import Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import (
    EthicsRole,
    EthicsUser,
    MbaDiscipline,
    MbaForm,
    MbaProject,
    MbaProjectDocument,
    MbaProjectSupervisorInvitation,
    MbaRole,
    MbaScholarProfile,
    MbaScholarRole,
    MbaStudentProfile,
    MbaUser,
    ProjectStatus,
    normalize_email,
)
from .route_support import *  # noqa: F403

ASSESSOR_IMPORT_FIELDS = {
    "email",
    "first_name",
    "last_name",
    "name",
    "surname",
    "title",
    "contact",
    "department",
    "position",
    "qualification",
    "affiliation",
    "skills",
    "research_themes",
    "research_interests",
    "research_disciplines",
    "students_supervised_total",
    "students_assessed_total",
    "publication_count",
    "selected_publications",
    "scholarly_profile_links",
}

STAFF_TEMPLATE_FIELDS = [
    "email",
    "first_name",
    "last_name",
    "title",
    "contact",
    "department",
    "position",
    "qualification",
    "affiliation",
    "skills",
    "research_themes",
    "research_interests",
    "research_disciplines",
    "students_supervised_total",
    "students_assessed_total",
    "publication_count",
    "selected_publications",
    "scholarly_profile_links",
]

SUPERVISOR_IMPORT_FIELDS = set(STAFF_TEMPLATE_FIELDS) | {"name", "surname"}

STUDENT_IMPORT_FIELDS = {
    "email",
    "student_email",
    "student_email_address",
    "student_number",
    "first_name",
    "last_name",
    "name",
    "surname",
    "title",
    "contact",
    "secondary_email",
    "module",
    "block_id",
    "degree",
    "address",
}

STUDENT_TEMPLATE_FIELDS = [
    "student_email_address",
    "student_number",
    "first_name",
    "last_name",
    "title",
    "contact",
    "secondary_email",
    "module",
    "block_id",
    "degree",
    "address",
]


def _profile_url():
    return url_for("mba.profile", _external=True)


def staff_onboarding_email(user, role_label, temporary_password=None):
    if temporary_password:
        password_line = f"Temporary password: {temporary_password}\n"
    else:
        password_line = "Use your existing MBA system password.\n"
    login_url = url_for("auth.login", system="mba", _external=True)
    return {
        "recipient": user.email,
        "subject": f"MBA {role_label} Profile Access",
        "body": (
            f"You have been added to the MBA {role_label.lower()} pool.\n\n"
            f"Login email: {user.email}\n"
            f"{password_line}\n"
            f"Website link: {login_url}\n"
            f"Profile link: {_profile_url()}\n\n"
            f"Please sign in to the MBA system and complete your {role_label.lower()} profile, including research themes, "
            "research interests, research disciplines, expertise, department, qualification, and affiliation. "
            "These profile details are used by the system to recommend MBA Capstone Project assignments."
        ),
    }


def assessor_onboarding_email(user, temporary_password=None):
    return staff_onboarding_email(user, "Assessor", temporary_password)


def supervisor_onboarding_email(user, temporary_password=None):
    return staff_onboarding_email(user, "Supervisor", temporary_password)


def student_onboarding_email(user, temporary_password=None):
    login_url = url_for("auth.login", system="mba", _external=True)
    password_line = f"Temporary password: {temporary_password}\n" if temporary_password else "Use your existing MBA system password.\n"
    return {
        "recipient": user.email,
        "subject": "MBA Student Account Access",
        "body": (
            "Your MBA student account is ready.\n\n"
            f"Login email: {user.email}\n"
            f"{password_line}\n"
            f"Website link: {login_url}\n"
            f"Profile link: {_profile_url()}\n\n"
            "Please sign in and complete or review your student profile."
        ),
    }


def _temporary_password():
    return secrets.token_urlsafe(12)


def _looks_like_email(email):
    return bool(email and "@" in email and "." in email.rsplit("@", 1)[-1])


def _sync_ethics_student_account(user, student_number, temporary_password):
    ethics_user = EthicsUser.find_by_email(user.email)
    if ethics_user and ethics_user.role != EthicsRole.STUDENT.value:
        return None
    if not ethics_user:
        ethics_user = EthicsUser(
            email=user.email,
            role=EthicsRole.STUDENT.value,
            student_number=student_number,
            first_name=user.first_name,
            last_name=user.last_name,
            authenticated_student=True,
            is_active=True,
        )
        db.session.add(ethics_user)
        db.session.flush()
    else:
        if student_number:
            ethics_user.student_number = student_number
        ethics_user.authenticated_student = True
        ethics_user.is_active = True
        ethics_user.first_name = user.first_name or ethics_user.first_name
        ethics_user.last_name = user.last_name or ethics_user.last_name
    ethics_user.set_password(temporary_password)
    return ethics_user


def _sync_ethics_supervisor_account(user, temporary_password):
    ethics_user = EthicsUser.find_by_email(user.email)
    if ethics_user and ethics_user.role != EthicsRole.SUPERVISOR.value:
        return None
    if not ethics_user:
        ethics_user = EthicsUser(
            email=user.email,
            role=EthicsRole.SUPERVISOR.value,
            first_name=user.first_name,
            last_name=user.last_name,
            is_active=True,
        )
        db.session.add(ethics_user)
        db.session.flush()
    else:
        ethics_user.is_active = True
        ethics_user.first_name = user.first_name or ethics_user.first_name
        ethics_user.last_name = user.last_name or ethics_user.last_name
    ethics_user.set_password(temporary_password)
    return ethics_user


def _email_failure_summary(email_result):
    failed = email_result.get("failed", [])
    if not failed:
        return ""
    reasons = {}
    for item in failed:
        reason = item.get("reason") or "unknown"
        if reason == "mail_not_configured":
            reason = "mail is not configured"
        reasons[reason] = reasons.get(reason, 0) + 1
    reason_text = "; ".join(f"{reason} ({count})" for reason, count in reasons.items())
    return f" Email failure reason(s): {reason_text}."


def _apply_scholar_profile_row(user, row, first_name, last_name):
    profile = user.scholar_profile or MbaScholarProfile(user_id=user.id)
    field_map = {
        "title": "title",
        "contact": "contact",
        "department": "department",
        "position": "position",
        "qualification": "qualification",
        "affiliation": "affiliation",
        "skills": "skills",
        "research_themes": "research_themes",
        "research_interests": "research_interests",
        "research_disciplines": "research_disciplines",
        "selected_publications": "selected_publications",
        "scholarly_profile_links": "scholarly_profile_links",
    }
    if first_name:
        profile.name = first_name
    if last_name:
        profile.surname = last_name
    for csv_field, profile_field in field_map.items():
        value = (row.get(csv_field) or "").strip()
        if value:
            setattr(profile, profile_field, value)
    profile.students_supervised_total = parse_non_negative_int(
        row.get("students_supervised_total"),
        profile.students_supervised_total or 0,
    )
    profile.students_assessed_total = parse_non_negative_int(
        row.get("students_assessed_total"),
        profile.students_assessed_total or 0,
    )
    profile.publication_count = parse_non_negative_int(
        row.get("publication_count"),
        profile.publication_count or 0,
    )
    db.session.add(profile)
    return profile


def _staff_names_from_row(row):
    return (
        (row.get("first_name") or row.get("name") or "").strip() or None,
        (row.get("last_name") or row.get("surname") or "").strip() or None,
    )


def _xlsx_column_letter(index):
    index += 1
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xlsx_cell(reference, value):
    return f'<c r="{reference}" t="inlineStr"><is><t>{xml_escape(str(value or ""))}</t></is></c>'


def _build_xlsx_template(filename, fields, sample_row):
    output = BytesIO()
    rows = [fields, [sample_row.get(field, "") for field in fields]]
    sheet_rows = []
    for row_index, row in enumerate(rows, start=1):
        cells = [
            _xlsx_cell(f"{_xlsx_column_letter(column_index)}{row_index}", value)
            for column_index, value in enumerate(row)
        ]
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="24" customWidth="1"/>'
        for index in range(1, len(fields) + 1)
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<cols>"
        f"{columns}"
        "</cols>"
        "<sheetData>"
        f"{''.join(sheet_rows)}"
        "</sheetData>"
        "</worksheet>"
    )
    with ZipFile(output, "w", ZIP_DEFLATED) as workbook:
        workbook.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        )
        workbook.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        workbook.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>"
            '<sheet name="Upload Template" sheetId="1" r:id="rId1"/>'
            "</sheets>"
            "</workbook>",
        )
        workbook.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        workbook.writestr("xl/worksheets/sheet1.xml", sheet_xml)
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _staff_template_sample(sample_email):
    return {
        "email": sample_email,
        "first_name": "Jane",
        "last_name": "Scholar",
        "title": "Dr",
        "contact": "0110000000",
        "department": "Business Management",
        "position": "Senior Lecturer",
        "qualification": "PhD",
        "affiliation": "University of Johannesburg",
        "skills": "Qualitative research; strategy",
        "research_themes": "Strategy; leadership",
        "research_interests": "Digital transformation; governance",
        "research_disciplines": "Strategic Management",
        "students_supervised_total": "18",
        "students_assessed_total": "27",
        "publication_count": "14",
        "selected_publications": "Strategic Innovation In Emerging Markets; Governance And Growth In Banking",
        "scholarly_profile_links": "https://orcid.org/0000-0000-0000-0000",
    }


def _student_template_sample():
    return {
        "student_email_address": "student@example.com",
        "student_number": "219001001",
        "first_name": "Thabo",
        "last_name": "Molefe",
        "title": "Mr",
        "contact": "0720000000",
        "secondary_email": "thabo.molefe@example.com",
        "module": "MBA General",
        "block_id": "Block A",
        "degree": "MBA",
        "address": "Johannesburg",
    }


def apply_assessor_csv_row(row):
    email = normalize_email(row.get("email"))
    if not email:
        raise ValueError("Missing email")

    user = MbaUser.find_by_email(email)
    temporary_password = _temporary_password()
    created = False
    if not user:
        user = MbaUser(
            email=email,
            role=MbaRole.EXAMINER.value,
            is_active=True,
            has_profile=False,
        )
        user.set_password(temporary_password)
        db.session.add(user)
        db.session.flush()
        created = True
    else:
        user.is_active = True
        if user.role not in {MbaRole.EXAMINER.value, MbaRole.SCHOLAR.value}:
            user.role = MbaRole.EXAMINER.value
        if user.role == MbaRole.SCHOLAR.value and user.scholar_role not in {
            MbaScholarRole.EXAMINER.value,
            MbaScholarRole.BOTH.value,
        }:
            user.scholar_role = MbaScholarRole.BOTH.value

    first_name, last_name = _staff_names_from_row(row)
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name

    user.set_password(temporary_password)
    user.has_profile = False
    _apply_scholar_profile_row(user, row, first_name, last_name)
    return user, temporary_password, created


def apply_supervisor_csv_row(row):
    email = normalize_email(row.get("email"))
    if not email:
        raise ValueError("Missing email")

    user = MbaUser.find_by_email(email)
    temporary_password = _temporary_password()
    created = False
    if not user:
        user = MbaUser(
            email=email,
            role=MbaRole.SCHOLAR.value,
            scholar_role=MbaScholarRole.SUPERVISOR.value,
            is_active=True,
            has_profile=False,
        )
        user.set_password(temporary_password)
        db.session.add(user)
        db.session.flush()
        created = True
    else:
        user.is_active = True
        if user.role == MbaRole.EXAMINER.value:
            user.role = MbaRole.SCHOLAR.value
            user.scholar_role = MbaScholarRole.BOTH.value
        elif user.role == MbaRole.SCHOLAR.value:
            if user.scholar_role == MbaScholarRole.EXAMINER.value:
                user.scholar_role = MbaScholarRole.BOTH.value
            elif user.scholar_role not in {MbaScholarRole.SUPERVISOR.value, MbaScholarRole.BOTH.value}:
                user.scholar_role = MbaScholarRole.SUPERVISOR.value
        elif user.role not in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value, MbaRole.HDC.value}:
            user.role = MbaRole.SCHOLAR.value
            user.scholar_role = MbaScholarRole.SUPERVISOR.value

    first_name, last_name = _staff_names_from_row(row)
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name

    user.set_password(temporary_password)
    user.has_profile = False
    _apply_scholar_profile_row(user, row, first_name, last_name)
    _sync_ethics_supervisor_account(user, temporary_password)
    return user, temporary_password, created


def apply_student_excel_row(row):
    email = normalize_email(row.get("student_email_address") or row.get("student_email") or row.get("email"))
    student_number = (row.get("student_number") or "").strip()
    if not email:
        raise ValueError("Missing student email address")
    if not _looks_like_email(email):
        raise ValueError("Invalid student email address")
    user = MbaUser.find_by_email(email)
    if student_number:
        existing_profile = MbaStudentProfile.query.filter(
            MbaStudentProfile.student_number == student_number,
            MbaStudentProfile.user_id != (user.id if user else 0),
        ).first()
        if existing_profile:
            raise ValueError("Student number is already linked to another student")

    temporary_password = _temporary_password()
    created = False
    if not user:
        user = MbaUser(
            email=email,
            role=MbaRole.STUDENT.value,
            is_active=True,
            has_profile=False,
        )
        user.set_password(temporary_password)
        db.session.add(user)
        db.session.flush()
        created = True
    else:
        if user.role != MbaRole.STUDENT.value:
            raise ValueError("Email already belongs to a non-student MBA account")
        user.is_active = True
        user.role = MbaRole.STUDENT.value

    first_name = (row.get("first_name") or row.get("name") or "").strip() or None
    last_name = (row.get("last_name") or row.get("surname") or "").strip() or None
    if first_name:
        user.first_name = first_name
    if last_name:
        user.last_name = last_name

    user.set_password(temporary_password)
    profile = user.student_profile or MbaStudentProfile(user_id=user.id, student_number=student_number or None)
    if student_number:
        profile.student_number = student_number
    profile.name = first_name or profile.name
    profile.surname = last_name or profile.surname
    profile.title = (row.get("title") or "").strip() or profile.title
    profile.contact = (row.get("contact") or "").strip() or profile.contact
    profile.secondary_email = normalize_email(row.get("secondary_email")) or profile.secondary_email
    profile.module = (row.get("module") or "").strip() or profile.module
    profile.block_id = (row.get("block_id") or "").strip() or profile.block_id
    profile.degree = (row.get("degree") or "").strip() or profile.degree or "MBA"
    profile.address = (row.get("address") or "").strip() or profile.address
    user.has_profile = False
    db.session.add(profile)
    _sync_ethics_student_account(user, profile.student_number, temporary_password)
    return user, temporary_password, created


@mba_bp.route("/admin-dashboard")
@login_required
def admin_dashboard():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    selected_view = (request.args.get("view") or "all").strip().lower()
    allowed_views = {"all", "awaiting", "declined", "accepted"}
    if selected_view not in allowed_views:
        selected_view = "all"
    search_text = (request.args.get("q") or "").strip()
    student_number = (request.args.get("student_number") or "").strip()
    status_filter = (request.args.get("status") or "").strip()
    project_page = parse_positive_int(request.args.get("project_page"), 1)
    project_per_page = parse_page_size(request.args.get("project_per_page"), 5)

    def apply_project_filters(base_query, view=None):
        if search_text:
            search_like = f"%{search_text}%"
            base_query = (
                base_query.join(MbaUser, MbaUser.id == MbaProject.student_id)
                .outerjoin(MbaStudentProfile, MbaStudentProfile.user_id == MbaProject.student_id)
                .outerjoin(MbaProjectDocument, MbaProjectDocument.project_id == MbaProject.id)
                .filter(
                    or_(
                        MbaProject.project_title.ilike(search_like),
                        MbaProject.project_description.ilike(search_like),
                        MbaProject.discipline.ilike(search_like),
                        MbaProject.qualification.ilike(search_like),
                        MbaProject.project_status.ilike(search_like),
                        MbaUser.email.ilike(search_like),
                        MbaUser.first_name.ilike(search_like),
                        MbaUser.last_name.ilike(search_like),
                        MbaStudentProfile.student_number.ilike(search_like),
                        MbaProjectDocument.original_name.ilike(search_like),
                        MbaProjectDocument.doc_type.ilike(search_like),
                    )
                )
                .distinct()
            )
        elif student_number:
            base_query = base_query.join(MbaStudentProfile, MbaStudentProfile.user_id == MbaProject.student_id)

        if student_number:
            base_query = base_query.filter(MbaStudentProfile.student_number.ilike(f"%{student_number}%"))

        if status_filter == "declined":
            base_query = base_query.filter((MbaProject.primary_supervisor_invitation_status == INVITATION_DECLINED) | (MbaProject.assessor_1_invitation_status == INVITATION_DECLINED) | (MbaProject.assessor_2_invitation_status == INVITATION_DECLINED))
        elif status_filter == "admin_submitted":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.ADMIN_SUBMITTED.value)
        elif status_filter == "supervisor_accepted":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.SUPERVISOR_ACCEPTED.value)
        elif status_filter == "jbs5_pending_hdc":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.JBS5_SUBMITTED_TO_HDC.value)
        elif status_filter == "jbs5_approved_hdc":
            base_query = base_query.filter(MbaProject.jbs5_hdc_approved_at.isnot(None))
        elif status_filter == "jbs5_declined_hdc":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.JBS5_HDC_DECLINED.value)
        elif status_filter == "approved_hdc":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.HDC_VERIFIED.value)
        elif status_filter == "results_approved":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.RESULTS_APPROVED.value)
        elif status_filter == "results_declined":
            base_query = base_query.filter(MbaProject.project_status == ProjectStatus.RESULTS_DECLINED.value)
        elif status_filter == "awaiting":
            base_query = base_query.filter((MbaProject.primary_supervisor_invitation_status == INVITATION_PENDING) | (MbaProject.assessor_1_invitation_status == INVITATION_PENDING) | (MbaProject.assessor_2_invitation_status == INVITATION_PENDING))

        active_view = selected_view if view is None else view
        if active_view == "awaiting":
            base_query = base_query.filter((MbaProject.primary_supervisor_invitation_status == INVITATION_PENDING) | (MbaProject.assessor_1_invitation_status == INVITATION_PENDING) | (MbaProject.assessor_2_invitation_status == INVITATION_PENDING))
        elif active_view == "declined":
            base_query = base_query.filter((MbaProject.primary_supervisor_invitation_status == INVITATION_DECLINED) | (MbaProject.assessor_1_invitation_status == INVITATION_DECLINED) | (MbaProject.assessor_2_invitation_status == INVITATION_DECLINED))
        elif active_view == "accepted":
            base_query = base_query.filter((MbaProject.primary_supervisor_invitation_status == INVITATION_ACCEPTED) & (MbaProject.assessor_1_invitation_status == INVITATION_ACCEPTED) & (MbaProject.assessor_2_invitation_status == INVITATION_ACCEPTED))
        return base_query

    query = apply_project_filters(
        MbaProject.query.filter(MbaProject.project_status != ProjectStatus.CREATED.value)
    ).order_by(MbaProject.updated_at.desc())
    admin_pagination_args = request_query_args({"project_page", "project_per_page"})
    admin_pagination_args["panel"] = "projects"
    projects, project_pagination = paginate_query(
        query,
        project_page,
        project_per_page,
        "mba.admin_dashboard",
        page_param="project_page",
        per_page_param="project_per_page",
        base_args=admin_pagination_args,
        anchor="project-queue",
    )
    disciplines = disciplines_query(include_inactive=True).all()
    students = MbaUser.query.filter_by(role=MbaRole.STUDENT.value).order_by(MbaUser.email).limit(30).all()
    supervisors = supervisors_query().all()
    examiners = examiners_query().all()
    supervisor_pool_candidates = (
        MbaProject.query.options(
            joinedload(MbaProject.documents),
            joinedload(MbaProject.supervisor_invitations),
        )
        .filter(MbaProject.project_status == ProjectStatus.ADMIN_SUBMITTED.value)
        .all()
    )
    supervisor_pool_release_candidates = [
        project
        for project in supervisor_pool_candidates
        if project_eligible_for_supervisor_pool_release(project)
        and not getattr(project, "supervisor_pool_released_at", None)
    ]
    supervisor_pool_available_projects = [
        project for project in supervisor_pool_candidates if project_available_for_supervisor_pool(project)
    ]
    supervisor_student_counts = dict(
        db.session.query(
            MbaProject.primary_supervisor_id,
            db.func.count(db.distinct(MbaProject.student_id)),
        )
        .filter(
            MbaProject.primary_supervisor_id.isnot(None),
            MbaProject.primary_supervisor_invitation_status == INVITATION_ACCEPTED,
            MbaProject.supervisor_accepted_at.isnot(None),
        )
        .group_by(MbaProject.primary_supervisor_id)
        .all()
    )

    def build_project_suggestions(project):
        recommendations = match_recommendations(project, supervisors, examiners)
        for item in recommendations["ranked_supervisors"]:
            item["supervised_student_count"] = supervisor_student_counts.get(item["user"].id, 0)
        return recommendations

    updated_auto_assignments = False
    for project in projects:
        needs_auto_assignment = (
            project.project_status == ProjectStatus.ADMIN_SUBMITTED.value
            and not project.primary_supervisor_id
            and not project.supervisor_invitations
            and not project.assessor_1_id
            and not project.assessor_2_id
            and not project_has_sent_invitations(project)
        )
        if needs_auto_assignment:
            apply_auto_assignments(project, supervisors, examiners)
            project.comments = append_comment(project.comments, "System auto-assigned best supervisor and assessors")
            updated_auto_assignments = True
        if apply_assessor_suggestions_if_ready(project):
            updated_auto_assignments = True
    if updated_auto_assignments:
        db.session.commit()

    def view_count_for(view):
        return apply_project_filters(
            MbaProject.query.filter(MbaProject.project_status != ProjectStatus.CREATED.value),
            view=view,
        ).count()

    view_counts = {"all": view_count_for("all"), "awaiting": view_count_for("awaiting"), "declined": view_count_for("declined"), "accepted": view_count_for("accepted")}
    invitation_state_by_project = {project.id: project_invitation_snapshot(project) for project in projects}
    suggestions_by_project = {project.id: build_project_suggestions(project) for project in projects}
    project_ids = [p.id for p in projects]
    all_docs = MbaProjectDocument.query.filter(MbaProjectDocument.project_id.in_(project_ids)).all() if project_ids else []
    documents_by_project = {}
    for doc in all_docs:
        documents_by_project.setdefault(doc.project_id, []).append(doc)
    return render_template(
        "mba/admin_dashboard.html",
        disciplines=disciplines,
        projects=projects,
        students=students,
        supervisors=supervisors,
        examiners=examiners,
        suggestions_by_project=suggestions_by_project,
        invitation_state_by_project=invitation_state_by_project,
        selected_view=selected_view,
        view_counts=view_counts,
        documents_by_project=documents_by_project,
        kpis=mba_kpis(),
        project_pagination=project_pagination,
        document_label=document_label,
        all_assessment_results_received=all_assessment_results_received,
        supervisor_suggestion_limit=SUPERVISOR_RECOMMENDATION_LIMIT,
        project_activity_entries=project_activity_entries,
        project_status_label=project_status_label,
        project_has_active_corrections=project_has_active_corrections,
        project_corrections_status=project_corrections_status,
        corrections_status_label=corrections_status_label,
        corrections_block_hdc_submission=corrections_block_hdc_submission,
        additional_assessment_required=additional_assessment_required,
        additional_assessment_stage=additional_assessment_stage,
        additional_assessment_status_label=additional_assessment_status_label,
        additional_assessment_blocks_hdc_submission=additional_assessment_blocks_hdc_submission,
        assessment_results_forwarded_to_supervisor=assessment_results_forwarded_to_supervisor,
        module_completion_status_label=module_completion_status_label,
        module_completion_allows_hdc_submission=module_completion_allows_hdc_submission,
        can_request_moodle_manuscript_submission=can_request_moodle_manuscript_submission,
        required_hdc_results_documents_missing=required_hdc_results_documents_missing,
        assessor_hdc_decision=assessor_hdc_decision,
        assessor_hdc_decision_label=assessor_hdc_decision_label,
        assessor_hdc_decision_alert_label=assessor_hdc_decision_alert_label,
        hdc_declined_assessor_slots=hdc_declined_assessor_slots,
        supervisor_pool_release_count=len(supervisor_pool_release_candidates),
        supervisor_pool_available_count=len(supervisor_pool_available_projects),
    )


@mba_bp.route("/admin/release-supervisor-project-pool", methods=["POST"])
@login_required
def admin_release_supervisor_project_pool():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    candidate_projects = (
        MbaProject.query.options(
            joinedload(MbaProject.documents),
            joinedload(MbaProject.supervisor_invitations),
        )
        .filter(MbaProject.project_status == ProjectStatus.ADMIN_SUBMITTED.value)
        .all()
    )
    projects_to_release = [
        project
        for project in candidate_projects
        if project_eligible_for_supervisor_pool_release(project)
        and not getattr(project, "supervisor_pool_released_at", None)
    ]
    if not projects_to_release:
        flash("There are no new JBS5 project titles ready to release to supervisors.", "info")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    released_at = datetime.utcnow()
    for project in projects_to_release:
        project.supervisor_pool_released_at = released_at
        project.supervisor_pool_released_by_id = current_user.id
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: released JBS5 project title to the supervisor selection pool.",
        )

    supervisors = supervisors_query().all()
    supervisor_recipients = [supervisor.email for supervisor in supervisors if supervisor.email]
    dashboard_url = url_for("mba.scholar_dashboard", _external=True) + "#available-supervisor-projects"
    title_lines = "\n".join(f"- {project.project_title}" for project in projects_to_release[:20])
    if len(projects_to_release) > 20:
        title_lines += f"\n- and {len(projects_to_release) - 20} more"
    messages = [
        {
            "recipient": supervisor_email,
            "subject": "MBA Capstone Project Titles Available for Supervision",
            "body": (
                f"MBA Admin has released {len(projects_to_release)} JBS5 project title"
                f"{'' if len(projects_to_release) == 1 else 's'} for supervisor selection.\n\n"
                "You can now sign in to the MBA system, open your Scholar Dashboard, review the available "
                "project titles, and choose the Capstone Project you wish to supervise.\n\n"
                f"Available title list:\n{title_lines}\n\n"
                f"Open available projects: {dashboard_url}"
            ),
        }
        for supervisor_email in supervisor_recipients
    ]
    email_result = send_bulk_emails(messages)
    delivered_count = len(email_result["delivered"])
    failed_count = len(email_result["failed"])
    for project in projects_to_release:
        project.comments = append_comment(
            project.comments,
            (
                "Supervisor project title release email result: "
                f"targeted={len(supervisor_recipients)}, delivered={delivered_count}, failed={failed_count}"
            ),
        )
    db.session.commit()

    if delivered_count and not failed_count:
        flash(
            f"Released {len(projects_to_release)} JBS5 project title(s) to supervisors and notified all {delivered_count} active supervisor(s).",
            "success",
        )
    elif delivered_count and failed_count:
        flash(
            f"Released {len(projects_to_release)} JBS5 project title(s). Notification targeted {len(supervisor_recipients)} active supervisor(s): {delivered_count} sent; {failed_count} failed.",
            "warning",
        )
    elif not supervisor_recipients:
        flash(
            f"Released {len(projects_to_release)} JBS5 project title(s), but no active supervisor email addresses were found.",
            "warning",
        )
    else:
        flash(
            f"Released {len(projects_to_release)} JBS5 project title(s). Notification targeted {len(supervisor_recipients)} active supervisor(s), but email delivery is not configured or failed.",
            "warning",
        )
    return redirect(url_for("mba.admin_dashboard", panel="projects"))


def _xlsx_column_index(cell_ref):
    letters = "".join(char for char in str(cell_ref or "") if char.isalpha()).upper()
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def _xlsx_cell_value(cell, shared_strings):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    cell_type = cell.attrib.get("t")
    value_node = cell.find(f"{ns}v")
    if cell_type == "inlineStr":
        text_node = cell.find(f"{ns}is/{ns}t")
        return text_node.text if text_node is not None and text_node.text is not None else ""
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (ValueError, IndexError):
            return ""
    return raw_value


def _read_xlsx_rows(uploaded_file):
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        with ZipFile(uploaded_file.stream) as workbook:
            shared_strings = []
            if "xl/sharedStrings.xml" in workbook.namelist():
                shared_root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
                for item in shared_root.findall(f"{ns}si"):
                    text_parts = [
                        node.text or ""
                        for node in item.findall(f".//{ns}t")
                    ]
                    shared_strings.append("".join(text_parts))
            sheet_name = "xl/worksheets/sheet1.xml"
            if sheet_name not in workbook.namelist():
                sheet_name = next(
                    (name for name in workbook.namelist() if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")),
                    None,
                )
            if not sheet_name:
                raise ValueError("No worksheet found")
            sheet_root = ET.fromstring(workbook.read(sheet_name))
    except BadZipFile as exc:
        raise ValueError("Excel file could not be read") from exc
    except ET.ParseError as exc:
        raise ValueError("Excel file XML could not be parsed") from exc

    rows = []
    for row_node in sheet_root.findall(f".//{ns}row"):
        values = {}
        for cell in row_node.findall(f"{ns}c"):
            index = _xlsx_column_index(cell.attrib.get("r"))
            if index >= 0:
                values[index] = _xlsx_cell_value(cell, shared_strings).strip()
        if values:
            max_index = max(values)
            rows.append([values.get(index, "") for index in range(max_index + 1)])
    return rows


def _xlsx_dict_rows_and_fields(uploaded_file):
    rows = _read_xlsx_rows(uploaded_file)
    if not rows:
        return [], set()
    headers = [str(value or "").strip().lower() for value in rows[0]]
    dict_rows = [
        {
            headers[index]: value
            for index, value in enumerate(row)
            if index < len(headers) and headers[index]
        }
        for row in rows[1:]
        if any(str(value or "").strip() for value in row)
    ]
    return dict_rows, set(headers)


def _uploaded_import_rows(uploaded_file):
    filename = (uploaded_file.filename or "").lower()
    if filename.endswith(".xlsx"):
        return _xlsx_dict_rows_and_fields(uploaded_file)
    if filename.endswith(".csv"):
        stream = TextIOWrapper(uploaded_file.stream, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(stream)
        fields = {field.strip().lower() for field in reader.fieldnames if field} if reader.fieldnames else set()
        return list(reader), fields
    raise ValueError("Only .xlsx Excel files or .csv files are accepted")


@mba_bp.route("/admin-corrections")
@login_required
def admin_corrections():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    correction_status = (request.args.get("corrections_status") or "all").strip().lower()
    allowed_statuses = {"all", "awaiting_student", "rejected_by_supervisor", "awaiting_supervisor", "ready_for_admin"}
    if correction_status not in allowed_statuses:
        correction_status = "all"
    student_number = (request.args.get("student_number") or "").strip()

    projects = (
        MbaProject.query.options(
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            joinedload(MbaProject.primary_supervisor),
            joinedload(MbaProject.assessor_1),
            joinedload(MbaProject.assessor_2),
        )
        .filter(MbaProject.project_status != ProjectStatus.CREATED.value)
        .order_by(MbaProject.updated_at.desc())
        .all()
    )
    project_ids = [project.id for project in projects]
    form_types = [assessment_doc_type(slot) for slot in ALL_ASSESSOR_SLOTS]
    forms = (
        MbaForm.query.filter(MbaForm.project_id.in_(project_ids), MbaForm.form_type.in_(form_types)).all()
        if project_ids
        else []
    )
    forms_by_project = {}
    for form in forms:
        forms_by_project.setdefault(form.project_id, {})[form.form_type] = form

    def project_student_number_matches(project, search_value):
        if not search_value:
            return True
        student = project.student
        profile = student.student_profile if student else None
        student_number_value = profile.student_number if profile else ""
        lowered = search_value.lower()
        return lowered in (student_number_value or "").lower()

    filtered_projects = [
        project
        for project in projects
        if project_has_active_corrections(project, forms_by_project=forms_by_project)
        and project_student_number_matches(project, student_number)
    ]
    correction_counts = {
        "all": len(filtered_projects),
        "awaiting_student": sum(
            1
            for project in filtered_projects
            if project_corrections_status(project, forms_by_project=forms_by_project) == "awaiting_student"
        ),
        "awaiting_supervisor": sum(
            1
            for project in filtered_projects
            if project_corrections_status(project, forms_by_project=forms_by_project) == "awaiting_supervisor"
        ),
        "rejected_by_supervisor": sum(
            1
            for project in filtered_projects
            if project_corrections_status(project, forms_by_project=forms_by_project) == "rejected_by_supervisor"
        ),
        "ready_for_admin": sum(
            1
            for project in filtered_projects
            if project_corrections_status(project, forms_by_project=forms_by_project) == "ready_for_admin"
        ),
    }
    visible_projects = [
        project
        for project in filtered_projects
        if correction_status == "all"
        or project_corrections_status(project, forms_by_project=forms_by_project) == correction_status
    ]
    return render_template(
        "mba/admin_corrections.html",
        projects=visible_projects,
        forms_by_project=forms_by_project,
        corrections_status=correction_status,
        correction_counts=correction_counts,
        student_number=student_number,
        project_correction_requests=project_correction_requests,
        project_corrections_status=project_corrections_status,
        corrections_status_label=corrections_status_label,
        uploaded_doc_for=uploaded_doc_for,
        student_submitted_corrections_response=student_submitted_corrections_response,
        student_submitted_corrections_pack=student_submitted_corrections_pack,
        supervisor_approved_corrections=supervisor_approved_corrections,
        correction_request_reference_time=correction_request_reference_time,
        document_label=document_label,
        project_status_label=project_status_label,
        corrections_block_hdc_submission=corrections_block_hdc_submission,
        all_assessment_results_received=all_assessment_results_received,
        assessment_results_forwarded_to_supervisor=assessment_results_forwarded_to_supervisor,
        hdc_results_approved=hdc_results_approved,
        results_released_to_supervisor=results_released_to_supervisor,
        kpis=mba_kpis(),
    )


def _reminder_student_detail_text(project):
    profile = project.student.student_profile if project.student and project.student.student_profile else None
    student_name = ""
    if profile:
        student_name = f"{profile.name or ''} {profile.surname or ''}".strip()
    if not student_name and project.student:
        student_name = f"{project.student.first_name or ''} {project.student.last_name or ''}".strip()
    return (
        f"Student: {student_name or (project.student.email if project.student else 'Unknown')}\n"
        f"Student email: {project.student.email if project.student else 'Unknown'}\n"
        f"Student number: {(profile.student_number if profile else '') or 'Not captured'}\n"
        f"Project: {project.project_title}\n"
        f"Discipline: {project.discipline_name}"
    )


def _module_completion_reminder_message(item):
    project = item["project"]
    yes_url = url_for(
        "mba.module_completion_verification_response",
        token=project.module_completion_verification_token,
        decision="yes",
        _external=True,
    )
    no_url = url_for(
        "mba.module_completion_verification_response",
        token=project.module_completion_verification_token,
        decision="no",
        _external=True,
    )
    text_body = (
        "Reminder: please confirm whether this student has passed all required modules.\n\n"
        f"{_reminder_student_detail_text(project)}\n\n"
        f"Yes, modules passed: {yes_url}\n"
        f"No, modules not passed: {no_url}\n\n"
        "These links are single-use. Once a response is recorded, both options become invalid."
    )
    button_style = (
        "display:inline-block;padding:10px 14px;border-radius:6px;text-decoration:none;"
        "font-weight:700;margin-right:8px;"
    )
    details_html = "".join(
        f"<li>{html_escape(line)}</li>" for line in _reminder_student_detail_text(project).splitlines()
    )
    html_body = (
        "<p>Reminder: please confirm whether this student has passed all required modules.</p>"
        f"<ul>{details_html}</ul>"
        "<p>"
        f"<a href=\"{html_escape(yes_url)}\" style=\"{button_style}background:#1f7a3a;color:#fff;\">Yes</a>"
        f"<a href=\"{html_escape(no_url)}\" style=\"{button_style}background:#b42318;color:#fff;\">No</a>"
        "</p>"
        "<p>These links are single-use. Once a response is recorded, both options become invalid.</p>"
    )
    return {
        "recipient": item["recipient_email"],
        "subject": f"Reminder: Module Completion Verification: {project.project_title}",
        "body": {"text": text_body, "html": html_body},
    }


def admin_reminder_email_message(item):
    project = item["project"]
    kind = item["kind"]
    student_details = _reminder_student_detail_text(project)
    if kind == "module_completion":
        return _module_completion_reminder_message(item)
    subject = f"Reminder: {item['type_label']}: {project.project_title}"
    action_text = "Please sign in to the MBA system and complete the pending action."
    if kind == "supervisor_invitation":
        action_text = "Please sign in to the MBA system to accept or decline the supervisor invitation."
    elif kind == "assessor_invitation":
        action_text = "Please sign in to the MBA system to accept or decline the assessor invitation and submit the required acceptance pack."
    elif kind == "moodle_manuscript_submission":
        action_text = (
            "Please submit the Capstone Manuscript through Moodle. Do not upload the Capstone Manuscript "
            "in the MBA system; upload only the required supporting documents there."
        )
    elif kind == "corrections_response":
        action_text = "Please upload the corrected Capstone Manuscript, fill the Response to Assessors' Comments form, and upload the resubmitted Turnitin report in the MBA system."
    elif kind == "assessment_summary_release":
        action_text = "Please review the anonymous assessment summary and release the assessor comments to the student when ready."
    elif kind == "corrections_supervisor_approval":
        action_text = "Please review and approve the corrected submission in the MBA system."
    elif kind == "assessor_result":
        action_text = "Please review the Capstone Project and submit the required assessor result pack."
    return {
        "recipient": item["recipient_email"],
        "subject": subject,
        "body": (
            f"This is a reminder for a pending MBA notification.\n\n"
            f"Notification type: {item['type_label']}\n"
            f"{student_details}\n\n"
            f"{action_text}"
        ),
    }


def _mark_manual_reminder_source(item, sent_at):
    project = item["project"]
    if item["kind"] == "supervisor_invitation":
        invitation = db.session.get(MbaProjectSupervisorInvitation, item["meta"].get("invitation_id"))
        if invitation:
            invitation.reminder_sent_at = sent_at
    elif item["kind"] == "assessor_invitation":
        slot = item["meta"].get("slot")
        if slot:
            setattr(project, f"{slot}_reminder_sent_at", sent_at)


@mba_bp.route("/admin-reminders")
@login_required
def admin_reminders():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    reminder_type = (request.args.get("type") or "all").strip()
    page = parse_positive_int(request.args.get("reminder_page"), 1)
    per_page = parse_page_size(request.args.get("reminder_per_page"), 10)
    all_items = admin_pending_reminder_items()
    reminder_types = sorted({item["type_label"] for item in all_items})
    if reminder_type != "all" and reminder_type not in reminder_types:
        reminder_type = "all"
    filtered_items = [
        item for item in all_items if reminder_type == "all" or item["type_label"] == reminder_type
    ]
    counts = {"all": len(all_items)}
    for type_label in reminder_types:
        counts[type_label] = sum(1 for item in all_items if item["type_label"] == type_label)
    pagination_args = request_query_args({"reminder_page", "reminder_per_page"})
    reminders, reminder_pagination = paginate_list(
        filtered_items,
        page,
        per_page,
        "mba.admin_reminders",
        page_param="reminder_page",
        per_page_param="reminder_per_page",
        base_args=pagination_args,
        anchor="admin-reminders",
    )
    return render_template(
        "mba/admin_reminders.html",
        reminders=reminders,
        reminder_types=reminder_types,
        reminder_type=reminder_type,
        reminder_counts=counts,
        reminder_pagination=reminder_pagination,
        kpis=mba_kpis(),
    )


@mba_bp.route("/admin-reminders/action", methods=["POST"])
@login_required
def admin_reminder_action():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    reminder_key = (request.form.get("reminder_key") or "").strip()
    action = (request.form.get("action") or "").strip()
    if not reminder_key:
        flash("Reminder not found.", "error")
        return redirect(url_for("mba.admin_reminders"))

    if action == "dismiss":
        state = reminder_state_for_key(reminder_key, create=True)
        state.dismissed_at = datetime.utcnow()
        state.dismissed_by_id = current_user.id
        db.session.commit()
        flash("Reminder dismissed.", "success")
        return redirect(url_for("mba.admin_reminders"))

    if action != "send":
        flash("Unknown reminder action.", "error")
        return redirect(url_for("mba.admin_reminders"))

    item = admin_pending_reminder_item(reminder_key)
    if not item:
        flash("This reminder is no longer pending or has been dismissed.", "info")
        return redirect(url_for("mba.admin_reminders"))
    message = admin_reminder_email_message(item)
    email_result = send_bulk_emails([message])
    now = datetime.utcnow()
    delivered_count = len(email_result["delivered"])
    failed_count = len(email_result["failed"])
    state = reminder_state_for_key(reminder_key, create=True)
    if delivered_count:
        state.last_sent_at = now
        state.last_sent_by_id = current_user.id
        _mark_manual_reminder_source(item, now)
    item["project"].comments = append_comment(
        item["project"].comments,
        (
            f"{current_user.email}: manual reminder sent for {item['type_label']} "
            f"to {item['recipient_email']}; delivered={delivered_count}; failed={failed_count}"
        ),
    )
    db.session.commit()
    if delivered_count and not failed_count:
        flash("Reminder email sent.", "success")
    elif failed_count:
        flash("Reminder was recorded, but email delivery failed or is not configured.", "warning")
    else:
        flash("Reminder was recorded. Email delivery is not configured.", "warning")
    return redirect(url_for("mba.admin_reminders"))


@mba_bp.route("/admin-additional-assessment")
@login_required
def admin_additional_assessment():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    assessment_status = (request.args.get("assessment_status") or "all").strip().lower()
    allowed_statuses = {"all", "needs_assignment", "awaiting_acceptance", "awaiting_result"}
    if assessment_status not in allowed_statuses:
        assessment_status = "all"
    student_number = (request.args.get("student_number") or "").strip()

    projects = (
        MbaProject.query.options(
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            joinedload(MbaProject.primary_supervisor),
            joinedload(MbaProject.assessor_1),
            joinedload(MbaProject.assessor_2),
            joinedload(MbaProject.assessor_3),
        )
        .filter(MbaProject.project_status != ProjectStatus.CREATED.value)
        .order_by(MbaProject.updated_at.desc())
        .all()
    )
    project_ids = [project.id for project in projects]
    form_types = [assessment_doc_type(slot) for slot in ALL_ASSESSOR_SLOTS]
    forms = (
        MbaForm.query.filter(MbaForm.project_id.in_(project_ids), MbaForm.form_type.in_(form_types)).all()
        if project_ids
        else []
    )
    forms_by_project = {}
    for form in forms:
        forms_by_project.setdefault(form.project_id, {})[form.form_type] = form

    def project_student_number_matches(project, search_value):
        if not search_value:
            return True
        student = project.student
        profile = student.student_profile if student else None
        student_number_value = profile.student_number if profile else ""
        lowered = search_value.lower()
        return lowered in (student_number_value or "").lower()

    filtered_projects = [
        project
        for project in projects
        if additional_assessment_pending(project, forms_by_project=forms_by_project)
        and project_student_number_matches(project, student_number)
    ]
    examiners = examiners_query().all()
    additional_assessor_suggestions = {
        project.id: suggested_additional_assessor(project, examiners)
        for project in filtered_projects
    }
    assessment_counts = {
        "all": len(filtered_projects),
        "needs_assignment": sum(
            1
            for project in filtered_projects
            if additional_assessment_stage(project, forms_by_project=forms_by_project) == "needs_assignment"
        ),
        "awaiting_acceptance": sum(
            1
            for project in filtered_projects
            if additional_assessment_stage(project, forms_by_project=forms_by_project) == "awaiting_acceptance"
        ),
        "awaiting_result": sum(
            1
            for project in filtered_projects
            if additional_assessment_stage(project, forms_by_project=forms_by_project) == "awaiting_result"
        ),
    }
    visible_projects = [
        project
        for project in filtered_projects
        if assessment_status == "all"
        or additional_assessment_stage(project, forms_by_project=forms_by_project) == assessment_status
    ]
    return render_template(
        "mba/admin_additional_assessment.html",
        projects=visible_projects,
        forms_by_project=forms_by_project,
        assessment_status=assessment_status,
        assessment_counts=assessment_counts,
        student_number=student_number,
        additional_assessor_suggestions=additional_assessor_suggestions,
        additional_assessment_stage=additional_assessment_stage,
        additional_assessment_status_label=additional_assessment_status_label,
        additional_assessment_required=additional_assessment_required,
        additional_assessment_blocks_hdc_submission=additional_assessment_blocks_hdc_submission,
        assessment_result_pack_complete=assessment_result_pack_complete,
        assessor_grade_for_slot=assessor_grade_for_slot,
        uploaded_doc_for=uploaded_doc_for,
        document_label=document_label,
        project_status_label=project_status_label,
        examiners=examiners,
        assessment_doc_type=assessment_doc_type,
        assessor_report_doc_type=assessor_report_doc_type,
        assessor_narrative_doc_type=assessor_narrative_doc_type,
        kpis=mba_kpis(),
    )


@mba_bp.route("/admin/disciplines", methods=["POST"])
@login_required
def admin_discipline_action():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    action = (request.form.get("action") or "create").strip().lower()
    if action == "create":
        name = " ".join((request.form.get("name") or "").strip().split())
        if not name:
            flash("Discipline name is required.", "error")
            return redirect(url_for("mba.admin_dashboard"))
        existing = MbaDiscipline.query.filter(db.func.lower(MbaDiscipline.name) == name.lower()).first()
        if existing:
            flash("That discipline already exists.", "error")
            return redirect(url_for("mba.admin_dashboard"))
        max_sort_order = db.session.query(db.func.max(MbaDiscipline.sort_order)).scalar()
        db.session.add(MbaDiscipline(name=name, sort_order=(max_sort_order or 0) + 1))
        db.session.commit()
        flash("Discipline added.", "success")
        return redirect(url_for("mba.admin_dashboard"))
    if action == "toggle":
        discipline_id = request.form.get("discipline_id", type=int)
        discipline = db.session.get(MbaDiscipline, discipline_id)
        if not discipline:
            abort(404)
        discipline.is_active = not discipline.is_active
        db.session.commit()
        flash(f"Discipline {'activated' if discipline.is_active else 'hidden'}.", "success")
        return redirect(url_for("mba.admin_dashboard"))
    flash("Unknown discipline action.", "error")
    return redirect(url_for("mba.admin_dashboard"))


@mba_bp.route("/admin/assessors/import", methods=["POST"])
@login_required
def admin_import_assessors():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    uploaded_file = request.files.get("assessor_file") or request.files.get("assessor_csv")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose an Excel or CSV file to upload.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="assessors"))

    created_count = 0
    updated_count = 0
    skipped_rows = []
    email_messages = []

    try:
        rows, available_fields = _uploaded_import_rows(uploaded_file)
        if "email" not in available_fields:
            flash("Upload file must include an email column.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="assessors"))
        if not rows:
            flash("Upload file has no assessor rows.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="assessors"))

        for line_number, raw_row in enumerate(rows, start=2):
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw_row.items()
                if (key or "").strip().lower() in ASSESSOR_IMPORT_FIELDS
            }
            try:
                user, temporary_password, created = apply_assessor_csv_row(row)
            except ValueError as exc:
                skipped_rows.append(f"line {line_number}: {exc}")
                continue
            if created:
                created_count += 1
            else:
                updated_count += 1
            email_messages.append(assessor_onboarding_email(user, temporary_password))

        db.session.commit()
    except UnicodeDecodeError:
        db.session.rollback()
        flash("Upload file could not be read. Save CSV files as UTF-8 or use the Excel template.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="assessors"))
    except csv.Error as exc:
        db.session.rollback()
        flash(f"CSV import failed: {exc}", "error")
        return redirect(url_for("mba.admin_dashboard", panel="assessors"))
    except ValueError as exc:
        db.session.rollback()
        flash(f"Assessor import failed: {exc}", "error")
        return redirect(url_for("mba.admin_dashboard", panel="assessors"))

    email_result = send_bulk_emails(email_messages)
    delivered_count = len(email_result["delivered"])
    failed_count = len(email_result["failed"])
    message = (
        f"Assessor import complete: {created_count} created, {updated_count} updated, "
        f"{delivered_count} emails delivered, {failed_count} emails failed."
    )
    if skipped_rows:
        message += f" Skipped {len(skipped_rows)} row(s): {'; '.join(skipped_rows[:3])}."
    message += _email_failure_summary(email_result)
    flash(message, "success" if created_count or updated_count else "info")
    return redirect(url_for("mba.admin_dashboard", panel="assessors"))


@mba_bp.route("/admin/assessors/template")
@login_required
def admin_assessor_csv_template():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    return _build_xlsx_template(
        "mba_assessor_upload_template.xlsx",
        STAFF_TEMPLATE_FIELDS,
        _staff_template_sample("assessor@example.com"),
    )


@mba_bp.route("/admin/supervisors/import", methods=["POST"])
@login_required
def admin_import_supervisors():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    uploaded_file = request.files.get("supervisor_file") or request.files.get("supervisor_csv")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose an Excel or CSV file to upload.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="supervisors"))

    created_count = 0
    updated_count = 0
    skipped_rows = []
    email_messages = []

    try:
        rows, available_fields = _uploaded_import_rows(uploaded_file)
        if "email" not in available_fields:
            flash("Upload file must include an email column.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="supervisors"))
        if not rows:
            flash("Upload file has no supervisor rows.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="supervisors"))

        for line_number, raw_row in enumerate(rows, start=2):
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw_row.items()
                if (key or "").strip().lower() in SUPERVISOR_IMPORT_FIELDS
            }
            try:
                user, temporary_password, created = apply_supervisor_csv_row(row)
            except ValueError as exc:
                skipped_rows.append(f"line {line_number}: {exc}")
                continue
            if created:
                created_count += 1
            else:
                updated_count += 1
            email_messages.append(supervisor_onboarding_email(user, temporary_password))

        db.session.commit()
    except UnicodeDecodeError:
        db.session.rollback()
        flash("Upload file could not be read. Save CSV files as UTF-8 or use the Excel template.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="supervisors"))
    except csv.Error as exc:
        db.session.rollback()
        flash(f"CSV import failed: {exc}", "error")
        return redirect(url_for("mba.admin_dashboard", panel="supervisors"))
    except ValueError as exc:
        db.session.rollback()
        flash(f"Supervisor import failed: {exc}", "error")
        return redirect(url_for("mba.admin_dashboard", panel="supervisors"))

    email_result = send_bulk_emails(email_messages)
    delivered_count = len(email_result["delivered"])
    failed_count = len(email_result["failed"])
    message = (
        f"Supervisor import complete: {created_count} created, {updated_count} updated, "
        f"{delivered_count} emails delivered, {failed_count} emails failed."
    )
    if skipped_rows:
        message += f" Skipped {len(skipped_rows)} row(s): {'; '.join(skipped_rows[:3])}."
    message += _email_failure_summary(email_result)
    flash(message, "success" if created_count or updated_count else "info")
    return redirect(url_for("mba.admin_dashboard", panel="supervisors"))


@mba_bp.route("/admin/supervisors/template")
@login_required
def admin_supervisor_csv_template():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    return _build_xlsx_template(
        "mba_supervisor_upload_template.xlsx",
        STAFF_TEMPLATE_FIELDS,
        _staff_template_sample("supervisor@example.com"),
    )


@mba_bp.route("/admin/students/template")
@login_required
def admin_student_xlsx_template():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    return _build_xlsx_template(
        "mba_student_upload_template.xlsx",
        STUDENT_TEMPLATE_FIELDS,
        _student_template_sample(),
    )


@mba_bp.route("/admin/students/import", methods=["POST"])
@login_required
def admin_import_students():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    uploaded_file = request.files.get("student_excel")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose an Excel file to upload.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="students"))
    if not uploaded_file.filename.lower().endswith(".xlsx"):
        flash("Only .xlsx Excel files are accepted for student imports.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="students"))

    created_count = 0
    updated_count = 0
    skipped_rows = []
    email_messages = []

    try:
        rows, available_fields = _xlsx_dict_rows_and_fields(uploaded_file)
        if not rows:
            flash("Excel file has no student rows.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="students"))
        if not ({"student_email_address", "student_email", "email"} & available_fields):
            flash("Excel file must include a student_email_address column.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="students"))

        for line_number, raw_row in enumerate(rows, start=2):
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in raw_row.items()
                if (key or "").strip().lower() in STUDENT_IMPORT_FIELDS
            }
            try:
                user, temporary_password, created = apply_student_excel_row(row)
            except ValueError as exc:
                skipped_rows.append(f"line {line_number}: {exc}")
                continue
            if created:
                created_count += 1
            else:
                updated_count += 1
            email_messages.append(student_onboarding_email(user, temporary_password))

        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(f"Student import failed: {exc}", "error")
        return redirect(url_for("mba.admin_dashboard", panel="students"))

    email_result = send_bulk_emails(email_messages)
    delivered_count = len(email_result["delivered"])
    failed_count = len(email_result["failed"])
    message = (
        f"Student import complete: {created_count} created, {updated_count} updated, "
        f"{delivered_count} emails delivered, {failed_count} emails failed."
    )
    if skipped_rows:
        message += f" Skipped {len(skipped_rows)} row(s): {'; '.join(skipped_rows[:3])}."
    message += _email_failure_summary(email_result)
    flash(message, "success" if created_count or updated_count else "info")
    return redirect(url_for("mba.admin_dashboard", panel="students"))


@mba_bp.route("/admin/student-number-suggest")
@login_required
def admin_student_number_suggest():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return jsonify(numbers=[])
    q = (request.args.get("q") or "").strip()
    if not q or len(q) < 2:
        return jsonify(numbers=[])
    numbers = set()
    for row in db.session.query(MbaStudentProfile.student_number).filter(MbaStudentProfile.student_number.ilike(f"%{q}%")).limit(10):
        if row[0] and row[0].isdigit():
            numbers.add(row[0])
    return jsonify(numbers=sorted(numbers, key=str))
