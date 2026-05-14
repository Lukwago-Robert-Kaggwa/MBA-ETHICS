from datetime import datetime
import os
import uuid
from io import BytesIO

from flask import abort, current_app, flash, redirect, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import MbaForm, MbaProject, MbaProjectDocument, MbaRole, ProjectStatus
from .route_support import *  # noqa: F403
from .route_support import (
    _project_has_document,
    _store_project_document,
    _uploads_dir,
    _validate_uploaded_pdf,
)


def _project_document_db_response(doc, *, as_attachment):
    if not getattr(doc, "file_data", None):
        return None
    return send_file(
        BytesIO(doc.file_data),
        mimetype=doc.mime_type or document_mime_type(doc.original_name, "application/pdf"),
        as_attachment=as_attachment,
        download_name=doc.original_name,
    )


def _live_form_html_response(project, doc, as_attachment=False):
    if not supports_exact_form_render(doc.doc_type):
        return None
    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form or not isinstance(form.payload, dict):
        return None
    html = build_form_display_html(project, doc.doc_type, form.payload)
    if not html:
        return None
    if as_attachment:
        return send_file(
            BytesIO(html.encode("utf-8")),
            mimetype="text/html; charset=utf-8",
            as_attachment=True,
            download_name=f"{doc.doc_type}_form.html",
        )
    return current_app.response_class(html, mimetype="text/html")


def _live_form_pdf_response(project, doc):
    if not supports_exact_form_render(doc.doc_type):
        return None
    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form or not isinstance(form.payload, dict):
        return None
    try:
        pdf_bytes = generate_exact_html_pdf_bytes(project, doc.doc_type, form.payload)
    except Exception:
        current_app.logger.exception("Unable to generate exact PDF for document %s", doc.id)
        pdf_bytes = None
    if not pdf_bytes:
        return current_app.response_class(
            "Unable to generate a PDF from the submitted form HTML right now.",
            status=503,
            mimetype="text/plain",
        )
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{doc.doc_type}_form.pdf",
    )

MBA_FORM_TEMPLATES = {
    "supervisor_agreement": {
        "filename": None,
        "label": document_label("supervisor_agreement"),
        "downloadable": False,
    },
    "jbs10": {"filename": "JBS10.pdf", "label": document_label("jbs10"), "downloadable": False},
    "intent_to_submit": {"filename": None, "label": document_label("intent_to_submit"), "downloadable": False},
    "ethics_certificate": {"filename": None, "label": document_label("ethics_certificate"), "downloadable": False},
    "ethics_exemption_form": {"filename": None, "label": document_label("ethics_exemption_form"), "downloadable": False},
    "dissertation": {"filename": None, "label": document_label("dissertation"), "downloadable": False},
    "global_document": {"filename": None, "label": document_label("global_document"), "downloadable": False},
    "combined_turnitin_ai_report": {"filename": None, "label": document_label("combined_turnitin_ai_report"), "downloadable": False},
}

MOODLE_CAPSTONE_SUBMISSION_MESSAGE = (
    "Submit the Capstone Manuscript through Moodle. "
    "Use this system only for supporting documents, including the combined Turnitin-AI report. "
    "MBA Admin will download the Capstone Manuscript from Moodle and upload it here."
)


def _jbs5_signed_by_supervisor(project):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    return bool(jbs5_form and jbs5_form.supervisor_signed)


def _validate_uploaded_file(uploaded_file, label, allowed_extensions, required=True):
    if not uploaded_file or not uploaded_file.filename:
        return f"{label} is required." if required else None
    extension = uploaded_file.filename.rsplit(".", 1)[-1].lower() if "." in uploaded_file.filename else ""
    if extension not in allowed_extensions:
        allowed = ", ".join(f".{item}" for item in sorted(allowed_extensions))
        return f"{label}: only {allowed} files are accepted."
    uploaded_file.seek(0, 2)
    file_size = uploaded_file.tell()
    uploaded_file.seek(0)
    if file_size > UPLOAD_MAX_BYTES:
        return f"{label}: file exceeds the 10 MB limit."
    return None


