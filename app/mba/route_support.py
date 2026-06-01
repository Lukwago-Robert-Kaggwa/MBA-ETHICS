from copy import deepcopy
from datetime import datetime
import base64
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import mimetypes
from pathlib import Path
import os
import quopri
import re
import shutil
import subprocess
import tempfile
import textwrap
import uuid
from xml.sax.saxutils import escape as xml_escape
import xml.etree.ElementTree as ET
import zipfile

from cryptography.fernet import Fernet, InvalidToken
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (
    MbaDiscipline,
    MbaDocumentTemplate,
    MbaForm,
    MbaProject,
    MbaProjectDocument,
    MbaProjectSupervisorInvitation,
    MbaReminderState,
    MbaRole,
    MbaScholarRole,
    MbaUserSignature,
    MbaUser,
    ProjectStatus,
)
from .recommendation import (
    SUPERVISOR_RECOMMENDATION_LIMIT,
    match_recommendations,
    recommend_assessors,
)

ALLOWED_UPLOAD_EXTENSIONS = {"pdf"}
DETAILED_REPORT_UPLOAD_EXTENSIONS = {"pdf", "doc", "docx"}
DASHBOARD_PAGE_SIZE_OPTIONS = (5, 10, 20, 50)
UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
SIGNATURE_MAX_BYTES = 2 * 1024 * 1024
SIGNATURE_UPLOAD_EXTENSIONS = {"png", "jpg", "jpeg"}
USER_SIGNATURE_PRIMARY = "primary"
USER_SIGNATURE_HEAD_OF_DEPARTMENT = "head_of_department"
USER_SIGNATURE_EXECUTIVE_DEAN = "executive_dean"
USER_SIGNATURE_DIRECTOR_OF_SCHOOL = "director_of_school"
USER_SIGNATURE_TYPES = {
    USER_SIGNATURE_PRIMARY,
    USER_SIGNATURE_HEAD_OF_DEPARTMENT,
    USER_SIGNATURE_EXECUTIVE_DEAN,
    USER_SIGNATURE_DIRECTOR_OF_SCHOOL,
}
USER_SIGNATURE_LABELS = {
    USER_SIGNATURE_PRIMARY: "Saved signature",
    USER_SIGNATURE_HEAD_OF_DEPARTMENT: "Head of Department signature",
    USER_SIGNATURE_EXECUTIVE_DEAN: "Executive Dean signature",
    USER_SIGNATURE_DIRECTOR_OF_SCHOOL: "Director of School signature",
}
SIGNATURE_FIELD_TYPE_MAP = {
    "head_of_department_signature": USER_SIGNATURE_HEAD_OF_DEPARTMENT,
    "hod_signature": USER_SIGNATURE_HEAD_OF_DEPARTMENT,
    "hod_signature_name": USER_SIGNATURE_HEAD_OF_DEPARTMENT,
    "director_signature": USER_SIGNATURE_DIRECTOR_OF_SCHOOL,
    "director_signature_name": USER_SIGNATURE_DIRECTOR_OF_SCHOOL,
    "executive_dean_signature_name": USER_SIGNATURE_EXECUTIVE_DEAN,
}
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
_SENSITIVE_DATA_KEY_WARNING_EMITTED = False
SUPERVISOR_SUGGESTION_LIMIT = SUPERVISOR_RECOMMENDATION_LIMIT
ASSESSOR_SLOTS = ("assessor_1", "assessor_2")
PRIMARY_ASSESSOR_SLOTS = ASSESSOR_SLOTS
ADDITIONAL_ASSESSOR_SLOT = "assessor_3"
ALL_ASSESSOR_SLOTS = PRIMARY_ASSESSOR_SLOTS + (ADDITIONAL_ASSESSOR_SLOT,)
SUMMARY_COURSEWORK_MODULES = (
    "AFM9X01",
    "CSM9X01",
    "DIG9X01",
    "PEM9X01",
    "CON9X00",
    "ADM9X02",
    "AEP9X02",
    "EIB9X02",
    "OPS9X02",
    "CSQ9X01",
    "ELD9X01",
    "IEA9X01",
    "BDD9X01",
    "CPM9X01",
)
ASSESSOR_PROJECT_DOCUMENT_VISIBLE_STATUSES = {
    ProjectStatus.ADMIN_APPROVED.value,
    ProjectStatus.HDC_VERIFIED.value,
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
    ProjectStatus.RESULTS_DECLINED.value,
    ProjectStatus.RESULTS_APPROVED.value,
    ProjectStatus.GRADUATED.value,
}
NOMINATION_FORWARDING_UNAVAILABLE_STATUSES = {
    ProjectStatus.ADMIN_APPROVED.value,
    ProjectStatus.HDC_VERIFIED.value,
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
    ProjectStatus.RESULTS_DECLINED.value,
    ProjectStatus.RESULTS_APPROVED.value,
    ProjectStatus.GRADUATED.value,
}
RESULTS_HDC_SUBMISSION_STATUSES = {
    ProjectStatus.HDC_VERIFIED.value,
    ProjectStatus.RESULTS_DECLINED.value,
}
DISSERTATION_CORRECTIONS_CLOSED_STATUSES = {
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
    ProjectStatus.RESULTS_DECLINED.value,
    ProjectStatus.RESULTS_APPROVED.value,
    ProjectStatus.GRADUATED.value,
}
CORRECTION_REQUEST_RECOMMENDATIONS = {
    "Accept subject to minor revisions to the satisfaction of the Supervisor / Head of School",
    "Accept subject to major revisions to the satisfaction of the Supervisor / Head of School",
    "Major revisions and re-examination by the same assessor",
}
HDC_ASSESSOR_NOMINATION_DOCUMENT_PREFIXES = (
    "assessor_cv_",
    "assessor_highest_qualification_",
)
HDC_ASSESSOR_RESULTS_DOCUMENT_PREFIXES = (
    "assessment_summary",
    "assessor_report_",
    "assessor_detailed_report_",
)
HDC_DOCUMENT_ALLOWED_STATUSES = {
    ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
    ProjectStatus.JBS5_HDC_APPROVED.value,
    ProjectStatus.JBS5_HDC_DECLINED.value,
    ProjectStatus.ADMIN_APPROVED.value,
    ProjectStatus.HDC_DECLINED.value,
    ProjectStatus.HDC_VERIFIED.value,
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
    ProjectStatus.RESULTS_DECLINED.value,
    ProjectStatus.RESULTS_APPROVED.value,
    ProjectStatus.GRADUATED.value,
}
PROJECT_TITLE_FORMAT_HELP = (
    "Use full words only. Acronyms, abbreviations, and special characters except commas and hyphens are not allowed. "
    "The system will capitalize the first letter of each word. Keep the title to 12 words where possible; maximum 15 words."
)
PROJECT_TITLE_INVALID_MESSAGE = (
    "Please edit the Capstone Project title. Use full words only with letters, numbers, spaces, commas, and hyphens. "
    "Acronyms, abbreviations, and other special characters are not allowed."
)
PROJECT_TITLE_RECOMMENDED_WORDS = 12
PROJECT_TITLE_MAX_WORDS = 15
PROJECT_TITLE_COMMON_ACRONYMS = {
    "ai",
    "api",
    "4ir",
    "b2b",
    "b2c",
    "bbbee",
    "bee",
    "ceo",
    "cfo",
    "covid",
    "covid19",
    "crm",
    "dept",
    "erp",
    "esg",
    "fin",
    "govt",
    "hr",
    "ict",
    "info",
    "intl",
    "it",
    "jbs",
    "jse",
    "kpi",
    "mba",
    "mgmt",
    "mgt",
    "ngo",
    "npo",
    "ops",
    "org",
    "popia",
    "roi",
    "sa",
    "sars",
    "sme",
    "smes",
    "uj",
    "uk",
    "usa",
    "vs",
}

MBA_DOCUMENT_LABELS = {
    "jbs5": "JBS 5 - Research Proposal Form",
    "jbs1_declaration": "JBS 1 Declaration",
    "supervisor_agreement": "Supervisor Agreement Form",
    "jbs10": "JBS10 - Project Submission Form",
    "intent_to_submit": "Intent to Submit",
    "dissertation": "Capstone Manuscript",
    "manuscript": "Capstone Manuscript",
    "global_document": "Global Document",
    "plagiarism_declaration": "Combined Plagiarism, Turnitin and AI Declaration",
    "combined_turnitin_ai_report": "Combined Turnitin-AI Report",
    "external_examiner_nomination": "Amended External Examiner Nomination Form",
    "additional_external_examiner_nomination": "Additional Assessor Nomination Form",
    "turnitin_report": "Turnitin / Plagiarism Form (Legacy)",
    "ai_report": "AI Report (Legacy)",
    "ethics_certificate": "Ethics Certificate",
    "ethics_exemption_form": "Ethics Exemption Form",
    "ai_declaration_form": "TII AI Declaration (JBS) (Legacy)",
    "affidavit": "JBS 2 Affidavit",
    "affidavit_stamped": "Stamped JBS 2 Affidavit",
    "corrected_dissertation": "Corrected Capstone Manuscript",
    "corrections_response": "Response to Assessors' Comments",
    "corrections_turnitin_report": "Resubmitted Turnitin Report",
}

MODULE_COMPLETION_STATUS_LABELS = {
    "not_checked": "Module Completion Not Checked",
    "completed": "Modules Completed",
    "awaiting_marks_committee": "Awaiting Coursework Marks from the Marks Committee",
    "modules_incomplete": "Modules Incomplete",
    "response_received": "Response Received",
}

PROJECT_STATUS_LABELS = {
    ProjectStatus.CREATED.value: "Draft",
    ProjectStatus.ADMIN_SUBMITTED.value: "Submitted to Admin",
    ProjectStatus.JBS5_SUBMITTED_TO_HDC.value: "JBS5 Pending HDC Review",
    ProjectStatus.JBS5_HDC_APPROVED.value: "JBS5 Approved by HDC",
    ProjectStatus.JBS5_HDC_DECLINED.value: "JBS5 Rejected by HDC",
    ProjectStatus.ADMIN_APPROVED.value: "Nominations Pending Review",
    ProjectStatus.ADMIN_DECLINED.value: "Declined by Admin",
    ProjectStatus.SUPERVISOR_ACCEPTED.value: "Supervisor Accepted",
    ProjectStatus.HDC_VERIFIED.value: "Nominations Approved",
    ProjectStatus.HDC_DECLINED.value: "Nominations Rejected",
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value: "Results Pending Review",
    ProjectStatus.RESULTS_APPROVED.value: "Results Approved by HDC",
    ProjectStatus.RESULTS_DECLINED.value: "Results Rejected",
    ProjectStatus.GRADUATED.value: "Graduated",
}

PUBLIC_PROJECT_STATUS_LABEL_OVERRIDES = {
    ProjectStatus.HDC_DECLINED.value: "Assessor Nominations In Progress",
    ProjectStatus.RESULTS_APPROVED.value: "Results Verified",
}

PUBLIC_PROJECT_STATUS_BADGE_CLASSES = {
    ProjectStatus.HDC_DECLINED.value: "nomination_pending_public",
    ProjectStatus.RESULTS_APPROVED.value: "results_approved",
}

ADDITIONAL_ASSESSMENT_STATUS_LABELS = {
    "needs_assignment": "Needs Third Assessor",
    "awaiting_nomination": "Awaiting Additional Nomination Approval",
    "awaiting_acceptance": "Awaiting Third Assessor Acceptance",
    "awaiting_result": "Awaiting Third Assessor Result",
    "completed": "Additional Assessment Complete",
    "none": "No Additional Assessment",
}

FORM_RENDER_VERSION = "v13"
EXTERNAL_EXAMINER_NOMINATION_RENDER_VERSION = "external_nomination_v7"
ADDITIONAL_EXTERNAL_EXAMINER_NOMINATION_RENDER_VERSION = "additional_external_nomination_v4"
ASSESSMENT_SUMMARY_RENDER_VERSION = "assessment_summary_v1"
FORM_HTML_PRINT_TEMPLATES = {
    "jbs5": "mba/form_fill_jbs5.html",
    "jbs10": "mba/form_fill_jbs10.html",
    "supervisor_agreement": "mba/form_fill_supervisor_agreement.html",
    "intent_to_submit": "mba/form_fill_intent_to_submit.html",
    "plagiarism_declaration": "mba/form_fill_plagiarism_declaration.html",
    "ai_declaration_form": "mba/form_fill_ai_declaration_form.html",
    "affidavit": "mba/form_fill_affidavit.html",
    "jbs1_declaration": "mba/form_fill_jbs1_declaration.html",
    "corrections_response": "mba/form_fill_corrections_response.html",
    "assessor_profile": "mba/form_fill_assessor_profile.html",
    "assessor_temp_appointment": "mba/form_fill_assessor_temp_appointment.html",
    "assessor_temp_claim": "mba/form_fill_assessor_temp_claim.html",
    "external_examiner_nomination": "mba/form_fill_external_examiner_nomination.html",
    "additional_external_examiner_nomination": "mba/form_fill_external_examiner_nomination.html",
    "assessment_summary": "mba/form_fill_assessment_summary.html",
    "assessment_result": "mba/form_fill_assessor_grade.html",
    "assessor_report": "mba/form_fill_assessor_grade.html",
    "assessor_narrative": "mba/form_fill_assessor_grade.html",
}
_FORM_FRAGMENT_START = "<!-- MBA_FORM_START -->"
_FORM_FRAGMENT_END = "<!-- MBA_FORM_END -->"

mba_bp = Blueprint("mba", __name__, template_folder="../templates")

INVITATION_PENDING = "pending"
INVITATION_ACCEPTED = "accepted"
INVITATION_DECLINED = "declined"
HDC_ASSESSOR_APPROVED = "approved"
HDC_ASSESSOR_DECLINED = "declined"
HDC_ASSESSOR_DECISIONS = {HDC_ASSESSOR_APPROVED, HDC_ASSESSOR_DECLINED}

ACTIVE_WORKLOAD_PROJECT_STATUSES = {
    ProjectStatus.ADMIN_SUBMITTED.value,
    ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
    ProjectStatus.JBS5_HDC_APPROVED.value,
    ProjectStatus.ADMIN_APPROVED.value,
    ProjectStatus.SUPERVISOR_ACCEPTED.value,
    ProjectStatus.HDC_DECLINED.value,
    ProjectStatus.HDC_VERIFIED.value,
    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
    ProjectStatus.RESULTS_DECLINED.value,
    ProjectStatus.RESULTS_APPROVED.value,
}
WORKLOAD_INVITATION_STATUSES = {INVITATION_PENDING, INVITATION_ACCEPTED}


def _add_student_workload(workloads, user_id, student_id):
    if user_id and student_id:
        workloads.setdefault(user_id, set()).add(student_id)


def _active_workload_projects(exclude_project_id=None):
    query = (
        MbaProject.query.options(joinedload(MbaProject.supervisor_invitations))
        .filter(MbaProject.project_status.in_(ACTIVE_WORKLOAD_PROJECT_STATUSES))
    )
    if exclude_project_id:
        query = query.filter(MbaProject.id != exclude_project_id)
    return query.all()


def supervisor_workload_counts(exclude_project_id=None):
    workloads = {}
    for project in _active_workload_projects(exclude_project_id=exclude_project_id):
        primary_status = getattr(project, "primary_supervisor_invitation_status", None)
        if (
            getattr(project, "primary_supervisor_id", None)
            and primary_status != INVITATION_DECLINED
            and (
                primary_status in WORKLOAD_INVITATION_STATUSES
                or getattr(project, "supervisor_accepted_at", None)
                or getattr(project, "supervisor_confirmed", False)
            )
        ):
            _add_student_workload(workloads, project.primary_supervisor_id, project.student_id)

        for invitation in getattr(project, "supervisor_invitations", []) or []:
            if invitation.status in WORKLOAD_INVITATION_STATUSES:
                _add_student_workload(workloads, invitation.supervisor_id, project.student_id)
    return {user_id: len(student_ids) for user_id, student_ids in workloads.items()}


def assessor_workload_counts(exclude_project_id=None):
    workloads = {}
    for project in _active_workload_projects(exclude_project_id=exclude_project_id):
        for slot in ALL_ASSESSOR_SLOTS:
            assessor_id = getattr(project, f"{slot}_id", None)
            if not assessor_id:
                continue
            invitation_status = getattr(project, f"{slot}_invitation_status", None)
            if (
                invitation_status == INVITATION_DECLINED
                or (slot in PRIMARY_ASSESSOR_SLOTS and assessor_hdc_decline_requires_replacement(project, slot))
            ):
                continue
            if invitation_status in WORKLOAD_INVITATION_STATUSES or (
                slot in PRIMARY_ASSESSOR_SLOTS and getattr(project, "assessors_confirmed", False)
            ):
                _add_student_workload(workloads, assessor_id, project.student_id)
    return {user_id: len(student_ids) for user_id, student_ids in workloads.items()}

INVITATION_SLOTS = {
    "primary_supervisor": {
        "id_field": "primary_supervisor_id",
        "status_field": "primary_supervisor_invitation_status",
        "label": "Supervisor",
    },
    "assessor_1": {
        "id_field": "assessor_1_id",
        "status_field": "assessor_1_invitation_status",
        "label": "Assessor 1",
    },
    "assessor_2": {
        "id_field": "assessor_2_id",
        "status_field": "assessor_2_invitation_status",
        "label": "Assessor 2",
    },
    "assessor_3": {
        "id_field": "assessor_3_id",
        "status_field": "assessor_3_invitation_status",
        "label": "Assessor 3",
    },
}

def set_invitations_sent(project):
    """Mark the project as having sent invitations."""
    project.invitations_sent_at = datetime.utcnow()
    db.session.add(project)


def mark_supervisor_invitations_sent(project, sent_at=None, invitations=None):
    sent_at = sent_at or datetime.utcnow()
    targets = invitations if invitations is not None else getattr(project, "supervisor_invitations", [])
    for invitation in targets:
        if invitation.status == INVITATION_PENDING:
            invitation.invited_at = sent_at
            invitation.reminder_sent_at = None


def mark_assessor_invitations_sent(project, slots=None, sent_at=None):
    sent_at = sent_at or datetime.utcnow()
    project.invitations_sent_at = sent_at
    for slot in (slots or ASSESSOR_SLOTS):
        if getattr(project, f"{slot}_id"):
            setattr(project, f"{slot}_invited_at", sent_at)
            setattr(project, f"{slot}_reminder_sent_at", None)


def assessor_hdc_decision(project, slot):
    if slot not in PRIMARY_ASSESSOR_SLOTS:
        return None
    return getattr(project, f"{slot}_hdc_decision", None)


def assessor_hdc_decision_label(decision):
    return {
        HDC_ASSESSOR_APPROVED: "Approved",
        HDC_ASSESSOR_DECLINED: "Declined",
    }.get(decision, "Pending Review")


def set_assessor_hdc_decision(project, slot, decision):
    if slot not in PRIMARY_ASSESSOR_SLOTS:
        return
    if decision in HDC_ASSESSOR_DECISIONS:
        setattr(project, f"{slot}_hdc_decision", decision)
        setattr(project, f"{slot}_hdc_decision_at", datetime.utcnow())
        setattr(project, f"{slot}_hdc_decision_assessor_id", getattr(project, f"{slot}_id", None))
        return
    setattr(project, f"{slot}_hdc_decision", None)
    setattr(project, f"{slot}_hdc_decision_at", None)
    setattr(project, f"{slot}_hdc_decision_assessor_id", None)


def reset_assessor_hdc_decisions(project, slots=None):
    for slot in (slots or PRIMARY_ASSESSOR_SLOTS):
        set_assessor_hdc_decision(project, slot, None)


def hdc_assessor_nomination_decisions(project):
    return {
        slot: assessor_hdc_decision(project, slot)
        for slot in PRIMARY_ASSESSOR_SLOTS
        if getattr(project, f"{slot}_id", None)
    }


def hdc_assessor_nomination_review_complete(project):
    return all(
        getattr(project, f"{slot}_id", None)
        and assessor_hdc_decision(project, slot) in HDC_ASSESSOR_DECISIONS
        for slot in PRIMARY_ASSESSOR_SLOTS
    )


HDC_DOCUMENT_SIGNATURE_REQUIREMENTS = {
    "jbs5": (
        (("head_of_department_signature",), "Head of Department signature"),
        (("head_of_department_signature_date",), "Head of Department signature date"),
        (("jbs_hdc_signature",), "JBS HDC signature"),
        (("jbs_hdc_signature_date",), "JBS HDC signature date"),
    ),
    "jbs10": (
        (("head_of_department_signature",), "Head of Department signature"),
        (("head_of_department_signature_date",), "Head of Department signature date"),
        (("jbs_hdc_signature",), "JBS HDC signature"),
        (("jbs_hdc_signature_date",), "JBS HDC signature date"),
    ),
    "intent_to_submit": (
        (("hod_signature",), "Head of Department signature"),
        (("hod_signature_date",), "Head of Department signature date"),
        (("director_signature",), "Director of School signature"),
        (("director_signature_date",), "Director of School signature date"),
    ),
    "external_examiner_nomination": (
        (("hod_signature_name",), "Head of Department signature"),
        (("hod_signature_date",), "Head of Department signature date"),
        (("executive_dean_signature_name",), "Executive Dean signature"),
        (("executive_dean_signature_date",), "Executive Dean signature date"),
    ),
    "additional_external_examiner_nomination": (
        (("hod_signature_name",), "Head of Department signature"),
        (("hod_signature_date",), "Head of Department signature date"),
        (("executive_dean_signature_name",), "Executive Dean signature"),
        (("executive_dean_signature_date",), "Executive Dean signature date"),
    ),
    "assessment_summary": (
        (("hod_signature_name",), "Head of Department signature"),
        (("hod_signature_date",), "Head of Department signature date"),
        (("chair_fhdc_signature_name", "hdc_signature_name"), "Chair of FHDC signature"),
        (("chair_fhdc_signature_date", "hdc_signature_date"), "Chair of FHDC signature date"),
    ),
}


def _project_form_payload(project, form_type, form=None):
    if form and getattr(form, "form_type", None) == form_type and isinstance(getattr(form, "payload", None), dict):
        return form.payload
    matched_form = next(
        (
            project_form
            for project_form in getattr(project, "forms", []) or []
            if project_form.form_type == form_type
        ),
        None,
    )
    if not matched_form and getattr(project, "id", None):
        matched_form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
    return matched_form.payload if matched_form and isinstance(matched_form.payload, dict) else {}


def hdc_document_signature_status(project, doc_type, form=None):
    form_type = str(doc_type or "")
    requirements = HDC_DOCUMENT_SIGNATURE_REQUIREMENTS.get(form_type)
    if not requirements:
        return None
    payload = _project_form_payload(project, form_type, form)
    missing = []
    completed_count = 0
    for field_names, label in requirements:
        if any(payload.get(field_name) for field_name in field_names):
            completed_count += 1
        else:
            missing.append(label)
    complete = not missing
    if complete:
        label = "Signed"
    elif completed_count:
        label = "Partially signed"
    else:
        label = "Not signed yet"
    return {
        "complete": complete,
        "label": label,
        "badge": "accepted" if complete else "pending",
        "missing": missing,
    }


def hdc_jbs10_signature_complete(project):
    if not project:
        return False
    jbs10_form = next(
        (
            form
            for form in getattr(project, "forms", []) or []
            if form.form_type == "jbs10"
        ),
        None,
    )
    if not jbs10_form and getattr(project, "id", None):
        jbs10_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs10").first()
    payload = jbs10_form.payload if jbs10_form and isinstance(jbs10_form.payload, dict) else {}
    return all(
        payload.get(field)
        for field in (
            "head_of_department_signature",
            "head_of_department_signature_date",
            "jbs_hdc_signature",
            "jbs_hdc_signature_date",
        )
    )


def hdc_intent_to_submit_signature_complete(project):
    if not project:
        return False
    intent_form = next(
        (
            form
            for form in getattr(project, "forms", []) or []
            if form.form_type == "intent_to_submit"
        ),
        None,
    )
    if not intent_form and getattr(project, "id", None):
        intent_form = MbaForm.query.filter_by(project_id=project.id, form_type="intent_to_submit").first()
    payload = intent_form.payload if intent_form and isinstance(intent_form.payload, dict) else {}
    return all(
        payload.get(field)
        for field in (
            "hod_signature",
            "hod_signature_date",
            "director_signature",
            "director_signature_date",
        )
    )


def hdc_external_examiner_nomination_signature_complete(project):
    if not project:
        return False
    form = next(
        (
            form
            for form in getattr(project, "forms", []) or []
            if form.form_type == "external_examiner_nomination"
        ),
        None,
    )
    if not form and getattr(project, "id", None):
        form = MbaForm.query.filter_by(project_id=project.id, form_type="external_examiner_nomination").first()
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return all(
        payload.get(field)
        for field in (
            "hod_signature_name",
            "hod_signature_date",
            "executive_dean_signature_name",
            "executive_dean_signature_date",
        )
    )


def hdc_additional_external_examiner_nomination_signature_complete(project):
    if not project:
        return False
    form = next(
        (
            form
            for form in getattr(project, "forms", []) or []
            if form.form_type == "additional_external_examiner_nomination"
        ),
        None,
    )
    if not form and getattr(project, "id", None):
        form = MbaForm.query.filter_by(
            project_id=project.id,
            form_type="additional_external_examiner_nomination",
        ).first()
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return all(
        payload.get(field)
        for field in (
            "hod_signature_name",
            "hod_signature_date",
            "executive_dean_signature_name",
            "executive_dean_signature_date",
        )
    )


def hdc_nomination_signature_documents_complete(project):
    return (
        hdc_jbs10_signature_complete(project)
        and hdc_intent_to_submit_signature_complete(project)
        and hdc_external_examiner_nomination_signature_complete(project)
    )


def sync_hdc_assessor_nomination_status(project):
    decisions = hdc_assessor_nomination_decisions(project)
    if not hdc_assessor_nomination_review_complete(project):
        project.nomination_form_approved = False
        if project.project_status == ProjectStatus.HDC_DECLINED.value:
            project.project_status = ProjectStatus.ADMIN_APPROVED.value
        return "pending"

    has_declined = any(decision == HDC_ASSESSOR_DECLINED for decision in decisions.values())
    if has_declined:
        if not hdc_nomination_signature_documents_complete(project):
            project.nomination_form_approved = False
            project.project_status = ProjectStatus.ADMIN_APPROVED.value
            return "signature_pending_declined"
        project.nomination_form_approved = False
        project.project_status = ProjectStatus.HDC_DECLINED.value
        return "declined"

    if all(decision == HDC_ASSESSOR_APPROVED for decision in decisions.values()):
        if not hdc_nomination_signature_documents_complete(project):
            project.nomination_form_approved = False
            project.project_status = ProjectStatus.ADMIN_APPROVED.value
            return "signature_pending"
        project.project_status = ProjectStatus.HDC_VERIFIED.value
        project.nomination_form_approved = True
        return "approved"

    project.project_status = ProjectStatus.HDC_DECLINED.value
    project.nomination_form_approved = False
    return "declined"


def hdc_declined_assessor_nomination(project):
    return project.project_status == ProjectStatus.HDC_DECLINED.value


def hdc_rejection_without_slot_decisions_requires_replacement(project):
    if project.project_status != ProjectStatus.HDC_DECLINED.value:
        return False
    if any(assessor_hdc_decision(project, slot) for slot in PRIMARY_ASSESSOR_SLOTS):
        return False
    return not (
        accepted_assessor_count(project) >= len(PRIMARY_ASSESSOR_SLOTS)
        and all_assessor_acceptance_packs_complete(project)
    )


def assessor_hdc_decline_requires_replacement(project, slot):
    if project.project_status != ProjectStatus.HDC_DECLINED.value:
        return False
    if assessor_hdc_decision(project, slot) != HDC_ASSESSOR_DECLINED:
        return False

    current_assessor_id = getattr(project, f"{slot}_id", None)
    decision_assessor_id = getattr(project, f"{slot}_hdc_decision_assessor_id", None)
    if decision_assessor_id and current_assessor_id and decision_assessor_id != current_assessor_id:
        return False

    decision_at = getattr(project, f"{slot}_hdc_decision_at", None)
    invited_at = getattr(project, f"{slot}_invited_at", None)
    if decision_at and invited_at and invited_at > decision_at:
        return False

    # Backward-compatible fallback for decisions recorded before decision timestamps existed.
    if not decision_assessor_id and not decision_at:
        return not (
            getattr(project, f"{slot}_invitation_status") == INVITATION_ACCEPTED
            and assessor_acceptance_pack_complete(project, slot)
        )

    return True


def hdc_declined_assessor_slots(project):
    return [
        slot
        for slot in PRIMARY_ASSESSOR_SLOTS
        if assessor_hdc_decline_requires_replacement(project, slot)
    ]


def hdc_resolved_declined_assessor_slots(project):
    return [
        slot
        for slot in PRIMARY_ASSESSOR_SLOTS
        if assessor_hdc_decision(project, slot) == HDC_ASSESSOR_DECLINED
        and not assessor_hdc_decline_requires_replacement(project, slot)
    ]


def reset_assessor_invitation_tracking(project, slots=None, clear_hdc_decisions=True):
    for slot in (slots or ASSESSOR_SLOTS):
        setattr(project, f"{slot}_invitation_status", None)
        setattr(project, f"{slot}_invited_at", None)
        setattr(project, f"{slot}_reminder_sent_at", None)
    if clear_hdc_decisions:
        reset_assessor_hdc_decisions(project, slots)


def _allowed_upload(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_UPLOAD_EXTENSIONS


def _uploads_dir():
    return os.path.join(current_app.root_path, "..", "uploads", "mba_forms")


def _signature_upload_dir(create=True):
    signature_dir = Path(current_app.root_path).parent / "uploads" / "mba_signatures"
    if create:
        signature_dir.mkdir(parents=True, exist_ok=True)
    return signature_dir


def _signature_extension_from_bytes(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    return ""


def _signature_mime_from_extension(extension):
    extension = str(extension or "").lower().lstrip(".")
    if extension == "png":
        return "image/png"
    if extension in {"jpg", "jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def _signature_sha256(data):
    return hashlib.sha256(data).hexdigest()


def normalize_user_signature_type(signature_type):
    signature_type = str(signature_type or USER_SIGNATURE_PRIMARY).strip() or USER_SIGNATURE_PRIMARY
    return signature_type if signature_type in USER_SIGNATURE_TYPES else USER_SIGNATURE_PRIMARY


def user_signature_type_label(signature_type):
    return USER_SIGNATURE_LABELS.get(normalize_user_signature_type(signature_type), USER_SIGNATURE_LABELS[USER_SIGNATURE_PRIMARY])


def signature_type_for_form_field(field):
    return SIGNATURE_FIELD_TYPE_MAP.get(str(field or ""), USER_SIGNATURE_PRIMARY)


def active_user_signature_record(user, signature_type=USER_SIGNATURE_PRIMARY):
    if not user or not getattr(user, "id", None):
        return None
    signature_type = normalize_user_signature_type(signature_type)
    try:
        return (
            MbaUserSignature.query.filter_by(user_id=user.id, signature_type=signature_type, is_active=True)
            .order_by(MbaUserSignature.updated_at.desc(), MbaUserSignature.id.desc())
            .first()
        )
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Could not load DB signature for MBA user %s", getattr(user, "id", None), exc_info=True)
        return None


def user_signature_path(user, signature_type=USER_SIGNATURE_PRIMARY):
    if not user or not getattr(user, "id", None):
        return None
    signature_type = normalize_user_signature_type(signature_type)
    if signature_type != USER_SIGNATURE_PRIMARY:
        return None
    signature_dir = _signature_upload_dir(create=False)
    if not signature_dir.exists():
        return None
    for extension in ("png", "jpg", "jpeg"):
        path = signature_dir / f"user_{user.id}.{extension}"
        if path.exists():
            return path
    return None


def user_has_signature(user, signature_type=USER_SIGNATURE_PRIMARY):
    return bool(active_user_signature_record(user, signature_type) or user_signature_path(user, signature_type))


def user_signature_printed_name(user, signature_type=USER_SIGNATURE_PRIMARY):
    signature_type = normalize_user_signature_type(signature_type)
    signature = active_user_signature_record(user, signature_type)
    printed_name = (getattr(signature, "printed_name", None) or "").strip() if signature else ""
    if printed_name:
        return printed_name
    if signature_type == USER_SIGNATURE_PRIMARY and user and getattr(user, "role", None) != MbaRole.HDC.value:
        return (
            f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
            or getattr(user, "email", "")
            or ""
        )
    return ""


def user_signature_bytes(user, signature_type=USER_SIGNATURE_PRIMARY):
    signature_type = normalize_user_signature_type(signature_type)
    signature = active_user_signature_record(user, signature_type)
    if signature:
        return bytes(signature.file_data or b""), signature.mime_type or "application/octet-stream"

    path = user_signature_path(user, signature_type)
    if not path:
        return b"", ""
    try:
        data = path.read_bytes()
    except OSError:
        return b"", ""
    return data, _signature_mime_from_extension(path.suffix)


def user_signature_cache_token(user, signature_type=USER_SIGNATURE_PRIMARY):
    signature_type = normalize_user_signature_type(signature_type)
    signature = active_user_signature_record(user, signature_type)
    if signature:
        return str(getattr(signature, "updated_at", None) or getattr(signature, "id", "") or "")
    path = user_signature_path(user, signature_type)
    if path:
        try:
            return str(int(path.stat().st_mtime))
        except OSError:
            return "1"
    return ""


def _clear_user_signature_files(user):
    if not user or not getattr(user, "id", None):
        return
    signature_dir = _signature_upload_dir(create=False)
    if not signature_dir.exists():
        return
    for extension in SIGNATURE_UPLOAD_EXTENSIONS:
        path = signature_dir / f"user_{user.id}.{extension}"
        if path.exists():
            try:
                path.unlink()
            except OSError:
                current_app.logger.warning("Could not remove signature file %s", path, exc_info=True)


def _refresh_user_signature_flag(user):
    if user:
        user.has_signature = user_has_signature(user, USER_SIGNATURE_PRIMARY)


def user_signature_mime_type(user, signature_type=USER_SIGNATURE_PRIMARY):
    signature_type = normalize_user_signature_type(signature_type)
    signature = active_user_signature_record(user, signature_type)
    if signature:
        return signature.mime_type or "application/octet-stream"
    path = user_signature_path(user, signature_type)
    if not path:
        return ""
    return _signature_mime_from_extension(path.suffix)


def user_signature_data_uri(user, signature_type=USER_SIGNATURE_PRIMARY):
    data, mime_type = user_signature_bytes(user, signature_type)
    if not data:
        return ""
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"


def save_user_signature(user, *, uploaded_file=None, signature_data=None, signature_type=USER_SIGNATURE_PRIMARY, printed_name=None):
    if not user or not getattr(user, "id", None):
        raise ValueError("A signed-in MBA user is required.")
    signature_type = normalize_user_signature_type(signature_type)
    printed_name = (printed_name or "").strip() or None
    data = b""
    source = "uploaded"
    if signature_data:
        source = "drawn"
        match = re.match(
            r"^data:image/(?P<subtype>png|jpeg|jpg);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
            str(signature_data).strip(),
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("The drawn signature could not be read. Please draw it again.")
        try:
            data = base64.b64decode(match.group("data"), validate=False)
        except Exception as exc:
            raise ValueError("The drawn signature could not be read. Please draw it again.") from exc
    elif uploaded_file and getattr(uploaded_file, "filename", ""):
        data = uploaded_file.read()
    else:
        existing_signature = active_user_signature_record(user, signature_type)
        if existing_signature:
            if printed_name is not None:
                existing_signature.printed_name = printed_name
                existing_signature.updated_at = datetime.utcnow()
            _refresh_user_signature_flag(user)
            return existing_signature
        existing_path = user_signature_path(user, signature_type)
        if existing_path:
            try:
                data = existing_path.read_bytes()
                source = "filesystem_import"
            except OSError:
                data = b""
        if not data:
            raise ValueError("Upload or draw a signature before saving.")

    if not data:
        raise ValueError("The signature file is empty.")
    if len(data) > SIGNATURE_MAX_BYTES:
        raise ValueError("The signature image must be 2 MB or smaller.")

    extension = _signature_extension_from_bytes(data)
    if extension not in SIGNATURE_UPLOAD_EXTENSIONS:
        raise ValueError("Use a PNG or JPG signature image.")

    digest = _signature_sha256(data)
    existing_active = active_user_signature_record(user, signature_type)
    if existing_active and existing_active.sha256 == digest:
        existing_active.mime_type = _signature_mime_from_extension(extension)
        existing_active.file_size = len(data)
        existing_active.source = source
        if printed_name is not None:
            existing_active.printed_name = printed_name
        existing_active.updated_at = datetime.utcnow()
        _refresh_user_signature_flag(user)
        _clear_user_signature_files(user)
        return existing_active

    MbaUserSignature.query.filter_by(user_id=user.id, signature_type=signature_type, is_active=True).update(
        {"is_active": False, "updated_at": datetime.utcnow()},
        synchronize_session=False,
    )
    signature = MbaUserSignature(
        user_id=user.id,
        file_data=data,
        mime_type=_signature_mime_from_extension(extension),
        file_size=len(data),
        sha256=digest,
        source=source,
        signature_type=signature_type,
        printed_name=printed_name,
        is_active=True,
    )
    db.session.add(signature)
    _clear_user_signature_files(user)
    _refresh_user_signature_flag(user)
    return signature


def delete_user_signature(user, signature_type=USER_SIGNATURE_PRIMARY):
    signature_type = normalize_user_signature_type(signature_type)
    if user and getattr(user, "id", None):
        MbaUserSignature.query.filter_by(user_id=user.id, signature_type=signature_type, is_active=True).update(
            {"is_active": False, "updated_at": datetime.utcnow()},
            synchronize_session=False,
        )
    if signature_type == USER_SIGNATURE_PRIMARY:
        _clear_user_signature_files(user)
    _refresh_user_signature_flag(user)


SIGNATURE_SNAPSHOT_SUFFIXES = ("_image", "_image_source", "_image_user_id", "_image_email")


def _signature_snapshot_keys(field):
    return tuple(f"{field}{suffix}" for suffix in SIGNATURE_SNAPSHOT_SUFFIXES)


def clear_signature_snapshots(payload, signature_fields):
    if not isinstance(payload, dict):
        return payload
    for field in signature_fields:
        for key in _signature_snapshot_keys(field):
            payload.pop(key, None)
    return payload


def copy_signature_snapshots(payload, source_payload, signature_fields):
    if not isinstance(payload, dict) or not isinstance(source_payload, dict):
        return payload
    for field in signature_fields:
        for key in _signature_snapshot_keys(field):
            if source_payload.get(key):
                payload[key] = source_payload[key]
    return payload


def apply_saved_signature_snapshot(payload, signature_fields, user=None, signature_type_by_field=None):
    user = user or current_user
    signature_type_by_field = signature_type_by_field or {}
    data_uri_cache = {}
    for field in signature_fields:
        signature_type = normalize_user_signature_type(
            signature_type_by_field.get(field) or signature_type_for_form_field(field)
        )
        if signature_type not in data_uri_cache:
            data_uri_cache[signature_type] = user_signature_data_uri(user, signature_type)
        data_uri = data_uri_cache[signature_type]
        if not data_uri:
            continue
        if not payload.get(field):
            printed_name = user_signature_printed_name(user, signature_type)
            if printed_name:
                payload[field] = printed_name
        payload[f"{field}_image"] = data_uri
        payload[f"{field}_image_source"] = "saved_profile_signature"
        payload[f"{field}_image_user_id"] = str(getattr(user, "id", "") or "")
        payload[f"{field}_image_email"] = getattr(user, "email", "") or ""
    return payload


def refresh_saved_signature_snapshot(payload, signature_fields, user=None, signature_type_by_field=None):
    clear_signature_snapshots(payload, signature_fields)
    return apply_saved_signature_snapshot(payload, signature_fields, user, signature_type_by_field)


def _validate_uploaded_pdf(uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return "No file selected."
    if not _allowed_upload(uploaded_file.filename):
        return "Only PDF files are accepted."
    uploaded_file.seek(0, 2)
    file_size = uploaded_file.tell()
    uploaded_file.seek(0)
    if file_size > UPLOAD_MAX_BYTES:
        return "File exceeds the 10 MB limit."
    return None


def _validate_optional_assessor_detailed_report(uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return None
    extension = uploaded_file.filename.rsplit(".", 1)[1].lower() if "." in uploaded_file.filename else ""
    if extension not in DETAILED_REPORT_UPLOAD_EXTENSIONS:
        return "Detailed report attachment must be a PDF or Word document."
    uploaded_file.seek(0, 2)
    file_size = uploaded_file.tell()
    uploaded_file.seek(0)
    if file_size > UPLOAD_MAX_BYTES:
        return "Detailed report attachment exceeds the 10 MB limit."
    return None


def document_mime_type(filename, fallback="application/octet-stream"):
    guessed, _encoding = mimetypes.guess_type(filename or "")
    return guessed or fallback


def _uploaded_file_bytes(uploaded_file):
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(0)
    return data


def _sensitive_data_key_material():
    global _SENSITIVE_DATA_KEY_WARNING_EMITTED
    configured = (
        current_app.config.get("MBA_DATA_ENCRYPTION_KEY")
        or os.getenv("MBA_DATA_ENCRYPTION_KEY")
        or ""
    )
    if configured:
        return str(configured).strip(), "mba_data_encryption_key"
    fallback = current_app.config.get("SECRET_KEY") or os.getenv("SECRET_KEY") or ""
    if fallback:
        if not _SENSITIVE_DATA_KEY_WARNING_EMITTED:
            current_app.logger.warning(
                "MBA_DATA_ENCRYPTION_KEY is not set; falling back to SECRET_KEY for sensitive MBA form encryption."
            )
            _SENSITIVE_DATA_KEY_WARNING_EMITTED = True
        derived = base64.urlsafe_b64encode(hashlib.sha256(str(fallback).encode("utf-8")).digest()).decode("ascii")
        return derived, "secret_key_fallback"
    raise RuntimeError("MBA_DATA_ENCRYPTION_KEY must be configured before storing sensitive banking details.")


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


def is_encrypted_sensitive_value(value):
    return isinstance(value, dict) and value.get("__encrypted__") == ENCRYPTED_PAYLOAD_MARKER and bool(value.get("ciphertext"))


def encrypt_sensitive_value(value):
    if is_encrypted_sensitive_value(value):
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


def decrypt_sensitive_value(value):
    if not is_encrypted_sensitive_value(value):
        return "" if value is None else str(value)
    try:
        return _sensitive_data_fernet().decrypt(str(value.get("ciphertext") or "").encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Sensitive banking data could not be decrypted with the configured key.") from exc


def _mask_account_number(value):
    text = str(value or "").strip()
    if not text:
        return ""
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 4:
        return f"{'*' * max(4, len(digits) - 4)}{digits[-4:]}"
    if len(text) <= 4:
        return "*" * len(text)
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def mask_sensitive_field_value(field_name, value):
    text = "" if value is None else str(value)
    if not text.strip():
        return ""
    if field_name in {"bank_account_number", "income_tax_number"}:
        return _mask_account_number(text)
    if field_name in {"bank_branch_code"}:
        return "****"
    return "********"


def encrypt_sensitive_payload_fields(payload):
    payload = dict(payload or {})
    for field_name in SENSITIVE_FORM_FIELD_NAMES:
        if field_name in payload and str(payload.get(field_name) or "").strip():
            payload[field_name] = encrypt_sensitive_value(payload[field_name])
    return payload


def decrypt_sensitive_payload_fields(payload, *, mask=False, blank=False):
    payload = dict(payload or {})
    for field_name in SENSITIVE_FORM_FIELD_NAMES:
        if field_name not in payload:
            continue
        if blank:
            payload[field_name] = ""
            continue
        value = decrypt_sensitive_value(payload[field_name])
        payload[field_name] = mask_sensitive_field_value(field_name, value) if mask else value
    return payload


def strip_sensitive_payload_fields(payload):
    return decrypt_sensitive_payload_fields(payload, blank=True)


def sensitive_document_type(doc_type):
    doc_type = str(doc_type or "")
    return doc_type.startswith(SENSITIVE_DOCUMENT_TYPE_PREFIXES)


def encrypted_document_bytes(data):
    return bool(data and bytes(data[: len(ENCRYPTED_DOCUMENT_PREFIX)]) == ENCRYPTED_DOCUMENT_PREFIX)


def encrypt_sensitive_document_bytes(doc_type, data):
    if not sensitive_document_type(doc_type) or not data or encrypted_document_bytes(data):
        return data
    return ENCRYPTED_DOCUMENT_PREFIX + _sensitive_data_fernet().encrypt(bytes(data))


def decrypt_sensitive_document_bytes(data):
    if not encrypted_document_bytes(data):
        return data
    try:
        return _sensitive_data_fernet().decrypt(bytes(data[len(ENCRYPTED_DOCUMENT_PREFIX) :]))
    except InvalidToken as exc:
        raise RuntimeError("Sensitive document data could not be decrypted with the configured key.") from exc


def append_comment(existing, comment):
    comment = (comment or "").strip()
    if not comment:
        return existing
    if existing:
        return f"{existing}\n{datetime.utcnow().isoformat(timespec='seconds')}: {comment}"
    return f"{datetime.utcnow().isoformat(timespec='seconds')}: {comment}"


def _format_project_title_word(word):
    parts = []
    for part in word.split("-"):
        lowered = part.lower()
        for index, char in enumerate(lowered):
            if char.isalpha():
                parts.append(f"{lowered[:index]}{char.upper()}{lowered[index + 1:]}")
                break
        else:
            parts.append(lowered)
    return "-".join(parts)


def _project_title_word_has_acronym_or_abbreviation(word):
    for part in re.split(r"[,-]+", word):
        letters = "".join(char for char in part if char.isalpha())
        lowered = part.lower()
        if lowered in PROJECT_TITLE_COMMON_ACRONYMS:
            return True
        if len(letters) > 1 and sum(1 for char in letters if char.isupper()) >= 2:
            return True
    return False


def project_title_validation_error(title):
    normalized = " ".join(str(title or "").split())
    if not normalized:
        return "Capstone Project title is required."
    if re.search(r"[^A-Za-z0-9\s,-]", normalized):
        return PROJECT_TITLE_INVALID_MESSAGE
    if any(_project_title_word_has_acronym_or_abbreviation(word) for word in normalized.split()):
        return PROJECT_TITLE_INVALID_MESSAGE
    word_count = len(normalized.split())
    if word_count > PROJECT_TITLE_MAX_WORDS:
        return (
            "Capstone Project title must be 15 words or fewer. "
            f"Your title is {word_count} word{'s' if word_count != 1 else ''}."
        )
    return None


def format_project_title(title):
    normalized = " ".join(str(title or "").split())
    if not normalized:
        return ""
    return " ".join(_format_project_title_word(word) for word in normalized.split())


def _pdf_text(value):
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _generated_form_pdf_marker(form_type):
    return f"MBA formatted web form {FORM_RENDER_VERSION}: {form_type}"


def _stamp_generated_pdf_bytes(pdf_bytes, marker):
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        return pdf_bytes
    header_end = pdf_bytes.find(b"\n")
    if header_end < 0:
        return pdf_bytes
    marker_line = f"% {marker}\n".encode("latin-1", "replace")
    if marker_line in pdf_bytes[:512]:
        return pdf_bytes
    return pdf_bytes[: header_end + 1] + marker_line + pdf_bytes[header_end + 1 :]


HTML_PDF_RENDERER_UNAVAILABLE_MESSAGE = (
    "The exact HTML-to-PDF renderer is unavailable. Install Chromium/Chrome on the server "
    "or set MBA_PDF_BROWSER_PATH to the browser executable."
)
FORM_WORD_EXTENSION = "docx"
FORM_WORD_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
JBS5_WORD_TEMPLATE = Path("mba") / "docx_templates" / "jbs5_registration_template.docx"
SUPERVISOR_AGREEMENT_WORD_TEMPLATE = Path("mba") / "docx_templates" / "supervisor_agreement_template.docx"
JBS10_WORD_TEMPLATE = Path("mba") / "docx_templates" / "jbs10_external_examiner_nomination_template.docx"
INTENT_TO_SUBMIT_WORD_TEMPLATE = Path("mba") / "docx_templates" / "intent_to_submit_template.docx"
CORRECTIONS_RESPONSE_WORD_TEMPLATE = Path("mba") / "docx_templates" / "corrections_response_template.docx"
JBS1_WORD_TEMPLATE = Path("mba") / "docx_templates" / "jbs1_declaration_template.docx"
AFFIDAVIT_WORD_TEMPLATE = Path("mba") / "docx_templates" / "affidavit_template.docx"
CAPSTONE_EVALUATION_WORD_TEMPLATE = Path("mba") / "docx_templates" / "capstone_final_submission_evaluation_template.docx"
CAPSTONE_ASSESSOR_REPORT_FORM_1_WORD_TEMPLATE = Path("mba") / "docx_templates" / "capstone_assessor_report_form_1_template.docx"
SUMMARY_ASSESSMENT_REPORT_WORD_TEMPLATE = Path("mba") / "docx_templates" / "summary_assessment_report_template.docx"
TII_AI_WORD_TEMPLATE = Path("mba") / "docx_templates" / "tii_ai_declaration_template.docx"
ASSESSOR_TEMP_APPOINTMENT_WORD_TEMPLATE = Path("mba") / "docx_templates" / "assessor_temp_appointment_template.docx"
ASSESSOR_TEMP_CLAIM_WORD_TEMPLATE = Path("mba") / "docx_templates" / "assessor_temp_claim_template.docx"
EXTERNAL_EXAMINER_NOMINATION_WORD_TEMPLATE = Path("mba") / "docx_templates" / "external_examiner_nomination_template.docx"
_DOCX_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DOCX_NS = {
    "w": _DOCX_W_NS,
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
for _docx_prefix, _docx_uri in _DOCX_NS.items():
    ET.register_namespace(_docx_prefix, _docx_uri)


def _word_template_key_for_path(template_path):
    if not template_path:
        return ""
    path = Path(template_path)
    try:
        relative_path = path.resolve().relative_to(Path(current_app.root_path).resolve())
    except Exception:
        relative_path = Path(str(template_path))
    return str(relative_path).replace("\\", "/")


def active_document_template_record(template_path_or_key):
    template_key = str(template_path_or_key or "")
    if template_key.lower().endswith(".docx") or "\\" in template_key:
        template_key = _word_template_key_for_path(template_path_or_key)
    if not template_key:
        return None
    try:
        return (
            MbaDocumentTemplate.query.filter_by(template_key=template_key, is_active=True)
            .order_by(MbaDocumentTemplate.version.desc(), MbaDocumentTemplate.uploaded_at.desc(), MbaDocumentTemplate.id.desc())
            .first()
        )
    except Exception:
        db.session.rollback()
        current_app.logger.warning("Could not load DB document template %s", template_key, exc_info=True)
        return None


def document_template_bytes(template_path):
    template = active_document_template_record(template_path)
    if template and template.file_data:
        return bytes(template.file_data)
    if template_path and Path(template_path).exists():
        return Path(template_path).read_bytes()
    return b""


def _docx_template_exists(template_path):
    return bool(document_template_bytes(template_path))


def _browser_pdf_executables():
    env_candidates = [
        os.getenv(name)
        for name in (
            "MBA_PDF_BROWSER_PATH",
            "CHROME_BIN",
            "CHROMIUM_BIN",
            "GOOGLE_CHROME_BIN",
        )
    ]
    candidates = (
        *env_candidates,
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("msedge.exe"),
        shutil.which("msedge"),
        shutil.which("microsoft-edge"),
        shutil.which("microsoft-edge-stable"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    )
    seen = set()
    available = []
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        normalized = os.path.normcase(os.path.abspath(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        available.append(candidate)
    return available


def _form_print_styles():
    static_dir = Path(current_app.root_path) / "static" / "css"
    styles = []
    for filename in ("ethics-shell.css", "app.css"):
        css_path = static_dir / filename
        if css_path.exists():
            styles.append(css_path.read_text(encoding="utf-8"))
    styles.append(
        """
        @page { size: A4; margin: 10mm; }
        html, body { margin: 0; padding: 0; background: #ffffff; }
        body.mba-print-body { padding: 0; color: #111827; }
        .ethics-layout, .ethics-main, .mba-page-stack, .ethics-panel { margin: 0; padding: 0; }
        .ethics-panel { background: transparent; border: 0; box-shadow: none; }
        .mba-doc-page { max-width: none; }
        .mba-doc-paper { box-shadow: none; border-radius: 0; }
        .mba-doc-actions { display: none !important; }
        .primary-button, .secondary-button { display: none !important; }
        body.mba-print-body .mba-doc-page {
          width: 100%;
          max-width: none;
        }
        body.mba-print-body .mba-doc-paper {
          border: 0;
          padding: 0;
        }
        body.mba-print-body .supervisor-agreement-doc {
          box-sizing: border-box;
          width: 794px;
          max-width: 100%;
          margin: 0 auto;
          padding: 46px 54px;
        }
        body.mba-print-body .supervisor-agreement-doc .mba-print-value {
          min-height: 1.12em !important;
          padding: 0 2px !important;
          line-height: 1.05 !important;
          font-family: "Times New Roman", Times, serif !important;
        }
        body.mba-print-body .mba-doc-paper--jbs-declaration {
          max-width: 794px;
          margin: 0 auto;
          padding: 32px 48px 28px;
          overflow: visible;
        }
        body.mba-print-body .mba-doc-paper--affidavit {
          max-width: 794px;
          margin: 0 auto;
          padding: 34px 50px 30px;
          overflow: visible;
        }
        body.mba-print-body .mba-doc-paper--integrity {
          max-width: 794px;
          margin: 0 auto;
          padding: 28px 44px 30px;
          overflow: visible;
        }
        body.mba-print-body .jbs-template-table .mba-print-value,
        body.mba-print-body .integrity-doc-table .mba-print-value {
          min-height: 17px !important;
          padding: 0 2px !important;
          border: 0 !important;
          border-radius: 0 !important;
          line-height: inherit !important;
          font-family: inherit !important;
        }
        body.mba-print-body .integrity-doc-table .mba-print-value--textarea,
        body.mba-print-body .jbs-template-table .mba-print-value--textarea {
          min-height: 34px !important;
        }
        body.mba-print-body .mba-doc-table {
          width: 100%;
          table-layout: fixed;
        }
        body.mba-print-body .mba-doc-table th,
        body.mba-print-body .mba-doc-table td {
          overflow-wrap: anywhere;
          word-break: normal;
        }
        body.mba-print-body .mba-doc-paper--jbs5 {
          max-width: none;
          overflow: visible;
        }
        body.mba-print-body .mba-doc-paper--jbs10 {
          max-width: none;
          overflow: visible;
        }
        body.mba-print-body .mba-doc-paper--intent {
          max-width: 760px;
          margin: 0 auto;
          overflow: visible;
          font-size: 0.66rem;
          line-height: 1.05;
        }
        body.mba-print-body .mba-doc-paper--assessor {
          max-width: 780px;
          margin: 0 auto;
          padding: 0;
          overflow: visible;
          font-size: 0.76rem;
          line-height: 1.2;
        }
        body.mba-print-body .mba-doc-paper--summary {
          max-width: 690px;
          margin: 0 auto;
          padding: 18px 22px;
          overflow: visible;
          font-family: Arial, Helvetica, sans-serif;
          font-size: 8pt;
          line-height: 1.05;
          color: #000;
        }
        body.mba-print-body .mba-doc-paper--intent .mba-print-value {
          min-height: 14px !important;
          padding: 0 2px !important;
          line-height: 1.05 !important;
        }
        body.mba-print-body .mba-doc-paper--intent .mba-print-value--textarea {
          min-height: 44px !important;
          padding: 2px 4px !important;
          border-radius: 0 !important;
        }
        body.mba-print-body .mba-doc-paper--intent .mba-print-value--line-textarea {
          min-height: 32px !important;
          padding: 2px !important;
        }
        body.mba-print-body .jbs5-doc-table,
        body.mba-print-body .jbs5-doc-sdgs,
        body.mba-print-body .jbs10-doc-header,
        body.mba-print-body .jbs10-doc-table,
        body.mba-print-body .assessor-doc-table,
        body.mba-print-body .summary-doc-table,
        body.mba-print-body .mba-doc-header--intent,
        body.mba-print-body .mba-doc-paper--intent .mba-doc-muted-box,
        body.mba-print-body .intent-doc-table,
        body.mba-print-body .jbs-template-table,
        body.mba-print-body .integrity-doc-table {
          min-width: 0;
        }
        body.mba-print-body .summary-doc-marks-table {
          width: 72.3%;
          min-width: 520px;
          table-layout: fixed;
        }
        body.mba-print-body .summary-doc-marks-table th,
        body.mba-print-body .summary-doc-marks-table td {
          border: 0.5pt solid #000;
          white-space: nowrap;
          overflow-wrap: normal;
          word-break: normal;
        }
        body.mba-print-body .mba-doc-checkline,
        body.mba-print-body label {
          break-inside: avoid;
        }
        body.mba-print-body .mba-print-value {
          box-sizing: border-box;
          display: block;
          width: 100%;
          min-width: 0;
          min-height: 1.9em;
          padding: 4px 2px;
          border: 0;
          border-bottom: 1px solid #111827;
          color: #111827;
          background: transparent;
          line-height: 1.4;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
          word-break: normal;
        }
        body.mba-print-body .mba-print-value--textarea {
          min-height: 3.6em;
          padding: 8px 10px;
          border: 1px solid #cbd5e1;
          border-radius: 6px;
        }
        body.mba-print-body .mba-print-check {
          box-sizing: border-box;
          display: inline-flex;
          width: 14px;
          height: 14px;
          flex: 0 0 auto;
          align-items: center;
          justify-content: center;
          margin: 0 6px 0 0;
          border: 1px solid #111827;
          color: #111827;
          font-size: 10px;
          line-height: 1;
          vertical-align: -2px;
        }
        body.mba-print-body .mba-print-check--checkbox {
          border-radius: 2px;
        }
        body.mba-print-body .mba-print-check--radio {
          border-radius: 999px;
        }
        body.mba-print-body .jbs10-print-mark {
          margin: 0;
          border-radius: 0;
          font-weight: 800;
        }
        input, textarea, select { caret-color: transparent; }
        body.mba-print-body input,
        body.mba-print-body textarea,
        body.mba-print-body select { pointer-events: none; }
        body.mba-print-body input:disabled,
        body.mba-print-body textarea:disabled,
        body.mba-print-body select:disabled {
          color: inherit;
          opacity: 1;
          -webkit-text-fill-color: currentColor;
        }
        """
    )
    return "\n".join(styles)


def _extract_form_fragment(rendered_html):
    if _FORM_FRAGMENT_START in rendered_html and _FORM_FRAGMENT_END in rendered_html:
        return rendered_html.split(_FORM_FRAGMENT_START, 1)[1].split(_FORM_FRAGMENT_END, 1)[0].strip()
    match = re.search(r'(<div class="mba-page-stack mba-doc-page">.*?</div>\s*</section>\s*</div>)', rendered_html, re.DOTALL)
    return match.group(1).strip() if match else None


def _replace_form_logo(fragment, logo_mode="web"):
    if logo_mode == "web":
        return fragment

    export_logos = {
        "img/jbs5_logo.jpeg": "img/jbs5_logo.jpeg",
        "img/jbs10_logo.png": "img/jbs10_logo.png",
        "img/intent_to_submit_logo.jpeg": "img/intent_to_submit_logo.jpeg",
        "img/summary_assessment_logo.png": "img/summary_assessment_logo.png",
        "img/supervisor_agreement_logo.png": "img/supervisor_agreement_logo.png",
        "img/tii_ai_logo.png": "img/tii_ai_logo.png",
        "img/uj_logo.png": "img/uj_orange_square.png",
        "img/uj_orange_square.png": "img/uj_orange_square.png",
    }
    for source_filename, export_filename in export_logos.items():
        logo_url = url_for("static", filename=source_filename)
        logo_path = Path(current_app.root_path) / "static" / export_filename
        if not logo_path.exists():
            continue
        if logo_mode == "file":
            fragment = fragment.replace(logo_url, logo_path.resolve().as_uri())
        elif logo_mode == "inline":
            encoded = base64.b64encode(logo_path.read_bytes()).decode("ascii")
            data_uri = f"data:image/{logo_path.suffix.lstrip('.').lower() or 'png'};base64,{encoded}"
            fragment = fragment.replace(logo_url, data_uri)
    return fragment


def _html_attr_value(attrs, attr_name):
    match = re.search(
        rf'\b{re.escape(attr_name)}\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))',
        attrs or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


def _html_has_attr(attrs, attr_name):
    return bool(
        re.search(
            rf'\b{re.escape(attr_name)}(?:\s*=|\b)',
            attrs or "",
            flags=re.IGNORECASE,
        )
    )


_PRINT_VALUE_STYLE = (
    "box-sizing:border-box;display:block;width:100%;min-width:0;min-height:1.9em;"
    "padding:4px 2px;border:0;border-bottom:1px solid #111827;color:#111827;"
    "background:transparent;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;"
    "word-break:normal;font-family:Arial,Helvetica,sans-serif;"
)
_PRINT_TEXTAREA_STYLE = (
    "box-sizing:border-box;display:block;width:100%;min-width:0;min-height:3.6em;"
    "padding:8px 10px;border:1px solid #cbd5e1;border-radius:6px;color:#111827;"
    "background:transparent;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;"
    "word-break:normal;font-family:Arial,Helvetica,sans-serif;"
)
_PRINT_LINE_TEXTAREA_STYLE = (
    "box-sizing:border-box;display:block;width:100%;min-width:0;min-height:3.2em;"
    "padding:4px 2px;border:0;border-bottom:1px solid #111827;color:#111827;"
    "background:transparent;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;"
    "word-break:normal;font-family:Arial,Helvetica,sans-serif;"
)
_PRINT_CHECK_STYLE = (
    "display:inline-block;width:14px;min-width:14px;height:14px;margin:0 6px 0 0;"
    "color:#111827;font-size:12px;line-height:14px;text-align:center;vertical-align:-2px;"
    "font-family:'Arial Unicode MS','Segoe UI Symbol',Arial,sans-serif;"
)


def _print_value_html(value_html, modifier=""):
    value_html = value_html if str(value_html or "").strip() else "&nbsp;"
    class_name = "mba-print-value"
    style = _PRINT_VALUE_STYLE
    if modifier:
        class_name = f"{class_name} {class_name}--{modifier}"
        if modifier == "textarea":
            style = _PRINT_TEXTAREA_STYLE
        elif modifier == "line-textarea":
            style = _PRINT_LINE_TEXTAREA_STYLE
    return f'<div class="{class_name}" style="{style}">{value_html}</div>'


def _replace_print_form_controls(fragment):
    def replace_textarea(match):
        class_attr = _html_attr_value(match.group(1), "class")
        modifier = "line-textarea" if "mba-doc-textarea--line" in class_attr else "textarea"
        return _print_value_html(match.group(2), modifier)

    def replace_select(match):
        option_matches = list(
            re.finditer(
                r"<option\b([^>]*)>(.*?)</option>",
                match.group(2),
                flags=re.IGNORECASE | re.DOTALL,
            )
        )
        selected_option = next(
            (option for option in option_matches if _html_has_attr(option.group(1), "selected")),
            None,
        )
        if not selected_option and option_matches:
            selected_option = option_matches[0]
        selected_text = ""
        if selected_option:
            selected_text = re.sub(r"<[^>]+>", "", selected_option.group(2)).strip()
            if not _html_attr_value(selected_option.group(1), "value") and selected_text.lower().startswith("select"):
                selected_text = ""
        return _print_value_html(selected_text)

    def replace_input(match):
        attrs = match.group(1)
        input_type = (_html_attr_value(attrs, "type") or "text").lower()
        class_attr = _html_attr_value(attrs, "class")
        if input_type in {"hidden", "submit", "button", "reset", "file"}:
            return ""
        if input_type in {"checkbox", "radio"}:
            is_checked = _html_has_attr(attrs, "checked")
            if "jbs10-doc-mark" in class_attr or "assessor-doc-mark" in class_attr:
                mark = "X" if is_checked else "&nbsp;"
                class_name = "mba-print-check mba-print-check--checkbox jbs10-print-mark"
                if is_checked:
                    class_name = f"{class_name} is-checked"
                return f'<span class="{class_name}" style="{_PRINT_CHECK_STYLE}" aria-hidden="true">{mark}</span>'
            class_name = f"mba-print-check mba-print-check--{input_type}"
            if is_checked:
                class_name = f"{class_name} is-checked"
            if input_type == "checkbox":
                mark = "&#9745;" if is_checked else "&#9744;"
            else:
                mark = "&#9679;" if is_checked else "&#9711;"
            return f'<span class="{class_name}" style="{_PRINT_CHECK_STYLE}" aria-hidden="true">{mark}</span>'
        if "supervisor-agreement-line-fill" in class_attr:
            safe_class = xml_escape(class_attr, {'"': "&quot;"})
            value_html = xml_escape(_html_attr_value(attrs, "value")) or "&nbsp;"
            return f'<span class="{safe_class}">{value_html}</span>'
        if "jbs-doc-line-fill" in class_attr:
            safe_class = xml_escape(class_attr, {'"': "&quot;"})
            value_html = xml_escape(_html_attr_value(attrs, "value")) or "&nbsp;"
            return f'<span class="{safe_class}">{value_html}</span>'
        return _print_value_html(_html_attr_value(attrs, "value"))

    fragment = re.sub(
        r"<textarea\b([^>]*)>(.*?)</textarea>",
        replace_textarea,
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(
        r"<select\b([^>]*)>(.*?)</select>",
        replace_select,
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(r"<input\b([^>]*)>", replace_input, fragment, flags=re.IGNORECASE)
    return fragment


def _strip_print_web_controls(fragment):
    fragment = re.sub(
        r"<section\b(?=[^>]*\bclass\s*=\s*(?:\"[^\"]*\bmba-print-hide\b[^\"]*\"|'[^']*\bmba-print-hide\b[^']*'))[^>]*>.*?</section>",
        "",
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(
        r"<div\b(?=[^>]*\bclass\s*=\s*(?:\"[^\"]*\bmba-print-hide\b[^\"]*\"|'[^']*\bmba-print-hide\b[^']*'))[^>]*>.*?</div>",
        "",
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(
        r"<div\b(?=[^>]*\bclass\s*=\s*(?:\"[^\"]*\bmba-doc-actions\b[^\"]*\"|'[^']*\bmba-doc-actions\b[^']*'))[^>]*>.*?</div>",
        "",
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(r"<button\b[^>]*>.*?</button>", "", fragment, flags=re.IGNORECASE | re.DOTALL)
    fragment = re.sub(
        r"<a\b(?=[^>]*\bclass\s*=\s*(?:\"[^\"]*\b(?:primary-button|secondary-button)\b[^\"]*\"|'[^']*\b(?:primary-button|secondary-button)\b[^']*'))[^>]*>.*?</a>",
        "",
        fragment,
        flags=re.IGNORECASE | re.DOTALL,
    )
    fragment = re.sub(r"<form\b([^>]*)>", r"<div\1>", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"</form>", "</div>", fragment, flags=re.IGNORECASE)
    return fragment


def _assessor_profile_render_prefill(project, slot, payload):
    payload = dict(payload or {})
    student_profile = getattr(project.student, "student_profile", None) if project.student else None
    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None

    student_name = (
        f"{student_profile.name or ''} {student_profile.surname or ''}".strip()
        if student_profile
        else (project.student.email if project.student else "")
    )
    student_initials = ""
    if student_profile:
        for part in [student_profile.name, student_profile.surname]:
            for token in str(part or "").replace(".", " ").split():
                if token:
                    student_initials += token[0].upper()
    qualification = project.qualification or (student_profile.degree if student_profile else "") or "MBA"
    degree_registered = (
        "MBA Master of Business Administration"
        if str(qualification).strip().upper() == "MBA"
        else qualification
    )
    supervisor_name = ""
    if supervisor_profile:
        supervisor_name = " ".join(
            part for part in [supervisor_profile.title, supervisor_profile.name, supervisor_profile.surname] if part
        ).strip()
    elif supervisor:
        supervisor_name = supervisor.email or ""

    payload.setdefault("student_name", student_name)
    payload.setdefault(
        "student_initials_surname",
        " ".join(part for part in [student_initials, student_profile.surname if student_profile else ""] if part).strip(),
    )
    payload.setdefault("student_number", student_profile.student_number if student_profile else "")
    payload.setdefault("current_degree_registered", degree_registered)
    payload.setdefault("qualification_description", "Capstone Project")
    payload.setdefault("project_title", project.project_title)
    payload.setdefault("supervisor_name", supervisor_name)
    payload.setdefault("supervisor_department", supervisor_profile.department if supervisor_profile else "Johannesburg Business School")
    payload.setdefault("supervisor_phone", supervisor_profile.contact if supervisor_profile else "")
    payload.setdefault("supervisor_email", supervisor.email if supervisor else "")
    payload.setdefault("slot_label", slot.replace("_", " ").title())
    return payload


def _build_html_form_fragment(project, form_type, payload, logo_mode="web"):
    template_name = FORM_HTML_PRINT_TEMPLATES.get(form_type)
    extra_context = {}
    prefill = dict(payload or {})
    if not template_name and str(form_type or "").startswith("assessor_profile_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessor_profile")
        slot = form_type.replace("assessor_profile_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["yes_no_options"] = ["Yes", "No"]
        extra_context["existing_cv_doc"] = uploaded_doc_for(project, assessor_cv_doc_type(slot))
        prefill = _assessor_profile_render_prefill(project, slot, prefill)
    elif not template_name and str(form_type or "").startswith("assessor_temp_appointment_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessor_temp_appointment")
        slot = form_type.replace("assessor_temp_appointment_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["reason_options"] = [
            "Services will not exceed 3 months",
            "Specific project for limited time and clear deliverable",
            "Temporary increase in volume of work, less than 12 months",
            "Seasonal increase in volume of work, less than 12 months",
            "Position funded by external (non UJ) funds for limited time",
            "Other",
        ]
        extra_context["yes_no_options"] = ["Yes", "No"]
        extra_context["gender_options"] = ["Male", "Female", "Other", "Prefer not to say"]
        extra_context["marital_status_options"] = ["Single", "Married", "Divorced", "Widowed", "Other"]
        extra_context["account_type_options"] = ["Cheque", "Savings", "Current", "Transmission", "Other"]
        extra_context["account_ownership_options"] = ["Own", "Joint"]
        extra_context["race_options"] = ["African", "Coloured", "Indian", "White", "Chinese", "Other", "Prefer not to say"]
    elif not template_name and str(form_type or "").startswith("assessor_temp_claim_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessor_temp_claim")
        slot = form_type.replace("assessor_temp_claim_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["yes_no_options"] = ["Yes", "No"]
    elif not template_name and str(form_type or "").startswith("assessment_result_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessment_result")
        slot = form_type.replace("assessment_result_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["display_doc_variant"] = "assessment_result"
        extra_context["recommendation_options"] = [
            "Accept as the research stands",
            "Accept subject to minor revisions to the satisfaction of the Supervisor / Head of School",
            "Accept subject to major revisions to the satisfaction of the Supervisor / Head of School",
            "Major revisions and re-examination by the same assessor",
            "Outright rejection",
        ]
        extra_context["yes_no_options"] = ["Yes", "No"]
    elif not template_name and str(form_type or "").startswith("assessor_report_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessor_report")
        slot = form_type.replace("assessor_report_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["display_doc_variant"] = "assessor_report"
        extra_context["detailed_report_doc"] = uploaded_doc_for(project, assessor_detailed_report_doc_type(slot))
        extra_context["recommendation_options"] = [
            "Accept as the research stands",
            "Accept subject to minor revisions to the satisfaction of the Supervisor / Head of School",
            "Accept subject to major revisions to the satisfaction of the Supervisor / Head of School",
            "Major revisions and re-examination by the same assessor",
            "Outright rejection",
        ]
        extra_context["yes_no_options"] = ["Yes", "No"]
    elif not template_name and str(form_type or "").startswith("assessor_narrative_"):
        template_name = FORM_HTML_PRINT_TEMPLATES.get("assessor_narrative")
        slot = form_type.replace("assessor_narrative_", "", 1)
        extra_context["slot"] = slot
        extra_context["slot_label"] = slot.replace("_", " ").title()
        extra_context["display_doc_variant"] = "assessor_narrative"
        extra_context["recommendation_options"] = [
            "Accept as the research stands",
            "Accept subject to minor revisions to the satisfaction of the Supervisor / Head of School",
            "Accept subject to major revisions to the satisfaction of the Supervisor / Head of School",
            "Major revisions and re-examination by the same assessor",
            "Outright rejection",
        ]
        extra_context["yes_no_options"] = ["Yes", "No"]
    if not template_name:
        return None
    context = {
        "project": project,
        "prefill": prefill,
        "student_acceptance": form_type == "supervisor_agreement" and bool((payload or {}).get("_student_acceptance")),
    }
    context.update(extra_context)
    rendered_html = render_template(template_name, **context)
    fragment = _extract_form_fragment(rendered_html)
    if not fragment:
        return None
    fragment = _replace_form_logo(fragment, logo_mode=logo_mode)
    fragment = re.sub(r"<script\b[^>]*>.*?</script>", "", fragment, flags=re.DOTALL)
    if logo_mode != "web":
        fragment = _strip_print_web_controls(fragment)
        fragment = _replace_print_form_controls(fragment)
    return fragment


def supports_exact_form_render(form_type):
    form_type = str(form_type or "")
    return (
        form_type in FORM_HTML_PRINT_TEMPLATES
        or form_type.startswith("assessor_profile_")
        or form_type.startswith("assessor_temp_appointment_")
        or form_type.startswith("assessor_temp_claim_")
        or form_type.startswith("assessment_result_")
        or form_type.startswith("assessor_report_")
        or form_type.startswith("assessor_narrative_")
    )


def project_status_label(status):
    status = str(status or "")
    return PROJECT_STATUS_LABELS.get(status, status.replace("_", " ").title())


def public_project_status_label(status):
    status = str(status or "")
    return PUBLIC_PROJECT_STATUS_LABEL_OVERRIDES.get(status, project_status_label(status))


def public_project_status_badge_class(status):
    status = str(status or "")
    return PUBLIC_PROJECT_STATUS_BADGE_CLASSES.get(status, status)


def build_form_display_html(project, form_type, payload):
    fragment = _build_html_form_fragment(project, form_type, payload, logo_mode="inline")
    if not fragment:
        return None
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<style>{_form_print_styles()}</style></head>"
        f"<body class=\"mba-print-body\">{fragment}</body></html>"
    )


class _DocxHtmlNode:
    __slots__ = ("tag", "attrs", "children", "text")

    def __init__(self, tag=None, attrs=None, text=None):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.children = []
        self.text = text


class _DocxHtmlParser(HTMLParser):
    _SKIP_TAGS = {"style", "script", "noscript", "svg"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _DocxHtmlNode("root")
        self.stack = [self.root]
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = (tag or "").lower()
        if self.skip_depth:
            self.skip_depth += 1
            return
        if tag in self._SKIP_TAGS:
            self.skip_depth = 1
            return
        if tag == "br":
            self.stack[-1].children.append(_DocxHtmlNode("br"))
            return
        if tag in self._VOID_TAGS:
            return
        node = _DocxHtmlNode(tag, attrs)
        self.stack[-1].children.append(node)
        self.stack.append(node)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if self.skip_depth:
            self.skip_depth -= 1
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.skip_depth or not data:
            return
        self.stack[-1].children.append(_DocxHtmlNode(text=data))


_DOCX_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "dd",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "ul",
}


def _docx_clean_text(text):
    return re.sub(r"\s+", " ", str(text or "").replace("\xa0", " ")).strip()


def _docx_find_first(node, tag_name):
    if node.tag == tag_name:
        return node
    for child in node.children:
        found = _docx_find_first(child, tag_name)
        if found:
            return found
    return None


def _docx_inline_runs(node, *, bold=False, italic=False):
    if node.text is not None:
        text = _docx_clean_text(node.text)
        return [(text, bold, italic)] if text else []
    if node.tag == "br":
        return [("\n", bold, italic)]

    tag = node.tag or ""
    child_bold = bold or tag in {"b", "strong", "th"}
    child_italic = italic or tag in {"em", "i"}
    runs = []
    for child in node.children:
        runs.extend(_docx_inline_runs(child, bold=child_bold, italic=child_italic))
    return runs


def _docx_direct_inline_runs(node):
    runs = []
    for child in node.children:
        if child.text is not None or child.tag == "br" or child.tag not in _DOCX_BLOCK_TAGS:
            runs.extend(_docx_inline_runs(child))
    return runs


def _docx_run_xml(text, *, bold=False, italic=False, size=None):
    if text == "\n":
        return "<w:r><w:br/></w:r>"
    run_props = []
    if bold:
        run_props.append("<w:b/>")
    if italic:
        run_props.append("<w:i/>")
    if size:
        run_props.append(f'<w:sz w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(run_props)}</w:rPr>" if run_props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'


def _docx_paragraph_xml(runs, *, heading_level=None, prefix=None):
    clean_runs = [(text, bold, italic) for text, bold, italic in runs if text == "\n" or _docx_clean_text(text)]
    if prefix:
        clean_runs.insert(0, (prefix, False, False))
    if not any(text != "\n" and _docx_clean_text(text) for text, _bold, _italic in clean_runs):
        return ""

    paragraph_props = ""
    default_size = None
    force_bold = False
    if heading_level:
        size_by_level = {1: "32", 2: "28", 3: "24", 4: "22", 5: "20", 6: "20"}
        default_size = size_by_level.get(heading_level, "22")
        force_bold = True
        paragraph_props = '<w:pPr><w:spacing w:before="180" w:after="80"/></w:pPr>'

    runs_xml = "".join(
        _docx_run_xml(text, bold=bold or force_bold, italic=italic, size=default_size)
        for text, bold, italic in clean_runs
    )
    return f"<w:p>{paragraph_props}{runs_xml}</w:p>"


def _docx_cell_text_runs(cell):
    runs = _docx_inline_runs(cell)
    text = " ".join(text for text, _bold, _italic in runs if text != "\n")
    return [(text, cell.tag == "th", False)] if _docx_clean_text(text) else []


def _docx_table_xml(table):
    rows = [child for child in table.children if child.tag == "tr"]
    if not rows:
        rows = [
            row
            for section in table.children
            if section.tag in {"thead", "tbody", "tfoot"}
            for row in section.children
            if row.tag == "tr"
        ]
    row_xml = []
    max_cols = 1
    parsed_rows = []
    for row in rows:
        cells = [child for child in row.children if child.tag in {"td", "th"}]
        if not cells:
            continue
        max_cols = max(max_cols, len(cells))
        parsed_rows.append(cells)

    for cells in parsed_rows:
        cell_width = max(1200, int(9000 / max(max_cols, 1)))
        cells_xml = []
        for cell in cells:
            paragraph = _docx_paragraph_xml(_docx_cell_text_runs(cell)) or "<w:p/>"
            cells_xml.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{cell_width}" w:type="dxa"/></w:tcPr>{paragraph}</w:tc>'
            )
        row_xml.append(f"<w:tr>{''.join(cells_xml)}</w:tr>")

    if not row_xml:
        return ""
    borders = (
        '<w:tblBorders>'
        '<w:top w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '<w:left w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '<w:bottom w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '<w:right w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="D1D5DB"/>'
        '</w:tblBorders>'
    )
    tbl_pr = f'<w:tblPr><w:tblW w:w="0" w:type="auto"/>{borders}</w:tblPr>'
    return f"<w:tbl>{tbl_pr}{''.join(row_xml)}</w:tbl>"


def _docx_render_blocks(node):
    if node.text is not None or node.tag in {"head", "meta", "link", "title"}:
        return []
    if node.tag == "table":
        table_xml = _docx_table_xml(node)
        return [table_xml] if table_xml else []

    tag = node.tag or ""
    if tag in _DOCX_BLOCK_TAGS:
        blocks = []
        direct_runs = _docx_direct_inline_runs(node)
        if direct_runs:
            heading_level = int(tag[1]) if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else None
            prefix = "- " if tag == "li" else None
            paragraph = _docx_paragraph_xml(direct_runs, heading_level=heading_level, prefix=prefix)
            if paragraph:
                blocks.append(paragraph)
        for child in node.children:
            if child.tag in _DOCX_BLOCK_TAGS:
                blocks.extend(_docx_render_blocks(child))
        return blocks

    blocks = []
    for child in node.children:
        blocks.extend(_docx_render_blocks(child))
    return blocks


def _normalize_word_html_document(html, title=None):
    html = str(html or "")
    title_text = _docx_clean_text(title) or "Document"
    if not re.search(r"<html\b", html, flags=re.IGNORECASE):
        html = (
            "<!doctype html><html><head>"
            f"<title>{xml_escape(title_text)}</title></head><body>{html}</body></html>"
        )
    office_head = (
        "<meta charset=\"utf-8\">"
        "<!--[if gte mso 9]><xml>"
        "<w:WordDocument>"
        "<w:View>Print</w:View>"
        "<w:Zoom>100</w:Zoom>"
        "<w:DoNotOptimizeForBrowser/>"
        "</w:WordDocument>"
        "</xml><![endif]-->"
        "<style>"
        "@page WordSection1 { size: 595.3pt 841.9pt; margin: 28.35pt 28.35pt 28.35pt 28.35pt; }"
        "body { font-family: Arial, Helvetica, sans-serif; }"
        ".mba-doc-logo { width: 92px !important; max-width: 92px !important; height: auto !important; }"
        ".corrections-source-logo { width: 118px !important; max-width: 118px !important; height: auto !important; }"
        "</style>"
    )
    if re.search(r"<head\b", html, flags=re.IGNORECASE):
        injection = office_head
        if re.search(r"<meta[^>]+charset=", html, flags=re.IGNORECASE):
            injection = injection.replace("<meta charset=\"utf-8\">", "", 1)
        html = re.sub(
            r"(<head\b[^>]*>)",
            lambda match: f"{match.group(1)}{injection}",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        html = re.sub(
            r"(<html\b[^>]*>)",
            lambda match: f"{match.group(1)}<head>{office_head}<title>{xml_escape(title_text)}</title></head>",
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    return _apply_word_image_dimensions(html)


def _set_html_tag_attr(attrs, attr_name, value):
    replacement = f'{attr_name}="{value}"'
    if re.search(rf'\b{re.escape(attr_name)}\s*=', attrs or "", flags=re.IGNORECASE):
        return re.sub(
            rf'\b{re.escape(attr_name)}\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
            replacement,
            attrs,
            count=1,
            flags=re.IGNORECASE,
        )
    return f"{attrs.rstrip()} {replacement}"


def _remove_html_tag_attr(attrs, attr_name):
    return re.sub(
        rf'\s+\b{re.escape(attr_name)}\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+)',
        "",
        attrs or "",
        count=1,
        flags=re.IGNORECASE,
    )


def _append_html_style(attrs, style):
    existing = _html_attr_value(attrs, "style")
    separator = "" if not existing or existing.rstrip().endswith(";") else ";"
    merged = f"{existing}{separator}{style}" if existing else style
    return _set_html_tag_attr(attrs, "style", merged)


def _apply_word_image_dimensions(html):
    def replace_img(match):
        attrs = match.group(1)
        class_value = _html_attr_value(attrs, "class")
        class_names = set(re.split(r"\s+", class_value.strip()))
        width = None
        if "mba-doc-logo" in class_names:
            width = 92
        elif "corrections-source-logo" in class_names:
            width = 118
        if not width:
            return match.group(0)
        attrs = _set_html_tag_attr(attrs, "width", str(width))
        attrs = _remove_html_tag_attr(attrs, "height")
        attrs = _append_html_style(
            attrs,
            f"width:{width}px;max-width:{width}px;height:auto;mso-width-alt:{width * 15};",
        )
        return f"<img{attrs}>"

    return re.sub(r"<img\b([^>]*)>", replace_img, html, flags=re.IGNORECASE)


def _docx_package_relationships_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '</Relationships>'
    )


def _docx_core_properties_xml(title=None):
    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    core_title = _docx_clean_text(title) or "Document"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{xml_escape(core_title)}</dc:title>"
        "<dc:creator>MBA Ethics System</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _docx_app_properties_xml():
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        "<Application>MBA Ethics System</Application>"
        "</Properties>"
    )


def _mhtml_data_uri_extension(content_type):
    subtype = str(content_type or "").split("/", 1)[-1].lower()
    return {
        "jpeg": "jpg",
        "pjpeg": "jpg",
        "svg+xml": "svg",
    }.get(subtype, subtype.split("+", 1)[0] or "bin")


def _html_to_mhtml_bytes(html):
    html = str(html or "")
    boundary = f"----=_MBA_WORD_{uuid.uuid4().hex}"
    data_uri_parts = {}
    embedded_parts = []

    def replace_data_uri(match):
        data_uri = match.group(0)
        if data_uri in data_uri_parts:
            return data_uri_parts[data_uri]["location"]

        content_type = f"image/{match.group('subtype').lower()}"
        raw_base64 = re.sub(r"\s+", "", match.group("data"))
        try:
            image_bytes = base64.b64decode(raw_base64, validate=False)
        except Exception:
            return data_uri
        if not image_bytes:
            return data_uri

        extension = _mhtml_data_uri_extension(content_type)
        location = f"mba-word-image-{len(embedded_parts) + 1}.{extension}"
        part = {
            "location": location,
            "content_type": content_type,
            "bytes": image_bytes,
        }
        data_uri_parts[data_uri] = part
        embedded_parts.append(part)
        return location

    html = re.sub(
        r"data:image/(?P<subtype>[a-zA-Z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)",
        replace_data_uri,
        html,
    )

    quoted_html = quopri.encodestring(html.encode("utf-8"), quotetabs=True).decode("ascii")
    chunks = [
        "MIME-Version: 1.0",
        f'Content-Type: multipart/related; boundary="{boundary}"; type="text/html"',
        "",
        f"--{boundary}",
        'Content-Type: text/html; charset="utf-8"',
        "Content-Transfer-Encoding: quoted-printable",
        "Content-Location: file:///mba-form.html",
        "",
        quoted_html,
    ]
    for part in embedded_parts:
        encoded = base64.encodebytes(part["bytes"]).decode("ascii").replace("\n", "\r\n").rstrip()
        chunks.extend(
            [
                f"--{boundary}",
                f"Content-Type: {part['content_type']}",
                "Content-Transfer-Encoding: base64",
                f"Content-Location: {part['location']}",
                "",
                encoded,
            ]
        )
    chunks.append(f"--{boundary}--")
    chunks.append("")
    return "\r\n".join(chunks).encode("utf-8")


def _html_to_formatted_word_document_bytes(html, title=None):
    html = _normalize_word_html_document(html, title=title)
    mhtml_bytes = _html_to_mhtml_bytes(html)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<w:body>'
        '<w:altChunk r:id="rIdHtml"/>'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="567" w:right="567" '
        'w:bottom="567" w:left="567" w:header="360" w:footer="360" w:gutter="0"/></w:sectPr>'
        '</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="mht" ContentType="message/rfc822"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rIdHtml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk" Target="afchunk.mht"/>'
        '</Relationships>'
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", _docx_package_relationships_xml())
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/_rels/document.xml.rels", document_rels)
        docx.writestr("word/afchunk.mht", mhtml_bytes)
        docx.writestr("docProps/core.xml", _docx_core_properties_xml(title))
        docx.writestr("docProps/app.xml", _docx_app_properties_xml())
    return buffer.getvalue()


def _html_to_basic_word_document_bytes(html, title=None):
    parser = _DocxHtmlParser()
    parser.feed(str(html or ""))
    body = _docx_find_first(parser.root, "body") or parser.root
    blocks = _docx_render_blocks(body)
    if not blocks:
        plain_text = _docx_clean_text(re.sub(r"<[^>]+>", " ", str(html or "")))
        blocks = [_docx_paragraph_xml([(plain_text or "Document", False, False)])]

    core_title = _docx_clean_text(title) or "Document"
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{''.join(blocks)}"
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1134" w:right="1134" '
        'w:bottom="1134" w:left="1134" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>'
        "</w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '</Types>'
    )
    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types)
        docx.writestr("_rels/.rels", _docx_package_relationships_xml())
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/_rels/document.xml.rels", document_rels)
        docx.writestr("docProps/core.xml", _docx_core_properties_xml(title))
        docx.writestr("docProps/app.xml", _docx_app_properties_xml())
    return buffer.getvalue()


def html_to_word_document_bytes(html, title=None):
    try:
        return _html_to_formatted_word_document_bytes(html, title=title)
    except Exception:
        current_app.logger.exception("Formatted HTML DOCX generation failed; using basic DOCX fallback")
        return _html_to_basic_word_document_bytes(html, title=title)


def _jbs5_word_template_path():
    return Path(current_app.root_path) / JBS5_WORD_TEMPLATE


def _supervisor_agreement_word_template_path():
    return Path(current_app.root_path) / SUPERVISOR_AGREEMENT_WORD_TEMPLATE


def _jbs10_word_template_path():
    return Path(current_app.root_path) / JBS10_WORD_TEMPLATE


def _intent_to_submit_word_template_path():
    return Path(current_app.root_path) / INTENT_TO_SUBMIT_WORD_TEMPLATE


def _corrections_response_word_template_path():
    return Path(current_app.root_path) / CORRECTIONS_RESPONSE_WORD_TEMPLATE


def _jbs1_word_template_path():
    return Path(current_app.root_path) / JBS1_WORD_TEMPLATE


def _affidavit_word_template_path():
    return Path(current_app.root_path) / AFFIDAVIT_WORD_TEMPLATE


def _capstone_evaluation_word_template_path():
    return Path(current_app.root_path) / CAPSTONE_EVALUATION_WORD_TEMPLATE


def _capstone_assessor_report_form_1_word_template_path():
    return Path(current_app.root_path) / CAPSTONE_ASSESSOR_REPORT_FORM_1_WORD_TEMPLATE


def _summary_assessment_report_word_template_path():
    return Path(current_app.root_path) / SUMMARY_ASSESSMENT_REPORT_WORD_TEMPLATE


def _tii_ai_word_template_path():
    return Path(current_app.root_path) / TII_AI_WORD_TEMPLATE


def _assessor_temp_appointment_word_template_path():
    return Path(current_app.root_path) / ASSESSOR_TEMP_APPOINTMENT_WORD_TEMPLATE


def _assessor_temp_claim_word_template_path():
    return Path(current_app.root_path) / ASSESSOR_TEMP_CLAIM_WORD_TEMPLATE


def _external_examiner_nomination_word_template_path():
    return Path(current_app.root_path) / EXTERNAL_EXAMINER_NOMINATION_WORD_TEMPLATE


def _native_word_template_path_for_form(form_type):
    form_type = str(form_type or "")
    exact_templates = {
        "jbs5": _jbs5_word_template_path,
        "supervisor_agreement": _supervisor_agreement_word_template_path,
        "jbs10": _jbs10_word_template_path,
        "intent_to_submit": _intent_to_submit_word_template_path,
        "corrections_response": _corrections_response_word_template_path,
        "jbs1_declaration": _jbs1_word_template_path,
        "affidavit": _affidavit_word_template_path,
        "plagiarism_declaration": _tii_ai_word_template_path,
        "ai_declaration_form": _tii_ai_word_template_path,
        "external_examiner_nomination": _external_examiner_nomination_word_template_path,
        "additional_external_examiner_nomination": _external_examiner_nomination_word_template_path,
        "assessment_summary": _summary_assessment_report_word_template_path,
    }
    template_path_factory = exact_templates.get(form_type)
    if template_path_factory:
        return template_path_factory()
    if form_type.startswith("assessment_result_"):
        return _capstone_evaluation_word_template_path()
    if form_type.startswith("assessor_report_"):
        return _capstone_assessor_report_form_1_word_template_path()
    if form_type.startswith("assessor_temp_appointment_"):
        return _assessor_temp_appointment_word_template_path()
    if form_type.startswith("assessor_temp_claim_"):
        return _assessor_temp_claim_word_template_path()
    return None


def _docx_tag(name):
    return f"{{{_DOCX_W_NS}}}{name}"


def _docx_xml_space_attr():
    return "{http://www.w3.org/XML/1998/namespace}space"


def _docx_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _docx_first_value(payload, *keys, default=""):
    for key in keys:
        value = (payload or {}).get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _jbs5_payload(project, payload):
    payload = dict(payload or {})
    student = getattr(project, "student", None)
    student_profile = getattr(student, "student_profile", None) if student else None
    if student_profile:
        payload.setdefault("surname", getattr(student_profile, "surname", "") or "")
        payload.setdefault("student_title", getattr(student_profile, "title", "") or "")
        payload.setdefault("student_number", getattr(student_profile, "student_number", "") or "")
        payload.setdefault("qualification", getattr(student_profile, "degree", "") or "")
        initials = "".join(
            part[0].upper()
            for part in str(getattr(student_profile, "name", "") or "").replace(".", " ").split()
            if part
        )
        if initials:
            payload.setdefault("student_initials", initials)
    payload.setdefault("qualification", getattr(project, "qualification", "") or "MBA")
    payload.setdefault("research_title", getattr(project, "project_title", "") or "")
    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    if supervisor_profile:
        supervisor_name = " ".join(
            part
            for part in [
                getattr(supervisor_profile, "title", None),
                getattr(supervisor_profile, "name", None),
                getattr(supervisor_profile, "surname", None),
            ]
            if part
        ).strip()
        payload.setdefault("proposed_supervisor", supervisor_name)
    return payload


def _docx_format_date(value, *, month_year=False):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    if month_year:
        return parsed.strftime("%b %Y")
    return parsed.strftime("%d %b %Y")


def _docx_format_date_numeric(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return value
    return parsed.strftime("%d-%m-%Y")


def _docx_day_month_year_parts(payload, date_field, prefix):
    day = _docx_first_value(payload, f"{prefix}_day")
    month = _docx_first_value(payload, f"{prefix}_month")
    year = _docx_first_value(payload, f"{prefix}_year")
    if day or month or year:
        return day, month, year

    value = _docx_first_value(payload, date_field)
    try:
        parsed = datetime.strptime(str(value or "")[:10], "%Y-%m-%d")
    except ValueError:
        return "", "", ""
    return parsed.strftime("%d"), parsed.strftime("%B"), parsed.strftime("%y")


def _docx_cell(root, table_index, row_index, cell_index):
    tables = root.findall(".//w:tbl", _DOCX_NS)
    if table_index >= len(tables):
        return None
    rows = tables[table_index].findall("w:tr", _DOCX_NS)
    if row_index >= len(rows):
        return None
    cells = rows[row_index].findall("w:tc", _DOCX_NS)
    if cell_index >= len(cells):
        return None
    return cells[cell_index]


def _docx_text_run(text, *, size="16", bold=False):
    run = ET.Element(_docx_tag("r"))
    run_props = ET.SubElement(run, _docx_tag("rPr"))
    if bold:
        ET.SubElement(run_props, _docx_tag("b"))
    size_node = ET.SubElement(run_props, _docx_tag("sz"))
    size_node.set(_docx_tag("val"), str(size))
    size_cs_node = ET.SubElement(run_props, _docx_tag("szCs"))
    size_cs_node.set(_docx_tag("val"), str(size))
    lang_node = ET.SubElement(run_props, _docx_tag("lang"))
    lang_node.set(_docx_tag("val"), "en-GB")
    text_node = ET.SubElement(run, _docx_tag("t"))
    if str(text).strip() != str(text):
        text_node.set(_docx_xml_space_attr(), "preserve")
    text_node.text = str(text)
    return run


def _docx_set_paragraph_text(paragraph, value, *, size="16", bold=False):
    if paragraph is None:
        return
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if index:
            break_run = ET.SubElement(paragraph, _docx_tag("r"))
            ET.SubElement(break_run, _docx_tag("br"))
        paragraph.append(_docx_text_run(line, size=size, bold=bold))


def _docx_run_like_existing(paragraph, text=None, *, tab=False, source_props=None):
    run = ET.Element(_docx_tag("r"))
    if source_props is None:
        source_run = paragraph.find("w:r", _DOCX_NS) if paragraph is not None else None
        source_props = source_run.find("w:rPr", _DOCX_NS) if source_run is not None else None
    if source_props is not None:
        run.append(deepcopy(source_props))
    if tab:
        ET.SubElement(run, _docx_tag("tab"))
    else:
        text_node = ET.SubElement(run, _docx_tag("t"))
        if str(text or "").strip() != str(text or ""):
            text_node.set(_docx_xml_space_attr(), "preserve")
        text_node.text = str(text or "")
    return run


def _docx_set_paragraph_text_preserving_style(paragraph, value):
    if paragraph is None:
        return
    source_run = paragraph.find("w:r", _DOCX_NS)
    source_props = deepcopy(source_run.find("w:rPr", _DOCX_NS)) if source_run is not None and source_run.find("w:rPr", _DOCX_NS) is not None else None
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line_index, line in enumerate(lines):
        if line_index:
            break_run = ET.SubElement(paragraph, _docx_tag("r"))
            ET.SubElement(break_run, _docx_tag("br"))
        tab_parts = line.split("\t")
        for part_index, part in enumerate(tab_parts):
            if part_index:
                paragraph.append(_docx_run_like_existing(paragraph, tab=True, source_props=source_props))
            if part:
                paragraph.append(_docx_run_like_existing(paragraph, part, source_props=source_props))
        if not tab_parts or line == "":
            paragraph.append(_docx_run_like_existing(paragraph, "", source_props=source_props))


def _docx_paragraph_text(paragraph):
    parts = []
    for node in paragraph.iter():
        if node.tag == _docx_tag("t"):
            parts.append(node.text or "")
        elif node.tag == _docx_tag("tab"):
            parts.append("\t")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _docx_set_indexed_paragraph_text(root, paragraph_index, value, *, size="16", bold=False):
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if 0 <= paragraph_index < len(paragraphs):
        _docx_set_paragraph_text(paragraphs[paragraph_index], value, size=size, bold=bold)


def _docx_set_indexed_paragraph_text_preserving_style(root, paragraph_index, value):
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if 0 <= paragraph_index < len(paragraphs):
        _docx_set_paragraph_text_preserving_style(paragraphs[paragraph_index], value)


def _docx_run_from_props(text, source_props=None, *, underline=False):
    run = ET.Element(_docx_tag("r"))
    run_props = deepcopy(source_props) if source_props is not None else None
    if underline:
        if run_props is None:
            run_props = ET.Element(_docx_tag("rPr"))
        for existing_underline in list(run_props.findall("w:u", _DOCX_NS)):
            run_props.remove(existing_underline)
        underline_node = ET.SubElement(run_props, _docx_tag("u"))
        underline_node.set(_docx_tag("val"), "single")
    if run_props is not None:
        run.append(run_props)
    text_node = ET.SubElement(run, _docx_tag("t"))
    text = str(text or "")
    if text.strip() != text or "\u00a0" in text:
        text_node.set(_docx_xml_space_attr(), "preserve")
    text_node.text = text
    return run


def _docx_set_paragraph_parts_preserving_style(paragraph, parts):
    if paragraph is None:
        return
    source_run = paragraph.find("w:r", _DOCX_NS)
    source_props = (
        deepcopy(source_run.find("w:rPr", _DOCX_NS))
        if source_run is not None and source_run.find("w:rPr", _DOCX_NS) is not None
        else None
    )
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    for text, underline in parts:
        if text is None:
            continue
        paragraph.append(_docx_run_from_props(text, source_props, underline=underline))


def _docx_run_props_matching(paragraph, *, bold=None):
    if paragraph is None:
        return None
    fallback = None
    for run in paragraph.findall("w:r", _DOCX_NS):
        run_props = run.find("w:rPr", _DOCX_NS)
        if run_props is not None and fallback is None:
            fallback = run_props
        if bold is None:
            continue
        has_bold = bool(run_props is not None and run_props.find("w:b", _DOCX_NS) is not None)
        if has_bold == bold:
            return deepcopy(run_props) if run_props is not None else None
    return deepcopy(fallback) if fallback is not None else None


def _docx_single_line(value):
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _docx_set_paragraph_layout_runs(paragraph, parts):
    if paragraph is None:
        return
    normal_props = _docx_run_props_matching(paragraph, bold=False)
    bold_props = _docx_run_props_matching(paragraph, bold=True) or normal_props
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    for kind, text, weight in parts:
        source_props = bold_props if weight == "bold" else normal_props
        if kind == "tab":
            paragraph.append(_docx_run_like_existing(paragraph, tab=True, source_props=source_props))
        elif kind == "text":
            paragraph.append(_docx_run_like_existing(paragraph, text, source_props=source_props))


def _docx_set_indexed_paragraph_layout_runs(root, paragraph_index, parts):
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if 0 <= paragraph_index < len(paragraphs):
        _docx_set_paragraph_layout_runs(paragraphs[paragraph_index], parts)


def _docx_paragraph_runs_at(root, paragraph_index):
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if not (0 <= paragraph_index < len(paragraphs)):
        return None, []
    paragraph = paragraphs[paragraph_index]
    return paragraph, paragraph.findall("w:r", _DOCX_NS)


def _docx_set_run_parts(run, parts):
    if run is None:
        return
    for child in list(run):
        if child.tag != _docx_tag("rPr"):
            run.remove(child)
    for kind, value in parts:
        if kind == "tab":
            ET.SubElement(run, _docx_tag("tab"))
        elif kind == "text":
            text_node = ET.SubElement(run, _docx_tag("t"))
            value = str(value or "")
            if value.strip() != value:
                text_node.set(_docx_xml_space_attr(), "preserve")
            text_node.text = value


def _docx_set_indexed_run_parts(root, paragraph_index, run_index, parts):
    _, runs = _docx_paragraph_runs_at(root, paragraph_index)
    if 0 <= run_index < len(runs):
        _docx_set_run_parts(runs[run_index], parts)


def _docx_append_run_after(root, paragraph_index, run_index, parts):
    paragraph, runs = _docx_paragraph_runs_at(root, paragraph_index)
    if paragraph is None:
        return
    source_props = None
    insert_after = None
    if 0 <= run_index < len(runs):
        insert_after = runs[run_index]
        source_props = insert_after.find("w:rPr", _DOCX_NS)
    elif runs:
        insert_after = runs[-1]
        source_props = insert_after.find("w:rPr", _DOCX_NS)
    new_run = ET.Element(_docx_tag("r"))
    if source_props is not None:
        new_run.append(deepcopy(source_props))
    _docx_set_run_parts(new_run, parts)
    children = list(paragraph)
    try:
        insert_position = children.index(insert_after) + 1 if insert_after is not None else len(children)
    except ValueError:
        insert_position = len(children)
    paragraph.insert(insert_position, new_run)


def _docx_set_indexed_paragraph_parts_preserving_style(root, paragraph_index, parts):
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if 0 <= paragraph_index < len(paragraphs):
        _docx_set_paragraph_parts_preserving_style(paragraphs[paragraph_index], parts)


def _docx_set_cell_element_text(cell, value, *, size="16", bold=False):
    if cell is None:
        return
    paragraphs = cell.findall("w:p", _DOCX_NS)
    if paragraphs:
        paragraph = paragraphs[0]
    else:
        paragraph = ET.SubElement(cell, _docx_tag("p"))
    _docx_set_paragraph_text(paragraph, value, size=size, bold=bold)


def _docx_set_cell_text(root, table_index, row_index, cell_index, value, *, size="16", bold=False):
    value = str(value or "").strip()
    if not value:
        return
    cell = _docx_cell(root, table_index, row_index, cell_index)
    _docx_set_cell_element_text(cell, value, size=size, bold=bold)


def _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value):
    value = str(value or "").strip()
    if not value:
        return
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    paragraphs = cell.findall("w:p", _DOCX_NS)
    paragraph = paragraphs[0] if paragraphs else ET.SubElement(cell, _docx_tag("p"))
    _docx_set_paragraph_text_preserving_style(paragraph, value)
    for extra_paragraph in paragraphs[1:]:
        cell.remove(extra_paragraph)


def _docx_set_cell_text_preserving_style_allow_blank(root, table_index, row_index, cell_index, value):
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    paragraphs = cell.findall("w:p", _DOCX_NS)
    paragraph = paragraphs[0] if paragraphs else ET.SubElement(cell, _docx_tag("p"))
    _docx_set_paragraph_text_preserving_style(paragraph, str(value or "").strip())
    for extra_paragraph in paragraphs[1:]:
        cell.remove(extra_paragraph)


def _docx_set_mark_cell(root, table_index, row_index, cell_index, checked):
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    paragraphs = cell.findall("w:p", _DOCX_NS)
    paragraph = paragraphs[0] if paragraphs else ET.SubElement(cell, _docx_tag("p"))
    source_props = None
    for source_paragraph in paragraphs:
        source_run = source_paragraph.find("w:r", _DOCX_NS)
        run_props = source_run.find("w:rPr", _DOCX_NS) if source_run is not None else None
        if run_props is not None:
            source_props = deepcopy(run_props)
            break
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    for extra_paragraph in paragraphs[1:]:
        cell.remove(extra_paragraph)
    paragraph.append(_docx_run_like_existing(paragraph, "X" if checked else "", source_props=source_props))


def _docx_normalized(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _docx_is_yes(value):
    return _docx_normalized(value) in {"yes", "y", "true", "1", "checked", "x"}


def _docx_yes_no_marks(root, table_index, row_index, yes_cell_index, no_cell_index, value):
    is_yes = _docx_is_yes(value)
    _docx_set_mark_cell(root, table_index, row_index, yes_cell_index, is_yes)
    _docx_set_mark_cell(root, table_index, row_index, no_cell_index, not is_yes)


def _docx_mark_matching_option(root, table_index, options, selected_value):
    selected = _docx_normalized(selected_value)
    for option_value, row_index, cell_index in options:
        _docx_set_mark_cell(root, table_index, row_index, cell_index, _docx_normalized(option_value) == selected)


def _docx_fill_character_cells(root, table_index, row_index, start_cell_index, end_cell_index, value):
    text = re.sub(r"\s+", "", str(value or ""))
    for offset, cell_index in enumerate(range(start_cell_index, end_cell_index + 1)):
        _docx_set_cell_text_preserving_style_allow_blank(
            root,
            table_index,
            row_index,
            cell_index,
            text[offset] if offset < len(text) else "",
        )


def _docx_cost_centre_parts(value):
    parts = re.split(r"[\s.:-]+", str(value or "").strip())
    parts = [part for part in parts if part]
    defaults = ["05", "05", "046904", "20", "31330"]
    return (parts + defaults[len(parts):])[:5]


def _docx_underlined_field_text(value, width):
    value = str(value or "").strip()
    return value + ("\u00a0" * max(2, width - len(value)))


def _docx_append_cell_text(root, table_index, row_index, cell_index, value, *, size="18", bold=False, center=False):
    value = str(value or "").strip()
    if not value:
        return
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    paragraph = ET.SubElement(cell, _docx_tag("p"))
    paragraph_props = ET.SubElement(paragraph, _docx_tag("pPr"))
    spacing = ET.SubElement(paragraph_props, _docx_tag("spacing"))
    spacing.set(_docx_tag("before"), "0")
    spacing.set(_docx_tag("after"), "0")
    if center:
        justification = ET.SubElement(paragraph_props, _docx_tag("jc"))
        justification.set(_docx_tag("val"), "center")
    for line_index, line in enumerate(value.replace("\r\n", "\n").replace("\r", "\n").split("\n")):
        if line_index:
            break_run = ET.SubElement(paragraph, _docx_tag("r"))
            ET.SubElement(break_run, _docx_tag("br"))
        paragraph.append(_docx_text_run(line, size=size, bold=bold))


def _docx_replace_cell_text(root, table_index, row_index, cell_index, value, *, size="18", bold=False, center=False):
    value = str(value or "").strip()
    if not value:
        return
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    for child in list(cell):
        if child.tag != _docx_tag("tcPr"):
            cell.remove(child)
    paragraph = ET.SubElement(cell, _docx_tag("p"))
    paragraph_props = ET.SubElement(paragraph, _docx_tag("pPr"))
    spacing = ET.SubElement(paragraph_props, _docx_tag("spacing"))
    spacing.set(_docx_tag("before"), "0")
    spacing.set(_docx_tag("after"), "0")
    if center:
        justification = ET.SubElement(paragraph_props, _docx_tag("jc"))
        justification.set(_docx_tag("val"), "center")
    _docx_set_paragraph_text(paragraph, value, size=size, bold=bold)


def _docx_set_checkbox(root, field_name, checked):
    for ff_data in root.findall(".//w:ffData", _DOCX_NS):
        name = ff_data.find("w:name", _DOCX_NS)
        if name is None or name.get(_docx_tag("val")) != field_name:
            continue
        checkbox = ff_data.find("w:checkBox", _DOCX_NS)
        if checkbox is None:
            continue
        value = "1" if checked else "0"
        for node_name in ("default", "checked"):
            node = checkbox.find(f"w:{node_name}", _DOCX_NS)
            if node is None:
                node = ET.SubElement(checkbox, _docx_tag(node_name))
            node.set(_docx_tag("val"), value)


def _docx_read_template(template_path):
    template_bytes = document_template_bytes(template_path)
    if not template_bytes:
        raise FileNotFoundError(f"Word template is not available: {template_path}")
    with zipfile.ZipFile(BytesIO(template_bytes), "r") as template:
        entries = template.infolist()
        contents = {entry.filename: template.read(entry.filename) for entry in entries}
    root = ET.fromstring(contents["word/document.xml"])
    return entries, contents, root


def _docx_write_template(entries, contents, root):
    contents["word/document.xml"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as generated:
        written = set()
        for entry in entries:
            generated.writestr(entry, contents[entry.filename])
            written.add(entry.filename)
        for filename, data in contents.items():
            if filename not in written:
                generated.writestr(filename, data)
    return buffer.getvalue()


_DOCX_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCX_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_DOCX_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


def _docx_package_tag(namespace, name):
    return f"{{{namespace}}}{name}"


def _docx_signature_image_payload(payload, field):
    data_uri = str((payload or {}).get(f"{field}_image") or "").strip()
    if not data_uri:
        return None
    match = re.match(
        r"^data:image/(?P<subtype>png|jpeg|jpg);base64,(?P<data>[A-Za-z0-9+/=\s]+)$",
        data_uri,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        image_bytes = base64.b64decode(match.group("data"), validate=False)
    except Exception:
        return None
    extension = _signature_extension_from_bytes(image_bytes)
    if extension not in SIGNATURE_UPLOAD_EXTENSIONS:
        return None
    mime_type = _signature_mime_from_extension(extension)
    return image_bytes, extension, mime_type


def _docx_ensure_image_content_type(contents, extension, mime_type):
    content_types_path = "[Content_Types].xml"
    if content_types_path not in contents:
        return
    root = ET.fromstring(contents[content_types_path])
    for default in root.findall(_docx_package_tag(_DOCX_CT_NS, "Default")):
        if str(default.get("Extension") or "").lower() == extension:
            default.set("ContentType", mime_type)
            contents[content_types_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            return
    default = ET.SubElement(root, _docx_package_tag(_DOCX_CT_NS, "Default"))
    default.set("Extension", extension)
    default.set("ContentType", mime_type)
    contents[content_types_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _docx_next_relationship_id(rels_root):
    used_ids = {
        rel.get("Id")
        for rel in rels_root.findall(_docx_package_tag(_DOCX_REL_NS, "Relationship"))
        if rel.get("Id")
    }
    index = 1
    while f"rId{index}" in used_ids:
        index += 1
    return f"rId{index}"


def _docx_add_image_relationship(contents, image_bytes, extension, mime_type):
    rels_path = "word/_rels/document.xml.rels"
    if rels_path in contents:
        rels_root = ET.fromstring(contents[rels_path])
    else:
        rels_root = ET.Element(_docx_package_tag(_DOCX_REL_NS, "Relationships"))
    rel_id = _docx_next_relationship_id(rels_root)
    media_filename = f"word/media/mba_signature_{uuid.uuid4().hex[:12]}.{extension}"
    contents[media_filename] = image_bytes
    relationship = ET.SubElement(rels_root, _docx_package_tag(_DOCX_REL_NS, "Relationship"))
    relationship.set("Id", rel_id)
    relationship.set("Type", _DOCX_IMAGE_REL_TYPE)
    relationship.set("Target", f"media/{Path(media_filename).name}")
    contents[rels_path] = ET.tostring(rels_root, encoding="utf-8", xml_declaration=True)
    _docx_ensure_image_content_type(contents, extension, mime_type)
    return rel_id


def _docx_signature_image_run(contents, payload, field, *, width_emu=1371600, height_emu=365760):
    image_payload = _docx_signature_image_payload(payload, field)
    if not image_payload:
        return None
    image_bytes, extension, mime_type = image_payload
    rel_id = _docx_add_image_relationship(contents, image_bytes, extension, mime_type)
    doc_pr_id = int(uuid.uuid4().int % 1000000) + 1
    name = xml_escape(f"Signature {field}")
    return ET.fromstring(
        f"""
        <w:r xmlns:w="{_DOCX_NS['w']}" xmlns:r="{_DOCX_NS['r']}" xmlns:wp="{_DOCX_NS['wp']}" xmlns:a="{_DOCX_NS['a']}" xmlns:pic="{_DOCX_NS['pic']}">
          <w:drawing>
            <wp:inline distT="0" distB="0" distL="0" distR="0">
              <wp:extent cx="{width_emu}" cy="{height_emu}"/>
              <wp:effectExtent l="0" t="0" r="0" b="0"/>
              <wp:docPr id="{doc_pr_id}" name="{name}"/>
              <wp:cNvGraphicFramePr>
                <a:graphicFrameLocks noChangeAspect="1"/>
              </wp:cNvGraphicFramePr>
              <a:graphic>
                <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                  <pic:pic>
                    <pic:nvPicPr>
                      <pic:cNvPr id="0" name="{name}"/>
                      <pic:cNvPicPr/>
                    </pic:nvPicPr>
                    <pic:blipFill>
                      <a:blip r:embed="{rel_id}"/>
                      <a:stretch><a:fillRect/></a:stretch>
                    </pic:blipFill>
                    <pic:spPr>
                      <a:xfrm>
                        <a:off x="0" y="0"/>
                        <a:ext cx="{width_emu}" cy="{height_emu}"/>
                      </a:xfrm>
                      <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
                    </pic:spPr>
                  </pic:pic>
                </a:graphicData>
              </a:graphic>
            </wp:inline>
          </w:drawing>
        </w:r>
        """
    )


def _docx_set_cell_signature_image(root, contents, table_index, row_index, cell_index, payload, field, **image_kwargs):
    run = _docx_signature_image_run(contents, payload, field, **image_kwargs)
    if run is None:
        return False
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return False
    paragraphs = cell.findall("w:p", _DOCX_NS)
    paragraph = paragraphs[0] if paragraphs else ET.SubElement(cell, _docx_tag("p"))
    for child in list(paragraph):
        if child.tag != _docx_tag("pPr"):
            paragraph.remove(child)
    paragraph.append(run)
    for extra_paragraph in paragraphs[1:]:
        cell.remove(extra_paragraph)
    return True


def _docx_append_paragraph_signature_image(root, contents, paragraph_index, payload, field, **image_kwargs):
    run = _docx_signature_image_run(contents, payload, field, **image_kwargs)
    if run is None:
        return False
    paragraphs = root.findall(".//w:p", _DOCX_NS)
    if not (0 <= paragraph_index < len(paragraphs)):
        return False
    paragraph = paragraphs[paragraph_index]
    paragraph.append(_docx_run_like_existing(paragraph, tab=True))
    paragraph.append(run)
    return True


def _jbs5_study_type_checks(payload):
    study_type = str(payload.get("study_type") or "Capstone Project").strip().lower()
    return {
        "Check4": study_type in {"capstone project", "capstone consultancy project", "capstone"},
        "Check5": study_type in {"research essay", "research article"},
        "Check6": study_type == "minor dissertation",
        "Check7": study_type == "dissertation",
        "Check8": study_type == "thesis",
    }


def _jbs10_study_type_checks(payload):
    study_type = str(payload.get("study_type") or "Capstone Project").strip().lower()
    normalized_study_type = re.sub(r"[^a-z0-9]+", " ", study_type).strip()
    return {
        "capstone": study_type in {"capstone project", "capstone consultancy project", "capstone"},
        "limited_scope": "limited scope" in study_type,
        "minor": study_type == "minor dissertation",
        "dissertation": study_type == "dissertation",
        "thesis_monograph": normalized_study_type in {"thesis", "thesis monograph", "monograph"},
        "thesis_article": normalized_study_type in {"thesis by article", "by article"},
    }


def _generate_jbs5_template_word_bytes(project, payload):
    template_path = _jbs5_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    register_title = _docx_truthy(payload.get("register_title_supervisors")) or not (
        _docx_truthy(payload.get("amend_title")) or _docx_truthy(payload.get("amend_supervisors"))
    )
    amend_title = _docx_truthy(payload.get("amend_title"))
    amend_supervisors = _docx_truthy(payload.get("amend_supervisors"))

    entries, contents, root = _docx_read_template(template_path)
    checkbox_values = {
        "Check1": register_title,
        "Check2": amend_title,
        "Check3": amend_supervisors,
        "Check10": register_title,
        "Check11": amend_title,
        **_jbs5_study_type_checks(payload),
    }
    for field_name, checked in checkbox_values.items():
        _docx_set_checkbox(root, field_name, checked)

    field_map = [
        (1, 1, 1, _docx_first_value(payload, "surname")),
        (1, 1, 3, _docx_first_value(payload, "student_title")),
        (1, 2, 1, _docx_first_value(payload, "student_initials")),
        (1, 2, 3, _docx_format_date(_docx_first_value(payload, "date_of_first_registration"), month_year=True)),
        (1, 3, 1, _docx_first_value(payload, "student_number")),
        (1, 3, 3, _docx_first_value(payload, "qualification", default="MBA")),
        (1, 4, 1, _docx_first_value(payload, "discipline")),
        (1, 6, 1, _docx_first_value(payload, "sdg_focus")),
        (2, 1, 1, _docx_first_value(payload, "research_title")),
        (2, 2, 1, _docx_first_value(payload, "proposed_supervisor", "supervisor_name")),
        (2, 3, 1, _docx_first_value(payload, "proposed_co_supervisors")),
        (3, 1, 1, _docx_first_value(payload, "previous_title")),
        (3, 2, 1, _docx_first_value(payload, "amended_title")),
        (4, 1, 1, _docx_first_value(payload, "previous_supervisor")),
        (4, 2, 1, _docx_first_value(payload, "previous_co_supervisors")),
        (4, 3, 1, _docx_first_value(payload, "amended_supervisor")),
        (4, 4, 1, _docx_first_value(payload, "amended_co_supervisors")),
        (5, 1, 1, _docx_first_value(payload, "discipline_specific", default="YES")),
        (5, 2, 1, _docx_first_value(payload, "has_secondary_focus", default="No").upper()),
        (5, 3, 1, _docx_first_value(payload, "secondary_focus")),
        (6, 0, 1, _docx_first_value(payload, "supervisor_signature", "proposed_supervisor", "supervisor_name")),
        (6, 0, 3, _docx_format_date(_docx_first_value(payload, "supervisor_signature_date"))),
        (6, 2, 1, _docx_first_value(payload, "head_of_department_signature")),
        (6, 2, 3, _docx_format_date(_docx_first_value(payload, "head_of_department_signature_date"))),
        (6, 4, 1, _docx_first_value(payload, "jbs_hdc_signature")),
        (6, 4, 3, _docx_format_date(_docx_first_value(payload, "jbs_hdc_signature_date"))),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text(root, table_index, row_index, cell_index, value)

    _docx_set_cell_signature_image(root, contents, 6, 0, 1, payload, "supervisor_signature")
    _docx_set_cell_signature_image(root, contents, 6, 2, 1, payload, "head_of_department_signature")
    _docx_set_cell_signature_image(root, contents, 6, 4, 1, payload, "jbs_hdc_signature")

    is_4ir = str(payload.get("is_4ir_research") or "No").strip().lower()
    _docx_set_cell_text(root, 1, 7, 3, "X" if is_4ir == "yes" else "")
    _docx_set_cell_text(root, 1, 7, 5, "X" if is_4ir != "yes" else "")

    return _docx_write_template(entries, contents, root)


def _docx_user_name(user):
    profile = getattr(user, "scholar_profile", None) if user else None
    if profile:
        return " ".join(
            part for part in [getattr(profile, "title", None), getattr(profile, "name", None), getattr(profile, "surname", None)] if part
        ).strip()
    if user:
        return " ".join(part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part).strip() or getattr(user, "email", "")
    return ""


def _docx_student_initials_surname(project, payload):
    value = _docx_first_value(payload, "student_initials_surname")
    if value:
        return value
    initials = _docx_first_value(payload, "student_initials")
    surname = _docx_first_value(payload, "surname")
    if initials or surname:
        return " ".join(part for part in [initials, surname] if part).strip()
    student = getattr(project, "student", None)
    profile = getattr(student, "student_profile", None) if student else None
    if profile:
        initials = "".join(
            part[0].upper()
            for part in str(getattr(profile, "name", "") or "").replace(".", " ").split()
            if part
        )
        return " ".join(part for part in [initials, getattr(profile, "surname", "")] if part).strip()
    return getattr(student, "email", "") if student else ""


def _docx_student_full_name(project, payload):
    value = _docx_first_value(payload, "full_name", "student_name", "signature_name")
    if value:
        return value
    title = _docx_first_value(payload, "student_title")
    initials = _docx_first_value(payload, "student_initials")
    surname = _docx_first_value(payload, "surname")
    if initials or surname:
        return " ".join(part for part in [title, initials, surname] if part).strip()
    student = getattr(project, "student", None)
    profile = getattr(student, "student_profile", None) if student else None
    if profile:
        return " ".join(
            part for part in [getattr(profile, "title", ""), getattr(profile, "name", ""), getattr(profile, "surname", "")] if part
        ).strip()
    return getattr(student, "email", "") if student else ""


def _docx_degree_registered(qualification):
    qualification = str(qualification or "").strip() or "MBA"
    if qualification.upper() == "MBA":
        return "MBA Master of Business Administration"
    return qualification


def _docx_yes_no(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    value = str(value or "").strip()
    if not value:
        return ""
    if value.lower() in {"1", "true", "yes", "on", "checked"}:
        return "Yes"
    if value.lower() in {"0", "false", "no", "off", "unchecked"}:
        return "No"
    return value


def _docx_supervisor_payload(project, payload):
    supervisor = getattr(project, "primary_supervisor", None)
    profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    name = _docx_first_value(payload, "supervisor_name", "proposed_supervisor") or _docx_user_name(supervisor)
    return {
        "name": name,
        "department": _docx_first_value(payload, "supervisor_department") or (getattr(profile, "department", "") if profile else "") or "Johannesburg Business School",
        "phone": _docx_first_value(payload, "supervisor_phone", "supervisor_contact") or (getattr(profile, "contact", "") if profile else ""),
        "email": _docx_first_value(payload, "supervisor_email") or (getattr(supervisor, "email", "") if supervisor else ""),
    }


def _docx_assessor_payload(project, payload, slot):
    user = getattr(project, slot, None)
    profile = getattr(user, "scholar_profile", None) if user else None
    prefix = f"{slot}_"
    contact = _docx_first_value(payload, f"{prefix}telephone", f"{prefix}cell", f"{prefix}contact") or (
        getattr(profile, "contact", "") if profile else ""
    )
    international = _docx_first_value(payload, f"{prefix}international_assessor")
    if not international and profile and getattr(profile, "international_assessor", None) is not None:
        international = getattr(profile, "international_assessor")
    return {
        "name": _docx_first_value(payload, f"{prefix}name") or _docx_user_name(user),
        "qualification": _docx_first_value(payload, f"{prefix}qualification", f"{prefix}highest_qualification")
        or (getattr(profile, "qualification", "") if profile else ""),
        "affiliation": _docx_first_value(payload, f"{prefix}affiliation")
        or (getattr(profile, "affiliation", "") if profile else "")
        or (getattr(profile, "department", "") if profile else ""),
        "address": _docx_first_value(payload, f"{prefix}address") or (getattr(profile, "address", "") if profile else ""),
        "telephone": contact,
        "cell": contact,
        "email": _docx_first_value(payload, f"{prefix}email") or (getattr(user, "email", "") if user else ""),
        "students_supervised": _docx_first_value(payload, f"{prefix}students_supervised_total")
        or (str(getattr(profile, "students_supervised_total", "")) if profile and getattr(profile, "students_supervised_total", None) is not None else ""),
        "current_affiliation": _docx_first_value(payload, f"{prefix}current_affiliation")
        or (getattr(profile, "affiliation", "") if profile else ""),
        "publications": _docx_first_value(payload, f"{prefix}publication_count")
        or (str(getattr(profile, "publication_count", "")) if profile and getattr(profile, "publication_count", None) is not None else ""),
        "international": _docx_yes_no(international),
    }


def _supervisor_agreement_payload(project, payload):
    payload = dict(payload or {})
    student = getattr(project, "student", None)
    student_profile = getattr(student, "student_profile", None) if student else None
    if student_profile:
        student_name = " ".join(
            part for part in [getattr(student_profile, "name", ""), getattr(student_profile, "surname", "")] if part
        ).strip()
        payload.setdefault("student_name", student_name)
        payload.setdefault("student_surname", getattr(student_profile, "surname", "") or "")
        payload.setdefault("student_number", getattr(student_profile, "student_number", "") or "")
        payload.setdefault("student_address", getattr(student_profile, "address", "") or "")
        payload.setdefault("degree", getattr(student_profile, "degree", "") or "")
    if not payload.get("student_name") and student:
        payload["student_name"] = getattr(student, "email", "") or ""
    payload.setdefault("degree", getattr(project, "qualification", "") or "MBA")
    payload.setdefault("research_title", getattr(project, "project_title", "") or "")

    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    if supervisor_profile:
        supervisor_name = " ".join(
            part
            for part in [
                getattr(supervisor_profile, "title", ""),
                getattr(supervisor_profile, "name", ""),
                getattr(supervisor_profile, "surname", ""),
            ]
            if part
        ).strip()
        payload.setdefault("supervisor_full_name", supervisor_name)
        payload.setdefault("supervisor_surname", getattr(supervisor_profile, "surname", "") or "")
        payload.setdefault("department", getattr(supervisor_profile, "department", "") or "")
        payload.setdefault("affiliation", getattr(supervisor_profile, "affiliation", "") or "")
        payload.setdefault("position", getattr(supervisor_profile, "position", "") or "")
    if not payload.get("supervisor_full_name") and supervisor:
        payload["supervisor_full_name"] = getattr(supervisor, "email", "") or ""

    def initials_from(value):
        return "".join(part[0].upper() for part in str(value or "").replace(".", " ").split() if part)

    payload.setdefault("student_initials", initials_from(payload.get("student_name")))
    payload.setdefault("supervisor_initials", initials_from(payload.get("supervisor_full_name")))
    payload.setdefault("co_supervisor_initials", initials_from(payload.get("co_supervisor_full_name")))
    payload.setdefault("student_signature_name", payload.get("student_name", ""))
    payload.setdefault("supervisor_signature_name", payload.get("supervisor_full_name", ""))
    payload.setdefault("co_supervisor_signature_name", payload.get("co_supervisor_full_name", ""))
    return payload


def _supervisor_agreement_date_parts(payload, prefix):
    day = _docx_first_value(payload, f"{prefix}_signature_day")
    month = _docx_first_value(payload, f"{prefix}_signature_month")
    year = _docx_first_value(payload, f"{prefix}_signature_year")
    date_value = _docx_first_value(payload, f"{prefix}_signature_date", "signature_date")
    if date_value and not (day and month and year):
        try:
            parsed = datetime.strptime(str(date_value)[:10], "%Y-%m-%d")
            day = day or parsed.strftime("%d")
            month = month or parsed.strftime("%B")
            year = year or parsed.strftime("%Y")
        except ValueError:
            pass
    if len(year) == 4 and year.startswith("20"):
        year = year[2:]
    return day, month, year


def _docx_underline_fill(value, width, *, leading_space=False, minimum_underscores=3):
    value = " ".join(str(value or "").split())
    if not value:
        return "_" * width
    prefix = " " if leading_space else ""
    remaining = max(width - len(value) - len(prefix), minimum_underscores)
    return f"{prefix}{value}{'_' * remaining}"


def _supervisor_agreement_party_line(label, surname, initials, suffix=""):
    return (
        f"{label}{_docx_underline_fill(surname, 49, leading_space=True)}"
        f"Initials{_docx_underline_fill(initials, 18, leading_space=True)}"
        f"{suffix}"
    )


def _supervisor_agreement_signed_line(payload, prefix, label):
    location = _docx_first_value(payload, f"{prefix}_signing_location")
    day, month, year = _supervisor_agreement_date_parts(payload, prefix)
    year = year[-2:] if len(year) == 4 and year.startswith("20") else year
    return (
        f"({label}) Signed at {_docx_underline_fill(location, 33)} on this "
        f"{_docx_underline_fill(day, 8)} day of"
        f"{_docx_underline_fill(month, 22, leading_space=True)}20{year or '___'}."
    )


def _supervisor_agreement_signature_line(payload, prefix, default_name=""):
    signature = _docx_first_value(payload, f"{prefix}_signature")
    printed_name = _docx_first_value(payload, f"{prefix}_signature_name") or default_name
    return (
        f"{_docx_underline_fill(signature, 44)}"
        "\t"
        f"{_docx_underline_fill(printed_name, 40)}"
    )


def _generate_supervisor_agreement_template_word_bytes(project, payload):
    template_path = _supervisor_agreement_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _supervisor_agreement_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)

    student_name = _docx_first_value(payload, "student_name") or _docx_student_full_name(project, payload)
    student_surname = _docx_first_value(payload, "student_surname", "surname")
    student_initials = _docx_first_value(payload, "student_initials")
    student_number = _docx_first_value(payload, "student_number")
    degree = _docx_first_value(payload, "degree", "qualification", default="MBA")
    supervisor_name = _docx_first_value(payload, "supervisor_full_name", "supervisor_name")
    supervisor_surname = _docx_first_value(payload, "supervisor_surname")
    supervisor_initials = _docx_first_value(payload, "supervisor_initials")
    department = _docx_first_value(payload, "department", "supervisor_department", default="Johannesburg Business School")
    co_supervisor_name = _docx_first_value(payload, "co_supervisor_full_name", "co_supervisor_name")
    co_supervisor_surname = _docx_first_value(payload, "co_supervisor_surname")
    co_supervisor_initials = _docx_first_value(payload, "co_supervisor_initials")
    co_supervisor_department = _docx_first_value(payload, "co_supervisor_department")
    co_supervisor_has_signature = bool(
        co_supervisor_name
        or _docx_first_value(payload, "co_supervisor_signature")
        or _docx_first_value(payload, "co_supervisor_signature_name")
    )
    co_supervisor_signature_payload = payload if co_supervisor_has_signature else {}

    field_map = [
        (1, 1, 1, student_name),
        (1, 2, 1, student_number),
        (1, 2, 3, degree),
        (2, 1, 1, supervisor_name),
        (2, 2, 1, department),
        (3, 1, 1, co_supervisor_name),
        (3, 2, 1, co_supervisor_department),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value)

    for index, digit in enumerate(student_number[:10]):
        _docx_set_cell_text_preserving_style(root, 4, 0, index, digit)

    paragraph_updates = {
        48: _supervisor_agreement_party_line("Surname", student_surname, student_initials, "(hereafter called the student)"),
        60: f"Address {_docx_underline_fill(_docx_first_value(payload, 'student_address'), 84)}",
        61: f"{'_' * 71} Postal code {_docx_underline_fill(_docx_first_value(payload, 'student_postal_code'), 10)}",
        62: f"Degree {_docx_underline_fill(degree, 49)}",
        65: _supervisor_agreement_party_line("Surname", supervisor_surname, supervisor_initials),
        67: f"School/Department {_docx_underline_fill(department, 74)},",
        73: _supervisor_agreement_party_line("Surname", co_supervisor_surname, co_supervisor_initials),
        75: f"School/Department {_docx_underline_fill(co_supervisor_department, 74)},",
        153: _supervisor_agreement_signed_line(payload, "student", "a"),
        156: _supervisor_agreement_signature_line(payload, "student", student_name),
        160: _supervisor_agreement_signed_line(payload, "supervisor", "b"),
        163: _supervisor_agreement_signature_line(payload, "supervisor", supervisor_name),
        167: _supervisor_agreement_signed_line(co_supervisor_signature_payload, "co_supervisor", "c"),
        170: _supervisor_agreement_signature_line(co_supervisor_signature_payload, "co_supervisor", co_supervisor_name),
    }
    for paragraph_index, value in paragraph_updates.items():
        _docx_set_indexed_paragraph_text_preserving_style(root, paragraph_index, value)

    _docx_append_paragraph_signature_image(root, contents, 156, payload, "student_signature")
    _docx_append_paragraph_signature_image(root, contents, 163, payload, "supervisor_signature")
    if co_supervisor_has_signature:
        _docx_append_paragraph_signature_image(root, contents, 170, payload, "co_supervisor_signature")

    return _docx_write_template(entries, contents, root)


def _generate_jbs10_template_word_bytes(project, payload):
    template_path = _jbs10_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    supervisor = _docx_supervisor_payload(project, payload)

    def user_staff_number(user):
        profile = getattr(user, "scholar_profile", None) if user else None
        return getattr(profile, "staff_number", "") if profile else ""

    def assessor_name_line(slot):
        assessor = _docx_assessor_payload(project, payload, slot)
        name = _docx_first_value(payload, f"{slot}_name") or assessor["name"]
        affiliation = _docx_first_value(payload, f"{slot}_affiliation") or assessor["affiliation"]
        if name and affiliation and affiliation.lower() not in name.lower():
            return f"{name}, {affiliation}"
        return name

    def assessor_value(slot, key):
        assessor = _docx_assessor_payload(project, payload, slot)
        if key == "staff_number":
            return _docx_first_value(payload, f"{slot}_staff_number") or user_staff_number(getattr(project, slot, None))
        if key == "email":
            return _docx_first_value(payload, f"{slot}_email") or assessor["email"]
        if key == "qualification":
            return _docx_first_value(payload, f"{slot}_qualification", f"{slot}_highest_qualification") or assessor["qualification"]
        return _docx_first_value(payload, f"{slot}_{key}")

    field_map = [
        (0, 1, 1, _docx_first_value(payload, "surname")),
        (0, 1, 3, _docx_first_value(payload, "student_title")),
        (0, 2, 1, _docx_first_value(payload, "student_initials")),
        (0, 2, 3, _docx_first_value(payload, "student_number")),
        (0, 2, 5, _docx_yes_no(_docx_first_value(payload, "student_is_staff_member", default="No"))),
        (0, 3, 1, _docx_first_value(payload, "qualification", default=getattr(project, "qualification", "") or "MBA")),
        (0, 4, 1, _docx_first_value(payload, "ethical_clearance_number")),
        (1, 2, 1, _docx_first_value(payload, "research_title")),
        (1, 5, 1, _docx_first_value(payload, "amended_title", "previous_title")),
        (1, 8, 1, _docx_first_value(payload, "supervisor_name", "proposed_supervisor") or supervisor["name"]),
        (1, 8, 3, _docx_first_value(payload, "supervisor_staff_number") or user_staff_number(getattr(project, "primary_supervisor", None))),
        (1, 9, 1, _docx_first_value(payload, "co_supervisor_1", "proposed_co_supervisors", "co_supervisor_name")),
        (1, 9, 3, _docx_first_value(payload, "co_supervisor_1_staff_number")),
        (1, 10, 1, _docx_first_value(payload, "co_supervisor_2")),
        (1, 10, 3, _docx_first_value(payload, "co_supervisor_2_staff_number")),
        (1, 12, 1, _docx_first_value(payload, "amended_supervisor_lineup", "amended_supervisor")),
        (1, 12, 3, _docx_first_value(payload, "amended_supervisor_staff_number")),
        (1, 13, 1, _docx_first_value(payload, "amended_co_supervisor_lineup", "amended_co_supervisors", "amended_co_supervisor")),
        (1, 13, 3, _docx_first_value(payload, "amended_co_supervisor_staff_number")),
        (1, 16, 1, _docx_first_value(payload, "internal_assessor_name")),
        (2, 1, 1, assessor_name_line("assessor_1")),
        (2, 1, 3, assessor_value("assessor_1", "staff_number")),
        (2, 2, 1, assessor_value("assessor_1", "qualification")),
        (2, 2, 3, assessor_value("assessor_1", "email")),
        (2, 3, 1, assessor_name_line("assessor_2")),
        (2, 3, 3, assessor_value("assessor_2", "staff_number")),
        (2, 4, 1, assessor_value("assessor_2", "qualification")),
        (2, 4, 3, assessor_value("assessor_2", "email")),
        (2, 5, 1, assessor_name_line("assessor_3")),
        (2, 5, 3, assessor_value("assessor_3", "staff_number")),
        (2, 6, 1, assessor_value("assessor_3", "qualification")),
        (2, 6, 3, assessor_value("assessor_3", "email")),
        (2, 7, 1, assessor_name_line("assessor_4")),
        (2, 7, 3, assessor_value("assessor_4", "staff_number")),
        (2, 8, 1, assessor_value("assessor_4", "qualification")),
        (2, 8, 3, assessor_value("assessor_4", "email")),
        (2, 10, 1, _docx_first_value(payload, "amended_internal_assessor_name")),
        (2, 11, 1, _docx_first_value(payload, "amended_internal_assessor_qualification")),
        (2, 11, 3, _docx_first_value(payload, "amended_internal_assessor_staff_number")),
        (2, 13, 1, _docx_first_value(payload, "amended_external_assessor_1_name")),
        (2, 13, 3, _docx_first_value(payload, "amended_external_assessor_1_staff_number")),
        (2, 14, 1, _docx_first_value(payload, "amended_external_assessor_1_qualification")),
        (2, 14, 3, _docx_first_value(payload, "amended_external_assessor_1_email")),
        (2, 15, 1, _docx_first_value(payload, "amended_external_assessor_2_name")),
        (2, 15, 3, _docx_first_value(payload, "amended_external_assessor_2_staff_number")),
        (2, 16, 1, _docx_first_value(payload, "amended_external_assessor_2_qualification")),
        (2, 16, 3, _docx_first_value(payload, "amended_external_assessor_2_email")),
        (2, 17, 1, _docx_first_value(payload, "amended_external_assessor_3_name")),
        (2, 17, 3, _docx_first_value(payload, "amended_external_assessor_3_staff_number")),
        (2, 18, 1, _docx_first_value(payload, "amended_external_assessor_3_qualification")),
        (2, 18, 3, _docx_first_value(payload, "amended_external_assessor_3_email")),
        (3, 0, 1, _docx_first_value(payload, "supervisor_signature")),
        (3, 0, 3, _docx_format_date(_docx_first_value(payload, "supervisor_signature_date"))),
        (3, 1, 1, _docx_first_value(payload, "co_supervisor_signature")),
        (3, 1, 3, _docx_format_date(_docx_first_value(payload, "co_supervisor_signature_date"))),
        (4, 0, 1, _docx_first_value(payload, "head_of_department_signature")),
        (4, 0, 3, _docx_format_date(_docx_first_value(payload, "head_of_department_signature_date"))),
        (5, 0, 1, _docx_first_value(payload, "jbs_hdc_signature")),
        (5, 0, 3, _docx_format_date(_docx_first_value(payload, "jbs_hdc_signature_date"))),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text_preserving_style_allow_blank(root, table_index, row_index, cell_index, value)

    _docx_set_cell_signature_image(root, contents, 3, 0, 1, payload, "supervisor_signature")
    _docx_set_cell_signature_image(root, contents, 3, 1, 1, payload, "co_supervisor_signature")
    _docx_set_cell_signature_image(root, contents, 4, 0, 1, payload, "head_of_department_signature")
    _docx_set_cell_signature_image(root, contents, 5, 0, 1, payload, "jbs_hdc_signature")

    study_type_checks = _jbs10_study_type_checks(payload)
    for table_index, row_index, cell_index, checked in (
        (0, 5, 1, study_type_checks["capstone"]),
        (0, 5, 3, study_type_checks["limited_scope"]),
        (0, 5, 5, study_type_checks["minor"]),
        (0, 5, 7, study_type_checks["dissertation"]),
        (0, 5, 10, study_type_checks["thesis_monograph"]),
        (0, 6, 10, study_type_checks["thesis_article"]),
    ):
        _docx_set_mark_cell(root, table_index, row_index, cell_index, checked)

    is_4ir = _docx_yes_no(_docx_first_value(payload, "is_4ir_research", default="No")).lower() == "yes"
    _docx_set_cell_text_preserving_style_allow_blank(root, 1, 3, 3, "X" if is_4ir else "")
    _docx_set_cell_text_preserving_style_allow_blank(root, 1, 3, 5, "" if is_4ir else "X")

    return _docx_write_template(entries, contents, root)


def _intent_signature_line(payload, signature_key, date_key):
    signature = _docx_first_value(payload, signature_key)
    date_value = _docx_format_date(_docx_first_value(payload, date_key))
    if signature and date_value:
        return f"{signature}\nDate: {date_value}"
    return signature or date_value


def _generate_intent_to_submit_template_word_bytes(project, payload):
    template_path = _intent_to_submit_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    supervisor = _docx_supervisor_payload(project, payload)

    simple_cells = [
        (0, 0, 1, _docx_student_full_name(project, payload)),
        (0, 1, 1, _docx_first_value(payload, "student_number")),
        (0, 2, 1, _docx_first_value(payload, "email")),
        (0, 2, 3, _docx_first_value(payload, "contact")),
        (0, 6, 1, _docx_first_value(payload, "supervisor_name", "proposed_supervisor") or supervisor["name"]),
        (0, 7, 1, _docx_first_value(payload, "co_supervisor_name", "co_supervisor_1", "proposed_co_supervisors")),
        (0, 20, 1, _docx_first_value(payload, "ethical_clearance_number")),
    ]
    for table_index, row_index, cell_index, value in simple_cells:
        _docx_set_cell_text(root, table_index, row_index, cell_index, value, size="18")

    intended_notice_date = _docx_format_date(_docx_first_value(payload, "intended_date"))
    replace_cells = [
        (0, 5, 0, _docx_first_value(payload, "research_title")),
        (0, 8, 0, f"Date on which this notice is given: {intended_notice_date}" if intended_notice_date else ""),
        (0, 11, 1, _intent_signature_line(payload, "signature_name", "signature_date")),
        (0, 13, 1, _docx_first_value(payload, "supervisor_agree_signature")),
        (0, 14, 1, _docx_first_value(payload, "co_supervisor_agree_signature")),
        (0, 15, 1, _docx_first_value(payload, "supervisor_disagree_signature")),
        (0, 16, 1, _docx_first_value(payload, "co_supervisor_disagree_signature")),
        (0, 17, 1, _docx_first_value(payload, "disagree_reasons")),
        (0, 18, 1, _docx_format_date(_docx_first_value(payload, "disagree_reasons_date"))),
    ]
    for table_index, row_index, cell_index, value in replace_cells:
        _docx_replace_cell_text(root, table_index, row_index, cell_index, value, size="18")
    _docx_set_cell_signature_image(root, contents, 0, 11, 1, payload, "signature_name")
    _docx_set_cell_signature_image(root, contents, 0, 13, 1, payload, "supervisor_agree_signature")
    _docx_set_cell_signature_image(root, contents, 0, 14, 1, payload, "co_supervisor_agree_signature")
    _docx_set_cell_signature_image(root, contents, 0, 15, 1, payload, "supervisor_disagree_signature")
    _docx_set_cell_signature_image(root, contents, 0, 16, 1, payload, "co_supervisor_disagree_signature")

    for row_index, field_name in (
        (19, "title_approved_by_hdc"),
        (21, "examiners_approved_by_hdc"),
        (22, "examiners_nominated_with_notice"),
    ):
        answer = _docx_yes_no(_docx_first_value(payload, field_name)).lower()
        if answer == "yes":
            _docx_replace_cell_text(root, 0, row_index, 1, "X  Yes", size="16", bold=True, center=True)
        elif answer == "no":
            _docx_replace_cell_text(root, 0, row_index, 2, "X  No", size="16", bold=True, center=True)

    approval_line = "\n".join(
        line
        for line in [
            _intent_signature_line(payload, "hod_signature", "hod_signature_date"),
            _intent_signature_line(payload, "director_signature", "director_signature_date"),
        ]
        if line
    )
    if approval_line:
        _docx_set_indexed_paragraph_text(root, 7, approval_line, size="18")

    return _docx_write_template(entries, contents, root)


def _corrections_response_rows(payload):
    rows = []
    for slot, limit, label in (
        ("assessor_1", 30, "ASSESSOR 1"),
        ("assessor_2", 15, "ASSESSOR 2"),
        ("assessor_3", 5, "ASSESSOR 3"),
    ):
        slot_rows = []
        for row_index in range(1, limit + 1):
            comment = _docx_first_value(payload, f"{slot}_comment_{row_index}")
            response = _docx_first_value(payload, f"{slot}_response_{row_index}")
            supervisor_comment = _docx_first_value(payload, f"{slot}_supervisor_comment_{row_index}")
            if comment or response or supervisor_comment:
                slot_rows.append((str(row_index), comment, response, supervisor_comment))
        if slot_rows:
            rows.append(("", label, "", ""))
            rows.extend(slot_rows)
    return rows


def _generate_corrections_response_template_word_bytes(project, payload):
    template_path = _corrections_response_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    student_label = _docx_student_initials_surname(project, payload) or "*Insert Student Name*"
    student_number = _docx_first_value(payload, "student_number")
    student_title = f"{student_label} ({student_number})" if student_number else student_label
    research_title = _docx_first_value(payload, "research_title")
    supervisor_name = _docx_first_value(payload, "supervisor_name") or _docx_supervisor_payload(project, payload)["name"]
    _docx_set_indexed_paragraph_text(
        root,
        0,
        f"Student's Response to Examiner's Report: {student_title}",
        size="28",
        bold=True,
    )
    if research_title:
        _docx_set_indexed_paragraph_text(
            root,
            2,
            f"Kindly consider my responses below to the assessors' feedback for: {research_title}",
        )

    table = root.find(".//w:tbl", _DOCX_NS)
    if table is None:
        return _docx_write_template(entries, contents, root)
    existing_rows = table.findall("w:tr", _DOCX_NS)
    if not existing_rows:
        return _docx_write_template(entries, contents, root)
    header = existing_rows[0]
    header_cells = header.findall("w:tc", _DOCX_NS)
    if len(header_cells) >= 4:
        _docx_set_cell_element_text(header_cells[3], f"Supervisors' comments ({supervisor_name})", bold=True)

    rows_to_write = _corrections_response_rows(payload)
    minimum_body_rows = max(len(existing_rows) - 1, 1)
    row_template = existing_rows[2] if len(existing_rows) > 2 else existing_rows[-1]
    for row in existing_rows[1:]:
        table.remove(row)

    body_row_count = max(len(rows_to_write), minimum_body_rows)
    for row_index in range(body_row_count):
        row = deepcopy(row_template)
        table.append(row)
        cells = row.findall("w:tc", _DOCX_NS)
        values = rows_to_write[row_index] if row_index < len(rows_to_write) else ("", "", "", "")
        for cell, value in zip(cells[:4], values):
            _docx_set_cell_element_text(cell, value)

    return _docx_write_template(entries, contents, root)


def _generate_jbs1_template_word_bytes(project, payload):
    template_path = _jbs1_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    field_map = [
        (1, 1, 1, _docx_first_value(payload, "surname")),
        (1, 1, 3, _docx_first_value(payload, "student_title")),
        (1, 2, 1, _docx_first_value(payload, "student_initials")),
        (1, 2, 3, _docx_first_value(payload, "student_id_number")),
        (1, 3, 1, _docx_first_value(payload, "student_number")),
        (1, 3, 3, _docx_first_value(payload, "ethical_clearance_number")),
        (1, 4, 1, _docx_first_value(payload, "qualification", default="MBA")),
        (1, 5, 1, _docx_first_value(payload, "email")),
        (1, 5, 3, _docx_first_value(payload, "contact")),
        (2, 2, 0, _docx_first_value(payload, "research_title")),
        (3, 0, 1, _docx_first_value(payload, "signature_name") or _docx_student_full_name(project, payload)),
        (3, 0, 3, _docx_format_date(_docx_first_value(payload, "signature_date"))),
        (4, 0, 1, _docx_first_value(payload, "supervisor_signature", "supervisor_name")),
        (4, 0, 3, _docx_format_date(_docx_first_value(payload, "supervisor_signature_date"))),
        (4, 1, 1, _docx_first_value(payload, "co_supervisor_signature", "co_supervisor_name")),
        (4, 1, 3, _docx_format_date(_docx_first_value(payload, "co_supervisor_signature_date"))),
        (5, 0, 0, _docx_first_value(payload, "office_registration")),
        (5, 1, 0, _docx_first_value(payload, "office_approved_title")),
        (5, 2, 0, _docx_first_value(payload, "office_affidavit")),
        (5, 3, 0, _docx_first_value(payload, "office_language_edited")),
        (5, 4, 0, _docx_first_value(payload, "office_turnitin_report")),
        (6, 0, 1, _docx_first_value(payload, "office_program_manager")),
        (6, 0, 3, _docx_format_date(_docx_first_value(payload, "office_program_manager_date"))),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value)
    _docx_set_cell_signature_image(root, contents, 3, 0, 1, payload, "signature_name")
    _docx_set_cell_signature_image(root, contents, 4, 0, 1, payload, "supervisor_signature")
    _docx_set_cell_signature_image(root, contents, 4, 1, 1, payload, "co_supervisor_signature")
    _docx_set_cell_signature_image(root, contents, 6, 0, 1, payload, "office_program_manager")
    return _docx_write_template(entries, contents, root)


def _generate_affidavit_template_word_bytes(project, payload):
    template_path = _affidavit_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    full_name = _docx_student_full_name(project, payload)
    affidavit_day, affidavit_month, affidavit_year = _docx_day_month_year_parts(payload, "affidavit_date", "affidavit")
    affidavit_year_tail = str(affidavit_year or "").strip()
    if len(affidavit_year_tail) == 4 and affidavit_year_tail.startswith("20"):
        affidavit_year_tail = affidavit_year_tail[-2:]
    student_id_number = _docx_first_value(payload, "student_id_number")
    student_number = _docx_first_value(payload, "student_number")
    qualification = _docx_first_value(payload, "qualification", default="MBA")
    signing_location = _docx_first_value(payload, "signing_location")
    signature_name = _docx_first_value(payload, "signature_name") or full_name
    updates = {
        12: [
            ("This serves to confirm that I ", False),
            (_docx_underlined_field_text(full_name, 68), True),
        ],
        15: [
            ("ID Number ", False),
            (_docx_underlined_field_text(student_id_number, 82), True),
        ],
        17: [
            ("Student number ", False),
            (_docx_underlined_field_text(student_number, 65), True),
            (" enrolled for the ", False),
        ],
        19: [
            ("Qualification ", False),
            (_docx_underlined_field_text(qualification, 76), True),
            (" in the", False),
        ],
        32: [
            ("Signed at ", False),
            (_docx_underlined_field_text(signing_location, 29), True),
            ("on this ", False),
            (_docx_underlined_field_text(affidavit_day, 14), True),
            ("day of ", False),
            (_docx_underlined_field_text(affidavit_month, 21), True),
            (" 20", False),
            (_docx_underlined_field_text(affidavit_year_tail, 3), True),
            (".", False),
        ],
        34: [
            ("Signature ", False),
            (_docx_underlined_field_text(signature_name, 34), True),
            (" Print name ", False),
            (_docx_underlined_field_text(full_name, 25), True),
        ],
    }
    for paragraph_index, parts in updates.items():
        _docx_set_indexed_paragraph_parts_preserving_style(root, paragraph_index, parts)
    _docx_append_paragraph_signature_image(root, contents, 34, payload, "signature_name")
    return _docx_write_template(entries, contents, root)


def _generate_plagiarism_template_word_bytes(project, payload):
    return _generate_tii_ai_template_word_bytes(project, payload)


def _generate_tii_ai_template_word_bytes(project, payload):
    template_path = _tii_ai_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    person_name = _docx_student_initials_surname(project, payload) or _docx_student_full_name(project, payload)
    signature_name = _docx_first_value(payload, "signature_name") or _docx_student_full_name(project, payload)
    common_fields = [
        (0, 0, 1, _docx_first_value(payload, "programme", "module_title", default="MBA")),
        (0, 1, 1, _docx_first_value(payload, "assessment_title", "research_title")),
        (0, 2, 1, _docx_first_value(payload, "module_lead", "lecturer_name", "supervisor_name")),
        (0, 3, 1, _docx_format_date(_docx_first_value(payload, "submission_date", "due_date"))),
        (1, 1, 0, person_name),
        (1, 1, 1, _docx_first_value(payload, "student_number")),
        (1, 1, 2, signature_name),
        (2, 0, 1, _docx_first_value(payload, "course_name", "qualification", default="MBA")),
        (2, 1, 1, _docx_first_value(payload, "module_title", "programme", default="MBA")),
        (2, 2, 1, _docx_first_value(payload, "assessment_title", "research_title")),
        (2, 3, 1, _docx_first_value(payload, "lecturer_name", "module_lead", "supervisor_name")),
        (2, 4, 1, _docx_format_date(_docx_first_value(payload, "due_date", "submission_date"))),
        (3, 1, 0, person_name),
        (3, 1, 1, _docx_first_value(payload, "student_number")),
        (3, 1, 2, signature_name),
        (4, 1, 0, _docx_first_value(payload, "ai_tools_used")),
        (4, 1, 1, _docx_first_value(payload, "ai_use_purpose")),
        (4, 1, 2, _docx_first_value(payload, "ai_use_motivation")),
    ]
    for table_index, row_index, cell_index, value in common_fields:
        _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value)
    _docx_set_cell_signature_image(root, contents, 1, 1, 2, payload, "signature_name")
    _docx_set_cell_signature_image(root, contents, 3, 1, 2, payload, "signature_name")
    signature_date = _docx_format_date(_docx_first_value(payload, "signature_date"))
    if signature_date:
        _docx_set_indexed_paragraph_text_preserving_style(root, 88, f"Date: {signature_date}")
    return _docx_write_template(entries, contents, root)


def _grade_bucket_row(grade):
    try:
        grade_value = int(str(grade or "").strip())
    except (TypeError, ValueError):
        return None
    if grade_value >= 75:
        return 1
    if grade_value >= 70:
        return 2
    if grade_value >= 60:
        return 3
    if grade_value >= 50:
        return 4
    return 5


def _generate_capstone_evaluation_template_word_bytes(project, payload):
    template_path = _capstone_evaluation_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    grade = _docx_first_value(payload, "grade", "final_mark")
    field_map = [
        (0, 1, 1, f"Total: {grade}/100" if grade else ""),
        (1, 1, 0, _docx_first_value(payload, "student_name") or _docx_student_full_name(project, payload)),
        (1, 1, 1, _docx_first_value(payload, "student_number")),
        (1, 1, 2, _docx_first_value(payload, "research_title")),
        (2, 1, 0, _docx_first_value(payload, "assessor_name")),
        (2, 1, 1, _docx_first_value(payload, "assessor_signature_name", "assessor_name")),
        (2, 1, 2, _docx_format_date(_docx_first_value(payload, "certification_date"))),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text(root, table_index, row_index, cell_index, value)

    grade_row = _grade_bucket_row(grade)
    if grade_row is not None:
        _docx_set_cell_text(root, 3, grade_row, 2, grade)

    written_assessment = _docx_first_value(payload, "written_assessment")
    recommendation = _docx_first_value(payload, "recommendation")
    feedback = "\n\n".join(part for part in [f"Recommendation: {recommendation}" if recommendation else "", written_assessment] if part)
    if feedback:
        _docx_set_cell_text(root, 5, 0, 0, feedback)
    return _docx_write_template(entries, contents, root)


def _docx_append_cell_paragraph_text(root, table_index, row_index, cell_index, value):
    value = str(value or "").strip()
    if not value:
        return
    cell = _docx_cell(root, table_index, row_index, cell_index)
    if cell is None:
        return
    paragraphs = cell.findall("w:p", _DOCX_NS)
    source_paragraph = paragraphs[-1] if paragraphs else None
    source_p_props = deepcopy(source_paragraph.find("w:pPr", _DOCX_NS)) if source_paragraph is not None and source_paragraph.find("w:pPr", _DOCX_NS) is not None else None
    source_run = source_paragraph.find("w:r", _DOCX_NS) if source_paragraph is not None else None
    source_run_props = deepcopy(source_run.find("w:rPr", _DOCX_NS)) if source_run is not None and source_run.find("w:rPr", _DOCX_NS) is not None else None
    for raw_line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraph = ET.SubElement(cell, _docx_tag("p"))
        if source_p_props is not None:
            paragraph.append(deepcopy(source_p_props))
        paragraph.append(_docx_run_like_existing(paragraph, raw_line, source_props=source_run_props))


def _generate_capstone_assessor_report_form_1_template_word_bytes(project, payload):
    template_path = _capstone_assessor_report_form_1_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = _jbs5_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    field_map = [
        (0, 0, 1, _docx_first_value(payload, "student_name") or _docx_student_full_name(project, payload)),
        (0, 1, 1, _docx_first_value(payload, "student_number")),
        (0, 2, 1, _docx_first_value(payload, "research_title")),
        (0, 3, 1, _docx_first_value(payload, "assessor_name")),
        (0, 4, 1, _docx_first_value(payload, "affiliation")),
        (0, 5, 1, "\n".join(part for part in [_docx_first_value(payload, "assessor_email"), _docx_first_value(payload, "assessor_contact")] if part)),
        (0, 6, 1, _docx_first_value(payload, "assessor_signature_name", "assessor_name")),
        (0, 6, 3, _docx_format_date(_docx_first_value(payload, "certification_date"))),
        (3, 0, 1, _docx_first_value(payload, "grade")),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value)
    _docx_set_cell_signature_image(root, contents, 0, 6, 1, payload, "assessor_signature_name")

    recommendation = _docx_normalized(_docx_first_value(payload, "recommendation"))
    recommendation_rows = [
        (2, "as the research stands" in recommendation or "accept as the research stands" in recommendation),
        (4, "minor revisions" in recommendation),
        (5, "major revisions" in recommendation and "re-examination" not in recommendation and "re examination" not in recommendation),
        (6, "re-examination" in recommendation or "re examination" in recommendation),
        (7, "outright" in recommendation or "rejection" in recommendation),
    ]
    if recommendation:
        for row_index, checked in recommendation_rows:
            _docx_set_mark_cell(root, 1, row_index, 2, checked)
            _docx_set_mark_cell(root, 1, row_index, 3, not checked)

    consent = _docx_first_value(payload, "consent_name_disclosure")
    if consent:
        _docx_yes_no_marks(root, 1, 8, 2, 3, consent)

    _docx_append_cell_paragraph_text(root, 4, 0, 0, _docx_first_value(payload, "written_assessment"))
    return _docx_write_template(entries, contents, root)


def _generate_assessment_summary_template_word_bytes(project, payload):
    template_path = _summary_assessment_report_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    payload = build_assessment_summary_payload(project, payload)
    entries, contents, root = _docx_read_template(template_path)
    field_map = [
        (0, 1, 1, _docx_first_value(payload, "student_surname")),
        (0, 1, 3, _docx_first_value(payload, "student_initials")),
        (0, 1, 5, _docx_first_value(payload, "student_title")),
        (0, 3, 1, _docx_first_value(payload, "qualification", default="Masters")),
        (0, 4, 1, _docx_first_value(payload, "discipline", default="Master of Business Administration")),
        (0, 5, 1, _docx_first_value(payload, "research_title")),
        (0, 6, 1, _docx_format_date(_docx_first_value(payload, "date_of_first_registration"), month_year=True)),
        (1, 1, 1, _docx_first_value(payload, "supervisor_surname")),
        (1, 1, 3, _docx_first_value(payload, "supervisor_initials")),
        (1, 1, 5, _docx_first_value(payload, "supervisor_title")),
        (1, 2, 1, _docx_first_value(payload, "supervisor_affiliation")),
        (1, 3, 1, _docx_first_value(payload, "supervisor_qualification")),
        (1, 4, 1, _docx_first_value(payload, "co_supervisor_surname")),
        (1, 4, 3, _docx_first_value(payload, "co_supervisor_initials")),
        (1, 4, 5, _docx_first_value(payload, "co_supervisor_title")),
        (1, 5, 1, _docx_first_value(payload, "co_supervisor_affiliation")),
        (1, 6, 1, _docx_first_value(payload, "co_supervisor_qualification")),
        (1, 7, 1, _docx_first_value(payload, "assessor_1_surname")),
        (1, 7, 3, _docx_first_value(payload, "assessor_1_initials")),
        (1, 7, 5, _docx_first_value(payload, "assessor_1_title")),
        (1, 8, 1, _docx_first_value(payload, "assessor_1_affiliation")),
        (1, 9, 1, _docx_first_value(payload, "assessor_1_qualification")),
        (1, 10, 1, _docx_first_value(payload, "assessor_2_surname")),
        (1, 10, 3, _docx_first_value(payload, "assessor_2_initials")),
        (1, 10, 5, _docx_first_value(payload, "assessor_2_title")),
        (1, 11, 1, _docx_first_value(payload, "assessor_2_affiliation")),
        (1, 12, 1, _docx_first_value(payload, "assessor_2_qualification")),
        (3, 20, 1, _docx_first_value(payload, "coursework_total")),
        (3, 20, 2, _docx_first_value(payload, "coursework_credit_total")),
        (3, 21, 1, _docx_first_value(payload, "coursework_average")),
        (3, 21, 2, _docx_first_value(payload, "coursework_credit_average")),
        (3, 24, 1, _docx_first_value(payload, "assessor_1_grade")),
        (3, 25, 1, _docx_first_value(payload, "assessor_2_grade")),
        (3, 26, 1, _docx_first_value(payload, "capstone_total")),
        (3, 27, 1, _docx_first_value(payload, "capstone_average")),
        (3, 30, 1, _docx_first_value(payload, "capstone_weighted_result")),
        (3, 31, 1, _docx_first_value(payload, "coursework_weighted_result")),
        (3, 33, 2, _docx_first_value(payload, "final_mark")),
        (4, 0, 1, _docx_first_value(payload, "final_mark")),
        (6, 2, 0, _docx_first_value(payload, "supervisor_signature_name")),
        (6, 2, 1, _docx_format_date(_docx_first_value(payload, "supervisor_signature_date"))),
        (6, 4, 0, _docx_first_value(payload, "hod_signature_name")),
        (6, 4, 1, _docx_format_date(_docx_first_value(payload, "hod_signature_date"))),
        (
            6,
            6,
            0,
            _docx_first_value(payload, "chair_fhdc_signature_name", "hdc_signature_name", "executive_dean_signature_name"),
        ),
        (
            6,
            6,
            1,
            _docx_format_date(
                _docx_first_value(payload, "chair_fhdc_signature_date", "hdc_signature_date", "executive_dean_signature_date")
            ),
        ),
    ]
    for table_index, row_index, cell_index, value in field_map:
        _docx_set_cell_text_preserving_style(root, table_index, row_index, cell_index, value)

    _docx_set_cell_signature_image(root, contents, 6, 2, 0, payload, "supervisor_signature_name")
    _docx_set_cell_signature_image(root, contents, 6, 4, 0, payload, "hod_signature_name")
    if not _docx_set_cell_signature_image(root, contents, 6, 6, 0, payload, "chair_fhdc_signature_name"):
        if not _docx_set_cell_signature_image(root, contents, 6, 6, 0, payload, "hdc_signature_name"):
            _docx_set_cell_signature_image(root, contents, 6, 6, 0, payload, "executive_dean_signature_name")

    for offset, module_code in enumerate(SUMMARY_COURSEWORK_MODULES, start=2):
        _docx_set_cell_text_preserving_style(root, 3, offset, 1, _docx_first_value(payload, f"module_{module_code}_result"))
        _docx_set_cell_text_preserving_style(root, 3, offset, 2, _docx_first_value(payload, f"module_{module_code}_credit"))

    _docx_fill_character_cells(root, 0, 2, 1, 9, _docx_first_value(payload, "student_number"))

    recommendation_rows = {
        "assessor_1": 2,
        "assessor_2": 3,
        "assessor_3": 4,
    }
    for slot, row_index in recommendation_rows.items():
        selected_column = _assessment_summary_recommendation_column(_docx_first_value(payload, f"{slot}_recommendation"))
        for column_index in (1, 2, 3, 4):
            _docx_set_mark_cell(root, 2, row_index, column_index, selected_column == column_index)

    corrections_complete = _docx_is_yes(_docx_first_value(payload, "corrections_complete", default="Yes"))
    _docx_set_mark_cell(root, 5, 1, 2, corrections_complete)
    _docx_set_mark_cell(root, 5, 1, 4, not corrections_complete)

    final_grade = _assessment_summary_int_grade(_docx_first_value(payload, "final_mark"))
    _docx_set_mark_cell(root, 4, 0, 3, bool(final_grade is not None and final_grade >= 75))
    return _docx_write_template(entries, contents, root)


def _generate_assessor_temp_appointment_template_word_bytes(project, payload):
    template_path = _assessor_temp_appointment_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    entries, contents, root = _docx_read_template(template_path)
    _docx_yes_no_marks(root, 0, 0, 2, 4, _docx_first_value(payload, "new_employee"))
    _docx_set_cell_text_preserving_style(root, 0, 0, 6, _docx_first_value(payload, "employee_number"))
    _docx_yes_no_marks(root, 0, 1, 2, 4, _docx_first_value(payload, "employed_at_uj"))
    _docx_set_cell_text_preserving_style(root, 0, 1, 6, _docx_first_value(payload, "uj_department_division"))
    _docx_set_cell_text_preserving_style(root, 0, 2, 1, _docx_first_value(payload, "appointed_as", default="External Assessor"))
    _docx_set_cell_text_preserving_style(root, 0, 4, 1, _docx_first_value(payload, "assessor_surname"))
    _docx_set_cell_text_preserving_style(root, 0, 4, 3, _docx_first_value(payload, "assessor_title"))
    _docx_set_cell_text_preserving_style(root, 0, 5, 1, _docx_first_value(payload, "assessor_first_names"))
    _docx_fill_character_cells(root, 0, 6, 1, 13, _docx_first_value(payload, "identity_passport_number"))
    _docx_set_cell_text_preserving_style(root, 0, 7, 1, _docx_format_date_numeric(_docx_first_value(payload, "date_of_birth")))
    _docx_set_cell_text_preserving_style(root, 0, 7, 3, _docx_first_value(payload, "work_visa_number"))
    _docx_mark_matching_option(root, 0, [("Male", 8, 2), ("Female", 8, 4)], _docx_first_value(payload, "gender"))
    _docx_mark_matching_option(
        root,
        0,
        [("Single", 8, 7), ("Married", 8, 9), ("Divorced", 8, 11), ("Widowed", 8, 13)],
        _docx_first_value(payload, "marital_status"),
    )
    _docx_yes_no_marks(root, 0, 9, 2, 4, _docx_first_value(payload, "sa_citizen"))
    _docx_set_cell_text_preserving_style(root, 0, 9, 6, _docx_first_value(payload, "nationality"))
    _docx_yes_no_marks(root, 0, 10, 2, 4, _docx_first_value(payload, "employed_outside_uj"))
    _docx_set_cell_text_preserving_style(root, 0, 10, 6, _docx_first_value(payload, "home_language"))
    _docx_set_cell_text_preserving_style(root, 0, 11, 1, _docx_first_value(payload, "income_tax_number"))
    care_of = _docx_first_value(payload, "care_of_intermediary")
    _docx_set_mark_cell(root, 0, 12, 2, bool(care_of and _docx_normalized(care_of) != "none"))
    _docx_set_cell_text_preserving_style(root, 0, 12, 4, care_of or "NONE")
    _docx_set_cell_text_preserving_style(root, 0, 13, 1, _docx_first_value(payload, "home_address"))
    _docx_set_cell_text_preserving_style(root, 0, 13, 3, _docx_first_value(payload, "postal_address"))
    _docx_set_cell_text_preserving_style(root, 0, 14, 2, _docx_first_value(payload, "home_postal_code"))
    _docx_set_cell_text_preserving_style(root, 0, 14, 4, _docx_first_value(payload, "postal_code"))
    _docx_set_cell_text_preserving_style(root, 0, 15, 2, _docx_first_value(payload, "home_tel"))
    _docx_set_cell_text_preserving_style(root, 0, 15, 4, _docx_first_value(payload, "assessor_contact"))
    _docx_set_cell_text_preserving_style(root, 0, 16, 1, _docx_first_value(payload, "assessor_email"))
    _docx_set_cell_text_preserving_style(root, 0, 16, 3, _docx_first_value(payload, "work_tel"))
    _docx_yes_no_marks(root, 0, 17, 2, 4, _docx_first_value(payload, "disability_status"))
    _docx_set_cell_text_preserving_style(root, 0, 17, 6, _docx_first_value(payload, "disability_nature"))
    _docx_mark_matching_option(
        root,
        0,
        [("African", 19, 2), ("Coloured", 19, 4), ("Indian", 19, 6), ("White", 19, 8), ("Chinese", 19, 10)],
        _docx_first_value(payload, "race"),
    )

    _docx_set_cell_text_preserving_style(root, 1, 1, 1, _docx_first_value(payload, "qualification_institution"))
    _docx_set_cell_text_preserving_style(root, 1, 1, 3, _docx_first_value(payload, "highest_qualification"))
    _docx_set_cell_text_preserving_style(root, 1, 2, 1, _docx_format_date_numeric(_docx_first_value(payload, "qualification_awarded_date")))
    _docx_mark_matching_option(root, 1, [("Passed", 2, 4), ("Completed", 2, 6)], _docx_first_value(payload, "qualification_status"))
    _docx_yes_no_marks(root, 1, 4, 5, 3, _docx_first_value(payload, "bank_changed"))
    _docx_set_cell_text_preserving_style(root, 1, 5, 1, _docx_first_value(payload, "bank_account_holder"))
    _docx_set_cell_text_preserving_style(root, 1, 6, 1, _docx_first_value(payload, "bank_name"))
    _docx_set_cell_text_preserving_style(root, 1, 7, 1, _docx_first_value(payload, "bank_branch_name"))
    _docx_set_cell_text_preserving_style(root, 1, 7, 3, _docx_first_value(payload, "bank_branch_code"))
    _docx_set_cell_text_preserving_style(root, 1, 8, 1, _docx_first_value(payload, "bank_account_number"))
    _docx_set_cell_text_preserving_style(root, 1, 12, 1, _docx_first_value(payload, "appointed_as", default="External Assessor"))
    _docx_set_cell_text_preserving_style(root, 1, 13, 1, _docx_first_value(payload, "appointment_category"))
    _docx_set_cell_text_preserving_style(root, 1, 14, 1, _docx_format_date_numeric(_docx_first_value(payload, "appointment_start_date")))
    _docx_set_cell_text_preserving_style(root, 1, 14, 3, _docx_format_date_numeric(_docx_first_value(payload, "appointment_end_date")))
    _docx_mark_matching_option(
        root,
        1,
        [
            ("Temporary increase in volume of work, less than 12 months", 18, 0),
            ("Seasonal increase in volume of work, less than 12 months", 19, 0),
            ("Position funded by external (non UJ) funds for limited time", 21, 0),
            ("Services will not exceed 3 months", 24, 0),
            ("Specific project for limited time and clear deliverable", 25, 0),
            ("Other", 26, 0),
        ],
        _docx_first_value(payload, "temporary_employment_reason"),
    )
    _docx_set_cell_text_preserving_style(root, 1, 26, 2, _docx_first_value(payload, "appointment_reason_other"))
    _docx_set_cell_text_preserving_style(root, 1, 27, 1, _docx_first_value(payload, "appointment_motivation"))

    cost_parts = _docx_cost_centre_parts(_docx_first_value(payload, "full_cost_centre_string"))
    _docx_set_cell_text_preserving_style(root, 2, 0, 2, _docx_first_value(payload, "rate_per_month", default="N/A"))
    _docx_set_cell_text_preserving_style(root, 2, 0, 4, _docx_first_value(payload, "rate_per_hour"))
    _docx_set_cell_text_preserving_style(root, 2, 2, 0, _docx_first_value(payload, "other_rate_basis"))
    _docx_set_cell_text_preserving_style(root, 2, 3, 1, _docx_first_value(payload, "total_units"))
    _docx_set_cell_text_preserving_style(root, 2, 3, 3, _docx_first_value(payload, "actual_hours"))
    for index, part in enumerate(cost_parts, start=1):
        _docx_set_cell_text_preserving_style(root, 2, 4, index, part)
    _docx_set_cell_text_preserving_style(root, 2, 4, 7, _docx_first_value(payload, "permanent_post_number", default="N/A"))
    budget = _docx_first_value(payload, "total_budget_for_appointment")
    _docx_set_cell_text_preserving_style(root, 2, 5, 1, f"R{budget}" if budget and not str(budget).startswith("R") else budget)
    _docx_set_cell_text_preserving_style(root, 2, 7, 0, _docx_first_value(payload, "conflict_of_interest_details", default="NONE"))

    signature_name = _docx_first_value(payload, "employee_signature_name", "assessor_name")
    _docx_set_cell_text_preserving_style(root, 3, 1, 0, signature_name)
    _docx_set_cell_text_preserving_style(root, 3, 1, 1, signature_name)
    _docx_set_cell_text_preserving_style(root, 3, 1, 2, _docx_format_date_numeric(_docx_first_value(payload, "employee_signature_date")))
    _docx_set_cell_signature_image(root, contents, 3, 1, 0, payload, "employee_signature_name")
    return _docx_write_template(entries, contents, root)


def _generate_assessor_temp_claim_template_word_bytes(project, payload):
    template_path = _assessor_temp_claim_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    entries, contents, root = _docx_read_template(template_path)
    _docx_set_cell_text_preserving_style(root, 0, 3, 1, _docx_first_value(payload, "faculty_division"))
    _docx_set_cell_text_preserving_style(root, 0, 3, 3, _docx_first_value(payload, "department_unit_centre"))
    _docx_set_cell_text_preserving_style(root, 0, 4, 1, _docx_first_value(payload, "employee_number"))
    _docx_set_cell_text_preserving_style(root, 0, 4, 3, _docx_first_value(payload, "month_of_claim"))
    _docx_set_cell_text_preserving_style(root, 0, 5, 1, _docx_first_value(payload, "assessor_surname"))
    _docx_set_cell_text_preserving_style(root, 0, 5, 3, _docx_first_value(payload, "assessor_title"))
    _docx_set_cell_text_preserving_style(root, 0, 6, 1, _docx_first_value(payload, "assessor_first_names"))
    _docx_set_cell_text_preserving_style(root, 0, 7, 1, _docx_first_value(payload, "assessor_contact"))
    _docx_set_cell_text_preserving_style(root, 0, 7, 3, _docx_first_value(payload, "assessor_email"))
    _docx_set_cell_text_preserving_style(root, 0, 8, 1, _docx_first_value(payload, "alternate_contact_number"))
    _docx_set_cell_text_preserving_style(root, 0, 8, 3, _docx_first_value(payload, "alternate_email_address"))
    _docx_set_cell_text_preserving_style(root, 0, 10, 1, _docx_first_value(payload, "requestor_extension"))
    _docx_set_cell_text_preserving_style(root, 0, 10, 3, _docx_first_value(payload, "requestor_email", default="vukonac@uj.ac.za"))
    _docx_set_cell_text_preserving_style(root, 0, 12, 1, "START DATE: " + _docx_format_date_numeric(_docx_first_value(payload, "appointment_start_date")))
    _docx_set_cell_text_preserving_style(root, 0, 12, 2, "END DATE: " + _docx_format_date_numeric(_docx_first_value(payload, "appointment_end_date")))
    _docx_set_cell_text_preserving_style(root, 0, 13, 1, _docx_first_value(payload, "appointed_as", default="External Assessor"))
    _docx_set_cell_text_preserving_style(root, 0, 14, 2, _docx_first_value(payload, "claim_total_units", "total_units"))
    _docx_set_cell_text_preserving_style(root, 0, 14, 4, _docx_first_value(payload, "other_rate_basis"))
    _docx_set_cell_text_preserving_style(root, 0, 15, 1, "ZAR " + _docx_first_value(payload, "claim_rate", "rate_per_hour"))
    _docx_set_cell_text_preserving_style(root, 0, 15, 3, _docx_first_value(payload, "actual_hours"))
    cost_parts = _docx_cost_centre_parts(_docx_first_value(payload, "claim_cost_centre_number", "full_cost_centre_string"))
    for index, part in enumerate(cost_parts, start=1):
        _docx_set_cell_text_preserving_style(root, 0, 16, index, part)
    _docx_set_cell_text_preserving_style(root, 0, 17, 4, _docx_first_value(payload, "position_number"))
    budget = _docx_first_value(payload, "total_budget_for_appointment")
    _docx_set_cell_text_preserving_style(root, 0, 18, 1, f"R{budget}" if budget and not str(budget).startswith("R") else budget)
    _docx_set_cell_text_preserving_style(root, 0, 21, 0, _docx_first_value(payload, "contract_eit_number"))
    _docx_set_cell_text_preserving_style(root, 0, 21, 1, _docx_first_value(payload, "claim_total_units"))
    _docx_set_cell_text_preserving_style(root, 0, 21, 2, _docx_first_value(payload, "claim_rate"))
    _docx_set_cell_text_preserving_style(root, 0, 21, 3, _docx_first_value(payload, "claim_currency", default="ZAR"))
    _docx_set_cell_text_preserving_style(root, 0, 21, 4, _docx_first_value(payload, "amount_claimed"))
    _docx_set_cell_text_preserving_style(root, 0, 21, 5, _docx_first_value(payload, "claim_cost_centre_number"))
    _docx_set_cell_text_preserving_style(root, 0, 24, 4, _docx_first_value(payload, "total_claimed"))
    _docx_yes_no_marks(root, 1, 0, 5, 3, _docx_first_value(payload, "bank_changed"))
    _docx_set_cell_text_preserving_style(root, 2, 0, 1, _docx_first_value(payload, "bank_account_holder"))
    _docx_set_cell_text_preserving_style(root, 2, 1, 1, _docx_first_value(payload, "bank_name"))
    _docx_set_cell_text_preserving_style(root, 2, 2, 1, _docx_first_value(payload, "bank_branch_name"))
    _docx_set_cell_text_preserving_style(root, 2, 2, 3, _docx_first_value(payload, "bank_branch_code"))
    _docx_set_cell_text_preserving_style(root, 2, 3, 1, _docx_first_value(payload, "bank_account_number"))
    signature_name = _docx_first_value(payload, "claim_signature_name", "assessor_name")
    _docx_set_cell_text_preserving_style(root, 3, 3, 0, signature_name)
    _docx_set_cell_text_preserving_style(root, 3, 3, 1, signature_name)
    _docx_set_cell_text_preserving_style(root, 3, 3, 2, _docx_format_date_numeric(_docx_first_value(payload, "claim_signature_date")))
    _docx_set_cell_signature_image(root, contents, 3, 3, 0, payload, "claim_signature_name")
    _docx_set_cell_text_preserving_style(root, 6, 0, 0, _docx_first_value(payload, "conflict_of_interest_details", default="NONE"))
    return _docx_write_template(entries, contents, root)


def _generate_external_examiner_nomination_template_word_bytes(project, payload):
    template_path = _external_examiner_nomination_word_template_path()
    if not _docx_template_exists(template_path):
        return None

    source_payload = dict(payload or {})
    try:
        if (
            source_payload.get("_nomination_context") == "additional_assessment"
            or source_payload.get("_additional_external_examiner_nomination_render_version")
        ):
            refreshed_payload = build_additional_external_examiner_nomination_payload(project, source_payload)
        else:
            refreshed_payload = build_external_examiner_nomination_payload(project, source_payload)
        for key, existing_value in source_payload.items():
            if str(existing_value or "").strip() and not str(refreshed_payload.get(key) or "").strip():
                refreshed_payload[key] = existing_value
        payload = refreshed_payload
    except Exception:
        current_app.logger.exception("Unable to refresh external examiner nomination payload before Word render")
        payload = source_payload

    entries, contents, root = _docx_read_template(template_path)

    def value(*keys, default=""):
        return _docx_single_line(_docx_first_value(payload, *keys, default=default))

    def text_part(text):
        return ("text", str(text or ""))

    def tab_part():
        return ("tab", "")

    def field_text(field_value, prefix=" "):
        field_value = str(field_value or "").strip()
        return f"{prefix}{field_value}" if field_value else prefix

    def set_parts(paragraph_index, run_index, parts):
        _docx_set_indexed_run_parts(root, paragraph_index, run_index, parts)

    def paragraph_at(paragraph_index):
        paragraphs = root.findall(".//w:p", _DOCX_NS)
        if 0 <= paragraph_index < len(paragraphs):
            return paragraphs[paragraph_index]
        return None

    def set_paragraph_tab_stop(paragraph_index, position):
        paragraph = paragraph_at(paragraph_index)
        if paragraph is None:
            return
        paragraph_props = paragraph.find("w:pPr", _DOCX_NS)
        if paragraph_props is None:
            paragraph_props = ET.Element(_docx_tag("pPr"))
            paragraph.insert(0, paragraph_props)
        tabs = paragraph_props.find("w:tabs", _DOCX_NS)
        if tabs is None:
            tabs = ET.SubElement(paragraph_props, _docx_tag("tabs"))
        for tab in list(tabs):
            tabs.remove(tab)
        tab = ET.SubElement(tabs, _docx_tag("tab"))
        tab.set(_docx_tag("val"), "left")
        tab.set(_docx_tag("pos"), str(position))

    def set_text(paragraph_index, run_index, text):
        set_parts(paragraph_index, run_index, [text_part(text)])

    def set_text_then_tab(paragraph_index, run_index, text):
        set_parts(paragraph_index, run_index, [text_part(text), tab_part()])

    def set_field_text(paragraph_index, run_index, field_value):
        if str(field_value or "").strip():
            set_text(paragraph_index, run_index, field_text(field_value))

    def set_field_text_then_tab(paragraph_index, run_index, field_value):
        if str(field_value or "").strip():
            set_text_then_tab(paragraph_index, run_index, field_text(field_value))

    def set_tab_then_text(paragraph_index, run_index, text):
        parts = [tab_part()]
        if str(text or "").strip():
            parts.append(text_part(text))
        set_parts(paragraph_index, run_index, parts)

    def aligned_value(value):
        value = str(value or "").strip()
        return f" {value}" if value else ""

    def set_aligned_two_field_row(paragraph_index, left_label, left_value, right_label, right_value):
        set_paragraph_tab_stop(paragraph_index, 4320)
        _docx_set_indexed_paragraph_layout_runs(
            root,
            paragraph_index,
            [
                ("text", left_label, "normal"),
                ("text", aligned_value(left_value), "normal"),
                ("tab", "", "normal"),
                ("text", right_label, "normal"),
                ("text", aligned_value(right_value), "normal"),
            ],
        )

    def run_text(run):
        return "".join(node.text or "" for node in run.findall("w:t", _DOCX_NS))

    def run_has_tab(run):
        return run.find("w:tab", _DOCX_NS) is not None

    def fill_after_label(paragraph_index, field_value):
        field_value = str(field_value or "").strip()
        if not field_value:
            return
        _, runs = _docx_paragraph_runs_at(root, paragraph_index)
        target_index = None
        target_is_tab = False
        for index, run in enumerate(runs[1:], start=1):
            text = run_text(run)
            if run_has_tab(run):
                target_index = index
                target_is_tab = True
                break
            if text and not text.strip():
                target_index = index
                break
        if target_index is None:
            _docx_append_run_after(root, paragraph_index, len(runs) - 1, [text_part(field_text(field_value))])
            return
        parts = [text_part(field_text(field_value))]
        if target_is_tab:
            parts = [tab_part(), text_part(field_text(field_value))]
        set_parts(paragraph_index, target_index, parts)

    degree_registered = value("current_degree_registered", default="MBA Master of Business Administration")
    qualification_description = value("qualification_description")
    study_type = value("study_type") or qualification_description or "Minor dissertation"

    set_field_text_then_tab(10, 4, value("student_initials_surname"))
    set_field_text(10, 6, value("student_number"))
    set_text(12, 6, degree_registered)
    for paragraph_run in (7, 8, 9):
        set_text(12, paragraph_run, "")
    set_field_text(13, 1, qualification_description)
    set_text(15, 0, study_type)
    set_field_text(16, 4, value("project_title"))
    set_aligned_two_field_row(
        17,
        "SUPERVISOR:",
        value("supervisor_name"),
        "DEPARTMENT:",
        value("supervisor_department", default="Johannesburg Business School"),
    )
    set_aligned_two_field_row(
        18,
        "PHONE/CELL PHONE:",
        value("supervisor_phone"),
        "EMAIL ADDRESS:",
        value("supervisor_email"),
    )
    set_aligned_two_field_row(
        20,
        "CO-SUPERVISOR:",
        value("co_supervisor_name"),
        "DEPARTMENT:",
        value("co_supervisor_department"),
    )
    set_aligned_two_field_row(
        21,
        "PHONE/CELL PHONE:",
        value("co_supervisor_phone"),
        "EMAIL ADDRESS:",
        value("co_supervisor_email"),
    )

    assessor_field_rows = {
        "assessor_1": (25, 26, 27, 28, 30, 31, 32, 33, 34, 35, 36),
        "assessor_2": (38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48),
        "standby": (52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62),
    }
    field_names = (
        "name",
        "qualification",
        "affiliation",
        "address",
        "telephone",
        "cell",
        "email",
        "students_supervised",
        "current_university_affiliation",
        "publication_count",
        "international",
    )
    for prefix, rows in assessor_field_rows.items():
        if prefix == "standby" and not value("standby_assessor_name", "standby_name"):
            continue
        key_prefix = "standby" if prefix == "standby" else prefix
        for paragraph_index, field_name in zip(rows, field_names):
            field_value = value(f"{key_prefix}_{field_name}")
            if prefix == "standby" and not field_value:
                field_value = value(f"standby_assessor_{field_name}")
            fill_after_label(paragraph_index, field_value)

    set_tab_then_text(64, 1, field_text(value("supervisor_signature_name")))
    set_parts(64, 7, [tab_part(), text_part(f"DATE: {_docx_format_date(value('supervisor_signature_date'))}".rstrip())])
    set_tab_then_text(67, 1, field_text(value("hod_signature_name")))
    set_parts(67, 5, [tab_part(), text_part(f"DATE: {_docx_format_date(value('hod_signature_date'))}".rstrip())])
    set_tab_then_text(70, 1, field_text(value("executive_dean_signature_name")))
    set_parts(70, 6, [tab_part(), text_part(f"DATE: {_docx_format_date(value('executive_dean_signature_date'))}".rstrip())])
    _docx_append_paragraph_signature_image(root, contents, 64, payload, "supervisor_signature_name")
    _docx_append_paragraph_signature_image(root, contents, 67, payload, "hod_signature_name")
    _docx_append_paragraph_signature_image(root, contents, 70, payload, "executive_dean_signature_name")
    return _docx_write_template(entries, contents, root)


def generate_form_submission_word_bytes(project, form_type, payload):
    payload = decrypt_sensitive_payload_fields(payload)
    if str(form_type or "") == "jbs5":
        template_bytes = _generate_jbs5_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "supervisor_agreement":
        template_bytes = _generate_supervisor_agreement_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "jbs10":
        template_bytes = _generate_jbs10_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "intent_to_submit":
        template_bytes = _generate_intent_to_submit_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "corrections_response":
        template_bytes = _generate_corrections_response_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "jbs1_declaration":
        template_bytes = _generate_jbs1_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "affidavit":
        template_bytes = _generate_affidavit_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "plagiarism_declaration":
        template_bytes = _generate_plagiarism_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == "ai_declaration_form":
        template_bytes = _generate_tii_ai_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") in {"external_examiner_nomination", "additional_external_examiner_nomination"}:
        template_bytes = _generate_external_examiner_nomination_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "") == assessment_summary_doc_type():
        template_bytes = _generate_assessment_summary_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "").startswith("assessor_temp_appointment_"):
        template_bytes = _generate_assessor_temp_appointment_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "").startswith("assessor_temp_claim_"):
        template_bytes = _generate_assessor_temp_claim_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "").startswith("assessment_result_"):
        template_bytes = _generate_capstone_evaluation_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    if str(form_type or "").startswith("assessor_report_"):
        template_bytes = _generate_capstone_assessor_report_form_1_template_word_bytes(project, payload)
        if template_bytes:
            return template_bytes
    html = build_form_display_html(project, form_type, payload)
    if not html:
        raise RuntimeError(f"Unable to render Word document HTML for {form_type}.")
    return html_to_word_document_bytes(html, title=document_label(form_type))


def _render_html_to_pdf_bytes(html):
    browsers = _browser_pdf_executables()
    if not browsers:
        return None

    with tempfile.TemporaryDirectory(prefix="mba_form_pdf_", ignore_cleanup_errors=True) as temp_dir:
        temp_path = Path(temp_dir)
        html_path = temp_path / "form.html"
        pdf_path = temp_path / "form.pdf"
        html_path.write_text(html, encoding="utf-8")
        browser_failures = []
        for browser_index, browser in enumerate(browsers, start=1):
            profile_dir = temp_path / f"profile_{browser_index}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            commands = [
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-gpu-sandbox",
                    "--in-process-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-crash-reporter",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--allow-file-access-from-files",
                    "--disable-sync",
                    "--disable-features=Crashpad,OptimizationGuideModelDownloading,OptimizationHintsFetching,MediaRouter",
                    "--no-pdf-header-footer",
                    f"--user-data-dir={profile_dir}",
                    f"--print-to-pdf={pdf_path}",
                    html_path.resolve().as_uri(),
                ],
                [
                    browser,
                    "--headless",
                    "--disable-gpu",
                    "--disable-gpu-compositing",
                    "--disable-gpu-sandbox",
                    "--in-process-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-crash-reporter",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--allow-file-access-from-files",
                    "--disable-sync",
                    "--disable-features=Crashpad,OptimizationGuideModelDownloading,OptimizationHintsFetching,MediaRouter",
                    "--no-pdf-header-footer",
                    f"--user-data-dir={profile_dir}",
                    f"--print-to-pdf={pdf_path}",
                    html_path.resolve().as_uri(),
                ],
            ]

            for command in commands:
                try:
                    result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
                except subprocess.TimeoutExpired as exc:
                    browser_failures.append(
                        {
                            "browser": browser,
                            "returncode": "timeout",
                            "stderr": str(exc),
                            "stdout": "",
                        }
                    )
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except OSError:
                            pass
                    break
                if result.returncode == 0 and pdf_path.exists() and pdf_path.stat().st_size > 0:
                    return pdf_path.read_bytes()
                browser_failures.append(
                    {
                        "browser": browser,
                        "returncode": result.returncode,
                        "stderr": (result.stderr or "").strip(),
                        "stdout": (result.stdout or "").strip(),
                    }
                )
                if pdf_path.exists():
                    try:
                        pdf_path.unlink()
                    except OSError:
                        pass
        for failure in browser_failures:
            current_app.logger.warning(
                "HTML form PDF render failed via %s (exit=%s): %s",
                os.path.basename(failure["browser"]),
                failure["returncode"],
                failure["stderr"] or failure["stdout"] or "no browser output",
            )
        return None


def _render_html_form_pdf_bytes(project, form_type, payload):
    fragment = _build_html_form_fragment(project, form_type, payload, logo_mode="file")
    if not fragment:
        return None
    html = (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<style>{_form_print_styles()}</style></head>"
        f"<body class=\"mba-print-body\">{fragment}</body></html>"
    )
    pdf_bytes = _render_html_to_pdf_bytes(html)
    if not pdf_bytes:
        return None
    return _stamp_generated_pdf_bytes(pdf_bytes, _generated_form_pdf_marker(form_type))


def _build_pdf_from_page_streams(page_streams, marker=None):
    page_count = len(page_streams)
    font_object_id = 3 + (page_count * 2)
    bold_font_object_id = font_object_id + 1
    page_object_ids = [3 + (index * 2) for index in range(page_count)]
    kids = " ".join(f"{object_id} 0 R" for object_id in page_object_ids)

    objects = [
        (1, "<< /Type /Catalog /Pages 2 0 R >>"),
        (2, f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>"),
    ]

    for index, stream in enumerate(page_streams):
        page_object_id = 3 + (index * 2)
        content_object_id = page_object_id + 1
        if isinstance(stream, str):
            stream = stream.encode("latin-1")
        objects.append(
            (
                page_object_id,
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Contents {content_object_id} 0 R "
                    f"/Resources << /Font << /F1 {font_object_id} 0 R /F2 {bold_font_object_id} 0 R >> >> >>"
                ),
            )
        )
        objects.append(
            (
                content_object_id,
                b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
            )
        )

    objects.append((font_object_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    objects.append((bold_font_object_id, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>"))

    pdf = bytearray(b"%PDF-1.4\n")
    if marker:
        marker_text = str(marker).encode("latin-1", "replace").decode("latin-1")
        marker_text = marker_text.replace("\r", " ").replace("\n", " ")
        pdf.extend(f"% {marker_text}\n".encode("latin-1"))
    offsets = {0: 0}
    for object_id, body in objects:
        offsets[object_id] = len(pdf)
        if isinstance(body, str):
            body = body.encode("latin-1")
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(body)
        pdf.extend(b"\nendobj\n")

    startxref = len(pdf)
    max_object_id = max(object_id for object_id, _ in objects)
    pdf.extend(f"xref\n0 {max_object_id + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_object_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {max_object_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{startxref}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(pdf)


FORM_PDF_DEFINITIONS = {
    "jbs5": {
        "title": "Form JBS5 - Registration of Title / Amendment of Title / Amendment of Supervisor(s)",
        "action": "Student Action",
        "intro": "Johannesburg Business School. The report is to be included in an agenda and must be typed.",
        "sections": [
            {
                "title": "Form Selection",
                "checkboxes": [
                    ("register_title_supervisors", "Registration of Title / Supervisor(s) (Section B)"),
                    ("amend_title", "Amendment of Title (Section C)"),
                    ("amend_supervisors", "Amendment of Supervisor(s) (Section D)"),
                ],
            },
            {
                "title": "Section A: Information about the student",
                "fields": [
                    ("surname", "Surname", "text"),
                    ("student_title", "Title (e.g. Mr / Mrs)", "text"),
                    ("student_initials", "Initials(s)", "text"),
                    ("date_of_first_registration", "Date of first registration", "date"),
                    ("student_number", "Student Number", "text"),
                    ("qualification", "Qualification (e.g. MBA)", "text"),
                    ("discipline", "Discipline / Qualifier", "text"),
                    ("study_type", "Study Type", "text"),
                    ("sdg_focus", "Sustainable Development Goals (SDGs)", "text"),
                    ("is_4ir_research", "Is this a 4IR research?", "text"),
                ],
            },
            {
                "title": "Section B: Registration title",
                "fields": [
                    ("research_title", "Proposed title", "textarea"),
                    ("proposed_supervisor", "Proposed supervisor (A)", "text"),
                    ("proposed_co_supervisors", "Proposed co-supervisor(s) (D)", "text"),
                ],
            },
            {
                "title": "Section C: Amendment of title",
                "fields": [
                    ("previous_title", "Previously approved title", "textarea"),
                    ("amended_title", "Proposed amendment to title", "textarea"),
                ],
            },
            {
                "title": "Section D: Amendment of supervisor(s)",
                "fields": [
                    ("previous_supervisor", "Previously approved supervisor (B)", "text"),
                    ("previous_co_supervisors", "Previously approved co-supervisor(s) (E)", "text"),
                    ("amended_supervisor", "Amended supervisor (C)", "text"),
                    ("amended_co_supervisors", "Amended co-supervisor(s) (F)", "text"),
                ],
            },
            {
                "title": "Section E: Declaration of research focus",
                "paragraph": "The proposed study field/title and/or amendment to study field/title is discipline specific in that it falls within the discipline/qualifier indicated above.",
                "fields": [
                    ("has_secondary_focus", "Is there a secondary focus?", "text"),
                    ("secondary_focus", "Secondary focus, if any", "text"),
                ],
            },
        ],
    },
    "jbs10": {
        "title": "Form JBS10 - Registration and/or Amendment of Title / Supervisor(s) / Assessor(s)",
        "action": "Student Action",
        "intro": "Johannesburg Business School registration and amendment form for title, supervisors, and assessors.",
        "sections": [
            {
                "title": "Section A: Student information",
                "fields": [
                    ("surname", "Surname", "text"),
                    ("student_title", "Title", "text"),
                    ("student_initials", "Initials(s)", "text"),
                    ("student_number", "Student Number", "text"),
                    ("student_is_staff_member", "Is this student a UJ staff member?", "text"),
                    ("qualification", "Qualification", "text"),
                    ("ethical_clearance_number", "Ethical clearance number", "text"),
                    ("study_type", "Study Type", "text"),
                ],
            },
            {
                "title": "Section B: Registration / Amendment of title",
                "fields": [
                    ("research_title", "Proposed / Approved title", "textarea"),
                    ("is_4ir_research", "Is this a 4IR research?", "text"),
                    ("previous_title", "Previously approved title", "textarea"),
                    ("amended_title", "Proposed amendment to title", "textarea"),
                ],
            },
            {
                "title": "Section C: Registration / Amendment of supervisor(s)",
                "fields": [
                    ("supervisor_name", "Supervisor: Title, initials, surname, affiliation", "text"),
                    ("supervisor_staff_number", "Supervisor staff number", "text"),
                    ("co_supervisor_1", "Co-supervisor I", "text"),
                    ("co_supervisor_1_staff_number", "Co-supervisor I staff number", "text"),
                    ("co_supervisor_2", "Co-supervisor II", "text"),
                    ("co_supervisor_2_staff_number", "Co-supervisor II staff number", "text"),
                    ("previous_supervisor_lineup", "Previously approved supervisor(s)", "text"),
                    ("amended_supervisor_lineup", "Amended supervisor(s)", "text"),
                ],
            },
            {
                "title": "Section D: Nomination / Amendment of assessors",
                "fields": [
                    ("internal_assessor_name", "Internal assessor", "text"),
                    ("assessor_1_name", "Assessor 1", "text"),
                    ("assessor_1_staff_number", "Assessor 1 staff number", "text"),
                    ("assessor_1_qualification", "Assessor 1 highest academic qualification", "text"),
                    ("assessor_1_email", "Assessor 1 e-mail address", "text"),
                    ("assessor_2_name", "Assessor 2", "text"),
                    ("assessor_2_staff_number", "Assessor 2 staff number", "text"),
                    ("assessor_2_qualification", "Assessor 2 highest academic qualification", "text"),
                    ("assessor_2_email", "Assessor 2 e-mail address", "text"),
                    ("assessor_3_name", "Assessor 3", "text"),
                    ("assessor_3_staff_number", "Assessor 3 staff number", "text"),
                    ("assessor_3_qualification", "Assessor 3 highest academic qualification", "text"),
                    ("assessor_3_email", "Assessor 3 e-mail address", "text"),
                    ("assessor_4_name", "Assessor 4", "text"),
                    ("assessor_4_staff_number", "Assessor 4 staff number", "text"),
                    ("assessor_4_qualification", "Assessor 4 highest academic qualification", "text"),
                    ("assessor_4_email", "Assessor 4 e-mail address", "text"),
                ],
            },
        ],
    },
    "supervisor_agreement": {
        "title": "Student / Supervisor Agreement",
        "action": "Supervisor Action",
        "intro": "This document records the student-supervisor expectations and responsibilities for postgraduate supervision at the Johannesburg Business School.",
        "sections": [
            {
                "title": "Agreement Parties",
                "fields": [
                    ("student_name", "Full name(s) and surname of student", "text"),
                    ("student_number", "Student number", "text"),
                    ("degree", "Degree", "text"),
                    ("student_address", "Address", "text"),
                    ("student_postal_code", "Postal code", "text"),
                    ("research_title", "Research title", "text"),
                    ("supervisor_full_name", "Full name(s) and surname of supervisor", "text"),
                    ("department", "School / Department", "text"),
                    ("affiliation", "University / Affiliation", "text"),
                    ("position", "Position / Designation", "text"),
                    ("co_supervisor_full_name", "Full name(s) and surname of co-supervisor", "text"),
                    ("co_supervisor_department", "Co-supervisor School / Department", "text"),
                ],
            },
            {
                "title": "Background and Understanding",
                "paragraph": "The basis of this agreement is to build a student-supervisor relationship for the duration of the research and study period based on mutual trust. The main focus of this agreement is the student-supervisor relationship and not the general relationship between the University of Johannesburg and the student or supervisor.",
            },
            {
                "title": "Student Responsibilities",
                "bullets": [
                    "Plan and implement the agreed research programme or project.",
                    "Successfully complete all the academic outputs of the study programme.",
                    "Write the research proposal within the time stipulated.",
                    "Prepare ethics documentation where applicable.",
                    "Attend to amendments or revisions required by supervisors or assessors.",
                    "Adhere at all times to academic integrity, plagiarism rules, and ethics requirements relating to the research work.",
                ],
            },
            {
                "title": "Supervisor Responsibilities",
                "bullets": [
                    "Clarify the respective roles of the supervisor and co-supervisor and communicate these clearly to the student.",
                    "Provide academic guidance to ensure the development of research skills and mastery of the field of specialization.",
                    "Meet with the student regularly to provide guidance, monitor progress, and recommend corrective measures where necessary.",
                    "Keep a written record of progress and provide timeous feedback.",
                    "Provide progress reports required by the University and its postgraduate structures.",
                    "Adhere at all times to academic integrity, plagiarism rules, and ethics requirements relating to the research work.",
                ],
            },
            {
                "title": "Acceptance",
                "fields": [("capacity_statement", "Statement of Capacity to Supervise", "textarea")],
                "paragraph": "The student and supervisor each confirm this agreement separately.",
                "checkbox_position": "right",
                "checkboxes": [
                    ("student_agreement_declaration", "Student confirms the supervisor agreement."),
                    ("supervisor_agreement_declaration", "Supervisor confirms the supervisor agreement."),
                ],
            },
        ],
    },
    "intent_to_submit": {
        "title": "Intent to Submit",
        "action": "Student Action",
        "intro": "Notify the MBA office of your intention to submit your Capstone Project. Your details have been pre-filled from your profile.",
        "sections": [
            {
                "title": "Student Details",
                "fields": [
                    ("full_name", "Full Name", "text"),
                    ("student_number", "Student Number", "text"),
                    ("email", "UJ Email", "text"),
                    ("programme", "Programme / Module", "text"),
                ],
            },
            {
                "title": "Submission Details",
                "fields": [
                    ("research_title", "Research Title", "text"),
                    ("supervisor_name", "Supervisor Name", "text"),
                    ("intended_date", "Intended Submission Date", "date"),
                    ("statement", "Statement / Additional Notes", "textarea"),
                ],
            },
        ],
    },
    "plagiarism_declaration": {
        "title": "Combined Plagiarism, Turnitin and AI Declaration",
        "action": "Student Action",
        "intro": "Complete the combined plagiarism declaration for the Capstone Project, including confirmation of the combined Turnitin-AI report.",
        "sections": [
            {
                "title": "Student and Submission Details",
                "fields": [
                    ("full_name", "Full Name", "text"),
                    ("student_number", "Student Number", "text"),
                    ("email", "UJ Email", "text"),
                    ("programme", "Programme / Module", "text"),
                    ("course_name", "Course", "text"),
                    ("module_title", "Module", "text"),
                    ("assessment_title", "Capstone Project Title", "textarea"),
                    ("module_lead", "Supervisor / Module Lead", "text"),
                    ("submission_date", "Submission Date", "date"),
                    ("due_date", "Due Date", "date"),
                ],
            },
            {
                "title": "Student Declaration",
                "paragraph": "By submitting this form, I confirm the following:",
                "bullets": [
                    "I understand plagiarism is presenting someone else's ideas as my own.",
                    "I have properly acknowledged and referenced all sources used in this submission.",
                    "This Capstone Project submission is my own original work and I have not allowed anyone else to copy it.",
                    "I understand that plagiarism and duplicate plagiarism are serious academic offences.",
                    "The combined Turnitin-AI report submitted with this Capstone Project belongs to this submission.",
                    "Any generative AI assistance has been acknowledged where relevant.",
                ],
                "fields": [
                    ("signature_name", "Full Name / Electronic Signature", "text"),
                    ("signature_date", "Declaration Date", "date"),
                ],
                "checkbox_position": "right",
                "checkboxes": [("plagiarism_consent", "I confirm the combined plagiarism, Turnitin and AI declaration above.")],
            },
            {
                "title": "AI Declaration",
                "fields": [
                    ("ai_tools_used", "Generative AI Tool(s) Used", "textarea"),
                    ("ai_use_purpose", "Purpose", "textarea"),
                    ("ai_use_motivation", "Motivation", "textarea"),
                ],
                "paragraph": "Acknowledge any generative AI tools used as required by the JBS declaration template.",
            },
        ],
    },
    "ai_declaration_form": {
        "title": "TII AI Declaration (JBS)",
        "action": "Student Action",
        "intro": "Declare any generative AI support used in preparing your Capstone Project submission. Your student and Capstone Project details have been pre-filled where available.",
        "sections": [
            {
                "title": "Student and Submission Details",
                "fields": [
                    ("full_name", "Full Name", "text"),
                    ("student_number", "Student Number", "text"),
                    ("email", "UJ Email", "text"),
                    ("course_name", "Course / Qualification", "text"),
                    ("module_title", "Module / Programme", "text"),
                    ("assessment_title", "Capstone Project Title", "textarea"),
                    ("lecturer_name", "Supervisor / Lecturer", "text"),
                    ("due_date", "Submission Date", "date"),
                ],
            },
            {
                "title": "Generative AI Disclosure",
                "paragraph": "List each generative AI tool used. If no generative AI tool was used, write 'None used' in the tools field.",
                "fields": [
                    ("ai_tools_used", "Generative AI Tool(s) Used", "textarea"),
                    ("ai_use_purpose", "Purpose of Use", "textarea"),
                    ("ai_use_motivation", "Motivation for Use", "textarea"),
                ],
            },
            {
                "title": "Declaration",
                "paragraph": "By submitting this form, I confirm that the document was written by me, that any generative AI use has been disclosed, and that I understand misuse of AI tools may amount to academic misconduct.",
                "fields": [
                    ("signature_name", "Full Name / Electronic Signature", "text"),
                    ("signature_date", "Declaration Date", "date"),
                ],
                "checkbox_position": "right",
                "checkboxes": [("ai_consent", "I confirm the AI declaration above.")],
            },
        ],
    },
    "affidavit": {
        "title": "JBS 2 Affidavit",
        "action": "Student Action",
        "intro": "Complete the affidavit that accompanies your Capstone Project submission. Your student and Capstone Project details have been pre-filled where available.",
        "sections": [
            {
                "title": "Student Details",
                "fields": [
                    ("full_name", "Full Name and Surname", "text"),
                    ("student_id_number", "ID Number", "text"),
                    ("student_number", "Student Number", "text"),
                    ("qualification", "Qualification", "text"),
                    ("work_type", "Research Output Type", "text"),
                    ("research_title", "Capstone Project Title", "textarea"),
                ],
            },
            {
                "title": "Affidavit Declaration",
                "paragraph": "I declare that this academic work complies with the University of Johannesburg plagiarism policy and that the submitted work is authentic and original unless clearly indicated otherwise and fully referenced.",
                "bullets": [
                    "I understand that plagiarism is a serious offence.",
                    "I understand that false declaration may amount to perjury.",
                    "I confirm that all quoted or referenced material has been properly acknowledged.",
                ],
                "fields": [
                    ("signing_location", "Signed At", "text"),
                    ("affidavit_date", "Affidavit Date", "date"),
                    ("signature_name", "Full Name / Electronic Signature", "text"),
                ],
                "checkbox_position": "right",
                "checkboxes": [("affidavit_consent", "I confirm the affidavit above.")],
            },
        ],
    },
    "jbs1_declaration": {
        "title": "JBS 1 Declaration",
        "action": "Student Action",
        "intro": "Complete the JBS 1 student declaration that accompanies your Capstone Project submission. Your student and Capstone Project details have been pre-filled where available.",
        "sections": [
            {
                "title": "Section A: Student Information",
                "fields": [
                    ("surname", "Surname", "text"),
                    ("student_title", "Title", "text"),
                    ("student_initials", "Initials", "text"),
                    ("student_id_number", "ID Number", "text"),
                    ("student_number", "Student Number", "text"),
                    ("ethical_clearance_number", "Ethical Clearance Number", "text"),
                    ("qualification", "Qualification", "text"),
                    ("email", "Email Address", "text"),
                    ("contact", "Cell Number", "text"),
                ],
            },
            {
                "title": "Section B: Student Declaration",
                "paragraph": "I hereby declare that this research submission for the qualification above, with the approved title below, is my own work apart from the sources recognised.",
                "fields": [
                    ("work_type", "Research Output Type", "text"),
                    ("research_title", "Approved Research Title", "textarea"),
                ],
                "bullets": [
                    "This work has not previously been submitted to any other university for any degree.",
                    "The work has been language edited by a professional external language editor.",
                    "I adhered to the ethical obligations and principles of research ethics prescribed by JBS during all phases of the research process.",
                ],
            },
            {
                "title": "Student Signature",
                "fields": [
                    ("signature_name", "Signature of Student / Electronic Signature", "text"),
                    ("signature_date", "Date", "date"),
                ],
                "checkbox_position": "right",
                "checkboxes": [("jbs1_consent", "I confirm the JBS 1 declaration above.")],
            },
            {
                "title": "Supervisor and Office Use",
                "paragraph": "After student submission, JBS 1 is routed to the supervisor for signature and then to MBA Admin for the Program Manager signature.",
            },
        ],
    },
}


ASSESSOR_FORM_DEFINITION = {
    "title": "Capstone Assessment Result Summary",
    "action": "Assessor Action",
    "intro": "Record the capstone examination outcome and final mark. The full assessor narrative is filed separately in the assessor report forms.",
    "sections": [
        {
            "title": "Assessor Details",
            "fields": [
                ("assessor_name", "Assessor Name", "text"),
                ("affiliation", "Institutional Affiliation", "text"),
                ("assessor_email", "Email Address", "text"),
                ("assessor_contact", "Contact Number(s)", "text"),
            ],
        },
        {
            "title": "Candidate Details",
            "fields": [
                ("student_name", "Name of Candidate", "text"),
                ("student_number", "Student Number", "text"),
                ("research_title", "Title of Research", "textarea"),
            ],
        },
        {
            "title": "Examination Outcome",
            "fields": [
                ("recommendation", "Recommended Examination Outcome", "textarea"),
                ("consent_name_disclosure", "May your name be divulged to a successful candidate?", "text"),
                ("grade", "Final Mark", "text"),
            ],
        },
        {
            "title": "Assessor Signature",
            "fields": [
                ("assessor_signature_name", "External Assessor Signature / Full Name", "text"),
                ("certification_date", "Date", "date"),
            ],
        },
    ],
}

ASSESSOR_REPORT_FORM_DEFINITION = {
    "title": "Capstone Assessors Report Form 1",
    "action": "Assessor Action",
    "intro": "Complete the examiner's report on the capstone research report. The same information is used to generate the official capstone report form submitted with your result.",
    "sections": [
        {
            "title": "Candidate and Assessor Details",
            "fields": [
                ("student_name", "Name of Candidate", "text"),
                ("student_number", "Student No.", "text"),
                ("research_title", "Title of Research", "textarea"),
                ("assessor_name", "Name of External Assessor (in full)", "text"),
                ("affiliation", "Institutional Affiliation", "text"),
                ("assessor_email", "Email Address", "text"),
                ("assessor_contact", "Contact Number(s)", "text"),
                ("assessor_signature_name", "External Assessor Signature / Full Name", "text"),
                ("certification_date", "Date", "date"),
            ],
        },
        {
            "title": "Examiner's Recommendations",
            "paragraph": "Please answer the recommendation items in line with the capstone examination outcome.",
            "fields": [
                ("recommendation", "Recommended Examination Outcome", "textarea"),
                ("consent_name_disclosure", "May your name be divulged to a successful candidate?", "text"),
                ("grade", "Final Mark", "text"),
            ],
        },
        {
            "title": "Detailed Report Guidance",
            "paragraph": "Please provide a detailed report giving attention to the issues below and any other points you would like to highlight.",
            "bullets": [
                "Is the research topic appropriate?",
                "Does it address a management, business, organisational, societal, relevant, and/or professional area?",
                "Is the scope of research sufficiently deep and broad?",
                "Are the statements of research problem, objective, research questions, propositions, or hypotheses clear and unambiguous?",
                "Does the report show familiarity with and integration of the appropriate literature?",
                "Is the research methodology acceptable in terms of design, sampling, instrument construction, validity, and reliability?",
                "Has the appropriate quantitative and/or qualitative analysis been used objectively?",
                "What is the quality and validity of the discussion and interpretation of the results?",
                "Does the report conform to the expected master's research report structure?",
                "Is the document appropriately referenced?",
                "Does the literary style conform to correct English usage and academic writing?",
            ],
        },
        {
            "title": "Examiner's Detailed Report",
            "fields": [
                ("written_assessment", "Detailed Report", "textarea"),
            ],
        },
    ],
}

ASSESSOR_NARRATIVE_FORM_DEFINITION = {
    "title": "Capstone Assessors Report Form 2",
    "action": "Assessor Action",
    "intro": "Companion narrative report for the capstone examination. The final mark and recommendation are recorded in the result summary.",
    "sections": [
        {
            "title": "Candidate and Assessor Details",
            "fields": [
                ("student_name", "Name of Candidate", "text"),
                ("student_number", "Student No.", "text"),
                ("research_title", "Title of Research", "textarea"),
                ("assessor_name", "Name of External Assessor (in full)", "text"),
                ("affiliation", "Institutional Affiliation", "text"),
                ("assessor_email", "Email Address", "text"),
                ("assessor_contact", "Contact Number(s)", "text"),
            ],
        },
        {
            "title": "Assessor's Narrative Report",
            "paragraph": "Narrative report copy. The companion result summary records the examination outcome, final mark, and recommendation.",
            "fields": [
                ("written_assessment", "Narrative Report", "textarea"),
            ],
        },
        {
            "title": "Assessor Signature",
            "fields": [
                ("assessor_signature_name", "External Assessor Signature / Full Name", "text"),
                ("certification_date", "Date", "date"),
            ],
        },
    ],
}

ASSESSOR_PROFILE_FORM_DEFINITION = {
    "title": "External Examiner Nomination Form",
    "action": "Assessor Action",
    "intro": "Legacy per-assessor nomination record retained for previously submitted documents.",
    "sections": [
        {
            "title": "Project and Assessor Details",
            "fields": [
                ("project_title", "Capstone Project Title", "text"),
                ("student_name", "Student", "text"),
                ("student_number", "Student Number", "text"),
                ("slot_label", "Assessor Slot", "text"),
                ("assessor_name", "Assessor Name", "text"),
                ("assessor_email", "Email Address", "text"),
                ("assessor_contact", "Contact Number", "text"),
            ],
        },
        {
            "title": "Institutional Profile",
            "fields": [
                ("assessor_department", "Department", "text"),
                ("assessor_position", "Position", "text"),
                ("assessor_affiliation", "Affiliation", "text"),
                ("highest_qualification", "Highest Qualification", "text"),
                ("academic_experience_years", "Academic Experience (Years)", "text"),
                ("assessor_address", "Address", "textarea"),
            ],
        },
        {
            "title": "Assessment Capacity and Experience",
            "fields": [
                ("current_student_load", "Current Active Student Load", "text"),
                ("students_supervised_total", "Students Supervised", "text"),
                ("students_assessed_total", "Students Assessed / Examined", "text"),
                ("approved_before", "Approved MBA Projects Before", "text"),
                ("international_assessor", "Available for International Assessment", "text"),
            ],
        },
        {
            "title": "Research and Expertise Profile",
            "fields": [
                ("skills", "Areas of Expertise", "textarea"),
                ("research_themes", "Research Themes", "textarea"),
                ("research_interests", "Research Interests", "textarea"),
                ("research_disciplines", "Research Disciplines", "textarea"),
            ],
        },
        {
            "title": "Publications and Supporting Evidence",
            "fields": [
                ("publication_count", "Publication Count", "text"),
                ("selected_publications", "Selected Publications", "textarea"),
                ("scholarly_profile_links", "ORCID / Google Scholar / Research Links", "textarea"),
                ("cv_filename", "Uploaded Curriculum Vitae", "text"),
            ],
        },
        {
            "title": "Declaration",
            "fields": [
                ("assessor_signature_name", "Full Name / Electronic Signature", "text"),
                ("assessor_profile_date", "Date", "date"),
            ],
            "checkbox_position": "right",
            "checkboxes": [
                ("assessor_profile_declaration", "I confirm the assessor profile information above is true and current.")
            ],
        },
    ],
}

ASSESSOR_BANKING_FORM_DEFINITION = {
    "title": "Assessor Banking Details",
    "action": "Assessor Action",
    "intro": "Provide the banking details that MBA Admin should use for assessor payment processing.",
    "sections": [
        {
            "title": "Assessor Banking Details",
            "fields": [
                ("assessor_name", "Assessor Name", "text"),
                ("assessor_email", "Email", "text"),
                ("bank_account_holder", "Account Holder Name", "text"),
                ("bank_name", "Bank Name", "text"),
                ("bank_branch_name", "Branch Name", "text"),
                ("bank_branch_code", "Branch Code", "text"),
                ("bank_account_number", "Account Number", "text"),
                ("bank_account_type", "Account Type", "text"),
                ("bank_swift_code", "SWIFT / BIC Code", "text"),
                ("bank_tax_or_id_number", "Tax Number / ID Number", "text"),
            ],
        },
        {
            "title": "Declaration",
            "paragraph": "I confirm that the banking details supplied above are accurate and may be used by MBA Admin for assessor payment processing.",
            "checkboxes": [("banking_declaration", "I confirm the above banking details are correct.")],
        },
    ],
}

ASSESSOR_TEMP_APPOINTMENT_FORM_DEFINITION = {
    "title": "Temporary Appointments Form",
    "action": "Assessor Action",
    "intro": "Complete the temporary appointment details required before you can accept the assessor invitation. Your profile details have been pre-filled where available.",
    "sections": [
        {
            "title": "Employment Status",
            "fields": [
                ("new_employee", "New Employee", "text"),
                ("employee_number", "Employee Number", "text"),
                ("employed_at_uj", "Employed at UJ", "text"),
                ("uj_department_division", "If employed at UJ, Department / Division", "text"),
                ("appointed_as", "Appointed As", "text"),
            ],
        },
        {
            "title": "Personal Particulars of Employee",
            "fields": [
                ("assessor_surname", "Surname", "text"),
                ("assessor_title", "Title", "text"),
                ("assessor_first_names", "First Names", "text"),
                ("identity_passport_number", "Identity / Passport Number", "text"),
                ("date_of_birth", "Date of Birth", "date"),
                ("work_visa_number", "Work Visa Number", "text"),
                ("gender", "Gender", "text"),
                ("marital_status", "Marital Status", "text"),
                ("sa_citizen", "South African Citizen", "text"),
                ("nationality", "Nationality", "text"),
                ("employed_outside_uj", "Employed Outside UJ", "text"),
                ("home_language", "Home Language", "text"),
                ("income_tax_number", "Income Tax Number", "text"),
                ("care_of_intermediary", "Care of Intermediary", "text"),
                ("home_address", "Home Address", "textarea"),
                ("postal_address", "Postal Address", "textarea"),
                ("home_postal_code", "Home Postal Code", "text"),
                ("postal_code", "Postal Code", "text"),
                ("home_tel", "Home Telephone", "text"),
                ("assessor_contact", "Cell / Mobile Phone", "text"),
                ("assessor_email", "Email Address", "text"),
                ("work_tel", "Work Telephone", "text"),
                ("disability_status", "Disability", "text"),
                ("disability_nature", "If yes, state nature", "textarea"),
                ("race", "Race", "text"),
            ],
        },
        {
            "title": "Qualification and Banking Details",
            "fields": [
                ("qualification_institution", "Highest Qualification Institution", "text"),
                ("highest_qualification", "Highest Qualification", "text"),
                ("qualification_awarded_date", "Awarded Date", "date"),
                ("qualification_status", "Qualification Status", "text"),
                ("bank_changed", "Banking Details Changed", "text"),
                ("bank_account_holder", "Account Holder Name", "text"),
                ("bank_name", "Bank Name", "text"),
                ("bank_branch_name", "Branch Name", "text"),
                ("bank_branch_code", "Branch Code", "text"),
                ("bank_account_number", "Account Number", "text"),
                ("bank_account_type", "Type of Account", "text"),
                ("bank_account_ownership", "Account Ownership", "text"),
            ],
        },
        {
            "title": "Temporary Appointment Details",
            "fields": [
                ("employment_group", "Employment Group", "text"),
                ("appointment_category", "Appointment Category", "text"),
                ("appointment_start_date", "Start Date", "date"),
                ("appointment_end_date", "End Date", "date"),
                ("temporary_employment_reason", "Reason for Temporary Employment", "textarea"),
                ("appointment_reason_other", "Other Reason (if applicable)", "text"),
                ("appointment_motivation", "Motivation", "textarea"),
                ("rate_per_month", "Rate Per Month", "text"),
                ("rate_per_hour", "Rate Per Hour", "text"),
                ("other_rate_basis", "Other Rate Basis", "text"),
                ("total_units", "Total Unit", "text"),
                ("actual_hours", "Actual Hours", "text"),
                ("full_cost_centre_string", "Full Cost Centre String", "text"),
                ("permanent_post_number", "Permanent Post Number", "text"),
                ("total_budget_for_appointment", "Total Budget For Appointment", "text"),
                ("conflict_of_interest_details", "Conflict(s) of Interest", "textarea"),
                ("employee_signature_name", "Employee Full Name / Signature", "text"),
                ("employee_signature_date", "Employee Signature Date", "date"),
            ],
            "checkbox_position": "right",
            "checkboxes": [("appointment_declaration", "I confirm the temporary appointment information above is complete and accurate.")],
        },
        {
            "title": "Approver Sections",
            "paragraph": "Primary line manager, HCM business partner, and secondary line manager sections are completed by the MBA office outside this assessor submission step.",
        },
    ],
}

ASSESSOR_TEMP_CLAIM_FORM_DEFINITION = {
    "title": "Temporary Appointment Claim Form",
    "action": "Assessor Action",
    "intro": "Complete the remuneration claim information required before you can accept the assessor invitation. Your profile details have been pre-filled where available.",
    "sections": [
        {
            "title": "Personal Particulars of Employee",
            "fields": [
                ("employed_at_uj", "Employed at UJ", "text"),
                ("employed_outside_uj", "Employed Outside UJ", "text"),
                ("faculty_division", "Faculty / Division", "text"),
                ("department_unit_centre", "Department / Unit / Centre", "text"),
                ("employee_number", "Employee Number", "text"),
                ("month_of_claim", "Month of Claim", "text"),
                ("assessor_surname", "Surname", "text"),
                ("assessor_title", "Title", "text"),
                ("assessor_first_names", "First Names", "text"),
                ("assessor_contact", "Cellphone / Mobile Number", "text"),
                ("assessor_email", "Email Address", "text"),
                ("alternate_contact_number", "Alternate Contact Number", "text"),
                ("alternate_email_address", "Alternate Email Address", "text"),
                ("requestor_extension", "Requestor Telephone Extension", "text"),
                ("requestor_email", "Requestor Email Address", "text"),
            ],
        },
        {
            "title": "Temporary Appointment Details",
            "fields": [
                ("appointment_start_date", "Start Date", "date"),
                ("appointment_end_date", "End Date", "date"),
                ("appointed_as", "Appointed As", "text"),
                ("claim_unit_basis", "Unit", "text"),
                ("rate_per_hour", "Rate", "text"),
                ("actual_hours", "Number of Hours Worked", "text"),
                ("full_cost_centre_string", "Full Cost String", "text"),
                ("appointed_against_permanent_position", "Appointed Against a Permanent Position", "text"),
                ("position_number", "If yes, Position Number", "text"),
                ("total_budget_for_appointment", "Total Budget For Appointment", "text"),
            ],
        },
        {
            "title": "Claim Details",
            "fields": [
                ("contract_eit_number", "Contract EIT Number", "text"),
                ("claim_total_units", "Total Units", "text"),
                ("claim_rate", "Rate", "text"),
                ("claim_currency", "Currency", "text"),
                ("amount_claimed", "Amount Claimed", "text"),
                ("claim_cost_centre_number", "Cost Centre Number", "text"),
                ("total_claimed", "Total Claimed", "text"),
            ],
        },
        {
            "title": "Banking Details",
            "fields": [
                ("bank_changed", "Banking Details Changed", "text"),
                ("bank_account_holder", "Account Holder Name", "text"),
                ("bank_name", "Bank Name", "text"),
                ("bank_branch_name", "Branch Name", "text"),
                ("bank_branch_code", "Branch Code", "text"),
                ("bank_account_number", "Account Number", "text"),
                ("bank_account_type", "Type of Account", "text"),
                ("bank_account_ownership", "Account Ownership", "text"),
                ("claim_signature_name", "Employee Full Name / Signature", "text"),
                ("claim_signature_date", "Employee Signature Date", "date"),
            ],
            "checkbox_position": "right",
            "checkboxes": [("claim_declaration", "I confirm the claim information above is complete and accurate.")],
        },
        {
            "title": "Approver Sections",
            "paragraph": "Line manager approval sections and payroll processing fields are completed by the MBA office outside this assessor submission step.",
        },
    ],
}


FORM_REQUIRED_FIELDS = {
    "jbs5": {"student_number", "research_title", "student_signature", "student_signature_date"},
    "jbs10": {"student_number", "research_title"},
    "supervisor_agreement": {"supervisor_full_name", "research_title"},
    "intent_to_submit": {
        "full_name",
        "student_number",
        "email",
        "contact",
        "qualification",
        "research_title",
        "supervisor_name",
        "intended_date",
        "signature_name",
        "signature_date",
    },
    "plagiarism_declaration": {
        "full_name",
        "student_number",
        "programme",
        "course_name",
        "module_title",
        "assessment_title",
        "submission_date",
        "due_date",
        "signature_name",
        "signature_date",
        "plagiarism_consent",
    },
    "ai_declaration_form": {
        "full_name",
        "student_number",
        "course_name",
        "module_title",
        "assessment_title",
        "due_date",
        "ai_tools_used",
        "signature_name",
        "signature_date",
        "ai_consent",
    },
    "affidavit": {
        "full_name",
        "student_id_number",
        "student_number",
        "qualification",
        "work_type",
        "research_title",
        "signing_location",
        "affidavit_date",
        "signature_name",
        "affidavit_consent",
    },
    "jbs1_declaration": {
        "surname",
        "student_title",
        "student_initials",
        "student_id_number",
        "student_number",
        "qualification",
        "email",
        "contact",
        "work_type",
        "research_title",
        "signature_name",
        "signature_date",
        "jbs1_consent",
    },
    "corrections_response": {
        "student_initials_surname",
        "student_number",
        "department",
        "supervisor_name",
        "research_title",
    },
}

FORM_READONLY_FIELDS = {
    "supervisor_agreement": {"student_name", "student_number", "research_title"},
    "intent_to_submit": {"research_title", "supervisor_name"},
    "plagiarism_declaration": {"assessment_title"},
    "ai_declaration_form": {"assessment_title"},
    "affidavit": {"research_title"},
    "jbs1_declaration": {"research_title"},
    "corrections_response": {"student_initials_surname", "student_number", "research_title"},
}


def _student_supervisor_agreement_pdf_definition():
    definition = {
        **FORM_PDF_DEFINITIONS["supervisor_agreement"],
        "action": "Student Action",
        "intro": "Review the completed supervisor agreement below. Submitting it confirms your acceptance and sends the agreement to MBA Admin and your supervisor.",
    }
    sections = []
    for section in FORM_PDF_DEFINITIONS["supervisor_agreement"]["sections"]:
        section_copy = {**section}
        if section_copy["title"] == "Acceptance":
            section_copy["checkboxes"] = [
                ("student_agreement_declaration", "Student confirms the supervisor agreement."),
                ("supervisor_agreement_declaration", "Supervisor confirms the supervisor agreement."),
            ]
        sections.append(section_copy)
    definition["sections"] = sections
    return definition


def _form_definition_for(form_type, payload=None):
    payload = payload or {}
    if form_type in FORM_PDF_DEFINITIONS:
        if form_type == "supervisor_agreement" and payload.get("_student_acceptance"):
            return _student_supervisor_agreement_pdf_definition()
        return FORM_PDF_DEFINITIONS[form_type]
    if str(form_type or "").startswith("assessor_profile_"):
        return {
            **ASSESSOR_PROFILE_FORM_DEFINITION,
            "title": f"External Examiner Nomination Form - {form_type.replace('assessor_profile_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessment_result_"):
        return {
            **ASSESSOR_FORM_DEFINITION,
            "title": f"Capstone Assessment Result Summary - {form_type.replace('assessment_result_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessor_report_"):
        return {
            **ASSESSOR_REPORT_FORM_DEFINITION,
            "title": f"Capstone Assessor Report - {form_type.replace('assessor_report_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessor_narrative_"):
        return {
            **ASSESSOR_NARRATIVE_FORM_DEFINITION,
            "title": f"Capstone Assessors Report Form 2 - {form_type.replace('assessor_narrative_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessor_banking_"):
        return {
            **ASSESSOR_BANKING_FORM_DEFINITION,
            "title": f"Assessor Banking Details - {form_type.replace('assessor_banking_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessor_temp_appointment_"):
        return {
            **ASSESSOR_TEMP_APPOINTMENT_FORM_DEFINITION,
            "title": f"Temporary Appointments Form - {form_type.replace('assessor_temp_appointment_', '').replace('_', ' ').title()}",
        }
    if str(form_type or "").startswith("assessor_temp_claim_"):
        return {
            **ASSESSOR_TEMP_CLAIM_FORM_DEFINITION,
            "title": f"Temporary Claim Form - {form_type.replace('assessor_temp_claim_', '').replace('_', ' ').title()}",
        }
    return {
        "title": document_label(form_type),
        "action": "MBA Form",
        "intro": "Generated from MBA web form submission.",
        "sections": [
            {
                "title": "Submitted Details",
                "fields": [(key, key.replace("_", " ").title(), "textarea") for key in (payload or {}).keys()],
            }
        ],
    }


def _pdf_wrapped(value, width):
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return textwrap.wrap(text, width=width, break_long_words=False) or [""]


def _pdf_add_text(commands, x, y, text, size=10, font="F1", color="0 0 0"):
    commands.append(f"BT {color} rg /{font} {size} Tf {x} {y} Td ({_pdf_text(text)}) Tj ET")


def _pdf_add_rect(commands, x, y, width, height, stroke="0.82 0.85 0.88", fill=None):
    if fill:
        commands.append(f"{fill} rg {x} {y} {width} {height} re f")
    commands.append(f"{stroke} RG {x} {y} {width} {height} re S")


def _pdf_add_line(commands, x1, y1, x2, y2, stroke="0.82 0.85 0.88"):
    commands.append(f"{stroke} RG {x1} {y1} m {x2} {y2} l S")


def _pdf_field_spec(field):
    key, label, field_type = field[:3]
    return key, label, field_type


def _pdf_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked", "confirmed"}


class _FormPdfRenderer:
    def __init__(self, form_type, definition, payload):
        self.form_type = form_type
        self.definition = definition
        self.payload = payload or {}
        self.required_fields = FORM_REQUIRED_FIELDS.get(form_type, set())
        if str(form_type or "").startswith("assessment_result_"):
            self.required_fields = {
                "assessor_name",
                "student_name",
                "student_number",
                "research_title",
                "recommendation",
                "consent_name_disclosure",
                "grade",
                "assessor_signature_name",
                "certification_date",
            }
        elif str(form_type or "").startswith("assessor_report_"):
            self.required_fields = {
                "student_name",
                "student_number",
                "research_title",
                "assessor_name",
                "affiliation",
                "assessor_email",
                "assessor_contact",
                "recommendation",
                "grade",
                "consent_name_disclosure",
                "assessor_signature_name",
                "certification_date",
            }
        elif str(form_type or "").startswith("assessor_narrative_"):
            self.required_fields = {
                "student_name",
                "student_number",
                "research_title",
                "assessor_name",
                "affiliation",
                "assessor_email",
                "assessor_contact",
                "assessor_signature_name",
                "certification_date",
            }
        elif str(form_type or "").startswith("assessor_banking_"):
            self.required_fields = {
                "bank_account_holder",
                "bank_name",
                "bank_branch_name",
                "bank_branch_code",
                "bank_account_number",
                "bank_account_type",
                "banking_declaration",
            }
        elif str(form_type or "").startswith("assessor_temp_appointment_"):
            self.required_fields = {
                "new_employee",
                "employed_at_uj",
                "appointed_as",
                "assessor_surname",
                "assessor_title",
                "assessor_first_names",
                "identity_passport_number",
                "assessor_email",
                "assessor_contact",
                "highest_qualification",
                "bank_account_holder",
                "bank_name",
                "bank_branch_name",
                "bank_branch_code",
                "bank_account_number",
                "bank_account_type",
                "appointment_category",
                "appointment_start_date",
                "appointment_end_date",
                "temporary_employment_reason",
                "rate_per_hour",
                "actual_hours",
                "full_cost_centre_string",
                "employee_signature_name",
                "employee_signature_date",
                "appointment_declaration",
            }
        elif str(form_type or "").startswith("assessor_temp_claim_"):
            self.required_fields = {
                "faculty_division",
                "department_unit_centre",
                "month_of_claim",
                "assessor_surname",
                "assessor_title",
                "assessor_first_names",
                "assessor_contact",
                "assessor_email",
                "appointment_start_date",
                "appointment_end_date",
                "appointed_as",
                "claim_unit_basis",
                "rate_per_hour",
                "actual_hours",
                "full_cost_centre_string",
                "contract_eit_number",
                "claim_total_units",
                "claim_rate",
                "claim_currency",
                "amount_claimed",
                "claim_cost_centre_number",
                "total_claimed",
                "bank_account_holder",
                "bank_name",
                "bank_branch_name",
                "bank_branch_code",
                "bank_account_number",
                "bank_account_type",
                "claim_signature_name",
                "claim_signature_date",
                "claim_declaration",
            }
        elif str(form_type or "").startswith("assessor_profile_"):
            self.required_fields = {
                "project_title",
                "student_name",
                "student_number",
                "slot_label",
                "assessor_name",
                "assessor_email",
                "assessor_contact",
                "highest_qualification",
                "academic_experience_years",
                "current_student_load",
                "students_supervised_total",
                "students_assessed_total",
                "publication_count",
                "selected_publications",
                "cv_filename",
                "assessor_signature_name",
                "assessor_profile_date",
                "assessor_profile_declaration",
            }
        self.readonly_fields = FORM_READONLY_FIELDS.get(form_type, set())
        self.pages = []
        self.commands = []
        self.y = 742
        self.margin = 54
        self.width = 504

    def render(self):
        self._start_page()
        self._draw_header()
        for section in self.definition["sections"]:
            self._draw_section(section)
        self._finish_page()
        pdf_bytes = _build_pdf_from_page_streams(
            self.pages,
            marker=_generated_form_pdf_marker(self.form_type),
        )
        return _stamp_generated_pdf_bytes(pdf_bytes, _generated_form_pdf_marker(self.form_type))

    def _start_page(self):
        self.commands = []
        self.y = 742

    def _finish_page(self):
        if self.commands:
            self.pages.append("\n".join(self.commands))

    def _new_page(self):
        self._finish_page()
        self._start_page()
        _pdf_add_text(self.commands, self.margin, self.y, self.definition["title"], size=11, font="F2")
        _pdf_add_line(self.commands, self.margin, self.y - 10, self.margin + self.width, self.y - 10)
        self.y -= 30

    def _ensure_space(self, height):
        if self.y - height < 54:
            self._new_page()

    def _draw_header(self):
        _pdf_add_rect(self.commands, self.margin, self.y - 62, self.width, 70, stroke="0.90 0.91 0.93", fill="1 1 1")
        _pdf_add_rect(self.commands, self.margin, self.y + 2, self.width, 6, stroke="0.94 0.51 0.05", fill="0.94 0.51 0.05")
        _pdf_add_text(self.commands, self.margin + 18, self.y - 15, self.definition["title"], size=17, font="F2")

        action = self.definition.get("action", "")
        if action:
            pill_width = max(78, min(150, 38 + (len(action) * 5)))
            pill_x = self.margin + self.width - pill_width - 18
            _pdf_add_rect(self.commands, pill_x, self.y - 31, pill_width, 20, stroke="0.94 0.51 0.05", fill="0.94 0.51 0.05")
            _pdf_add_text(self.commands, pill_x + 12, self.y - 25, action, size=8, font="F2", color="1 1 1")

        self.y -= 84
        for line in _pdf_wrapped(self.definition.get("intro", ""), 94):
            _pdf_add_text(self.commands, self.margin, self.y, line, size=9, color="0.38 0.38 0.38")
            self.y -= 13
        self.y -= 10

    def _draw_section(self, section):
        section_height = self._section_height(section)
        self._ensure_space(section_height)
        top_y = self.y
        _pdf_add_rect(self.commands, self.margin, top_y - section_height, self.width, section_height, stroke="0.82 0.85 0.88", fill="1 1 1")
        _pdf_add_rect(self.commands, self.margin + 12, top_y - 14, 150, 22, stroke="1 1 1", fill="1 1 1")
        _pdf_add_text(self.commands, self.margin + 18, top_y - 8, section["title"], size=11, font="F2")
        self.y -= 34

        for field in section.get("fields", []):
            self._draw_field(*_pdf_field_spec(field))

        if section.get("paragraph"):
            self.y -= 2
            for line in _pdf_wrapped(section["paragraph"], 88):
                _pdf_add_text(self.commands, self.margin + 14, self.y, line, size=9, color="0.35 0.35 0.35")
                self.y -= 13
            self.y -= 4

        for bullet in section.get("bullets", []):
            for line_index, line in enumerate(_pdf_wrapped(bullet, 84)):
                prefix = "- " if line_index == 0 else "  "
                _pdf_add_text(self.commands, self.margin + 22, self.y, f"{prefix}{line}", size=9, color="0.35 0.35 0.35")
                self.y -= 13
            self.y -= 1

        for key, label in section.get("checkboxes", []):
            self._draw_checkbox(key, label, section.get("checkbox_position", "left"))

        self.y = top_y - section_height - 18

    def _draw_field(self, key, label, field_type):
        value = self.payload.get(key, "")
        box_height = self._field_box_height(key, field_type)
        label_text = f"{label} *" if key in self.required_fields else label
        self._ensure_space(box_height + 32)
        _pdf_add_text(self.commands, self.margin + 16, self.y, label_text, size=9, font="F2", color="0.24 0.29 0.35")
        self.y -= 15
        fill = "0.96 0.97 0.98" if key in self.readonly_fields else "0.99 0.99 0.99"
        _pdf_add_rect(self.commands, self.margin + 16, self.y - box_height, self.width - 32, box_height, stroke="0.82 0.85 0.88", fill=fill)
        max_lines = max(1, int((box_height - 16) / 13))
        wrap_width = 82
        for line_index, line in enumerate(_pdf_wrapped(value or "", wrap_width)[:max_lines]):
            _pdf_add_text(self.commands, self.margin + 26, self.y - 17 - (line_index * 13), line, size=9)
        self.y -= box_height + 14

    def _draw_checkbox(self, key, label, position="left"):
        self._ensure_space(30)
        checked = _pdf_truthy(self.payload.get(key))
        label_text = f"{label} *" if key in self.required_fields else label
        box_x = self.margin + 16 if position == "left" else self.margin + self.width - 30
        box_y = self.y - 11
        text_x = self.margin + 36 if position == "left" else self.margin + 16
        _pdf_add_text(self.commands, text_x, self.y, label_text, size=9, color="0.24 0.29 0.35")
        _pdf_add_rect(self.commands, box_x, box_y, 12, 12, stroke="0.35 0.35 0.35")
        if checked:
            _pdf_add_text(self.commands, box_x + 2.5, box_y + 2.5, "X", size=8, font="F2")
        self.y -= 26

    def _field_box_height(self, key, field_type):
        if field_type != "textarea":
            return 30
        line_count = len(_pdf_wrapped(self.payload.get(key, ""), 82))
        visible_lines = min(max(line_count, 4), 8)
        return max(70, 20 + (visible_lines * 13))

    def _section_height(self, section):
        height = 34
        for field in section.get("fields", []):
            key, _label, field_type = _pdf_field_spec(field)
            height += self._field_box_height(key, field_type) + 29
        if section.get("paragraph"):
            height += 22 + (len(_pdf_wrapped(section["paragraph"], 88)) * 13)
        for bullet in section.get("bullets", []):
            height += len(_pdf_wrapped(bullet, 84)) * 13 + 1
        height += len(section.get("checkboxes", [])) * 26
        return max(height + 12, 76)


def generate_form_submission_pdf_bytes(form_type, payload):
    definition = _form_definition_for(form_type, payload)
    renderer = _FormPdfRenderer(form_type, definition, payload)
    return renderer.render()


def generate_form_submission_document_bytes(project, form_type, payload, *, allow_plain_fallback=True):
    payload = decrypt_sensitive_payload_fields(payload)
    try:
        html_pdf_bytes = _render_html_form_pdf_bytes(project, form_type, payload)
        if html_pdf_bytes:
            return html_pdf_bytes
    except Exception:
        current_app.logger.exception("HTML form PDF render failed for %s", form_type)
    if not allow_plain_fallback:
        raise RuntimeError(HTML_PDF_RENDERER_UNAVAILABLE_MESSAGE)
    return generate_form_submission_pdf_bytes(form_type, payload)


def generate_form_submission_download_bytes(project, form_type, payload):
    payload = decrypt_sensitive_payload_fields(payload)
    template_path = _native_word_template_path_for_form(form_type)
    if template_path and _docx_template_exists(template_path):
        return generate_form_submission_word_bytes(project, form_type, payload), FORM_WORD_EXTENSION, FORM_WORD_MIME_TYPE
    try:
        return (
            generate_form_submission_document_bytes(project, form_type, payload, allow_plain_fallback=False),
            "pdf",
            "application/pdf",
        )
    except RuntimeError as exc:
        current_app.logger.warning(
            "Unable to generate exact PDF for %s; using DOCX fallback instead: %s",
            form_type,
            exc,
        )
        return generate_form_submission_word_bytes(project, form_type, payload), FORM_WORD_EXTENSION, FORM_WORD_MIME_TYPE


_ACTIVITY_START_RE = re.compile(r"(?m)(?=^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}: )")
_ACTIVITY_TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}):\s*(.*)$", re.DOTALL)
_ACTIVITY_EMAIL_PREFIX_RE = re.compile(r"^([^\s:@]+@[^\s:@]+)\s+(.*)$", re.DOTALL)


def _activity_title(message):
    lower_message = (message or "").lower()
    if re.match(r"^[^\s:@]+@[^\s:@]+:\s+", message or ""):
        return "Project Comment"
    if "debug:" in lower_message:
        return "Diagnostics"
    if "uploaded" in lower_message:
        return "Document Uploaded"
    if "invitation" in lower_message or "invite" in lower_message:
        return "Invitation Update"
    if "assigned" in lower_message or "assignment" in lower_message or "assessor" in lower_message:
        return "Assignment Update"
    if "approved" in lower_message or "declined" in lower_message or "accepted" in lower_message:
        return "Decision Recorded"
    if "submitted" in lower_message or "sent to hdc" in lower_message:
        return "Submission Update"
    if "comment" in lower_message or "note" in lower_message:
        return "Project Comment"
    return "Project Activity"


def _activity_actor_and_message(message):
    message = (message or "").strip()
    if not message:
        return None, ""

    if ": " in message:
        actor, body = message.split(": ", 1)
        if "@" in actor and len(actor) <= 255:
            return actor, body.strip()

    email_match = _ACTIVITY_EMAIL_PREFIX_RE.match(message)
    if email_match:
        return email_match.group(1), email_match.group(2).strip()

    if message.startswith("DEBUG:"):
        return "System", message.replace("DEBUG:", "", 1).strip()

    return "System", message


def project_activity_entries(activity_text):
    if not activity_text:
        return []

    chunks = []
    normalized = activity_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    for legacy_chunk in normalized.split("***"):
        legacy_chunk = legacy_chunk.strip()
        if not legacy_chunk:
            continue
        timestamped_chunks = [chunk.strip() for chunk in _ACTIVITY_START_RE.split(legacy_chunk) if chunk.strip()]
        chunks.extend(timestamped_chunks or [legacy_chunk])

    entries = []
    for chunk in chunks:
        timestamp = None
        timestamp_label = "No timestamp"
        message = chunk
        timestamp_match = _ACTIVITY_TIMESTAMP_RE.match(chunk)
        if timestamp_match:
            try:
                timestamp = datetime.fromisoformat(timestamp_match.group(1))
                timestamp_label = timestamp.strftime("%d %b %Y %H:%M")
            except ValueError:
                timestamp = None
            message = timestamp_match.group(2).strip()

        actor, readable_message = _activity_actor_and_message(message)
        entries.append(
            {
                "timestamp": timestamp,
                "timestamp_label": timestamp_label,
                "actor": actor,
                "title": _activity_title(message),
                "message": readable_message,
            }
        )

    return entries


def _store_project_document(project, doc_key, uploaded_file, replace_existing=True):
    project_dir = os.path.join(_uploads_dir(), str(project.id))

    safe_original = secure_filename(uploaded_file.filename)
    file_bytes = _uploaded_file_bytes(uploaded_file)
    mime_type = uploaded_file.mimetype or document_mime_type(safe_original)
    unique_name = f"{doc_key}_{uuid.uuid4().hex[:8]}_{safe_original}"

    existing_doc = None
    if replace_existing:
        existing_doc = MbaProjectDocument.query.filter_by(project_id=project.id, doc_type=doc_key).first()

    if existing_doc:
        old_path = os.path.join(project_dir, existing_doc.stored_name or "")
        if existing_doc.stored_name and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        existing_doc.original_name = safe_original
        existing_doc.stored_name = unique_name
        existing_doc.file_data = file_bytes
        existing_doc.mime_type = mime_type
        existing_doc.file_size = len(file_bytes)
        existing_doc.uploaded_by_id = current_user.id
        existing_doc.uploaded_at = datetime.utcnow()
        doc = existing_doc
    else:
        doc = MbaProjectDocument(
            project_id=project.id,
            doc_type=doc_key,
            original_name=safe_original,
            stored_name=unique_name,
            file_data=file_bytes,
            mime_type=mime_type,
            file_size=len(file_bytes),
            uploaded_by_id=current_user.id,
        )
        db.session.add(doc)

    project.comments = append_comment(
        project.comments,
        f"{current_user.email} uploaded {document_label(doc_key)} ({safe_original})",
    )
    return doc


def _project_has_document(project_id, doc_key):
    return (
        db.session.query(MbaProjectDocument.id)
        .filter_by(project_id=project_id, doc_type=doc_key)
        .first()
        is not None
    )


def document_label(doc_key):
    if doc_key in MBA_DOCUMENT_LABELS:
        return MBA_DOCUMENT_LABELS[doc_key]
    if doc_key == assessment_summary_doc_type():
        return "Summary Assessment Report - Capstone Project"
    if doc_key.startswith("assessment_result_"):
        suffix = doc_key.replace("assessment_result_", "").replace("_", " ").title()
        return f"Capstone Assessment Result Summary - {suffix}"
    if doc_key.startswith("assessor_report_"):
        suffix = doc_key.replace("assessor_report_", "").replace("_", " ").title()
        return f"Capstone Assessor Report - {suffix}"
    if doc_key.startswith("assessor_narrative_"):
        suffix = doc_key.replace("assessor_narrative_", "").replace("_", " ").title()
        return f"Capstone Assessors Report Form 2 - {suffix}"
    if doc_key.startswith("assessor_detailed_report_"):
        suffix = doc_key.replace("assessor_detailed_report_", "").replace("_", " ").title()
        return f"Separate Detailed Assessor Report - {suffix}"
    if doc_key.startswith("assessor_banking_"):
        suffix = doc_key.replace("assessor_banking_", "").replace("_", " ").title()
        return f"Assessor Banking Details - {suffix}"
    if doc_key.startswith("assessor_temp_appointment_"):
        suffix = doc_key.replace("assessor_temp_appointment_", "").replace("_", " ").title()
        return f"Temporary Appointments Form - {suffix}"
    if doc_key.startswith("assessor_temp_claim_"):
        suffix = doc_key.replace("assessor_temp_claim_", "").replace("_", " ").title()
        return f"Temporary Claim Form - {suffix}"
    if doc_key.startswith("assessor_profile_"):
        suffix = doc_key.replace("assessor_profile_", "").replace("_", " ").title()
        return f"External Examiner Nomination Form - {suffix}"
    if doc_key.startswith("assessor_cv_"):
        suffix = doc_key.replace("assessor_cv_", "").replace("_", " ").title()
        return f"Assessor Curriculum Vitae - {suffix}"
    if doc_key.startswith("assessor_highest_qualification_"):
        suffix = doc_key.replace("assessor_highest_qualification_", "").replace("_", " ").title()
        return f"Assessor Highest Qualification - {suffix}"
    if doc_key.startswith("admin_supporting_"):
        return "Admin Supporting Document"
    return doc_key.replace("_", " ").title()


def assessment_doc_type(slot):
    return f"assessment_result_{slot}"


def assessor_report_doc_type(slot):
    return f"assessor_report_{slot}"


def assessor_narrative_doc_type(slot):
    return f"assessor_narrative_{slot}"


def assessor_detailed_report_doc_type(slot):
    return f"assessor_detailed_report_{slot}"


def assessment_summary_doc_type():
    return "assessment_summary"


def assessor_temp_appointment_doc_type(slot):
    return f"assessor_temp_appointment_{slot}"


def assessor_temp_claim_doc_type(slot):
    return f"assessor_temp_claim_{slot}"


def assessor_profile_doc_type(slot):
    return f"assessor_profile_{slot}"


def assessor_cv_doc_type(slot):
    return f"assessor_cv_{slot}"


def assessor_highest_qualification_doc_type(slot):
    return f"assessor_highest_qualification_{slot}"


def external_examiner_nomination_doc_type():
    return "external_examiner_nomination"


def additional_external_examiner_nomination_doc_type():
    return "additional_external_examiner_nomination"


def assessor_slot_document_types(slot):
    doc_types = (
        assessor_temp_appointment_doc_type(slot),
        assessor_temp_claim_doc_type(slot),
        assessor_profile_doc_type(slot),
        assessor_cv_doc_type(slot),
        assessor_highest_qualification_doc_type(slot),
        assessment_doc_type(slot),
        assessor_report_doc_type(slot),
        assessor_narrative_doc_type(slot),
        assessor_detailed_report_doc_type(slot),
    )
    if slot == ADDITIONAL_ASSESSOR_SLOT:
        doc_types = doc_types + (additional_external_examiner_nomination_doc_type(),)
    return doc_types


def _assessor_display_name(user):
    if not user:
        return ""
    profile = getattr(user, "scholar_profile", None)
    if profile:
        name = " ".join(part for part in [profile.title, profile.name, profile.surname] if part).strip()
        if name:
            return name
    return " ".join(part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if part).strip() or user.email or ""


def _initials_from_name_parts(*parts):
    letters = []
    for part in parts:
        for token in str(part or "").replace(".", " ").split():
            if token:
                letters.append(token[0].upper())
    return "".join(letters)


def _yes_no_from_bool(value):
    return "Yes" if value else "No"


def _assessor_nomination_payload_for_slot(project, slot):
    assessor = getattr(project, slot, None)
    profile = getattr(assessor, "scholar_profile", None) if assessor else None
    appointment_form = MbaForm.query.filter_by(project_id=project.id, form_type=assessor_temp_appointment_doc_type(slot)).first()
    claim_form = MbaForm.query.filter_by(project_id=project.id, form_type=assessor_temp_claim_doc_type(slot)).first()
    appointment_payload = appointment_form.payload if appointment_form and isinstance(appointment_form.payload, dict) else {}
    claim_payload = claim_form.payload if claim_form and isinstance(claim_form.payload, dict) else {}
    payload = {**claim_payload, **appointment_payload}

    def first(*keys, default=""):
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return str(default or "")

    return {
        "name": first("assessor_name", default=_assessor_display_name(assessor)),
        "qualification": first("highest_qualification", default=getattr(profile, "qualification", "") if profile else ""),
        "affiliation": first(
            "assessor_affiliation",
            "current_university_affiliation",
            "qualification_institution",
            default=getattr(profile, "affiliation", "") if profile else "",
        ),
        "address": first("assessor_address", "home_address", "postal_address", default=getattr(profile, "address", "") if profile else ""),
        "telephone": first("assessor_telephone_number", "work_tel", "home_tel"),
        "cell": first("assessor_contact", "alternate_contact_number", default=getattr(profile, "contact", "") if profile else ""),
        "email": first("assessor_email", "alternate_email_address", default=getattr(assessor, "email", "") if assessor else ""),
        "students_supervised": first("students_supervised_total", default=getattr(profile, "students_supervised_total", "") if profile else ""),
        "current_university_affiliation": first(
            "current_university_affiliation",
            "assessor_affiliation",
            default=getattr(profile, "affiliation", "") if profile else "",
        ),
        "publication_count": first("publication_count", default=getattr(profile, "publication_count", "") if profile else ""),
        "international": first(
            "international_assessor",
            default=_yes_no_from_bool(getattr(profile, "international_assessor", False)) if profile else "",
        ),
    }


def build_external_examiner_nomination_payload(project, existing_payload=None):
    existing_payload = existing_payload if isinstance(existing_payload, dict) else {}
    student_profile = getattr(project.student, "student_profile", None) if project and project.student else None
    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    student_initials = _initials_from_name_parts(
        student_profile.name if student_profile else "",
        student_profile.surname if student_profile else "",
    )
    supervisor_name = _assessor_display_name(supervisor)
    supervisor_department = getattr(supervisor_profile, "department", "") if supervisor_profile else "Johannesburg Business School"
    payload = {
        "_external_examiner_nomination_render_version": EXTERNAL_EXAMINER_NOMINATION_RENDER_VERSION,
        "student_initials_surname": " ".join(
            part for part in [student_initials, getattr(student_profile, "surname", "") if student_profile else ""] if part
        ).strip(),
        "student_number": getattr(student_profile, "student_number", "") if student_profile else "",
        "current_degree_registered": "MBA Master of Business Administration",
        "qualification_description": getattr(project, "qualification", "") or "Minor dissertation",
        "study_type": existing_payload.get("study_type") or "Minor dissertation",
        "project_title": getattr(project, "project_title", "") or "",
        "supervisor_name": supervisor_name,
        "supervisor_department": supervisor_department,
        "supervisor_phone": getattr(supervisor_profile, "contact", "") if supervisor_profile else "",
        "supervisor_email": getattr(supervisor, "email", "") if supervisor else "",
        "co_supervisor_name": existing_payload.get("co_supervisor_name", ""),
        "co_supervisor_department": existing_payload.get("co_supervisor_department", ""),
        "co_supervisor_phone": existing_payload.get("co_supervisor_phone", ""),
        "co_supervisor_email": existing_payload.get("co_supervisor_email", ""),
    }
    for slot in PRIMARY_ASSESSOR_SLOTS:
        assessor_payload = _assessor_nomination_payload_for_slot(project, slot)
        for key, value in assessor_payload.items():
            payload[f"{slot}_{key}"] = value
        payload[f"_{slot}_id"] = str(getattr(project, f"{slot}_id", "") or "")
    for field in (
        "supervisor_signature_name",
        "supervisor_signature_date",
        "hod_signature_name",
        "hod_signature_date",
        "executive_dean_signature_name",
        "executive_dean_signature_date",
        "nomination_forwarded_to_supervisor_at",
        "nomination_forwarded_to_supervisor_by",
        "assessor_hr_documents_sent_at",
        "assessor_hr_documents_sent_to",
        "assessor_hr_documents_sent_by",
        "assessor_hr_documents_sent_count",
    ):
        if existing_payload.get(field):
            payload[field] = existing_payload[field]
    copy_signature_snapshots(
        payload,
        existing_payload,
        ("supervisor_signature_name", "hod_signature_name", "executive_dean_signature_name"),
    )
    return payload


def build_additional_external_examiner_nomination_payload(project, existing_payload=None):
    existing_payload = existing_payload if isinstance(existing_payload, dict) else {}
    payload = build_external_examiner_nomination_payload(project, existing_payload)
    payload["_additional_external_examiner_nomination_render_version"] = ADDITIONAL_EXTERNAL_EXAMINER_NOMINATION_RENDER_VERSION
    payload["_nomination_context"] = "additional_assessment"
    payload["_assessor_3_id"] = str(getattr(project, "assessor_3_id", "") or "")

    additional_assessor_payload = _assessor_nomination_payload_for_slot(project, ADDITIONAL_ASSESSOR_SLOT)
    assessor_field_names = (
        "name",
        "qualification",
        "affiliation",
        "address",
        "telephone",
        "cell",
        "email",
        "students_supervised",
        "current_university_affiliation",
        "publication_count",
        "international",
    )
    for field_name in assessor_field_names:
        payload[f"assessor_1_{field_name}"] = additional_assessor_payload.get(field_name, "")
        payload[f"assessor_2_{field_name}"] = ""
        payload[f"standby_{field_name}"] = ""
    payload["_assessor_1_id"] = str(getattr(project, "assessor_3_id", "") or "")
    payload["_assessor_2_id"] = ""
    payload["study_type"] = existing_payload.get("study_type") or payload.get("study_type") or "Additional assessment"
    payload["additional_assessor_name"] = additional_assessor_payload.get("name", "")

    for field in (
        "supervisor_signature_name",
        "supervisor_signature_date",
        "hod_signature_name",
        "hod_signature_date",
        "executive_dean_signature_name",
        "executive_dean_signature_date",
        "nomination_forwarded_to_supervisor_at",
        "nomination_forwarded_to_supervisor_by",
    ):
        if existing_payload.get(field):
            payload[field] = existing_payload[field]
    copy_signature_snapshots(
        payload,
        existing_payload,
        ("supervisor_signature_name", "hod_signature_name", "executive_dean_signature_name"),
    )
    return payload


def _summary_person_identity(user, fallback_name=""):
    profile = getattr(user, "scholar_profile", None) if user else None
    fallback_tokens = str(fallback_name or "").split()
    fallback_title = fallback_tokens[0] if fallback_tokens and fallback_tokens[0].rstrip(".").lower() in {"dr", "prof", "mr", "mrs", "ms"} else ""
    fallback_without_title = fallback_tokens[1:] if fallback_title else fallback_tokens
    fallback_surname = fallback_without_title[-1] if fallback_without_title else ""
    fallback_first_names = " ".join(fallback_without_title[:-1]) if len(fallback_without_title) > 1 else ""
    first_names = (
        getattr(profile, "name", None)
        or getattr(user, "first_name", None)
        or fallback_first_names
        or str(fallback_name or "")
    )
    surname = getattr(profile, "surname", None) or getattr(user, "last_name", None) or fallback_surname
    title = getattr(profile, "title", None) or fallback_title
    return {
        "title": title or "",
        "initials": _initials_from_name_parts(first_names),
        "surname": surname or "",
        "full_name": _assessor_display_name(user) or fallback_name or "",
    }


def _assessment_summary_result_form(project, slot, forms_by_project=None):
    form_type = assessment_doc_type(slot)
    if forms_by_project is not None:
        return forms_by_project.get(getattr(project, "id", None), {}).get(form_type)
    return MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()


def _assessment_summary_slot_payload(project, slot, forms_by_project=None):
    form = _assessment_summary_result_form(project, slot, forms_by_project=forms_by_project)
    result_payload = form.payload if form and isinstance(form.payload, dict) else {}
    submitted_at = getattr(form, "submitted_at", None)
    assessor = getattr(project, slot, None)
    nomination_payload = _assessor_nomination_payload_for_slot(project, slot)
    identity = _summary_person_identity(assessor, result_payload.get("assessor_name") or nomination_payload.get("name", ""))
    profile = getattr(assessor, "scholar_profile", None) if assessor else None
    affiliation = (
        result_payload.get("affiliation")
        or nomination_payload.get("affiliation")
        or getattr(profile, "affiliation", "")
        or getattr(profile, "department", "")
        or ""
    )
    qualification = (
        nomination_payload.get("qualification")
        or getattr(profile, "qualification", "")
        or ""
    )
    return {
        "id": str(getattr(project, f"{slot}_id", "") or ""),
        "title": identity["title"],
        "initials": identity["initials"],
        "surname": identity["surname"],
        "name": result_payload.get("assessor_name") or nomination_payload.get("name") or identity["full_name"],
        "affiliation": affiliation,
        "qualification": qualification,
        "grade": str(result_payload.get("grade") or "").strip(),
        "recommendation": str(result_payload.get("recommendation") or "").strip(),
        "submitted_at": submitted_at.isoformat() if submitted_at else "",
    }


def _assessment_summary_int_grade(value):
    try:
        grade = float(str(value or "").strip())
    except (TypeError, ValueError):
        return None
    return grade if 0 <= grade <= 100 else None


def _assessment_summary_recommendation_column(recommendation):
    normalized = _docx_normalized(recommendation)
    if not normalized:
        return None
    if "outright" in normalized or "rejection" in normalized or "rejected" in normalized:
        return 4
    if "re-examination" in normalized or "re examination" in normalized or "major revisions" in normalized:
        return 3
    if "minor revisions" in normalized or "minor corrections" in normalized:
        return 2
    if "accept as" in normalized or "research stands" in normalized:
        return 1
    return None


def _assessment_summary_grade_totals(slot_payloads):
    grades = [
        _assessment_summary_int_grade(slot_payload.get("grade"))
        for slot_payload in slot_payloads
        if slot_payload.get("grade") not in (None, "")
    ]
    grades = [grade for grade in grades if grade is not None]
    if not grades:
        return "", ""
    total = sum(grades)
    average = total / len(grades)
    average_text = f"{average:.1f}".rstrip("0").rstrip(".")
    return str(total), average_text


def build_assessment_summary_payload(project, existing_payload=None, forms_by_project=None):
    existing_payload = existing_payload if isinstance(existing_payload, dict) else {}
    student = getattr(project, "student", None)
    student_profile = getattr(student, "student_profile", None) if student else None
    student_identity = _summary_person_identity(student, "")
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    jbs5_payload = jbs5_form.payload if jbs5_form and isinstance(jbs5_form.payload, dict) else {}

    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    supervisor_identity = _summary_person_identity(supervisor, "")

    slot_payloads = {
        slot: _assessment_summary_slot_payload(project, slot, forms_by_project=forms_by_project)
        for slot in ALL_ASSESSOR_SLOTS
    }
    active_slots = [
        slot
        for slot in ALL_ASSESSOR_SLOTS
        if slot in PRIMARY_ASSESSOR_SLOTS
        or (slot == ADDITIONAL_ASSESSOR_SLOT and getattr(project, f"{slot}_id", None))
    ]
    active_slot_payloads = [
        slot_payloads[slot]
        for slot in active_slots
        if slot_payloads.get(slot) and slot_payloads[slot].get("grade")
    ]
    grade_total, grade_average = _assessment_summary_grade_totals(active_slot_payloads)
    corrections_required = project_has_active_corrections(project, forms_by_project=forms_by_project) or bool(
        getattr(project, "corrections_requested_at", None)
    )
    corrections_complete = not corrections_required or project_corrections_status(project, forms_by_project=forms_by_project) == "ready_for_admin"

    payload = {
        "_assessment_summary_render_version": ASSESSMENT_SUMMARY_RENDER_VERSION,
        "student_surname": getattr(student_profile, "surname", "") if student_profile else student_identity["surname"],
        "student_initials": _initials_from_name_parts(getattr(student_profile, "name", "")) if student_profile else student_identity["initials"],
        "student_title": getattr(student_profile, "title", "") if student_profile else student_identity["title"],
        "student_number": getattr(student_profile, "student_number", "") if student_profile else "",
        "qualification": "Masters",
        "discipline": "Master of Business Administration",
        "research_title": jbs5_payload.get("research_title") or getattr(project, "project_title", "") or "",
        "date_of_first_registration": jbs5_payload.get("date_of_first_registration") or "",
        "supervisor_surname": supervisor_identity["surname"],
        "supervisor_initials": supervisor_identity["initials"],
        "supervisor_title": supervisor_identity["title"],
        "supervisor_affiliation": (getattr(supervisor_profile, "affiliation", "") if supervisor_profile else "") or "Johannesburg Business School",
        "supervisor_qualification": getattr(supervisor_profile, "qualification", "") if supervisor_profile else "",
        "co_supervisor_surname": existing_payload.get("co_supervisor_surname", ""),
        "co_supervisor_initials": existing_payload.get("co_supervisor_initials", ""),
        "co_supervisor_title": existing_payload.get("co_supervisor_title", ""),
        "co_supervisor_affiliation": existing_payload.get("co_supervisor_affiliation", ""),
        "co_supervisor_qualification": existing_payload.get("co_supervisor_qualification", ""),
        "coursework_total": existing_payload.get("coursework_total", ""),
        "coursework_average": existing_payload.get("coursework_average", ""),
        "coursework_credit_total": existing_payload.get("coursework_credit_total", ""),
        "coursework_credit_average": existing_payload.get("coursework_credit_average", ""),
        "capstone_total": grade_total,
        "capstone_average": grade_average,
        "capstone_weighted_result": existing_payload.get("capstone_weighted_result", ""),
        "coursework_weighted_result": existing_payload.get("coursework_weighted_result", ""),
        "final_mark": existing_payload.get("final_mark") or grade_average,
        "corrections_complete": "Yes" if corrections_complete else "No",
        "corrections_required": "Yes" if corrections_required else "No",
    }
    for slot, slot_payload in slot_payloads.items():
        for key, value in slot_payload.items():
            payload[f"{slot}_{key}"] = value

    for module_code in SUMMARY_COURSEWORK_MODULES:
        for suffix in ("result", "credit"):
            field = f"module_{module_code}_{suffix}"
            if existing_payload.get(field):
                payload[field] = existing_payload[field]

    for field in (
        "supervisor_signature_name",
        "supervisor_signature_date",
        "hod_signature_name",
        "hod_signature_date",
        "chair_fhdc_signature_name",
        "chair_fhdc_signature_date",
        "hdc_signature_name",
        "hdc_signature_date",
        "executive_dean_signature_name",
        "executive_dean_signature_date",
    ):
        if existing_payload.get(field):
            payload[field] = existing_payload[field]
    copy_signature_snapshots(
        payload,
        existing_payload,
        (
            "supervisor_signature_name",
            "hod_signature_name",
            "chair_fhdc_signature_name",
            "hdc_signature_name",
            "executive_dean_signature_name",
        ),
    )
    return payload


def additional_external_examiner_nomination_can_generate(project):
    return bool(
        project
        and additional_assessment_required(project)
        and getattr(project, f"{ADDITIONAL_ASSESSOR_SLOT}_id", None)
    )


def additional_external_examiner_nomination_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(
        project_id=project.id,
        form_type=additional_external_examiner_nomination_doc_type(),
    ).first()


def additional_external_examiner_nomination_supervisor_signed(project):
    form = additional_external_examiner_nomination_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, additional_external_examiner_nomination_doc_type())
        and form
        and (
            form.supervisor_signed
            or (payload.get("supervisor_signature_name") and payload.get("supervisor_signature_date"))
        )
    )


def additional_assessor_nomination_fully_approved(project):
    return bool(
        additional_external_examiner_nomination_supervisor_signed(project)
        and hdc_additional_external_examiner_nomination_signature_complete(project)
    )


def external_examiner_nomination_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(project_id=project.id, form_type=external_examiner_nomination_doc_type()).first()


def external_examiner_nomination_supervisor_signed(project):
    form = external_examiner_nomination_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, external_examiner_nomination_doc_type())
        and form
        and (
            form.supervisor_signed
            or (payload.get("supervisor_signature_name") and payload.get("supervisor_signature_date"))
        )
    )


def assessor_hr_documents_sent(project):
    form = external_examiner_nomination_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(payload.get("assessor_hr_documents_sent_at") and payload.get("assessor_hr_documents_sent_to"))


def assessor_hr_documents_sent_to(project):
    form = external_examiner_nomination_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return payload.get("assessor_hr_documents_sent_to") or ""


def reset_assessor_slot_artifacts(project, slot):
    project_dir = os.path.join(_uploads_dir(), str(project.id))
    doc_types = set(assessor_slot_document_types(slot))
    for doc in list(project.documents):
        if doc.doc_type not in doc_types:
            continue
        stored_path = os.path.join(project_dir, doc.stored_name or "")
        if doc.stored_name and os.path.exists(stored_path):
            try:
                os.remove(stored_path)
            except OSError:
                pass
        db.session.delete(doc)

    forms = MbaForm.query.filter(
        MbaForm.project_id == project.id,
        MbaForm.form_type.in_(doc_types),
    ).all()
    for form in forms:
        db.session.delete(form)


def recommendation_requests_corrections(recommendation):
    return str(recommendation or "").strip() in CORRECTION_REQUEST_RECOMMENDATIONS


def correction_request_reference_time(project, forms_by_project=None):
    if getattr(project, "corrections_requested_at", None):
        return project.corrections_requested_at
    requests = project_correction_requests(project, forms_by_project=forms_by_project)
    submitted_times = [item["submitted_at"] for item in requests if item.get("submitted_at")]
    if submitted_times:
        return max(submitted_times)
    return None


def project_correction_requests(project, forms_by_project=None):
    if not project:
        return []
    requests = []
    form_lookup = forms_by_project.get(project.id, {}) if forms_by_project else None
    for slot in ALL_ASSESSOR_SLOTS:
        form_type = assessment_doc_type(slot)
        form = (
            form_lookup.get(form_type)
            if form_lookup is not None
            else MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
        )
        payload = form.payload if form and isinstance(form.payload, dict) else {}
        if not recommendation_requests_corrections(payload.get("recommendation")):
            continue
        assessor = getattr(project, slot, None)
        assessor_name = (payload.get("assessor_name") or "").strip()
        if not assessor_name and assessor:
            assessor_name = (
                f"{(assessor.first_name or '').strip()} {(assessor.last_name or '').strip()}".strip()
                or assessor.email
            )
        requests.append(
            {
                "slot": slot,
                "slot_label": INVITATION_SLOTS.get(slot, {}).get("label", slot.replace("_", " ").title()),
                "assessor": assessor,
                "assessor_name": assessor_name or slot.replace("_", " ").title(),
                "recommendation": (payload.get("recommendation") or "").strip(),
                "written_assessment": (payload.get("written_assessment") or "").strip(),
                "detailed_report_doc": uploaded_doc_for(project, assessor_detailed_report_doc_type(slot)),
                "detailed_report_filename": (payload.get("detailed_report_filename") or "").strip(),
                "grade": (payload.get("grade") or "").strip(),
                "submitted_at": getattr(form, "submitted_at", None),
            }
        )
    return requests


def project_has_active_corrections(project, forms_by_project=None):
    if not project or project.project_status in DISSERTATION_CORRECTIONS_CLOSED_STATUSES:
        return False
    return bool(project_correction_requests(project, forms_by_project=forms_by_project))


def assessment_results_forwarded_to_supervisor(project):
    return bool(getattr(project, "assessment_results_forwarded_to_supervisor_at", None))


def assessment_summary_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(project_id=project.id, form_type=assessment_summary_doc_type()).first()


def assessment_summary_supervisor_signed(project):
    form = assessment_summary_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, assessment_summary_doc_type())
        and form
        and (
            form.supervisor_signed
            or (payload.get("supervisor_signature_name") and payload.get("supervisor_signature_date"))
        )
    )


def assessment_summary_supervisor_signing_block_reason(project, forms_by_project=None):
    if not project:
        return "Assessment summary is not available."
    if additional_assessment_blocks_hdc_submission(project, forms_by_project=forms_by_project):
        return "Additional assessment must be completed before the assessment summary can be signed."
    if not all_assessment_results_received(project):
        return "All required assessor reports must be received before the assessment summary can be signed."
    if project_has_active_corrections(project, forms_by_project=forms_by_project):
        correction_status = project_corrections_status(project, forms_by_project=forms_by_project)
        if correction_status != "ready_for_admin":
            return "The supervisor must approve the student's Response to Assessors' Comments before signing the assessment summary."
    if not assessment_results_forwarded_to_supervisor(project):
        return "MBA Admin must forward the assessment summary to the supervisor before it can be signed."
    if not uploaded_doc_for(project, assessment_summary_doc_type()):
        return "The assessment summary document is not ready yet."
    return ""


def hdc_results_approved(project):
    return (
        getattr(project, "project_status", None) == ProjectStatus.RESULTS_APPROVED.value
        and getattr(project, "results_hdc_decision", None) == "approved"
    )


def results_released_to_supervisor(project):
    return bool(getattr(project, "results_released_to_supervisor_at", None))


def corrections_released_to_student(project):
    return bool(getattr(project, "corrections_released_to_student_at", None))


def _student_corrections_doc_current(project, doc_key):
    requested_at = correction_request_reference_time(project)
    if not requested_at:
        return False
    doc = uploaded_doc_for(project, doc_key)
    submitted_at = getattr(project, "corrections_student_resubmitted_at", None)
    if not doc or not submitted_at or submitted_at < requested_at:
        return False
    return doc.uploaded_at >= requested_at


def student_uploaded_corrections_response_form(project):
    return _student_corrections_doc_current(project, "corrections_response")


def student_submitted_corrections_turnitin(project):
    return _student_corrections_doc_current(project, "corrections_turnitin_report")


def student_uploaded_corrected_dissertation(project):
    return _student_corrections_doc_current(project, "corrected_dissertation")


def student_submitted_corrections_response(project):
    return student_uploaded_corrections_response_form(project) and student_submitted_corrections_turnitin(project)


def student_submitted_corrections_pack(project):
    return student_submitted_corrections_response(project) and student_uploaded_corrected_dissertation(project)


def supervisor_rejected_corrections(project):
    student_submitted_at = getattr(project, "corrections_student_resubmitted_at", None)
    rejected_at = getattr(project, "corrections_supervisor_rejected_at", None)
    return bool(student_submitted_at and rejected_at and rejected_at >= student_submitted_at)


def supervisor_approved_corrections(project):
    if not student_submitted_corrections_pack(project):
        return False
    requested_at = correction_request_reference_time(project)
    student_submitted_at = getattr(project, "corrections_student_resubmitted_at", None)
    approved_at = getattr(project, "corrections_supervisor_approved_at", None)
    corrected_doc = uploaded_doc_for(project, "corrected_dissertation")
    if not (requested_at and student_submitted_at and approved_at):
        return False
    return (
        student_submitted_at >= requested_at
        and approved_at >= student_submitted_at
        and corrected_doc
        and approved_at >= corrected_doc.uploaded_at
    )


def project_corrections_status(project, forms_by_project=None):
    if not project_has_active_corrections(project, forms_by_project=forms_by_project):
        return "none"
    if not student_submitted_corrections_pack(project):
        return "awaiting_student"
    if supervisor_rejected_corrections(project):
        return "rejected_by_supervisor"
    if not supervisor_approved_corrections(project):
        return "awaiting_supervisor"
    return "ready_for_admin"


def corrections_status_label(status):
    labels = {
        "awaiting_student": "Awaiting Student",
        "rejected_by_supervisor": "Returned to Student",
        "awaiting_supervisor": "Awaiting Supervisor",
        "ready_for_admin": "Ready for Admin Review",
        "none": "No Active Corrections",
    }
    return labels.get(str(status or ""), str(status or "").replace("_", " ").title())


def additional_assessment_status_label(status):
    return ADDITIONAL_ASSESSMENT_STATUS_LABELS.get(
        str(status or ""),
        str(status or "").replace("_", " ").title(),
    )


def corrections_block_hdc_submission(project, forms_by_project=None):
    return project_has_active_corrections(project, forms_by_project=forms_by_project) and (
        project_corrections_status(project, forms_by_project=forms_by_project) != "ready_for_admin"
    )


def module_completion_status_label(status):
    return MODULE_COMPLETION_STATUS_LABELS.get(
        str(status or ""),
        str(status or "").replace("_", " ").title(),
    )


def module_completion_allows_hdc_submission(project):
    return str(getattr(project, "module_completion_status", "") or "") in {
        "completed",
        "response_received",
    }


def required_hdc_results_documents_missing(project):
    required = [
        "jbs10",
        assessment_summary_doc_type(),
        "jbs1_declaration",
        "plagiarism_declaration",
        "affidavit_stamped",
        "global_document",
        "combined_turnitin_ai_report",
    ]
    if project_has_active_corrections(project) or getattr(project, "corrections_requested_at", None):
        required.extend(["corrected_dissertation", "corrections_response", "corrections_turnitin_report"])
    return [
        doc_key
        for doc_key in required
        if not uploaded_doc_for(project, doc_key)
    ]


def activate_project_corrections(project, requested_at=None):
    requested_at = requested_at or datetime.utcnow()
    project.corrections_requested_at = requested_at
    project.corrections_student_resubmitted_at = None
    project.corrections_released_to_student_at = None
    project.corrections_supervisor_approved_at = None
    project.corrections_supervisor_comments = None
    project.corrections_supervisor_rejected_at = None
    project.corrections_supervisor_rejection_comments = None


def clear_project_corrections(project):
    project.corrections_requested_at = None
    project.corrections_student_resubmitted_at = None
    project.corrections_released_to_student_at = None
    project.corrections_supervisor_approved_at = None
    project.corrections_supervisor_comments = None
    project.corrections_supervisor_rejected_at = None
    project.corrections_supervisor_rejection_comments = None


def _slot_assessor_result_form(project, slot, forms_by_project=None):
    form_type = assessment_doc_type(slot)
    if forms_by_project is not None:
        form_lookup = forms_by_project.get(project.id, {})
        return form_lookup.get(form_type)
    return MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()


def assessor_grade_for_slot(project, slot, forms_by_project=None):
    form = _slot_assessor_result_form(project, slot, forms_by_project=forms_by_project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    try:
        grade = int(payload.get("grade", ""))
    except (TypeError, ValueError):
        return None
    return grade if 0 <= grade <= 100 else None


def assessment_result_pack_complete(project, slot):
    assessor_id = getattr(project, f"{slot}_id", None)
    if not assessor_id or getattr(project, f"{slot}_invitation_status", None) != INVITATION_ACCEPTED:
        return False
    assessment_form = _slot_assessor_result_form(project, slot)
    payload = assessment_form.payload if assessment_form and isinstance(assessment_form.payload, dict) else {}
    if not (payload.get("grade") and payload.get("recommendation")):
        return False
    report_doc = uploaded_doc_for(project, assessor_report_doc_type(slot))
    return bool(report_doc and report_doc.uploaded_by_id == assessor_id)


def assessment_result_submitted(project, slot, forms_by_project=None):
    assessor_id = getattr(project, f"{slot}_id", None)
    if not assessor_id:
        return False
    assessment_form = _slot_assessor_result_form(project, slot, forms_by_project=forms_by_project)
    if forms_by_project is not None:
        form_lookup = forms_by_project.get(project.id, {})
        report_form = form_lookup.get(assessor_report_doc_type(slot))
    else:
        report_form = MbaForm.query.filter_by(project_id=project.id, form_type=assessor_report_doc_type(slot)).first()
    for form in (assessment_form, report_form):
        payload = form.payload if form and isinstance(form.payload, dict) else {}
        if form and (payload.get("grade") or payload.get("recommendation")):
            return True
    report_doc = uploaded_doc_for(project, assessor_report_doc_type(slot))
    return bool(report_doc and report_doc.uploaded_by_id == assessor_id)


def primary_assessment_conflict_detected(project, forms_by_project=None):
    primary_grades = [
        assessor_grade_for_slot(project, slot, forms_by_project=forms_by_project)
        for slot in PRIMARY_ASSESSOR_SLOTS
    ]
    if any(grade is None for grade in primary_grades):
        return False
    return min(primary_grades) < 50 <= max(primary_grades)


def additional_assessment_required(project, forms_by_project=None):
    return bool(project) and (
        bool(getattr(project, "additional_assessment_requested_at", None))
        or primary_assessment_conflict_detected(project, forms_by_project=forms_by_project)
    )


def activate_additional_assessment(project, requested_at=None):
    if not getattr(project, "additional_assessment_requested_at", None):
        project.additional_assessment_requested_at = requested_at or datetime.utcnow()


def clear_additional_assessment(project):
    project.additional_assessment_requested_at = None
    project.assessor_3_id = None
    project.assessor_3_invitation_status = None
    project.assessor_3_invited_at = None
    project.assessor_3_reminder_sent_at = None


def additional_assessment_complete(project):
    return (
        additional_assessment_required(project)
        and assessment_result_pack_complete(project, ADDITIONAL_ASSESSOR_SLOT)
        and additional_assessor_nomination_fully_approved(project)
    )


def additional_assessment_pending(project, forms_by_project=None):
    return additional_assessment_required(project, forms_by_project=forms_by_project) and not additional_assessment_complete(project)


def additional_assessment_stage(project, forms_by_project=None):
    if not additional_assessment_required(project, forms_by_project=forms_by_project):
        return "none"
    if assessment_result_pack_complete(project, ADDITIONAL_ASSESSOR_SLOT):
        return "completed" if additional_assessor_nomination_fully_approved(project) else "awaiting_nomination"
    if not getattr(project, "assessor_3_id", None):
        return "needs_assignment"
    if not additional_assessor_nomination_fully_approved(project):
        return "awaiting_nomination"
    if getattr(project, "assessor_3_invitation_status", None) != INVITATION_ACCEPTED:
        return "awaiting_acceptance"
    return "awaiting_result"


def additional_assessment_blocks_hdc_submission(project, forms_by_project=None):
    return additional_assessment_pending(project, forms_by_project=forms_by_project)


def suggested_additional_assessor(project, examiners=None):
    excluded_ids = {
        assessor_id
        for assessor_id in (
            project.primary_supervisor_id,
            project.assessor_1_id,
            project.assessor_2_id,
            project.assessor_3_id,
        )
        if assessor_id
    }
    ranked = recommend_assessors(
        project,
        examiners or examiners_query().all(),
        excluded_user_ids=excluded_ids,
        limit=1,
        workload_by_user_id=assessor_workload_counts(exclude_project_id=getattr(project, "id", None)),
    )
    return ranked[0]["user"] if ranked else None


def uploaded_doc_for(project, doc_key):
    return next((doc for doc in project.documents if doc.doc_type == doc_key), None)


def jbs10_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(project_id=project.id, form_type="jbs10").first()


def jbs10_supervisor_signed(project):
    form = jbs10_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "jbs10")
        and form
        and (
            form.supervisor_signed
            or (payload.get("supervisor_signature") and payload.get("supervisor_signature_date"))
        )
    )


def jbs10_supervisor_return_pending(project):
    form = jbs10_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        form
        and not form.supervisor_signed
        and payload.get("_supervisor_return_requested_at")
        and not payload.get("_supervisor_return_resolved_at")
    )


def jbs1_declaration_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(project_id=project.id, form_type="jbs1_declaration").first()


def jbs1_supervisor_signed(project):
    form = jbs1_declaration_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "jbs1_declaration")
        and form
        and (
            form.supervisor_signed
            or (payload.get("supervisor_signature") and payload.get("supervisor_signature_date"))
        )
    )


def jbs1_program_manager_signed(project):
    form = jbs1_declaration_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "jbs1_declaration")
        and form
        and payload.get("office_program_manager")
        and payload.get("office_program_manager_date")
    )


def jbs1_declaration_complete(project):
    form = jbs1_declaration_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "jbs1_declaration")
        and form
        and (form.student_signed or (payload.get("signature_name") and payload.get("signature_date")))
        and jbs1_supervisor_signed(project)
        and jbs1_program_manager_signed(project)
    )


def intent_to_submit_form(project):
    if not project:
        return None
    return MbaForm.query.filter_by(project_id=project.id, form_type="intent_to_submit").first()


def intent_to_submit_supervisor_signed(project):
    form = intent_to_submit_form(project)
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "intent_to_submit")
        and form
        and (
            form.supervisor_signed
            or payload.get("supervisor_agree_signature")
        )
    )


def hdc_can_access_document(project, doc_type):
    if not project or project.project_status not in HDC_DOCUMENT_ALLOWED_STATUSES:
        return False

    doc_type = str(doc_type or "")
    jbs5_stage_statuses = {
        ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
        ProjectStatus.JBS5_HDC_APPROVED.value,
        ProjectStatus.JBS5_HDC_DECLINED.value,
    }
    nomination_stage_statuses = {
        ProjectStatus.ADMIN_APPROVED.value,
        ProjectStatus.HDC_DECLINED.value,
        ProjectStatus.HDC_VERIFIED.value,
    }
    results_stage_statuses = {
        ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
        ProjectStatus.RESULTS_DECLINED.value,
        ProjectStatus.RESULTS_APPROVED.value,
        ProjectStatus.GRADUATED.value,
    }

    if doc_type in {"jbs10", "intent_to_submit"}:
        return project.project_status in nomination_stage_statuses or project.project_status in results_stage_statuses

    if doc_type in {external_examiner_nomination_doc_type(), additional_external_examiner_nomination_doc_type()}:
        return project.project_status in nomination_stage_statuses or project.project_status in results_stage_statuses

    if doc_type == "jbs5":
        return project.project_status in jbs5_stage_statuses or project.project_status in nomination_stage_statuses

    if doc_type.startswith(HDC_ASSESSOR_NOMINATION_DOCUMENT_PREFIXES):
        return project.project_status in nomination_stage_statuses or project.project_status in results_stage_statuses

    if project.project_status in results_stage_statuses and doc_type in {
        "global_document",
        "combined_turnitin_ai_report",
        "dissertation",
        "manuscript",
        "jbs1_declaration",
        "plagiarism_declaration",
        "affidavit_stamped",
        "corrected_dissertation",
        "corrections_response",
        "corrections_turnitin_report",
    }:
        return True

    if project.project_status in results_stage_statuses and doc_type.startswith(HDC_ASSESSOR_RESULTS_DOCUMENT_PREFIXES):
        return True

    return False


def student_has_uploaded_doc(project, doc_key):
    if project and project.id and project.student_id:
        return (
            db.session.query(MbaProjectDocument.id)
            .filter_by(
                project_id=project.id,
                doc_type=doc_key,
                uploaded_by_id=project.student_id,
            )
            .first()
            is not None
        )
    return any(doc.doc_type == doc_key and doc.uploaded_by_id == project.student_id for doc in project.documents)


def student_submitted_assessor_prerequisite_docs(project):
    return (
        bool(project and project.jbs5_hdc_approved_at)
        and jbs10_supervisor_signed(project)
        and intent_to_submit_supervisor_signed(project)
    )


def can_request_moodle_manuscript_submission(project):
    return bool(
        project
        and project.student
        and project.student.email
        and student_submitted_assessor_prerequisite_docs(project)
        and assessor_hr_documents_sent(project)
        and not uploaded_doc_for(project, "dissertation")
    )


def assessor_acceptance_pack_complete(project, slot):
    assessor_id = getattr(project, f"{slot}_id", None)
    if not assessor_id:
        return False
    required_doc_types = (
        assessor_temp_appointment_doc_type(slot),
        assessor_temp_claim_doc_type(slot),
        assessor_cv_doc_type(slot),
        assessor_highest_qualification_doc_type(slot),
    )
    for doc_type in required_doc_types:
        doc = uploaded_doc_for(project, doc_type)
        if not doc or doc.uploaded_by_id != assessor_id:
            return False
    return True


def all_assessor_acceptance_packs_complete(project):
    return all(assessor_acceptance_pack_complete(project, slot) for slot in ASSESSOR_SLOTS)


def apply_assessor_suggestions_if_ready(project):
    """Fill missing assessor slots after HDC-approved JBS5 and supervisor-signed JBS10/Intent submissions."""
    if not project or project.assessors_confirmed:
        return []
    if not (project.supervisor_confirmed or project.supervisor_accepted_at):
        return []
    if not project.jbs5_hdc_approved_at:
        return []
    if not student_submitted_assessor_prerequisite_docs(project):
        return []

    excluded_user_ids = {project.primary_supervisor_id} if project.primary_supervisor_id else set()
    for slot in ASSESSOR_SLOTS:
        existing_assessor_id = getattr(project, f"{slot}_id")
        if existing_assessor_id:
            excluded_user_ids.add(existing_assessor_id)

    ranked_assessors = recommend_assessors(
        project,
        examiners_query().all(),
        excluded_user_ids=excluded_user_ids,
        limit=len(ASSESSOR_SLOTS),
        workload_by_user_id=assessor_workload_counts(exclude_project_id=getattr(project, "id", None)),
    )
    suggested_assessors = [item["user"] for item in ranked_assessors]
    if not suggested_assessors:
        return []

    applied_assessors = []
    for slot in ASSESSOR_SLOTS:
        if getattr(project, f"{slot}_id"):
            continue
        if not suggested_assessors:
            break
        assessor = suggested_assessors.pop(0)
        setattr(project, f"{slot}_id", assessor.id)
        setattr(project, f"{slot}_invitation_status", None)
        applied_assessors.append(assessor)

    if applied_assessors:
        project.assessors_confirmed = False
        project.assessors_nominated_at = None
        project.nomination_form_submitted = False
        assessor_emails = ", ".join(assessor.email for assessor in applied_assessors)
        project.comments = append_comment(
            project.comments,
            f"System suggested assessors after supervisor-signed JBS10 and Intent to Submit were ready: {assessor_emails}",
        )

    return applied_assessors


def mba_admin_notification_emails():
    admin_users = MbaUser.query.filter(MbaUser.role.in_([MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value])).all()
    return [admin.email for admin in admin_users if admin.email]


def assessor_hdc_decision_alert_label(decision):
    return {
        HDC_ASSESSOR_APPROVED: "Approved",
        HDC_ASSESSOR_DECLINED: "Rejected",
    }.get(decision, "Pending Review")


def hdc_assessor_nomination_decision_lines(project):
    lines = []
    for slot in PRIMARY_ASSESSOR_SLOTS:
        assessor = getattr(project, slot, None)
        label = INVITATION_SLOTS.get(slot, {}).get("label", slot.replace("_", " ").title())
        name = _user_display(assessor) if assessor else "Unassigned"
        email = assessor.email if assessor and assessor.email else "No email"
        decision = assessor_hdc_decision_alert_label(assessor_hdc_decision(project, slot))
        lines.append(f"{label}: {name} ({email}) - {decision}")
    return lines


def hdc_assessor_nomination_decision_summary(project):
    return "; ".join(hdc_assessor_nomination_decision_lines(project))


def hdc_assessor_nomination_admin_email_messages(project, decided_by_email=None):
    recipients = [email for email in dict.fromkeys(mba_admin_notification_emails()) if email]
    if not recipients:
        return []

    if project.project_status == ProjectStatus.HDC_VERIFIED.value:
        outcome = "approved"
        action_text = "The assessor nominations have been approved. Please send the approved temporary appointment and claim forms to HR before continuing."
    elif project.project_status == ProjectStatus.HDC_DECLINED.value:
        outcome = "rejected"
        action_text = "One or more assessor nominations were rejected. Please replace the rejected assessor before forwarding nominations to HDC again."
    else:
        outcome = "updated"
        action_text = "One assessor nomination has been reviewed. No admin action is required until HDC completes the remaining nomination review."

    decision_lines = "\n".join(hdc_assessor_nomination_decision_lines(project))
    reviewer_line = f"\nReviewed by: {decided_by_email}" if decided_by_email else ""
    body = (
        f"HDC has {outcome} the assessor nomination decision for this MBA Capstone Project.\n\n"
        f"Project: {project.project_title}\n"
        f"Student: {project.student.email if project.student else 'Unknown'}\n"
        f"Discipline: {project.discipline_name}{reviewer_line}\n\n"
        f"HDC decision:\n{decision_lines}\n\n"
        f"{action_text}"
    )
    return [
        {
            "recipient": recipient,
            "subject": f"HDC Assessor Nomination Decision: {project.project_title}",
            "body": body,
        }
        for recipient in recipients
    ]


def hdc_results_admin_email_messages(project, decided_by_email=None):
    recipients = [email for email in dict.fromkeys(mba_admin_notification_emails()) if email]
    if not recipients:
        return []

    approved = project.project_status == ProjectStatus.RESULTS_APPROVED.value
    outcome = "approved" if approved else "rejected"
    action_text = (
        "Please open the MBA Admin Capstone Project queue and release the HDC-approved results to the supervisor."
        if approved
        else "Please open the MBA Admin Capstone Project queue and follow up on the rejected results."
    )
    reviewer_line = f"\nReviewed by: {decided_by_email}" if decided_by_email else ""
    reviewed_line = (
        f"\nReviewed at: {project.results_hdc_reviewed_at.strftime('%d %b %Y %H:%M')}"
        if project.results_hdc_reviewed_at
        else ""
    )
    dashboard_url = url_for("mba.admin_dashboard", panel="projects", status="results_approved" if approved else "results_declined", _external=True)
    body = (
        f"HDC has {outcome} the assessment results for this MBA Capstone Project.\n\n"
        f"Project: {project.project_title}\n"
        f"Student: {project.student.email if project.student else 'Unknown'}\n"
        f"Discipline: {project.discipline_name}{reviewer_line}{reviewed_line}\n\n"
        f"{action_text}\n\n"
        f"Admin queue: {dashboard_url}"
    )
    return [
        {
            "recipient": recipient,
            "subject": f"HDC Results {outcome.title()}: {project.project_title}",
            "body": body,
        }
        for recipient in recipients
    ]


def project_supervisor_notification_emails(project):
    emails = []
    if project.primary_supervisor and project.primary_supervisor.email:
        emails.append(project.primary_supervisor.email)
    for invitation in getattr(project, "supervisor_invitations", []):
        supervisor = invitation.supervisor
        if invitation.status == INVITATION_ACCEPTED and supervisor and supervisor.email:
            emails.append(supervisor.email)
    return list(dict.fromkeys(emails))


def corrections_requested_email_messages(project, correction_request):
    recommendation = correction_request.get("recommendation") or "Corrections requested"
    detailed_report_filename = correction_request.get("detailed_report_filename") or ""
    detailed_report_line = f"\nDetailed report attachment: {detailed_report_filename}\n" if detailed_report_filename else "\n"
    body = (
        f"An assessor requested corrections or raised comments for the MBA Capstone Project '{project.project_title}'.\n\n"
        f"Student: {project.student.email if project.student else 'Unknown'}\n"
        f"Recommendation: {recommendation}\n"
        f"{detailed_report_line}"
        "Only MBA Admin can access the assessor result pack at this stage. "
        "Forward the assessment summary to the supervisor when it is ready for supervisor review."
    )
    recipients = mba_admin_notification_emails()
    deduped_recipients = [email for email in dict.fromkeys(recipients) if email]
    return [
        {
            "recipient": recipient,
            "subject": f"Assessor Comments Await Admin Review: {project.project_title}",
            "body": body,
        }
        for recipient in deduped_recipients
    ]


def supervisor_can_manage_corrections(project, user):
    if not project or not user or user.role != MbaRole.SCHOLAR.value:
        return False
    accepted_invitation = any(
        invitation.supervisor_id == user.id and invitation.status == INVITATION_ACCEPTED
        for invitation in getattr(project, "supervisor_invitations", []) or []
    )
    primary_supervisor_accepted = (
        project.primary_supervisor_id == user.id
        and (
            effective_supervisor_invitation_status(project) == INVITATION_ACCEPTED
            or project.supervisor_accepted_at is not None
        )
    )
    return primary_supervisor_accepted or accepted_invitation


def assessor_slots_for_user(project, user_id):
    return [slot for slot in ALL_ASSESSOR_SLOTS if getattr(project, f"{slot}_id") == user_id]


def all_assessment_results_received(project):
    primary_complete = all(assessment_result_pack_complete(project, slot) for slot in PRIMARY_ASSESSOR_SLOTS)
    if not primary_complete:
        return False
    if additional_assessment_required(project):
        return assessment_result_pack_complete(project, ADDITIONAL_ASSESSOR_SLOT)
    return True


def assessor_can_view_project_documents(project):
    return project.project_status in ASSESSOR_PROJECT_DOCUMENT_VISIBLE_STATUSES


def assessor_can_view_student_dissertation(project):
    return assessor_can_view_project_documents(project) and bool(
        getattr(project, "dissertation_released_to_assessors", False)
    )


def require_mba_user():
    if not current_user.is_authenticated or current_user.system_name != "mba":
        flash("Please log in with an MBA account.", "error")
        return False
    return True


def require_mba_role(*roles):
    if not require_mba_user():
        return False
    if current_user.role not in roles:
        flash("You do not have access to that MBA workspace.", "error")
        return False
    return True


def role_landing_url():
    if current_user.role == MbaRole.STUDENT.value:
        return url_for("mba.student_dashboard")
    if current_user.role == MbaRole.SCHOLAR.value:
        return url_for("mba.scholar_dashboard")
    if current_user.role == MbaRole.EXAMINER.value:
        return url_for("mba.examiner_dashboard")
    if current_user.role == MbaRole.HDC.value:
        return url_for("mba.hdc_dashboard")
    if current_user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
        return url_for("mba.admin_dashboard", panel="projects")
    return url_for("mba.dashboard")


def _profile_missing_labels(fields):
    return [label for label, value in fields if not str(value or "").strip()]


def mba_profile_requires_academic_fields(user):
    return getattr(user, "role", None) in {MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value}


def mba_profile_is_committee(user):
    return getattr(user, "role", None) == MbaRole.HDC.value


def mba_required_signature_types(user):
    role = getattr(user, "role", None)
    if role == MbaRole.HDC.value:
        return (
            USER_SIGNATURE_PRIMARY,
            USER_SIGNATURE_HEAD_OF_DEPARTMENT,
            USER_SIGNATURE_DIRECTOR_OF_SCHOOL,
            USER_SIGNATURE_EXECUTIVE_DEAN,
        )
    if role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
        return (USER_SIGNATURE_PRIMARY,)
    if role in {
        MbaRole.STUDENT.value,
        MbaRole.SCHOLAR.value,
        MbaRole.EXAMINER.value,
    }:
        return (USER_SIGNATURE_PRIMARY,)
    return ()


def mba_profile_requires_signature(user):
    return bool(mba_required_signature_types(user))


def mba_profile_signature_label(user, signature_type):
    role = getattr(user, "role", None)
    signature_type = normalize_user_signature_type(signature_type)
    if signature_type == USER_SIGNATURE_PRIMARY and role == MbaRole.HDC.value:
        return "HDC / Committee Chair signature"
    if signature_type == USER_SIGNATURE_PRIMARY and role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
        return "Program Manager signature"
    return user_signature_type_label(signature_type)


def mba_profile_signature_slots(user):
    slots = []
    for signature_type in mba_required_signature_types(user):
        label = mba_profile_signature_label(user, signature_type)
        slots.append(
            {
                "type": signature_type,
                "label": label,
                "required": True,
            }
        )
    return slots


def mba_profile_missing_fields(user, *, profile=None, submitted_student_number=None, include_signature=True):
    if getattr(user, "system_name", None) != "mba":
        return []

    role = getattr(user, "role", None)
    if role == MbaRole.STUDENT.value:
        profile = profile or getattr(user, "student_profile", None)
        missing = _profile_missing_labels(
            [
                ("title", getattr(profile, "title", None)),
                ("first name", getattr(profile, "name", None) or getattr(user, "first_name", None)),
                ("surname", getattr(profile, "surname", None) or getattr(user, "last_name", None)),
                ("contact number", getattr(profile, "contact", None)),
                ("student number", submitted_student_number if submitted_student_number is not None else getattr(profile, "student_number", None)),
                ("ID / passport number", getattr(profile, "id_passport_number", None)),
                ("module", getattr(profile, "module", None)),
                ("block", getattr(profile, "block_id", None)),
                ("degree", getattr(profile, "degree", None)),
                ("address", getattr(profile, "address", None)),
                ("postal code", getattr(profile, "postal_code", None)),
                ("default signing location", getattr(profile, "default_signing_location", None)),
            ]
        )
    elif role in {
        MbaRole.MAIN_ADMIN.value,
        MbaRole.ADMIN.value,
        MbaRole.SCHOLAR.value,
        MbaRole.EXAMINER.value,
        MbaRole.HDC.value,
    }:
        profile = profile or getattr(user, "scholar_profile", None)
        if role == MbaRole.HDC.value:
            fields = [
                ("committee name", getattr(profile, "name", None)),
                ("committee contact number", getattr(profile, "contact", None)),
                ("department", getattr(profile, "department", None)),
                ("affiliation", getattr(profile, "affiliation", None)),
                ("office address", getattr(profile, "address", None)),
                ("postal code", getattr(profile, "postal_code", None)),
                ("default signing location", getattr(profile, "default_signing_location", None)),
            ]
        else:
            fields = [
                ("title", getattr(profile, "title", None)),
                ("first name", getattr(profile, "name", None) or getattr(user, "first_name", None)),
                ("surname", getattr(profile, "surname", None) or getattr(user, "last_name", None)),
                ("contact number", getattr(profile, "contact", None)),
                ("department", getattr(profile, "department", None)),
                ("position", getattr(profile, "position", None)),
                ("affiliation", getattr(profile, "affiliation", None)),
                ("address", getattr(profile, "address", None)),
                ("postal code", getattr(profile, "postal_code", None)),
                ("default signing location", getattr(profile, "default_signing_location", None)),
            ]
        if mba_profile_requires_academic_fields(user):
            fields.extend(
                [
                    ("ID / passport number", getattr(profile, "id_passport_number", None)),
                    ("highest qualification", getattr(profile, "qualification", None)),
                    ("areas of expertise", getattr(profile, "skills", None)),
                    ("research themes", getattr(profile, "research_themes", None)),
                    ("research interests", getattr(profile, "research_interests", None)),
                    ("research disciplines", getattr(profile, "research_disciplines", None)),
                ]
            )
        missing = _profile_missing_labels(fields)
    else:
        missing = []

    if include_signature and mba_profile_requires_signature(user):
        for signature_type in mba_required_signature_types(user):
            label = mba_profile_signature_label(user, signature_type)
            if user_has_signature(user, signature_type):
                if (
                    (signature_type != USER_SIGNATURE_PRIMARY or role == MbaRole.HDC.value)
                    and not user_signature_printed_name(user, signature_type)
                ):
                    missing.append(f"{label} printed name")
                continue
            missing.append(label)
    return missing


def mba_user_requires_profile_completion(user):
    return bool(mba_profile_missing_fields(user))


@mba_bp.before_request
def require_profile_completion_before_workspace_access():
    if not current_user.is_authenticated:
        return None
    if request.endpoint in {"mba.profile", "mba.profile_signature_image"}:
        return None
    missing_fields = mba_profile_missing_fields(current_user)
    if not missing_fields:
        return None
    preview = ", ".join(missing_fields[:6])
    suffix = "..." if len(missing_fields) > 6 else ""
    flash(f"Complete your MBA profile before opening your dashboard: {preview}{suffix}.", "info")
    return redirect(url_for("mba.profile"))


def mba_kpis():
    total_projects = MbaProject.query.count()
    submitted_projects = MbaProject.query.filter(MbaProject.project_status != ProjectStatus.CREATED.value).count()
    draft_projects = MbaProject.query.filter(MbaProject.project_status == ProjectStatus.CREATED.value).count()
    return {
        "students": MbaUser.query.filter_by(role=MbaRole.STUDENT.value).count(),
        "supervisors": MbaUser.query.filter(
            MbaUser.role == MbaRole.SCHOLAR.value,
            MbaUser.scholar_role.in_([MbaScholarRole.SUPERVISOR.value, MbaScholarRole.BOTH.value]),
        ).count(),
        "examiners": MbaUser.query.filter(
            (MbaUser.role == MbaRole.EXAMINER.value)
            | (
                (MbaUser.role == MbaRole.SCHOLAR.value)
                & MbaUser.scholar_role.in_([MbaScholarRole.EXAMINER.value, MbaScholarRole.BOTH.value])
            )
        ).count(),
        "projects": total_projects,
        "submitted_projects": submitted_projects,
        "draft_projects": draft_projects,
        "admin_queue": MbaProject.query.filter_by(project_status=ProjectStatus.ADMIN_SUBMITTED.value).count(),
        "hdc_queue": MbaProject.query.filter(
            MbaProject.project_status.in_(
                [
                    ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
                    ProjectStatus.ADMIN_APPROVED.value,
                    ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
                ]
            )
        ).count(),
    }


def supervisors_query():
    return MbaUser.query.filter(
        MbaUser.role == MbaRole.SCHOLAR.value,
        MbaUser.scholar_role.in_([MbaScholarRole.SUPERVISOR.value, MbaScholarRole.BOTH.value]),
        MbaUser.is_active.is_(True),
    ).order_by(MbaUser.email)


def examiners_query():
    return MbaUser.query.filter(
        (MbaUser.role == MbaRole.EXAMINER.value)
        | (
            (MbaUser.role == MbaRole.SCHOLAR.value)
            & MbaUser.scholar_role.in_([MbaScholarRole.EXAMINER.value, MbaScholarRole.BOTH.value])
        ),
        MbaUser.is_active.is_(True),
    ).order_by(MbaUser.email)


def disciplines_query(include_inactive=False):
    query = MbaDiscipline.query
    if not include_inactive:
        query = query.filter(MbaDiscipline.is_active.is_(True))
    return query.order_by(MbaDiscipline.sort_order.asc(), MbaDiscipline.name.asc())


def selected_discipline_from_form():
    raw_id = (request.form.get("discipline_id") or "").strip()
    if raw_id:
        try:
            discipline_id = int(raw_id)
        except ValueError:
            discipline_id = None
        else:
            discipline = db.session.get(MbaDiscipline, discipline_id)
            if discipline and discipline.is_active:
                return discipline

    legacy_name = (request.form.get("discipline") or "").strip()
    if legacy_name:
        return MbaDiscipline.query.filter(db.func.lower(MbaDiscipline.name) == legacy_name.lower()).first()
    return None


def profile_role_label(user):
    if user.role == MbaRole.STUDENT.value:
        return "Student"
    if user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
        return "Admin"
    if user.role == MbaRole.HDC.value:
        return "Higher Degree Committee"
    if user.is_supervisor_role() and user.is_examiner_role():
        return "Supervisor and Assessor"
    if user.is_supervisor_role():
        return "Supervisor"
    if user.is_examiner_role():
        return "Assessor"
    return user.role.replace("_", " ").title()


def parse_non_negative_int(value, fallback=0):
    try:
        parsed = int((value or "").strip())
    except (TypeError, ValueError, AttributeError):
        return fallback
    return parsed if parsed >= 0 else fallback


def parse_positive_int(value, fallback=1):
    parsed = parse_non_negative_int(value, fallback)
    return parsed if parsed >= 1 else fallback


def parse_page_size(value, default=5, allowed_sizes=DASHBOARD_PAGE_SIZE_OPTIONS):
    parsed = parse_positive_int(value, default)
    return parsed if parsed in allowed_sizes else default


def request_query_args(exclude=None):
    excluded = set(exclude or [])
    return {
        key: value
        for key, value in request.args.items()
        if key not in excluded and value not in (None, "")
    }


def _pagination_window(page, total_pages):
    if total_pages <= 7:
        return list(range(1, total_pages + 1))

    pages = {1, total_pages, page}
    for candidate in range(page - 1, page + 2):
        if 1 <= candidate <= total_pages:
            pages.add(candidate)

    if page <= 4:
        pages.update(range(1, min(total_pages, 5) + 1))
    if page >= total_pages - 3:
        pages.update(range(max(1, total_pages - 4), total_pages + 1))

    ordered = sorted(pages)
    window = []
    previous = None
    for number in ordered:
        if previous is not None and number - previous > 1:
            window.append(None)
        window.append(number)
        previous = number
    return window


def build_pagination(
    endpoint,
    total,
    page,
    per_page,
    *,
    page_param="page",
    per_page_param="per_page",
    base_args=None,
    anchor=None,
    page_size_options=DASHBOARD_PAGE_SIZE_OPTIONS,
):
    total = max(int(total or 0), 0)
    per_page = parse_page_size(per_page, default=page_size_options[0], allowed_sizes=page_size_options)
    total_pages = max(1, (total + per_page - 1) // per_page) if total else 1
    page = min(parse_positive_int(page, 1), total_pages)
    start_index = ((page - 1) * per_page) + 1 if total else 0
    end_index = min(page * per_page, total) if total else 0
    base_args = dict(base_args or {})

    def build_url(page_number, size=None):
        params = dict(base_args)
        params[page_param] = page_number
        params[per_page_param] = size or per_page
        if anchor:
            params["_anchor"] = anchor
        return url_for(endpoint, **params)

    page_links = []
    for number in _pagination_window(page, total_pages):
        if number is None:
            page_links.append({"is_gap": True})
            continue
        page_links.append(
            {
                "number": number,
                "url": build_url(number),
                "is_active": number == page,
                "is_gap": False,
            }
        )

    form_action_params = {"_anchor": anchor} if anchor else {}
    form_action = url_for(endpoint, **form_action_params)
    hidden_fields = [{"name": key, "value": value} for key, value in base_args.items()]

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "start_index": start_index,
        "end_index": end_index,
        "page_param": page_param,
        "per_page_param": per_page_param,
        "page_size_options": page_size_options,
        "prev_url": build_url(page - 1) if page > 1 else None,
        "next_url": build_url(page + 1) if page < total_pages else None,
        "page_links": page_links,
        "form_action": form_action,
        "hidden_fields": hidden_fields,
        "has_multiple_pages": total_pages > 1,
    }


def paginate_list(
    items,
    page,
    per_page,
    endpoint,
    *,
    page_param="page",
    per_page_param="per_page",
    base_args=None,
    anchor=None,
    page_size_options=DASHBOARD_PAGE_SIZE_OPTIONS,
):
    items = list(items)
    pagination = build_pagination(
        endpoint,
        len(items),
        page,
        per_page,
        page_param=page_param,
        per_page_param=per_page_param,
        base_args=base_args,
        anchor=anchor,
        page_size_options=page_size_options,
    )
    start = max(pagination["start_index"] - 1, 0)
    end = pagination["end_index"]
    return items[start:end], pagination


def paginate_query(
    query,
    page,
    per_page,
    endpoint,
    *,
    page_param="page",
    per_page_param="per_page",
    base_args=None,
    anchor=None,
    page_size_options=DASHBOARD_PAGE_SIZE_OPTIONS,
):
    per_page = parse_page_size(per_page, default=page_size_options[0], allowed_sizes=page_size_options)
    total = query.order_by(None).count()
    pagination = build_pagination(
        endpoint,
        total,
        page,
        per_page,
        page_param=page_param,
        per_page_param=per_page_param,
        base_args=base_args,
        anchor=anchor,
        page_size_options=page_size_options,
    )
    offset = max(pagination["start_index"] - 1, 0)
    items = query.offset(offset).limit(pagination["per_page"]).all()
    return items, pagination


def reset_invitation_tracking(project):
    project.invitations_sent_at = None
    for meta in INVITATION_SLOTS.values():
        setattr(project, meta["status_field"], None)
    reset_assessor_invitation_tracking(project)


def has_complete_assignment(project):
    if not getattr(project, "primary_supervisor_id", None):
        return False
    return all(getattr(project, f"{slot}_id") for slot in ASSESSOR_SLOTS)


def invitation_status_or_not_sent(project, status_field):
    status_value = getattr(project, status_field)
    return status_value if status_value else "not_sent"


def project_has_any_invitation_response(project):
    if getattr(project, "primary_supervisor_invitation_status") in {INVITATION_PENDING, INVITATION_ACCEPTED, INVITATION_DECLINED}:
        return True
    return any(
        getattr(project, f"{slot}_invitation_status") in {INVITATION_PENDING, INVITATION_ACCEPTED, INVITATION_DECLINED}
        for slot in ASSESSOR_SLOTS
    )


def project_has_sent_invitations(project):
    return bool(project.invitations_sent_at) or project_has_any_invitation_response(project)


def project_has_jbs5_document(project):
    return any(doc.doc_type == "jbs5" for doc in getattr(project, "documents", []) or [])


def project_has_active_supervisor_assignment(project):
    if not project:
        return False
    if getattr(project, "supervisor_accepted_at", None):
        return True
    if getattr(project, "primary_supervisor_id", None):
        primary_status = getattr(project, "primary_supervisor_invitation_status", None)
        if primary_status in {None, "", INVITATION_PENDING, INVITATION_ACCEPTED}:
            return True
    for invitation in getattr(project, "supervisor_invitations", []) or []:
        if invitation.status == INVITATION_ACCEPTED:
            return True
        if invitation.status == INVITATION_PENDING and supervisor_invitation_has_been_sent(project, invitation):
            return True
    return False


def project_eligible_for_supervisor_pool_release(project):
    return (
        bool(project)
        and getattr(project, "project_status", None) == ProjectStatus.ADMIN_SUBMITTED.value
        and project_has_jbs5_document(project)
        and not project_has_active_supervisor_assignment(project)
    )


def project_available_for_supervisor_pool(project):
    return (
        project_eligible_for_supervisor_pool_release(project)
        and bool(getattr(project, "supervisor_pool_released_at", None))
    )


def apply_auto_assignments(project, supervisors, examiners):
    recommendations = match_recommendations(
        project,
        supervisors,
        examiners,
        supervisor_workload_by_user_id=supervisor_workload_counts(exclude_project_id=getattr(project, "id", None)),
        assessor_workload_by_user_id=assessor_workload_counts(exclude_project_id=getattr(project, "id", None)),
    )
    invited_supervisors = [item["user"] for item in recommendations["ranked_supervisors"][:SUPERVISOR_SUGGESTION_LIMIT]]
    project.supervisor_invitations.clear()
    for sup in invited_supervisors:
        invitation = MbaProjectSupervisorInvitation(
            project=project,
            supervisor=sup,
            status="not_sent",
            invited_at=datetime.utcnow(),
        )
        project.supervisor_invitations.append(invitation)
    project.assignment_confirmed = False
    reset_invitation_tracking(project)
    return recommendations


JBS5_ADMIN_SUBMISSION_STATUSES = {
    ProjectStatus.CREATED.value,
    ProjectStatus.ADMIN_DECLINED.value,
    ProjectStatus.JBS5_HDC_DECLINED.value,
}


def _refresh_existing_form_document(project, doc_type, form_type, payload, uploaded_by_id=None):
    existing_doc = MbaProjectDocument.query.filter_by(project_id=project.id, doc_type=doc_type).first()
    if not existing_doc:
        return

    project_dir = os.path.join(_uploads_dir(), str(project.id))
    file_bytes, file_extension, mime_type = generate_form_submission_download_bytes(project, form_type, payload)
    stored_file_bytes = encrypt_sensitive_document_bytes(doc_type, file_bytes)
    original_name = f"{doc_type}_form.{file_extension}"
    unique_name = f"{doc_type}_{uuid.uuid4().hex[:8]}_form.{file_extension}"

    old_path = os.path.join(project_dir, existing_doc.stored_name or "")
    if existing_doc.stored_name and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    existing_doc.original_name = original_name
    existing_doc.stored_name = unique_name
    existing_doc.file_data = stored_file_bytes
    existing_doc.mime_type = mime_type
    existing_doc.file_size = len(file_bytes)
    existing_doc.uploaded_by_id = uploaded_by_id or existing_doc.uploaded_by_id or project.student_id
    existing_doc.uploaded_at = datetime.utcnow()


def reset_jbs5_review_state(project, *, clear_supervisor_signature=True, clear_hdc_signature=True):
    """Return JBS5 to an editable review state without losing the accepted supervisor."""
    project.title_approved = False
    project.jbs5_hdc_approved_at = None

    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if jbs5_form and isinstance(jbs5_form.payload, dict):
        payload = dict(jbs5_form.payload or {})
        if clear_supervisor_signature:
            for field in (
                "supervisor_signature",
                "supervisor_signature_date",
                "supervisor_signature_user_id",
                "supervisor_signature_email",
            ):
                payload.pop(field, None)
            clear_signature_snapshots(payload, ("supervisor_signature",))
            jbs5_form.supervisor_signed = False
        if clear_hdc_signature:
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)
            payload.pop("head_of_department_signature", None)
            payload.pop("head_of_department_signature_date", None)
            clear_signature_snapshots(payload, ("jbs_hdc_signature", "head_of_department_signature"))
        jbs5_form.payload = payload
    elif jbs5_form and clear_supervisor_signature:
        jbs5_form.supervisor_signed = False

    if clear_hdc_signature:
        jbs10_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs10").first()
        if jbs10_form and isinstance(jbs10_form.payload, dict):
            payload = dict(jbs10_form.payload or {})
            payload.pop("supervisor_signature", None)
            payload.pop("supervisor_signature_date", None)
            payload.pop("supervisor_signature_user_id", None)
            payload.pop("supervisor_signature_email", None)
            clear_signature_snapshots(payload, ("supervisor_signature",))
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)
            payload.pop("head_of_department_signature", None)
            payload.pop("head_of_department_signature_date", None)
            clear_signature_snapshots(payload, ("jbs_hdc_signature", "head_of_department_signature"))
            jbs10_form.payload = payload
            jbs10_form.supervisor_signed = False
            _refresh_existing_form_document(
                project,
                "jbs10",
                "jbs10",
                payload,
                uploaded_by_id=project.student_id,
            )

    project.nomination_form_approved = False
    project.nomination_form_submitted = False
    project.assessors_confirmed = False
    project.assessors_nominated_at = None
    reset_assessor_invitation_tracking(project)
    project.results_hdc_decision = None
    project.results_submitted_to_hdc_at = None
    project.results_hdc_reviewed_at = None
    project.results_hdc_approved_mark = None
    project.results_hdc_approved_classification = None
    project.results_released_to_supervisor_at = None
    project.dissertation_released_to_assessors = False
    project.dissertation_released_at = None
    project.assessment_results_forwarded_to_supervisor_at = None
    project.corrections_released_to_student_at = None
    reset_assessor_hdc_decisions(project)


def sync_project_from_saved_jbs5(project):
    existing_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if not existing_form or not isinstance(existing_form.payload, dict):
        return None

    payload = dict(existing_form.payload or {})
    updated = False
    raw_research_title = (payload.get("research_title") or "").strip()
    title_error = project_title_validation_error(raw_research_title)
    if title_error:
        return title_error
    research_title = format_project_title(raw_research_title)
    abstract = (payload.get("abstract") or "").strip()

    if raw_research_title and research_title != raw_research_title:
        payload["research_title"] = research_title
        existing_form.payload = payload
        updated = True
    if research_title and research_title != project.project_title:
        project.project_title = research_title
        updated = True
    if abstract and abstract != project.project_description:
        project.project_description = abstract
        updated = True

    if updated:
        project.comments = append_comment(project.comments, "Synced project details from saved JBS 5 form before admin submission")
    return None


def submit_project_to_admin_from_jbs5(project, supervisors=None, examiners=None):
    if project.project_status not in JBS5_ADMIN_SUBMISSION_STATUSES:
        return False
    if not _project_has_document(project.id, "jbs5"):
        raise ValueError("Complete your JBS 5 form before submitting this Capstone Project.")

    title_error = sync_project_from_saved_jbs5(project)
    if title_error:
        raise ValueError(title_error)

    if project.project_status in {ProjectStatus.ADMIN_DECLINED.value, ProjectStatus.JBS5_HDC_DECLINED.value}:
        reset_jbs5_review_state(project, clear_supervisor_signature=True, clear_hdc_signature=True)

    existing_supervisor_stage = project_has_active_supervisor_assignment(project)
    if existing_supervisor_stage:
        project.comments = append_comment(
            project.comments,
            "Student resubmitted revised JBS5; existing supervisor assignment was preserved.",
        )
        if project.supervisor_accepted_at or project.primary_supervisor_invitation_status == INVITATION_ACCEPTED:
            project.primary_supervisor_invitation_status = INVITATION_ACCEPTED
            project.supervisor_confirmed = True
            project.assignment_confirmed = True
    else:
        supervisors = supervisors if supervisors is not None else supervisors_query().all()
        examiners = examiners if examiners is not None else examiners_query().all()
        auto_recommendations = apply_auto_assignments(project, supervisors, examiners)
        auto_supervisor = auto_recommendations["supervisor"].email if auto_recommendations["supervisor"] else "none"
        auto_assessors = ", ".join(user.email for user in auto_recommendations["assessors"]) or "none"
        project.comments = append_comment(
            project.comments,
            f"System suggested assignments: supervisor={auto_supervisor}; assessors={auto_assessors}",
        )
    project.project_status = ProjectStatus.ADMIN_SUBMITTED.value
    project.comments = append_comment(
        project.comments,
        "Student submitted JBS 5; Capstone Project automatically submitted to admin",
    )
    return True


def sign_student_jbs5_as_supervisor(project, supervisor_name, signature_date=None, supervisor_user=None):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if not jbs5_form or not isinstance(jbs5_form.payload, dict):
        raise ValueError("The student must submit JBS5 before the supervisor can sign it.")

    signature_date = signature_date or datetime.utcnow().strftime("%Y-%m-%d")
    payload = dict(jbs5_form.payload or {})
    payload["supervisor_signature"] = supervisor_name
    payload["supervisor_signature_date"] = signature_date
    if supervisor_user is not None:
        payload["supervisor_signature_user_id"] = str(getattr(supervisor_user, "id", "") or "")
        payload["supervisor_signature_email"] = getattr(supervisor_user, "email", "") or ""
        refresh_saved_signature_snapshot(payload, ("supervisor_signature",), supervisor_user)
    jbs5_form.payload = payload
    jbs5_form.supervisor_signed = True
    _refresh_existing_form_document(
        project,
        "jbs5",
        "jbs5",
        payload,
        uploaded_by_id=getattr(project, "student_id", None),
    )
    project.comments = append_comment(
        project.comments,
        f"Supervisor signed the student-submitted JBS5 form ({supervisor_name})",
    )
    return jbs5_form


def sign_student_jbs10_as_supervisor(project, supervisor_name, signature_date=None, supervisor_user=None):
    form = jbs10_form(project)
    if not form or not isinstance(form.payload, dict):
        raise ValueError("The student must submit JBS10 before the supervisor can sign it.")

    signature_date = signature_date or datetime.utcnow().strftime("%Y-%m-%d")
    payload = dict(form.payload or {})
    payload["supervisor_signature"] = supervisor_name
    payload["supervisor_signature_date"] = signature_date
    payload.pop("_supervisor_return_requested_at", None)
    payload.pop("_supervisor_return_request", None)
    payload["_supervisor_return_resolved_at"] = datetime.utcnow().isoformat()
    if supervisor_user is not None:
        payload["supervisor_signature_user_id"] = str(getattr(supervisor_user, "id", "") or "")
        payload["supervisor_signature_email"] = getattr(supervisor_user, "email", "") or ""
        refresh_saved_signature_snapshot(payload, ("supervisor_signature",), supervisor_user)
    form.payload = payload
    form.supervisor_signed = True
    _refresh_existing_form_document(
        project,
        "jbs10",
        "jbs10",
        payload,
        uploaded_by_id=getattr(project, "student_id", None),
    )
    project.comments = append_comment(
        project.comments,
        f"Supervisor signed the student-submitted JBS10 form ({supervisor_name})",
    )
    return form


def sign_jbs1_declaration_as_supervisor(project, supervisor_name, signature_date=None, supervisor_user=None):
    form = jbs1_declaration_form(project)
    if not form or not isinstance(form.payload, dict):
        raise ValueError("The student must submit JBS 1 Declaration before the supervisor can sign it.")

    signature_date = signature_date or datetime.utcnow().strftime("%Y-%m-%d")
    payload = dict(form.payload or {})
    payload["supervisor_signature"] = supervisor_name
    payload["supervisor_signature_date"] = signature_date
    if supervisor_user is not None:
        payload["supervisor_signature_user_id"] = str(getattr(supervisor_user, "id", "") or "")
        payload["supervisor_signature_email"] = getattr(supervisor_user, "email", "") or ""
        refresh_saved_signature_snapshot(payload, ("supervisor_signature",), supervisor_user)
    form.payload = payload
    form.supervisor_signed = True
    _refresh_existing_form_document(
        project,
        "jbs1_declaration",
        "jbs1_declaration",
        payload,
        uploaded_by_id=getattr(project, "student_id", None),
    )
    project.comments = append_comment(
        project.comments,
        f"Supervisor signed the student-submitted JBS 1 Declaration ({supervisor_name})",
    )
    return form


def sign_jbs1_declaration_as_program_manager(project, program_manager_name, signature_date=None, admin_user=None, office_values=None):
    form = jbs1_declaration_form(project)
    if not form or not isinstance(form.payload, dict):
        raise ValueError("The student must submit JBS 1 Declaration before Admin can sign it.")
    if not jbs1_supervisor_signed(project):
        raise ValueError("The supervisor must sign JBS 1 Declaration before Admin signs as Program Manager.")

    signature_date = signature_date or datetime.utcnow().strftime("%Y-%m-%d")
    payload = dict(form.payload or {})
    for field, value in (office_values or {}).items():
        payload[field] = value
    payload["office_program_manager"] = program_manager_name
    payload["office_program_manager_date"] = signature_date
    if admin_user is not None:
        payload["office_program_manager_user_id"] = str(getattr(admin_user, "id", "") or "")
        payload["office_program_manager_email"] = getattr(admin_user, "email", "") or ""
        refresh_saved_signature_snapshot(payload, ("office_program_manager",), admin_user)
    form.payload = payload
    _refresh_existing_form_document(
        project,
        "jbs1_declaration",
        "jbs1_declaration",
        payload,
        uploaded_by_id=getattr(project, "student_id", None),
    )
    project.comments = append_comment(
        project.comments,
        f"MBA Admin signed JBS 1 Declaration as Program Manager ({program_manager_name})",
    )
    return form


def sign_intent_to_submit_as_supervisor(project, supervisor_name, supervisor_user=None):
    form = intent_to_submit_form(project)
    if not form or not isinstance(form.payload, dict):
        raise ValueError("The student must submit Intent to Submit before the supervisor can sign it.")

    payload = dict(form.payload or {})
    payload["supervisor_agree_signature"] = supervisor_name
    payload.pop("supervisor_disagree_signature", None)
    payload.pop("co_supervisor_agree_signature", None)
    payload.pop("co_supervisor_disagree_signature", None)
    payload.pop("disagree_reasons", None)
    payload.pop("disagree_reasons_date", None)
    clear_signature_snapshots(
        payload,
        (
            "supervisor_agree_signature",
            "supervisor_disagree_signature",
            "co_supervisor_agree_signature",
            "co_supervisor_disagree_signature",
        ),
    )
    if supervisor_user is not None:
        payload["supervisor_agree_signature_user_id"] = str(getattr(supervisor_user, "id", "") or "")
        payload["supervisor_agree_signature_email"] = getattr(supervisor_user, "email", "") or ""
        refresh_saved_signature_snapshot(payload, ("supervisor_agree_signature",), supervisor_user)
    form.payload = payload
    form.supervisor_signed = True
    _refresh_existing_form_document(
        project,
        "intent_to_submit",
        "intent_to_submit",
        payload,
        uploaded_by_id=getattr(project, "student_id", None),
    )
    project.comments = append_comment(
        project.comments,
        f"Supervisor signed the student-submitted Intent to Submit form ({supervisor_name})",
    )
    return form


def invitation_status_for_user(project, user_id):
    statuses = []
    for slot, meta in INVITATION_SLOTS.items():
        if getattr(project, meta["id_field"]) == user_id:
            statuses.append(
                {
                    "slot": slot,
                    "label": meta["label"],
                    "status": getattr(project, meta["status_field"]) or INVITATION_PENDING,
                }
            )
    return statuses


def effective_supervisor_invitation_status(project):
    supervisor_invitations = list(getattr(project, "supervisor_invitations", []) or [])
    invitation_statuses = [inv.status for inv in supervisor_invitations if inv.status]
    if INVITATION_ACCEPTED in invitation_statuses:
        return INVITATION_ACCEPTED
    if any(
        inv.status == INVITATION_PENDING and supervisor_invitation_has_been_sent(project, inv)
        for inv in supervisor_invitations
    ):
        return INVITATION_PENDING
    if INVITATION_DECLINED in invitation_statuses:
        return INVITATION_DECLINED
    if "expired" in invitation_statuses:
        return "expired"
    if supervisor_invitations:
        return "not_sent"
    return project.primary_supervisor_invitation_status


def accepted_assessor_count(project):
    return accepted_assessor_count_for_slots(project, PRIMARY_ASSESSOR_SLOTS)


def accepted_assessor_count_for_slots(project, slots):
    return sum(
        1
        for slot in slots
        if getattr(project, f"{slot}_id")
        and getattr(project, f"{slot}_invitation_status") == INVITATION_ACCEPTED
    )


def required_assessor_slots(project):
    return ALL_ASSESSOR_SLOTS if additional_assessment_required(project) else PRIMARY_ASSESSOR_SLOTS


def supervisor_invitation_is_still_valid(project, invitation):
    if not invitation or invitation.status != INVITATION_PENDING:
        return False
    if not supervisor_invitation_has_been_sent(project, invitation):
        return False
    if any(other.status == INVITATION_ACCEPTED for other in getattr(project, "supervisor_invitations", [])):
        return False
    return effective_supervisor_invitation_status(project) != INVITATION_ACCEPTED


def supervisor_invitation_has_been_sent(project, invitation):
    if not invitation:
        return False
    if invitation.status in {INVITATION_ACCEPTED, INVITATION_DECLINED, "expired"}:
        return True
    if invitation.status != INVITATION_PENDING:
        return False
    return bool(
        getattr(project, "invitations_sent_at", None)
        or getattr(project, "primary_supervisor_invitation_status", None) == INVITATION_PENDING
    )


def supervisor_invitation_count_status(project, invitation):
    if not invitation:
        return None
    if invitation.status == INVITATION_PENDING:
        return INVITATION_PENDING if supervisor_invitation_has_been_sent(project, invitation) else "not_sent"
    if invitation.status in {INVITATION_ACCEPTED, INVITATION_DECLINED, "expired"}:
        return invitation.status
    return "not_sent"


def invitation_email_messages(project, include_supervisors=True, include_assessors=True, assessor_slots=None):
    recipients = []
    assessor_slot_filter = set(assessor_slots) if assessor_slots is not None else None
    if include_supervisors:
        for invitation in getattr(project, "supervisor_invitations", []):
            supervisor = invitation.supervisor
            if invitation.status == INVITATION_PENDING and supervisor and supervisor.email:
                recipients.append(
                    {
                        "recipient": supervisor.email,
                        "subject": f"MBA Supervisor Invitation: {project.project_title}",
                        "body": (
                            f"You have been invited to serve as Supervisor for the MBA Capstone Project '{project.project_title}'.\n\n"
                            f"Student: {project.student.email if project.student else 'Unknown'}\n"
                            f"Discipline: {project.discipline_name}\n\n"
                            "Please sign in to the MBA system to accept or decline this invitation."
                        ),
                    }
                )
    if include_assessors:
        for index in range(1, 4):
            slot = f"assessor_{index}"
            if assessor_slot_filter is not None and slot not in assessor_slot_filter:
                continue
            assessor = getattr(project, f"assessor_{index}")
            if (
                not assessor
                or getattr(project, f"assessor_{index}_invitation_status") != INVITATION_PENDING
            ):
                continue
            recipients.append(
                {
                    "recipient": assessor.email,
                    "subject": f"MBA Assessor Invitation: {project.project_title}",
                    "body": (
                        f"You have been invited to serve as Assessor {index} for the MBA Capstone Project '{project.project_title}'.\n\n"
                        f"Student: {project.student.email if project.student else 'Unknown'}\n"
                        f"Discipline: {project.discipline_name}\n\n"
                        "Please sign in to the MBA system to accept or decline this invitation."
                    ),
                }
            )
    return recipients


def _user_display(user, fallback_email=None):
    if user:
        full_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
        return full_name or user.email
    return fallback_email or "Unknown"


def reminder_elapsed_label(sent_at, reference_time=None):
    if not sent_at:
        return "Unknown"
    reference_time = reference_time or datetime.utcnow()
    elapsed = max(reference_time - sent_at, reference_time - reference_time)
    days = elapsed.days
    hours = elapsed.seconds // 3600
    minutes = (elapsed.seconds % 3600) // 60
    if days:
        return f"{days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''}"
    if hours:
        return f"{hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def _reminder_reference_time(*timestamps):
    return next((timestamp for timestamp in timestamps if timestamp), None)


def _reminder_state_map():
    return {state.reminder_key: state for state in MbaReminderState.query.all()}


def _reminder_project_student_number(project):
    profile = project.student.student_profile if project.student and project.student.student_profile else None
    return profile.student_number if profile and profile.student_number else "Not captured"


def _reminder_item(
    *,
    key,
    kind,
    type_label,
    project,
    recipient_email,
    recipient_name,
    sent_at,
    state_map,
    reference_time,
    status="Pending",
    meta=None,
):
    state = state_map.get(key)
    if state and state.dismissed_at:
        return None
    return {
        "key": key,
        "kind": kind,
        "type_label": type_label,
        "project": project,
        "project_id": project.id if project else None,
        "project_title": project.project_title if project else "",
        "student_email": project.student.email if project and project.student else "Unknown",
        "student_number": _reminder_project_student_number(project) if project else "Unknown",
        "recipient_email": recipient_email,
        "recipient_name": recipient_name or recipient_email,
        "sent_at": sent_at,
        "sent_at_label": sent_at.strftime("%d %b %Y %H:%M") if sent_at else "Unknown",
        "elapsed_label": reminder_elapsed_label(sent_at, reference_time=reference_time),
        "status": status,
        "last_sent_at": state.last_sent_at if state else None,
        "last_sent_label": state.last_sent_at.strftime("%d %b %Y %H:%M") if state and state.last_sent_at else "Not sent yet",
        "meta": meta or {},
    }


def admin_pending_reminder_items(reference_time=None):
    reference_time = reference_time or datetime.utcnow()
    state_map = _reminder_state_map()
    items = []

    projects = (
        MbaProject.query.options(
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            joinedload(MbaProject.primary_supervisor),
            joinedload(MbaProject.assessor_1),
            joinedload(MbaProject.assessor_2),
            joinedload(MbaProject.assessor_3),
            joinedload(MbaProject.supervisor_invitations).joinedload(MbaProjectSupervisorInvitation.supervisor),
            joinedload(MbaProject.documents),
        )
        .filter(MbaProject.project_status != ProjectStatus.CREATED.value)
        .all()
    )

    for project in projects:
        for invitation in getattr(project, "supervisor_invitations", []) or []:
            supervisor = invitation.supervisor
            sent_at = _reminder_reference_time(
                invitation.invited_at,
                getattr(project, "invitations_sent_at", None),
                getattr(project, "updated_at", None),
                getattr(project, "created_at", None),
            )
            if (
                supervisor_invitation_is_still_valid(project, invitation)
                and sent_at
                and supervisor
                and supervisor.email
            ):
                item = _reminder_item(
                    key=f"supervisor_invitation:{invitation.id}",
                    kind="supervisor_invitation",
                    type_label="Supervisor Invitation",
                    project=project,
                    recipient_email=supervisor.email,
                    recipient_name=_user_display(supervisor),
                    sent_at=sent_at,
                    state_map=state_map,
                    reference_time=reference_time,
                    meta={"invitation_id": invitation.id},
                )
                if item:
                    items.append(item)

        for slot in required_assessor_slots(project):
            assessor = getattr(project, slot, None)
            sent_at = _reminder_reference_time(
                getattr(project, f"{slot}_invited_at", None),
                getattr(project, "assessors_nominated_at", None),
                getattr(project, "invitations_sent_at", None),
                getattr(project, "updated_at", None),
                getattr(project, "created_at", None),
            )
            if (
                assessor
                and assessor.email
                and getattr(project, f"{slot}_invitation_status") == INVITATION_PENDING
                and sent_at
            ):
                item = _reminder_item(
                    key=f"assessor_invitation:{project.id}:{slot}:{assessor.id}",
                    kind="assessor_invitation",
                    type_label=f"{INVITATION_SLOTS.get(slot, {}).get('label', slot.replace('_', ' ').title())} Invitation",
                    project=project,
                    recipient_email=assessor.email,
                    recipient_name=_user_display(assessor),
                    sent_at=sent_at,
                    state_map=state_map,
                    reference_time=reference_time,
                    meta={"slot": slot},
                )
                if item:
                    items.append(item)

        if (
            project.module_completion_status == "awaiting_marks_committee"
            and project.module_completion_marks_email
            and project.module_completion_verification_token
            and project.module_completion_requested_at
            and not project.module_completion_responded_at
        ):
            item = _reminder_item(
                key=f"module_completion:{project.id}:{project.module_completion_verification_token}",
                kind="module_completion",
                type_label="Module Completion Verification",
                project=project,
                recipient_email=project.module_completion_marks_email,
                recipient_name="Marks Committee Representative",
                sent_at=project.module_completion_requested_at,
                state_map=state_map,
                reference_time=reference_time,
                meta={"token": project.module_completion_verification_token},
            )
            if item:
                items.append(item)

        if (
            project.dissertation_moodle_request_sent_at
            and project.student
            and project.student.email
            and not uploaded_doc_for(project, "dissertation")
        ):
            item = _reminder_item(
                key=(
                    "moodle_manuscript_submission:"
                    f"{project.id}:{project.dissertation_moodle_request_sent_at.isoformat()}"
                ),
                kind="moodle_manuscript_submission",
                type_label="Moodle Capstone Submission",
                project=project,
                recipient_email=project.student.email,
                recipient_name=_user_display(project.student),
                sent_at=project.dissertation_moodle_request_sent_at,
                state_map=state_map,
                reference_time=reference_time,
            )
            if item:
                items.append(item)

        if (
            project_has_active_corrections(project)
            and corrections_released_to_student(project)
            and (
                not student_submitted_corrections_pack(project)
                or supervisor_rejected_corrections(project)
            )
            and project.student
            and project.student.email
        ):
            corrections_reference_at = (
                project.corrections_supervisor_rejected_at
                if supervisor_rejected_corrections(project)
                else project.corrections_released_to_student_at
            )
            item = _reminder_item(
                key=f"corrections_response:{project.id}:{corrections_reference_at.isoformat()}",
                kind="corrections_response",
                type_label="Response to Assessors' Comments",
                project=project,
                recipient_email=project.student.email,
                recipient_name=_user_display(project.student),
                sent_at=corrections_reference_at,
                state_map=state_map,
                reference_time=reference_time,
            )
            if item:
                items.append(item)

        if (
            project_has_active_corrections(project)
            and assessment_results_forwarded_to_supervisor(project)
            and not corrections_released_to_student(project)
        ):
            for supervisor_email in project_supervisor_notification_emails(project):
                item = _reminder_item(
                    key=f"assessment_summary_release:{project.id}:{supervisor_email}:{project.assessment_results_forwarded_to_supervisor_at.isoformat()}",
                    kind="assessment_summary_release",
                    type_label="Assessment Summary Review",
                    project=project,
                    recipient_email=supervisor_email,
                    recipient_name=_user_display(project.primary_supervisor, supervisor_email),
                    sent_at=project.assessment_results_forwarded_to_supervisor_at,
                    state_map=state_map,
                    reference_time=reference_time,
                )
                if item:
                    items.append(item)

        if (
            student_submitted_corrections_pack(project)
            and not supervisor_approved_corrections(project)
            and not supervisor_rejected_corrections(project)
        ):
            corrected_doc = uploaded_doc_for(project, "corrected_dissertation")
            for supervisor_email in project_supervisor_notification_emails(project):
                item = _reminder_item(
                    key=f"corrections_supervisor_approval:{project.id}:{supervisor_email}:{corrected_doc.id}",
                    kind="corrections_supervisor_approval",
                    type_label="Corrected Response Pack Approval",
                    project=project,
                    recipient_email=supervisor_email,
                    recipient_name=_user_display(project.primary_supervisor, supervisor_email),
                    sent_at=corrected_doc.uploaded_at,
                    state_map=state_map,
                    reference_time=reference_time,
                )
                if item:
                    items.append(item)

        if project.dissertation_released_to_assessors and project.dissertation_released_at:
            for slot in required_assessor_slots(project):
                assessor = getattr(project, slot, None)
                if (
                    assessor
                    and assessor.email
                    and getattr(project, f"{slot}_invitation_status") == INVITATION_ACCEPTED
                    and not assessment_result_pack_complete(project, slot)
                ):
                    item = _reminder_item(
                        key=f"assessor_result:{project.id}:{slot}:{assessor.id}:{project.dissertation_released_at.isoformat()}",
                        kind="assessor_result",
                        type_label=f"{INVITATION_SLOTS.get(slot, {}).get('label', slot.replace('_', ' ').title())} Result Pack",
                        project=project,
                        recipient_email=assessor.email,
                        recipient_name=_user_display(assessor),
                        sent_at=project.dissertation_released_at,
                        state_map=state_map,
                        reference_time=reference_time,
                        meta={"slot": slot},
                    )
                    if item:
                        items.append(item)

    return sorted(items, key=lambda item: item["sent_at"] or datetime.max)


def admin_pending_reminder_count():
    return len(admin_pending_reminder_items())


def reminder_state_for_key(reminder_key, create=False):
    state = MbaReminderState.query.filter_by(reminder_key=reminder_key).first()
    if not state and create:
        state = MbaReminderState(reminder_key=reminder_key)
        db.session.add(state)
    return state


def admin_pending_reminder_item(reminder_key):
    return next((item for item in admin_pending_reminder_items() if item["key"] == reminder_key), None)


def project_invitation_snapshot(project):
    supervisor_invitations = list(getattr(project, "supervisor_invitations", []) or [])
    supervisor_status = (
        effective_supervisor_invitation_status(project)
        if project.primary_supervisor_id or supervisor_invitations
        else None
    )
    statuses = {
        "supervisor": supervisor_status if supervisor_status else (invitation_status_or_not_sent(project, "primary_supervisor_invitation_status") if project.primary_supervisor_id else None),
        "assessor_1": invitation_status_or_not_sent(project, "assessor_1_invitation_status") if project.assessor_1_id else None,
        "assessor_2": invitation_status_or_not_sent(project, "assessor_2_invitation_status") if project.assessor_2_id else None,
    }
    supervisor_count_statuses = [
        supervisor_invitation_count_status(project, invitation)
        for invitation in supervisor_invitations
    ]
    if not supervisor_count_statuses and project.primary_supervisor_id:
        supervisor_count_statuses = [invitation_status_or_not_sent(project, "primary_supervisor_invitation_status")]
    count_statuses = [
        status
        for status in (
            *supervisor_count_statuses,
            statuses["assessor_1"],
            statuses["assessor_2"],
        )
        if status
    ]
    pending_count = sum(1 for status in count_statuses if status == INVITATION_PENDING)
    not_sent_count = sum(1 for status in count_statuses if status == "not_sent")
    declined_count = sum(1 for status in count_statuses if status == INVITATION_DECLINED)
    accepted_count = sum(1 for status in count_statuses if status == INVITATION_ACCEPTED)
    complete_assignment = has_complete_assignment(project)
    primary_assessor_acceptance_count = accepted_assessor_count(project)
    primary_assessors_accepted = primary_assessor_acceptance_count >= len(PRIMARY_ASSESSOR_SLOTS)
    all_assigned_accepted = complete_assignment and primary_assessors_accepted and (
        supervisor_status == INVITATION_ACCEPTED
    )
    invitations_sent = project_has_sent_invitations(project)
    assessor_packs_complete = all_assessor_acceptance_packs_complete(project)
    nomination_forwarding_unavailable = project.project_status in NOMINATION_FORWARDING_UNAVAILABLE_STATUSES

    return {
        "assigned_count": len(count_statuses),
        "not_sent_count": not_sent_count,
        "pending_count": pending_count,
        "declined_count": declined_count,
        "accepted_count": accepted_count,
        "statuses": statuses,
        "all_assigned": complete_assignment,
        "all_assigned_accepted": all_assigned_accepted,
        "accepted_assessor_count": primary_assessor_acceptance_count,
        "primary_assessors_accepted": primary_assessors_accepted,
        "assessor_packs_complete": assessor_packs_complete,
        "invitations_sent": invitations_sent,
        "can_approve_to_hdc": invitations_sent
        and all_assigned_accepted
        and assessor_packs_complete
        and not nomination_forwarding_unavailable,
    }
