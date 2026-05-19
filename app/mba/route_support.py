from datetime import datetime
import base64
import mimetypes
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import uuid

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy.orm import joinedload
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (
    MbaDiscipline,
    MbaForm,
    MbaProject,
    MbaProjectDocument,
    MbaProjectSupervisorInvitation,
    MbaReminderState,
    MbaRole,
    MbaScholarRole,
    MbaUser,
    ProjectStatus,
)
from .recommendation import (
    SUPERVISOR_RECOMMENDATION_LIMIT,
    match_recommendations,
    recommend_assessors,
)

ALLOWED_UPLOAD_EXTENSIONS = {"pdf"}
DASHBOARD_PAGE_SIZE_OPTIONS = (5, 10, 20, 50)
UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
SUPERVISOR_SUGGESTION_LIMIT = SUPERVISOR_RECOMMENDATION_LIMIT
ASSESSOR_SLOTS = ("assessor_1", "assessor_2")
PRIMARY_ASSESSOR_SLOTS = ASSESSOR_SLOTS
ADDITIONAL_ASSESSOR_SLOT = "assessor_3"
ALL_ASSESSOR_SLOTS = PRIMARY_ASSESSOR_SLOTS + (ADDITIONAL_ASSESSOR_SLOT,)
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
    "assessor_profile_",
    "assessor_cv_",
    "assessor_highest_qualification_",
)
HDC_ASSESSOR_RESULTS_DOCUMENT_PREFIXES = (
    "assessment_result_",
    "assessor_report_",
    "assessor_narrative_",
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
    "turnitin_report": "Turnitin / Plagiarism Form (Legacy)",
    "ai_report": "AI Report (Legacy)",
    "ethics_certificate": "Ethics Certificate",
    "ethics_exemption_form": "Ethics Exemption Form",
    "ai_declaration_form": "TII AI Declaration (JBS) (Legacy)",
    "affidavit": "JBS 2 Affidavit",
    "corrected_dissertation": "Corrected Capstone Manuscript",
    "corrections_response": "Response to Assessors' Comments",
    "corrections_turnitin_report": "Resubmitted Turnitin Report",
}

MODULE_COMPLETION_STATUS_LABELS = {
    "not_checked": "Module Completion Not Checked",
    "completed": "Modules Completed",
    "awaiting_marks_committee": "Awaiting Response from the Marks Committee",
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
    ProjectStatus.RESULTS_APPROVED.value: "Final Results Processing",
}

PUBLIC_PROJECT_STATUS_BADGE_CLASSES = {
    ProjectStatus.HDC_DECLINED.value: "nomination_pending_public",
    ProjectStatus.RESULTS_APPROVED.value: "results_submitted_to_hdc",
}

ADDITIONAL_ASSESSMENT_STATUS_LABELS = {
    "needs_assignment": "Needs Third Assessor",
    "awaiting_acceptance": "Awaiting Third Assessor Acceptance",
    "awaiting_result": "Awaiting Third Assessor Result",
    "completed": "Additional Assessment Complete",
    "none": "No Additional Assessment",
}

FORM_RENDER_VERSION = "v8"
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
    return bool(payload.get("jbs_hdc_signature") and payload.get("jbs_hdc_signature_date"))


def sync_hdc_assessor_nomination_status(project, finalize_declined=False):
    decisions = hdc_assessor_nomination_decisions(project)
    if not hdc_assessor_nomination_review_complete(project):
        project.nomination_form_approved = False
        if project.project_status == ProjectStatus.HDC_DECLINED.value:
            project.project_status = ProjectStatus.ADMIN_APPROVED.value
        return "pending"

    has_declined = any(decision == HDC_ASSESSOR_DECLINED for decision in decisions.values())
    if has_declined:
        project.nomination_form_approved = False
        if finalize_declined or hdc_jbs10_signature_complete(project):
            project.project_status = ProjectStatus.HDC_DECLINED.value
            return "declined"
        project.project_status = ProjectStatus.ADMIN_APPROVED.value
        return "signature_pending_declined"

    if all(decision == HDC_ASSESSOR_APPROVED for decision in decisions.values()):
        if hdc_jbs10_signature_complete(project):
            project.project_status = ProjectStatus.HDC_VERIFIED.value
            project.nomination_form_approved = True
            return "approved"
        project.project_status = ProjectStatus.ADMIN_APPROVED.value
        project.nomination_form_approved = False
        return "signature_pending"

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