def dissertation_assessor_email_messages(project, dissertation_doc, assessor_user_ids=None):
    messages = []
    seen_recipients = set()
    allowed_user_ids = set(assessor_user_ids or [])
    for index in range(1, 4):
        assessor = getattr(project, f"assessor_{index}")
        slot = f"assessor_{index}"
        if getattr(project, f"{slot}_invitation_status") != INVITATION_ACCEPTED:
            continue
        if allowed_user_ids and assessor and assessor.id not in allowed_user_ids:
            continue
        if not assessor or not assessor.email or assessor.email in seen_recipients:
            continue
        seen_recipients.add(assessor.email)
        messages.append(
            {
                "recipient": assessor.email,
                "subject": f"MBA Capstone Manuscript Released for Assessment: {project.project_title}",
                "body": (
                    f"MBA Admin has released the Capstone Manuscript for assessment for '{project.project_title}'.\n\n"
                    f"Student: {project.student.email if project.student else 'Unknown'}\n"
                    f"Discipline: {project.discipline_name}\n"
                    f"File: {dissertation_doc.original_name}\n\n"
                    "Please sign in to the MBA system to download the Capstone Manuscript. "
                    "Assessor pack submission opens after HDC verifies the assessor nominations."
                ),
            }
        )
    return messages


def _validate_required_pdf(uploaded_file, label):
    file_error = _validate_uploaded_pdf(uploaded_file)
    if not file_error:
        return None
    if file_error == "No file selected.":
        return f"{label} is required."
    return f"{label}: {file_error}"


def supervisor_agreement_submission_email_messages(project, doc_key):
    if doc_key != "supervisor_agreement":
        return []
    messages = []
    for supervisor_email in project_supervisor_notification_emails(project):
        messages.append(
            {
                "recipient": supervisor_email,
                "subject": f"Student Submitted {document_label(doc_key)}",
                "body": (
                    f"Student {current_user.first_name} ({current_user.email}) submitted "
                    f"{document_label(doc_key)} for Capstone Project '{project.project_title}'.\n\n"
                    "Please sign in to the MBA system to view the submitted document."
                ),
            }
        )
    return messages


def corrections_response_supervisor_email_messages(project, response_doc, turnitin_doc, corrected_doc):
    student_label = (
        f"{(current_user.first_name or '').strip()} {(current_user.last_name or '').strip()}".strip()
        or current_user.email
    )
    response_filename = response_doc.original_name if response_doc else document_label("corrections_response")
    turnitin_filename = turnitin_doc.original_name if turnitin_doc else document_label("corrections_turnitin_report")
    corrected_filename = corrected_doc.original_name if corrected_doc else document_label("corrected_dissertation")
    review_url = url_for("mba.scholar_corrections", corrections_status="awaiting_supervisor", _external=True)
    return [
        {
            "recipient": supervisor_email,
            "subject": f"Student Submitted Corrected Capstone Pack: {project.project_title}",
            "body": (
                f"{student_label} ({current_user.email}) submitted the corrected response pack for "
                f"'{project.project_title}'.\n\n"
                f"Corrected Capstone Manuscript: {corrected_filename}\n"
                f"Response file: {response_filename}\n"
                f"Resubmitted Turnitin report: {turnitin_filename}\n"
                f"Student: {project.student.email if project.student else current_user.email}\n"
                f"Discipline: {project.discipline_name}\n\n"
                "Please sign in to the MBA system, review the corrected Capstone Manuscript, "
                "Response to Assessors' Comments, and resubmitted Turnitin report, then approve the response pack.\n\n"
                f"Review queue: {review_url}"
            ),
        }
        for supervisor_email in project_supervisor_notification_emails(project)
    ]


def corrections_approval_admin_email_messages(project, response_doc, turnitin_doc, corrected_doc):
    supervisor_label = (
        f"{(current_user.first_name or '').strip()} {(current_user.last_name or '').strip()}".strip()
        or current_user.email
    )
    response_filename = response_doc.original_name if response_doc else document_label("corrections_response")
    turnitin_filename = turnitin_doc.original_name if turnitin_doc else document_label("corrections_turnitin_report")
    corrected_filename = corrected_doc.original_name if corrected_doc else document_label("corrected_dissertation")
    admin_url = url_for("mba.admin_corrections", corrections_status="ready_for_admin", _external=True)
    return [
        {
            "recipient": admin_email,
            "subject": f"Supervisor Approved Corrected Capstone Pack: {project.project_title}",
            "body": (
                f"{supervisor_label} ({current_user.email}) approved the student's corrected response pack for "
                f"'{project.project_title}'.\n\n"
                f"Corrected Capstone Manuscript: {corrected_filename}\n"
                f"Response to Assessors' Comments: {response_filename}\n"
                f"Resubmitted Turnitin report: {turnitin_filename}\n"
                f"Student: {project.student.email if project.student else 'Unknown'}\n"
                f"Discipline: {project.discipline_name}\n\n"
                "MBA Admin can now open the Assessors' Comments queue to review the approved documents "
                "and continue the HDC results workflow.\n\n"
                f"Approved queue: {admin_url}"
            ),
        }
        for admin_email in mba_admin_notification_emails()
    ]


def corrected_dissertation_supervisor_email_messages(project, corrected_doc):
    turnitin_doc = uploaded_doc_for(project, "corrections_turnitin_report")
    turnitin_line = (
        f"Resubmitted Turnitin report: {turnitin_doc.original_name}\n"
        if turnitin_doc
        else ""
    )
    return [
        {
            "recipient": supervisor_email,
            "subject": f"Corrected Capstone Manuscript Ready for Review: {project.project_title}",
            "body": (
                f"The corrected Capstone Manuscript is ready for review for '{project.project_title}'.\n\n"
                f"File: {corrected_doc.original_name}\n"
                f"{turnitin_line}"
                f"Student: {project.student.email if project.student else 'Unknown'}\n\n"
                "Please sign in to the MBA system and review the corrected Capstone Manuscript together with the "
                "student's Response to Assessors' Comments and resubmitted Turnitin report."
            ),
        }
        for supervisor_email in project_supervisor_notification_emails(project)
    ]


@mba_bp.route("/forms/download/<doc_key>")
@login_required
def download_form_template(doc_key):
    """Let authenticated MBA users download a blank form template."""
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    template_info = MBA_FORM_TEMPLATES.get(doc_key)
    if not template_info or not template_info.get("downloadable") or not template_info.get("filename"):
        abort(404)
    docs_dir = os.path.join(current_app.root_path, "static", "docs")
    template_path = os.path.join(docs_dir, template_info["filename"])
    if doc_key == "jbs10" and (
        not os.path.exists(template_path)
        or os.path.getsize(template_path) < 500
        or not _file_starts_with_pdf_header(template_path)
    ):
        return send_file(
            BytesIO(generate_jbs10_template_pdf_bytes()),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=template_info["filename"],
        )
    return send_from_directory(docs_dir, template_info["filename"], as_attachment=True)


@mba_bp.route("/resources/paper-template-manuscript")
@login_required
def download_manuscript_template():
    """Let authenticated MBA users download the Capstone Manuscript template."""
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    docs_dir = os.path.join(current_app.root_path, "static", "docs")
    source_filename = "Paper Template Manuscript.docx"
    template_path = os.path.join(docs_dir, source_filename)
    if not os.path.exists(template_path):
        abort(404)
    return send_file(
        template_path,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name="Capstone Manuscript Template.docx",
        conditional=False,
    )