def document_mime_type(filename, fallback="application/octet-stream"):
    guessed, _encoding = mimetypes.guess_type(filename or "")
    return guessed or fallback


def _uploaded_file_bytes(uploaded_file):
    uploaded_file.seek(0)
    data = uploaded_file.read()
    uploaded_file.seek(0)
    return data


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


def _browser_pdf_executables():
    candidates = (
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
        shutil.which("msedge.exe"),
        shutil.which("msedge"),
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
        body.mba-print-body .mba-doc-table {
          width: 100%;
          table-layout: fixed;
        }
        body.mba-print-body .mba-doc-table th,
        body.mba-print-body .mba-doc-table td {
          overflow-wrap: anywhere;
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

    for filename in ("img/uj_logo.png", "img/uj_orange_square.png"):
        logo_url = url_for("static", filename=filename)
        logo_path = Path(current_app.root_path) / "static" / filename
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


def _print_value_html(value_html, modifier=""):
    value_html = value_html if str(value_html or "").strip() else "&nbsp;"
    class_name = "mba-print-value"
    if modifier:
        class_name = f"{class_name} {class_name}--{modifier}"
    return f'<div class="{class_name}">{value_html}</div>'


def _replace_print_form_controls(fragment):
    def replace_textarea(match):
        return _print_value_html(match.group(2), "textarea")

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
        if input_type in {"hidden", "submit", "button", "reset", "file"}:
            return ""
        if input_type in {"checkbox", "radio"}:
            is_checked = _html_has_attr(attrs, "checked")
            class_name = f"mba-print-check mba-print-check--{input_type}"
            if is_checked:
                class_name = f"{class_name} is-checked"
            mark = "&#10003;" if input_type == "checkbox" and is_checked else ("&#9679;" if is_checked else "")
            return f'<span class="{class_name}" aria-hidden="true">{mark}</span>'
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


def generate_exact_html_pdf_bytes(project, form_type, payload):
    return _render_html_form_pdf_bytes(project, form_type, payload)


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
                    ("assessor_1_name", "Assessor 1", "text"),
                    ("assessor_1_qualification", "Assessor 1 highest academic qualification", "text"),
                    ("assessor_1_email", "Assessor 1 e-mail address", "text"),
                    ("assessor_2_name", "Assessor 2", "text"),
                    ("assessor_2_qualification", "Assessor 2 highest academic qualification", "text"),
                    ("assessor_2_email", "Assessor 2 e-mail address", "text"),
                ],
            },
            {
                "title": "Declaration",
                "paragraph": "I confirm the title, supervisor, and assessor registration details supplied in this submission.",
                "checkbox_position": "right",
                "checkboxes": [("declaration", "I confirm the above declaration and agree to the terms of submission.")],
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
                    ("assessment_title", "Capstone Project Title", "textarea"),
                    ("module_lead", "Supervisor / Module Lead", "text"),
                    ("submission_date", "Submission Date", "date"),
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
                "title": "Supervisor Declaration",
                "fields": [
                    ("supervisor_signature_name", "Supervisor Full Name / Electronic Signature", "text"),
                    ("supervisor_signature_date", "Supervisor Signature Date", "date"),
                ],
                "paragraph": "The supervisor confirms that the combined Turnitin-AI report and declaration are ready to accompany the Capstone Project upload.",
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
                "paragraph": "The supervisor declaration and office-use sections on the original JBS 1 form are completed outside this student submission step.",
            },
        ],
    },
}


ASSESSOR_FORM_DEFINITION = {
    "title": "Capstone Assessment Result Summary",
    "action": "Assessor Action",
    "intro": "Record the capstone examination outcome, final mark, and declaration below. These details are also used to generate the capstone assessor report forms.",
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
            "title": "Declaration",
            "fields": [
                ("written_assessment", "Examiner's Detailed Report", "textarea"),
                ("assessor_signature_name", "External Assessor Signature / Full Name", "text"),
                ("certification_date", "Date", "date"),
            ],
            "paragraph": "I confirm that this assessment represents my independent and impartial evaluation of the submitted capstone research report, and that I have no undisclosed conflict of interest.",
            "checkbox_position": "right",
            "checkboxes": [("declaration", "I confirm the above declaration.")],
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
            "checkbox_position": "right",
            "checkboxes": [("declaration", "I confirm this capstone assessment report.")],
        },
    ],
}