def _file_starts_with_pdf_header(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def _pdf_head(path, byte_count=2048):
    try:
        with open(path, "rb") as fh:
            return fh.read(byte_count)
    except OSError:
        return b""


def _is_old_blank_generated_pdf(path):
    data = _pdf_head(path)
    return b"% MBA web form:" in data and b"/Contents" not in data


def _is_current_form_pdf(path):
    marker = f"% MBA formatted web form {FORM_RENDER_VERSION}:".encode("utf-8")
    return marker in _pdf_head(path, 512)


def _looks_like_generated_form_document(doc, stored_path):
    expected_original = f"{doc.doc_type}_form.pdf"
    return (
        doc.original_name == expected_original
        or str(doc.stored_name or "").endswith("_form.pdf")
        or _is_old_blank_generated_pdf(stored_path)
    )


def _regenerate_generated_document_if_needed(project, doc, project_dir):
    stored_path = os.path.join(project_dir, doc.stored_name or "")
    generated_doc_type = doc.doc_type in {
        "jbs5",
        "jbs10",
        "intent_to_submit",
        "supervisor_agreement",
        "jbs1_declaration",
        "plagiarism_declaration",
        "ai_declaration_form",
        "affidavit",
    } or doc.doc_type.startswith(
        (
            "assessor_profile_",
            "assessment_result_",
            "assessor_report_",
            "assessor_narrative_",
            "assessor_banking_",
            "assessor_temp_appointment_",
            "assessor_temp_claim_",
        )
    )
    if not generated_doc_type or _is_current_form_pdf(stored_path):
        return

    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form:
        return

    if not _looks_like_generated_form_document(doc, stored_path):
        return

    os.makedirs(project_dir, exist_ok=True)
    payload = dict(form.payload or {})
    if doc.doc_type == "supervisor_agreement" and doc.uploaded_by_id == project.student_id:
        payload["_student_acceptance"] = "1"

    with open(stored_path, "wb") as fh:
        file_bytes = generate_form_submission_document_bytes(project, form.form_type, payload)
        fh.write(file_bytes)
    doc.file_data = file_bytes
    doc.mime_type = "application/pdf"
    doc.file_size = len(file_bytes)


@mba_bp.route("/projects/<int:project_id>/request-dissertation-resubmission", methods=["POST"])
@login_required
def request_dissertation_resubmission(project_id):
    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    is_student = current_user.role == MbaRole.STUDENT.value and project.student_id == current_user.id
    if not is_student:
        return redirect(role_landing_url())

    flash(MOODLE_CAPSTONE_SUBMISSION_MESSAGE, "info")
    return redirect(url_for("mba.student_dashboard"))


@mba_bp.route("/projects/<int:project_id>/upload-corrections-pack", methods=["POST"])
@login_required
def upload_corrections_pack(project_id):
    """Compatibility guard for the retired upload-only corrections endpoint."""
    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    is_student = current_user.role == MbaRole.STUDENT.value and project.student_id == current_user.id
    if not is_student:
        return redirect(role_landing_url())
    if not project_has_active_corrections(project):
        flash("There are no active assessor comments on this Capstone Project.", "error")
        return redirect(url_for("mba.student_corrections"))
    if not corrections_released_to_student(project):
        flash("Your supervisor has not released assessor comments for response yet.", "error")
        return redirect(url_for("mba.student_corrections"))
    if project.project_status in DISSERTATION_CORRECTIONS_CLOSED_STATUSES:
        flash("The response workflow is closed for this Capstone Project.", "error")
        return redirect(url_for("mba.student_corrections"))
    if supervisor_approved_corrections(project):
        flash("Your supervisor has already approved this corrections submission.", "info")
        return redirect(url_for("mba.student_corrections"))

    flash(
        "Use the in-app Response to Assessors' Comments form to submit the corrected pack.",
        "info",
    )
    return redirect(url_for("mba.fill_project_form", project_id=project.id, form_type="corrections_response"))


@mba_bp.route("/projects/<int:project_id>/upload-form", methods=["POST"])
@login_required
def upload_project_form(project_id):
    """Student uploads a completed form for their project."""
    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    is_student = current_user.role == MbaRole.STUDENT.value and project.student_id == current_user.id
    if not is_student:
        return redirect(role_landing_url())

    doc_key = (request.form.get("doc_type") or "").strip()
    if doc_key not in MBA_FORM_TEMPLATES:
        flash("Unknown form type.", "error")
        return redirect(url_for("mba.student_dashboard"))

    if doc_key == "dissertation":
        flash(MOODLE_CAPSTONE_SUBMISSION_MESSAGE, "info")
        return redirect(url_for("mba.student_dashboard"))

    if doc_key == "supervisor_agreement":
        if not project.supervisor_accepted_at:
            flash("You can upload the signed supervisor agreement after a supervisor accepts the invitation.", "error")
            return redirect(url_for("mba.student_dashboard"))
    elif doc_key in {"ethics_certificate", "ethics_exemption_form"}:
        if not project.supervisor_accepted_at:
            flash("These ethics documents become available after a supervisor accepts the invitation.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not student_has_uploaded_doc(project, "supervisor_agreement"):
            flash("Upload your signed supervisor agreement before submitting these ethics documents.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not _jbs5_signed_by_supervisor(project):
            flash("Ethics Certificate or Ethics Exemption Form can only be uploaded after the supervisor signs JBS5.", "error")
            return redirect(url_for("mba.student_dashboard"))
    elif doc_key in {"jbs10", "intent_to_submit"}:
        if not project.supervisor_accepted_at:
            flash("These forms become available after a supervisor accepts the invitation.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not student_has_uploaded_doc(project, "supervisor_agreement"):
            flash("Upload your signed supervisor agreement before submitting these forms.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not _jbs5_signed_by_supervisor(project):
            flash("These forms become available after the supervisor signs JBS5.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not project.jbs5_hdc_approved_at:
            flash("JBS10 and Intent to Submit are available only after HDC approves JBS5.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not (
            student_has_uploaded_doc(project, "ethics_certificate")
            or student_has_uploaded_doc(project, "ethics_exemption_form")
        ):
            flash("Upload the Ethics Certificate or Ethics Exemption Form before submitting these forms.", "error")
            return redirect(url_for("mba.student_dashboard"))
    elif doc_key in {"global_document", "combined_turnitin_ai_report"}:
        if not project.jbs5_hdc_approved_at:
            flash("JBS5 must be approved by HDC before supporting documents can be uploaded.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not _project_has_document(project.id, "jbs10") or not _project_has_document(project.id, "intent_to_submit"):
            flash("Submit JBS10 and Intent to Submit before uploading supporting documents.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not (
            _project_has_document(project.id, "ethics_certificate")
            or _project_has_document(project.id, "ethics_exemption_form")
        ):
            flash("Upload the Ethics Certificate or Ethics Exemption Form before uploading supporting documents.", "error")
            return redirect(url_for("mba.student_dashboard"))

    uploaded_file = request.files.get("form_file")
    file_error = _validate_uploaded_pdf(uploaded_file)
    if file_error:
        flash(file_error, "error")
        return redirect(url_for("mba.student_dashboard"))

    assessor_suggestions_created = False
    try:
        doc = _store_project_document(project, doc_key, uploaded_file)
        db.session.flush()
        if not doc.id:
            raise RuntimeError("Document metadata row was not persisted")
        if doc_key in {"jbs10", "intent_to_submit"}:
            assessor_suggestions_created = bool(apply_assessor_suggestions_if_ready(project))
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Upload failed because metadata was not stored in mba_project_documents.", "error")
        return redirect(url_for("mba.student_dashboard"))

    if doc_key in {
        "jbs10",
        "supervisor_agreement",
        "intent_to_submit",
        "ethics_certificate",
        "ethics_exemption_form",
        "global_document",
        "combined_turnitin_ai_report",
    }:
        from ..mail import send_email

        for admin_email in mba_admin_notification_emails():
            try:
                subject = f"Student Uploaded {MBA_FORM_TEMPLATES[doc_key]['label']}"
                body = (
                    f"Student {current_user.first_name} ({current_user.email}) uploaded "
                    f"{MBA_FORM_TEMPLATES[doc_key]['label']} for Capstone Project '{project.project_title}'."
                )
                send_email(
                    admin_email,
                    subject,
                    body,
                )
            except Exception:
                pass
        if doc_key == "supervisor_agreement":
            send_bulk_emails(supervisor_agreement_submission_email_messages(project, doc_key))

    if assessor_suggestions_created:
        flash("Assessor suggestions were generated for MBA Admin.", "info")
    flash(f"{MBA_FORM_TEMPLATES[doc_key]['label']} uploaded successfully.", "success")
    return redirect(url_for("mba.student_dashboard"))


@mba_bp.route("/projects/<int:project_id>/assessor-result", methods=["POST"])
@login_required
def assessor_submit_result(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    if project.project_status not in {
        ProjectStatus.HDC_VERIFIED.value,
        ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
        ProjectStatus.RESULTS_DECLINED.value,
    }:
        flash("Assessment results can be uploaded after HDC approves the assessor nominations.", "error")
        return redirect(role_landing_url())

    slot = (request.form.get("slot") or "").strip()
    user_slots = assessor_slots_for_user(project, current_user.id)
    if slot not in user_slots:
        abort(403)
    if getattr(project, f"{slot}_invitation_status") != INVITATION_ACCEPTED:
        flash("Accept the assessor invitation before uploading your assessment result.", "error")
        return redirect(role_landing_url())

    uploaded_file = request.files.get("form_file")
    file_error = _validate_uploaded_pdf(uploaded_file)
    if file_error:
        flash(file_error, "error")
        return redirect(role_landing_url())

    try:
        _store_project_document(project, assessment_doc_type(slot), uploaded_file)
        if all_assessment_results_received(project):
            project.comments = append_comment(project.comments, "All assessor results have been received.")
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Assessment result upload failed.", "error")
        return redirect(role_landing_url())

    flash("Assessment result uploaded.", "success")
    return redirect(role_landing_url())


def _combined_declaration_ready(project):
    form = MbaForm.query.filter_by(project_id=project.id, form_type="plagiarism_declaration").first()
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "plagiarism_declaration")
        and payload.get("signature_name")
        and payload.get("signature_date")
        and payload.get("supervisor_signature_name")
        and payload.get("supervisor_signature_date")
    )


@mba_bp.route("/projects/<int:project_id>/admin-capstone-submission", methods=["POST"])
@login_required
def admin_upload_capstone_submission(project_id):
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    if not _combined_declaration_ready(project):
        flash(
            "The combined plagiarism, Turnitin and AI declaration must be signed by the student and supervisor before Admin uploads the Capstone Manuscript.",
            "error",
        )
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    capstone_file = request.files.get("capstone_file")
    capstone_error = _validate_required_pdf(capstone_file, document_label("dissertation"))
    if capstone_error:
        flash(capstone_error, "error")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    try:
        _store_project_document(project, "dissertation", capstone_file)
        project.dissertation_released_to_assessors = False
        project.dissertation_released_at = None
        project.dissertation_resubmission_open = False
        project.dissertation_resubmission_requested_at = None
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: uploaded the Admin-only Capstone Manuscript from Moodle.",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Capstone Manuscript upload failed.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    flash("Capstone Manuscript uploaded.", "success")
    return redirect(url_for("mba.admin_dashboard", panel="projects"))


@mba_bp.route("/projects/<int:project_id>/admin-corrected-dissertation", methods=["POST"])
@login_required
def admin_upload_corrected_dissertation(project_id):
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    if not project_has_active_corrections(project):
        flash("There are no active assessor comments on this Capstone Project.", "error")
        return redirect(url_for("mba.admin_corrections"))
    if not student_submitted_corrections_response(project):
        flash("The student must submit the Response to Assessors' Comments and resubmitted Turnitin report before the corrected Capstone Manuscript can be updated.", "error")
        return redirect(url_for("mba.admin_corrections"))
    if supervisor_approved_corrections(project):
        flash("The supervisor has already approved this corrections submission.", "info")
        return redirect(url_for("mba.admin_corrections"))

    corrected_dissertation_file = request.files.get("corrected_dissertation_file")
    corrected_dissertation_error = _validate_required_pdf(
        corrected_dissertation_file,
        document_label("corrected_dissertation"),
    )
    if corrected_dissertation_error:
        flash(corrected_dissertation_error, "error")
        return redirect(url_for("mba.admin_corrections"))

    try:
        corrected_doc = _store_project_document(project, "corrected_dissertation", corrected_dissertation_file)
        project.corrections_supervisor_approved_at = None
        project.corrections_supervisor_comments = None
        project.corrections_supervisor_rejected_at = None
        project.corrections_supervisor_rejection_comments = None
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: uploaded the corrected Capstone Manuscript for the assessor comments response workflow.",
        )
        email_result = send_bulk_emails(corrected_dissertation_supervisor_email_messages(project, corrected_doc))
        project.comments = append_comment(
            project.comments,
            f"Corrected Capstone supervisor email result: delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}",
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Corrected Capstone Manuscript upload failed.", "error")
        return redirect(url_for("mba.admin_corrections"))

    flash("Corrected Capstone Manuscript uploaded. The supervisor can now review the response pack.", "success")
    return redirect(url_for("mba.admin_corrections", corrections_status="awaiting_supervisor"))


@mba_bp.route("/projects/<int:project_id>/admin-supporting-document", methods=["POST"])
@login_required
def admin_upload_supporting_document(project_id):
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    uploaded_file = request.files.get("form_file")
    file_error = _validate_uploaded_pdf(uploaded_file)
    if file_error:
        flash(file_error, "error")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    try:
        _store_project_document(
            project,
            f"admin_supporting_{uuid.uuid4().hex[:8]}",
            uploaded_file,
            replace_existing=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Supporting document upload failed.", "error")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    flash("Supporting document uploaded.", "success")
    return redirect(url_for("mba.admin_dashboard", panel="projects"))

def _load_project_document_for_current_user(project_id, doc_id):
    if not require_mba_user():
        return None, None, redirect(url_for("auth.login"))

    project = db.session.get(MbaProject, project_id)
    doc = db.session.get(MbaProjectDocument, doc_id)
    if not project or not doc or doc.project_id != project_id:
        abort(404)

    is_admin = current_user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}
    is_owner = current_user.id == project.student_id
    is_hdc = current_user.role == MbaRole.HDC.value
    is_supervisor = current_user.id == project.primary_supervisor_id
    is_pending_invited_supervisor = any(
        inv.supervisor_id == current_user.id and inv.status == INVITATION_PENDING
        for inv in project.supervisor_invitations
    )
    can_view_pending_supervisor_jbs5 = is_pending_invited_supervisor and doc.doc_type == "jbs5"
    can_view_released_pool_jbs5 = (
        current_user.role == MbaRole.SCHOLAR.value
        and current_user.is_supervisor_role()
        and doc.doc_type == "jbs5"
        and project_available_for_supervisor_pool(project)
    )
    assessor_slots = assessor_slots_for_user(project, current_user.id)
    accepted_assessor_slots = [
        slot for slot in assessor_slots
        if getattr(project, f"{slot}_invitation_status") == INVITATION_ACCEPTED
    ]
    is_assessor = bool(assessor_slots)
    is_project_staff = current_user.id in {
        project.primary_supervisor_id,
        project.assessor_1_id,
        project.assessor_2_id,
        project.assessor_3_id,
    }
    if (
        not is_admin
        and not is_owner
        and not is_hdc
        and not is_project_staff
        and not can_view_pending_supervisor_jbs5
        and not can_view_released_pool_jbs5
    ):
        abort(403)
    restricted_assessor_doc = doc.doc_type.startswith(
        (
            "assessment_result_",
            "assessor_report_",
            "assessor_narrative_",
            "assessor_profile_",
            "assessor_cv_",
            "assessor_highest_qualification_",
            "assessor_banking_",
            "assessor_temp_appointment_",
            "assessor_temp_claim_",
        )
    )
    if is_owner and not (is_admin or is_hdc) and restricted_assessor_doc:
        abort(403)
    is_hdc_results_document = doc.doc_type.startswith(HDC_ASSESSOR_RESULTS_DOCUMENT_PREFIXES)
    supervisor_can_view_hdc_results = (
        is_hdc_results_document
        and supervisor_can_manage_corrections(project, current_user)
        and hdc_results_approved(project)
        and results_released_to_supervisor(project)
    )
    if is_supervisor and not (is_admin or is_hdc) and is_hdc_results_document and not supervisor_can_view_hdc_results:
        abort(403)
    if is_hdc:
        if not hdc_can_access_document(project, doc.doc_type):
            abort(403)
        return project, doc, None
    if can_view_pending_supervisor_jbs5:
        return project, doc, None
    if can_view_released_pool_jbs5:
        return project, doc, None
    if doc.doc_type.startswith(("assessor_banking_", "assessor_temp_appointment_", "assessor_temp_claim_")):
        if not is_admin and doc.uploaded_by_id != current_user.id:
            abort(403)
    if doc.doc_type.startswith(("assessor_profile_", "assessor_cv_", "assessor_highest_qualification_")):
        hdc_assessor_doc_allowed_statuses = {
            ProjectStatus.ADMIN_APPROVED.value,
            ProjectStatus.HDC_DECLINED.value,
            ProjectStatus.HDC_VERIFIED.value,
            ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
            ProjectStatus.RESULTS_DECLINED.value,
            ProjectStatus.RESULTS_APPROVED.value,
            ProjectStatus.GRADUATED.value,
        }
        if is_admin:
            pass
        elif is_hdc and project.project_status in hdc_assessor_doc_allowed_statuses:
            pass
        elif doc.uploaded_by_id == current_user.id:
            pass
        else:
            abort(403)
    if is_assessor and not (is_admin or is_owner or is_hdc or is_supervisor):
        if not accepted_assessor_slots:
            abort(403)
        if doc.doc_type in {"jbs5", "jbs10"}:
            abort(403)
        if doc.doc_type == "dissertation":
            if not assessor_can_view_student_dissertation(project):
                abort(403)
        elif doc.uploaded_by_id != project.student_id:
            if doc.uploaded_by_id != current_user.id:
                abort(403)
        elif doc.doc_type.startswith((
            "assessor_temp_appointment_",
            "assessor_temp_claim_",
            "assessor_banking_",
            "assessor_profile_",
            "assessor_cv_",
            "assessor_highest_qualification_",
        )):
            pass
        else:
            abort(403)

    return project, doc, None


@mba_bp.route("/projects/<int:project_id>/documents/<int:doc_id>/download")
@login_required
def download_project_document(project_id, doc_id):
    """Allow project participants, MBA admins, and HDC to download uploaded forms."""
    project, doc, redirect_response = _load_project_document_for_current_user(project_id, doc_id)
    if redirect_response:
        return redirect_response

    project_dir = os.path.join(_uploads_dir(), str(project_id))
    _regenerate_generated_document_if_needed(project, doc, project_dir)
    db_response = _project_document_db_response(doc, as_attachment=True)
    if db_response:
        db.session.commit()
        return db_response

    if supports_exact_form_render(doc.doc_type):
        live_form_response = _live_form_pdf_response(project, doc)
        if live_form_response:
            return live_form_response
        return current_app.response_class(
            "Unable to generate a PDF from the submitted form HTML right now.",
            status=503,
            mimetype="text/plain",
        )

    return send_from_directory(project_dir, doc.stored_name, as_attachment=True, download_name=doc.original_name)


@mba_bp.route("/projects/<int:project_id>/documents/<int:doc_id>/view")
@login_required
def view_project_document(project_id, doc_id):
    """Allow permitted users to open a project document inline in the browser."""
    project, doc, redirect_response = _load_project_document_for_current_user(project_id, doc_id)
    if redirect_response:
        return redirect_response

    if supports_exact_form_render(doc.doc_type):
        live_form_response = _live_form_html_response(project, doc, as_attachment=False)
        if live_form_response:
            return live_form_response
        db_response = _project_document_db_response(doc, as_attachment=False)
        if db_response:
            return db_response
        return current_app.response_class(
            "Unable to render the submitted form HTML right now.",
            status=503,
            mimetype="text/plain",
        )

    project_dir = os.path.join(_uploads_dir(), str(project_id))
    _regenerate_generated_document_if_needed(project, doc, project_dir)
    db_response = _project_document_db_response(doc, as_attachment=not str(doc.original_name or "").lower().endswith(".pdf"))
    if db_response:
        db.session.commit()
        return db_response
    if not str(doc.original_name or "").lower().endswith(".pdf"):
        return send_from_directory(project_dir, doc.stored_name, as_attachment=True, download_name=doc.original_name)
    return send_from_directory(
        project_dir,
        doc.stored_name,
        as_attachment=False,
        download_name=doc.original_name,
        mimetype="application/pdf",
    )