ASSESSOR_NARRATIVE_FORM_DEFINITION = {
    "title": "Capstone Assessors Report Form 2",
    "action": "Assessor Action",
    "intro": "Complete the second capstone examiner's report copy. The same submitted assessment details are used to generate this companion report form.",
    "sections": [
        *ASSESSOR_REPORT_FORM_DEFINITION["sections"],
    ],
}

ASSESSOR_PROFILE_FORM_DEFINITION = {
    "title": "External Examiner Nomination Form",
    "action": "Assessor Action",
    "intro": "External Examiner Nomination Form completed during invitation acceptance for HDC nomination review.",
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
        "assessment_title",
        "submission_date",
        "signature_name",
        "signature_date",
        "supervisor_signature_name",
        "supervisor_signature_date",
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
            "title": f"Capstone Assessors Report Form 1 - {form_type.replace('assessor_report_', '').replace('_', ' ').title()}",
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
                "written_assessment",
                "assessor_signature_name",
                "certification_date",
                "declaration",
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
                "written_assessment",
                "assessor_signature_name",
                "certification_date",
                "declaration",
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
                "recommendation",
                "grade",
                "consent_name_disclosure",
                "written_assessment",
                "assessor_signature_name",
                "certification_date",
                "declaration",
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


def generate_form_submission_document_bytes(project, form_type, payload):
    try:
        html_pdf_bytes = _render_html_form_pdf_bytes(project, form_type, payload)
        if html_pdf_bytes:
            return html_pdf_bytes
    except Exception:
        current_app.logger.exception("HTML form PDF render failed for %s", form_type)
    return generate_form_submission_pdf_bytes(form_type, payload)


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
    os.makedirs(project_dir, exist_ok=True)

    safe_original = secure_filename(uploaded_file.filename)
    file_bytes = _uploaded_file_bytes(uploaded_file)
    mime_type = uploaded_file.mimetype or document_mime_type(safe_original)
    unique_name = f"{doc_key}_{uuid.uuid4().hex[:8]}_{safe_original}"
    dest_path = os.path.join(project_dir, unique_name)
    with open(dest_path, "wb") as fh:
        fh.write(file_bytes)

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
    if doc_key.startswith("assessment_result_"):
        suffix = doc_key.replace("assessment_result_", "").replace("_", " ").title()
        return f"Capstone Assessment Result Summary - {suffix}"
    if doc_key.startswith("assessor_report_"):
        suffix = doc_key.replace("assessor_report_", "").replace("_", " ").title()
        return f"Capstone Assessors Report Form 1 - {suffix}"
    if doc_key.startswith("assessor_narrative_"):
        suffix = doc_key.replace("assessor_narrative_", "").replace("_", " ").title()
        return f"Capstone Assessors Report Form 2 - {suffix}"
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


def assessor_slot_document_types(slot):
    return (
        assessor_temp_appointment_doc_type(slot),
        assessor_temp_claim_doc_type(slot),
        assessor_profile_doc_type(slot),
        assessor_cv_doc_type(slot),
        assessor_highest_qualification_doc_type(slot),
        assessment_doc_type(slot),
        assessor_report_doc_type(slot),
        assessor_narrative_doc_type(slot),
    )


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
    required = ["jbs10", "global_document", "combined_turnitin_ai_report"]
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
    required_doc_types = (
        assessment_doc_type(slot),
        assessor_report_doc_type(slot),
        assessor_narrative_doc_type(slot),
    )
    for doc_type in required_doc_types:
        doc = uploaded_doc_for(project, doc_type)
        if not doc or doc.uploaded_by_id != assessor_id:
            return False
    return True


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
    return additional_assessment_required(project) and assessment_result_pack_complete(project, ADDITIONAL_ASSESSOR_SLOT)


def additional_assessment_pending(project, forms_by_project=None):
    return additional_assessment_required(project, forms_by_project=forms_by_project) and not additional_assessment_complete(project)


def additional_assessment_stage(project, forms_by_project=None):
    if not additional_assessment_required(project, forms_by_project=forms_by_project):
        return "none"
    if assessment_result_pack_complete(project, ADDITIONAL_ASSESSOR_SLOT):
        return "completed"
    if not getattr(project, "assessor_3_id", None):
        return "needs_assignment"
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

    if doc_type == "jbs10":
        return project.project_status in nomination_stage_statuses or project.project_status in results_stage_statuses

    if doc_type == "jbs5":
        return project.project_status in jbs5_stage_statuses or project.project_status in nomination_stage_statuses

    if doc_type.startswith(HDC_ASSESSOR_NOMINATION_DOCUMENT_PREFIXES):
        return project.project_status in nomination_stage_statuses

    if project.project_status in results_stage_statuses and doc_type in {
        "global_document",
        "combined_turnitin_ai_report",
        "dissertation",
        "manuscript",
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
        and student_has_uploaded_doc(project, "jbs10")
        and student_has_uploaded_doc(project, "intent_to_submit")
    )


def can_request_moodle_manuscript_submission(project):
    return bool(
        project
        and project.student
        and project.student.email
        and student_submitted_assessor_prerequisite_docs(project)
        and not uploaded_doc_for(project, "dissertation")
    )


def assessor_acceptance_pack_complete(project, slot):
    assessor_id = getattr(project, f"{slot}_id", None)
    if not assessor_id:
        return False
    required_doc_types = (
        assessor_temp_appointment_doc_type(slot),
        assessor_temp_claim_doc_type(slot),
        assessor_profile_doc_type(slot),
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
    """Fill missing assessor slots after HDC-approved JBS5 and student JBS10/Intent submissions."""
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
            f"System suggested assessors after student submitted JBS10 and Intent to Submit: {assessor_emails}",
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
        action_text = "The assessor nominations have been approved. Please continue with the next MBA Admin workflow step."
    elif project.project_status == ProjectStatus.HDC_DECLINED.value:
        outcome = "rejected"
        action_text = "One or more assessor nominations were rejected. Please replace the rejected assessor before forwarding nominations to HDC again."
    elif (
        hdc_assessor_nomination_review_complete(project)
        and any(
            decision == HDC_ASSESSOR_DECLINED
            for decision in hdc_assessor_nomination_decisions(project).values()
        )
    ):
        outcome = "updated"
        action_text = "One or more assessor nominations were rejected by HDC, but JBS10 still needs the HDC signature before MBA Admin can replace rejected assessor(s)."
    elif hdc_assessor_nomination_review_complete(project) and not hdc_jbs10_signature_complete(project):
        outcome = "updated"
        action_text = "Both assessor nominations have been approved by HDC, but JBS10 still needs the HDC signature before the nomination approval is complete."
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
    body = (
        f"An assessor requested corrections or raised comments for the MBA Capstone Project '{project.project_title}'.\n\n"
        f"Student: {project.student.email if project.student else 'Unknown'}\n"
        f"Recommendation: {recommendation}\n\n"
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
        return url_for("mba.admin_dashboard")
    return url_for("mba.dashboard")


def mba_user_requires_profile_completion(user):
    return (
        getattr(user, "system_name", None) == "mba"
        and getattr(user, "role", None) in {MbaRole.STUDENT.value, MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value}
        and not getattr(user, "has_profile", False)
    )


@mba_bp.before_request
def require_profile_completion_before_workspace_access():
    if not current_user.is_authenticated:
        return None
    if request.endpoint in {"mba.profile"}:
        return None
    if not mba_user_requires_profile_completion(current_user):
        return None
    flash("Complete your profile before opening your MBA dashboard.", "info")
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
    os.makedirs(project_dir, exist_ok=True)
    original_name = f"{doc_type}_form.pdf"
    unique_name = f"{doc_type}_{uuid.uuid4().hex[:8]}_form.pdf"
    dest_path = os.path.join(project_dir, unique_name)
    file_bytes = generate_form_submission_document_bytes(project, form_type, payload)
    with open(dest_path, "wb") as fh:
        fh.write(file_bytes)

    old_path = os.path.join(project_dir, existing_doc.stored_name or "")
    if existing_doc.stored_name and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    existing_doc.original_name = original_name
    existing_doc.stored_name = unique_name
    existing_doc.file_data = file_bytes
    existing_doc.mime_type = "application/pdf"
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
            jbs5_form.supervisor_signed = False
        if clear_hdc_signature:
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)
        jbs5_form.payload = payload
    elif jbs5_form and clear_supervisor_signature:
        jbs5_form.supervisor_signed = False

    if clear_hdc_signature:
        jbs10_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs10").first()
        if jbs10_form and isinstance(jbs10_form.payload, dict):
            payload = dict(jbs10_form.payload or {})
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)
            jbs10_form.payload = payload
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
    jbs5_form.payload = payload
    jbs5_form.supervisor_signed = True
    project.comments = append_comment(
        project.comments,
        f"Supervisor signed the student-submitted JBS5 form ({supervisor_name})",
    )
    return jbs5_form


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
