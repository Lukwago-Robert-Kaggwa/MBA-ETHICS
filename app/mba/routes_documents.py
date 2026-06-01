import os
import uuid
from html import escape as html_escape
from io import BytesIO

from flask import abort, current_app, flash, redirect, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import MbaForm, MbaProject, MbaProjectDocument, MbaRole, ProjectStatus
from .route_support import *  # noqa: F403
from .route_support import (
    _project_has_document,
    _render_html_to_pdf_bytes,
    _store_project_document,
    _uploads_dir,
    _validate_uploaded_pdf,
)


def _looks_like_html_document(doc):
    mime_type = str(getattr(doc, "mime_type", "") or "").split(";", 1)[0].strip().lower()
    names = [
        str(getattr(doc, "original_name", "") or "").lower(),
        str(getattr(doc, "stored_name", "") or "").lower(),
    ]
    return (
        mime_type == "text/html"
        or any(name.endswith((".html", ".htm")) for name in names)
        or _bytes_look_like_html(getattr(doc, "file_data", None))
    )


def _bytes_look_like_html(data):
    if not data:
        return False
    if isinstance(data, str):
        head = data[:512].lstrip().lower()
        return head.startswith(("<!doctype html", "<html", "<body", "<div"))
    head = bytes(data[:512]).lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<body", b"<div"))


def _file_looks_like_html(path):
    try:
        with open(path, "rb") as fh:
            return _bytes_look_like_html(fh.read(512))
    except OSError:
        return False


def _download_name_with_extension(doc, extension):
    extension = str(extension or "").lstrip(".") or "doc"
    filename = str(getattr(doc, "original_name", "") or getattr(doc, "stored_name", "") or "").strip()
    if not filename:
        filename = f"{getattr(doc, 'doc_type', 'document')}_form.{extension}"
    stem, ext = os.path.splitext(filename)
    base = stem if ext else filename
    return f"{base or getattr(doc, 'doc_type', 'document')}.{extension}"


def _pdf_download_name(doc):
    return _download_name_with_extension(doc, "pdf")


def _word_download_name(doc):
    return _download_name_with_extension(doc, FORM_WORD_EXTENSION)


def _pdf_bytes_response(pdf_bytes, *, download_name, as_attachment=True):
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        return None
    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=as_attachment,
        download_name=download_name,
    )


def _word_bytes_response(word_bytes, *, download_name, as_attachment=True):
    if not word_bytes:
        return None
    if isinstance(word_bytes, str):
        word_bytes = word_bytes.encode("utf-8")
    return send_file(
        BytesIO(word_bytes),
        mimetype=FORM_WORD_MIME_TYPE,
        as_attachment=as_attachment,
        download_name=download_name,
    )


def _html_bytes_pdf_response(html_bytes, doc, *, as_attachment=True):
    if not html_bytes:
        return None
    try:
        html = html_bytes.decode("utf-8", errors="replace")
        pdf_bytes = _render_html_to_pdf_bytes(html)
    except Exception:
        current_app.logger.exception("Unable to convert stored HTML document %s to PDF", getattr(doc, "id", None))
        return None
    return _pdf_bytes_response(pdf_bytes, download_name=_pdf_download_name(doc), as_attachment=as_attachment)


def _html_file_pdf_response(path, doc, *, as_attachment=True):
    try:
        with open(path, "rb") as fh:
            return _html_bytes_pdf_response(fh.read(), doc, as_attachment=as_attachment)
    except OSError:
        return None


def _html_bytes_word_response(html_bytes, doc, *, as_attachment=True):
    if not html_bytes:
        return None
    try:
        html = html_bytes.decode("utf-8", errors="replace") if isinstance(html_bytes, bytes) else str(html_bytes)
        word_bytes = html_to_word_document_bytes(html, title=document_label(doc.doc_type))
    except Exception:
        current_app.logger.exception("Unable to convert stored HTML document %s to Word", getattr(doc, "id", None))
        return None
    return _word_bytes_response(word_bytes, download_name=_word_download_name(doc), as_attachment=as_attachment)


def _html_file_word_response(path, doc, *, as_attachment=True):
    try:
        with open(path, "rb") as fh:
            return _html_bytes_word_response(fh.read(), doc, as_attachment=as_attachment)
    except OSError:
        return None


def _project_document_db_response(doc, *, as_attachment):
    if not getattr(doc, "file_data", None):
        return None
    if not as_attachment and sensitive_document_type(doc.doc_type):
        return None
    try:
        file_data = decrypt_sensitive_document_bytes(doc.file_data)
    except RuntimeError as exc:
        current_app.logger.exception("Unable to decrypt sensitive project document %s", getattr(doc, "id", None))
        return current_app.response_class(str(exc), status=503, mimetype="text/plain")
    if file_data.startswith(b"%PDF-"):
        download_name = doc.original_name
        if as_attachment and not str(doc.original_name or "").lower().endswith(".pdf"):
            download_name = _pdf_download_name(doc)
        return send_file(
            BytesIO(file_data),
            mimetype="application/pdf",
            as_attachment=as_attachment,
            download_name=download_name,
        )
    if as_attachment and _looks_like_html_document(doc):
        pdf_response = _html_bytes_pdf_response(file_data, doc, as_attachment=True)
        if pdf_response:
            return pdf_response
        return _html_bytes_word_response(file_data, doc, as_attachment=True)
    return send_file(
        BytesIO(file_data),
        mimetype=doc.mime_type or document_mime_type(doc.original_name, "application/pdf"),
        as_attachment=as_attachment,
        download_name=doc.original_name,
    )


def _download_only_view_response(project, doc):
    download_url = url_for("mba.download_project_document", project_id=project.id, doc_id=doc.id)
    document_name = html_escape(str(doc.original_name or document_label(doc.doc_type)))
    document_label_text = html_escape(document_label(doc.doc_type))
    safe_download_url = html_escape(download_url, quote=True)
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{document_label_text}</title>
  <style>
    body {{ margin:0; font-family: Arial, sans-serif; background:#f8fafc; color:#111827; }}
    main {{ max-width: 720px; margin: 56px auto; padding: 28px; background:#fff; border:1px solid #e5e7eb; border-radius:12px; box-shadow:0 12px 30px rgba(15,23,42,.08); }}
    h1 {{ margin:0 0 8px; font-size:1.35rem; }}
    p {{ margin:0 0 18px; color:#4b5563; line-height:1.5; }}
    a {{ display:inline-flex; align-items:center; justify-content:center; min-height:38px; padding:0 16px; border-radius:8px; background:#ef820d; color:#fff; text-decoration:none; font-weight:700; }}
    .filename {{ margin-top:10px; font-size:.9rem; color:#6b7280; word-break:break-word; }}
  </style>
</head>
<body>
  <main>
    <h1>{document_label_text}</h1>
    <p>This document is a native Word file so the exact template formatting is preserved in the downloaded document.</p>
    <a href="{safe_download_url}">Download Word Document</a>
    <div class="filename">{document_name}</div>
  </main>
</body>
</html>"""
    return current_app.response_class(html, mimetype="text/html")


def _payload_for_live_form_render(project, doc, form, *, mask_sensitive=False):
    payload = dict(form.payload or {})
    if doc.doc_type == external_examiner_nomination_doc_type():
        refreshed = build_external_examiner_nomination_payload(project, payload)
        for key, existing_value in payload.items():
            if str(existing_value or "").strip() and not str(refreshed.get(key) or "").strip():
                refreshed[key] = existing_value
        payload = refreshed
    elif doc.doc_type == additional_external_examiner_nomination_doc_type():
        refreshed = build_additional_external_examiner_nomination_payload(project, payload)
        for key, existing_value in payload.items():
            if str(existing_value or "").strip() and not str(refreshed.get(key) or "").strip():
                refreshed[key] = existing_value
        payload = refreshed
    elif doc.doc_type == assessment_summary_doc_type():
        refreshed = build_assessment_summary_payload(project, payload)
        for key, existing_value in payload.items():
            if str(existing_value or "").strip() and not str(refreshed.get(key) or "").strip():
                refreshed[key] = existing_value
        payload = refreshed
    if doc.doc_type == "supervisor_agreement" and doc.uploaded_by_id == project.student_id:
        payload["_student_acceptance"] = "1"
    return decrypt_sensitive_payload_fields(payload, mask=mask_sensitive)


def _live_form_html_response(project, doc):
    if not supports_exact_form_render(doc.doc_type):
        return None
    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form or not isinstance(form.payload, dict):
        return None
    html = build_form_display_html(
        project,
        doc.doc_type,
        _payload_for_live_form_render(project, doc, form, mask_sensitive=True),
    )
    if not html:
        return None
    return current_app.response_class(html, mimetype="text/html")


def _live_form_download_response(project, doc):
    if not supports_exact_form_render(doc.doc_type):
        return None
    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form or not isinstance(form.payload, dict):
        return None
    payload = _payload_for_live_form_render(project, doc, form, mask_sensitive=False)
    try:
        file_bytes, file_extension, mime_type = generate_form_submission_download_bytes(project, doc.doc_type, payload)
    except Exception:
        current_app.logger.exception("Unable to generate downloadable form document %s", doc.id)
        return current_app.response_class(
            "Unable to generate a downloadable document from the submitted form HTML right now.",
            status=503,
            mimetype="text/plain",
        )
    if file_extension == "pdf":
        return _pdf_bytes_response(file_bytes, download_name=f"{doc.doc_type}_form.pdf", as_attachment=True)
    return send_file(
        BytesIO(file_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=f"{doc.doc_type}_form.{file_extension}",
    )


@mba_bp.route("/projects/<int:project_id>/generated-form/<form_type>/download")
@login_required
def download_generated_project_form(project_id, form_type):
    """Download a generated form that still needs an external wet signature/stamp."""
    if form_type != "affidavit":
        abort(404)

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    is_student_owner = current_user.role == MbaRole.STUDENT.value and project.student_id == current_user.id
    is_admin = current_user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}
    if not (is_student_owner or is_admin):
        abort(403)

    form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
    if not form or not isinstance(form.payload, dict):
        flash("Complete the JBS 2 Affidavit form before downloading it for stamping.", "error")
        return redirect(url_for("mba.student_dashboard"))

    try:
        file_bytes, file_extension, mime_type = generate_form_submission_download_bytes(
            project,
            form_type,
            dict(form.payload or {}),
        )
    except Exception:
        current_app.logger.exception("Unable to generate %s for project %s", form_type, project.id)
        return current_app.response_class(
            "Unable to generate the affidavit document right now.",
            status=503,
            mimetype="text/plain",
        )

    return send_file(
        BytesIO(file_bytes),
        mimetype=mime_type,
        as_attachment=True,
        download_name=f"{form_type}_for_commissioner.{file_extension}",
    )


MBA_FORM_TEMPLATES = {
    "supervisor_agreement": {"label": document_label("supervisor_agreement")},
    "jbs10": {"label": document_label("jbs10")},
    "intent_to_submit": {"label": document_label("intent_to_submit")},
    "ethics_certificate": {"label": document_label("ethics_certificate")},
    "ethics_exemption_form": {"label": document_label("ethics_exemption_form")},
    "dissertation": {"label": document_label("dissertation")},
    "global_document": {"label": document_label("global_document")},
    "combined_turnitin_ai_report": {"label": document_label("combined_turnitin_ai_report")},
    "affidavit_stamped": {"label": document_label("affidavit_stamped")},
}

MOODLE_CAPSTONE_SUBMISSION_MESSAGE = (
    "Submit the Capstone Manuscript through Moodle. "
    "Use this system only for supporting documents, including the combined Turnitin-AI report. "
    "MBA Admin will download the Capstone Manuscript from Moodle and upload it here."
)


def _jbs5_signed_by_supervisor(project):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    return bool(jbs5_form and jbs5_form.supervisor_signed)


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
                    "Assessor result submission opens after HDC verifies the assessor nominations."
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


def _is_current_form_pdf_bytes(data):
    if not data:
        return False
    marker = f"% MBA formatted web form {FORM_RENDER_VERSION}:".encode("utf-8")
    return marker in bytes(data[:512])


def _has_current_db_generated_document(doc):
    data = getattr(doc, "file_data", None)
    if not data:
        return False
    if encrypted_document_bytes(data):
        return True
    filename = f"{getattr(doc, 'original_name', '')} {getattr(doc, 'stored_name', '')}".lower()
    mime_type = str(getattr(doc, "mime_type", "") or "").lower()
    if FORM_WORD_EXTENSION in filename or mime_type == FORM_WORD_MIME_TYPE:
        return True
    return _is_current_form_pdf_bytes(data)


def _looks_like_generated_form_document(doc, stored_path):
    expected_originals = {f"{doc.doc_type}_form.pdf", f"{doc.doc_type}_form.{FORM_WORD_EXTENSION}"}
    return (
        doc.original_name in expected_originals
        or str(doc.stored_name or "").endswith(("_form.pdf", f"_form.{FORM_WORD_EXTENSION}"))
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
        "assessment_summary",
    } or doc.doc_type.startswith(
        (
            "assessor_profile_",
            "assessor_report_",
            "assessor_banking_",
            "assessor_temp_appointment_",
            "assessor_temp_claim_",
        )
    )
    if not generated_doc_type or _has_current_db_generated_document(doc) or _is_current_form_pdf(stored_path):
        return

    form = MbaForm.query.filter_by(project_id=project.id, form_type=doc.doc_type).first()
    if not form:
        return

    if not _looks_like_generated_form_document(doc, stored_path):
        return

    payload = dict(form.payload or {})
    if doc.doc_type == "supervisor_agreement" and doc.uploaded_by_id == project.student_id:
        payload["_student_acceptance"] = "1"

    file_bytes, file_extension, mime_type = generate_form_submission_download_bytes(project, form.form_type, payload)
    stored_file_bytes = encrypt_sensitive_document_bytes(doc.doc_type, file_bytes)
    unique_name = f"{doc.doc_type}_{uuid.uuid4().hex[:8]}_form.{file_extension}"
    if doc.stored_name and os.path.exists(stored_path):
        try:
            os.remove(stored_path)
        except OSError:
            pass
    doc.original_name = f"{doc.doc_type}_form.{file_extension}"
    doc.stored_name = unique_name
    doc.file_data = stored_file_bytes
    doc.mime_type = mime_type
    doc.file_size = len(file_bytes)


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
    if doc_key == "jbs10":
        flash("Please complete JBS10 using the fillable web form so your supervisor can review and sign it.", "error")
        return redirect(url_for("mba.student_dashboard"))
    if doc_key == "intent_to_submit":
        flash("Please complete Intent to Submit using the fillable web form so your supervisor can review and sign it.", "error")
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
    elif doc_key in {"global_document", "combined_turnitin_ai_report"}:
        if not project.jbs5_hdc_approved_at:
            flash("JBS5 must be approved by HDC before supporting documents can be uploaded.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not jbs10_supervisor_signed(project) or not intent_to_submit_supervisor_signed(project):
            flash("JBS10 and Intent to Submit must be signed by your supervisor before uploading supporting documents.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not (
            _project_has_document(project.id, "ethics_certificate")
            or _project_has_document(project.id, "ethics_exemption_form")
        ):
            flash("Upload the Ethics Certificate or Ethics Exemption Form before uploading supporting documents.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not (
            _project_has_document(project.id, "jbs1_declaration")
            and _project_has_document(project.id, "plagiarism_declaration")
            and _project_has_document(project.id, "affidavit_stamped")
        ):
            flash(
                "Complete JBS 1 Declaration, the combined plagiarism declaration, and upload the stamped JBS 2 Affidavit before uploading supporting documents.",
                "error",
            )
            return redirect(url_for("mba.student_dashboard"))
    elif doc_key == "affidavit_stamped":
        if not project.jbs5_hdc_approved_at:
            flash("JBS5 must be approved by HDC before the stamped affidavit can be uploaded.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not jbs10_supervisor_signed(project) or not intent_to_submit_supervisor_signed(project):
            flash("JBS10 and Intent to Submit must be signed by your supervisor before uploading the stamped affidavit.", "error")
            return redirect(url_for("mba.student_dashboard"))
        affidavit_form = MbaForm.query.filter_by(project_id=project.id, form_type="affidavit").first()
        if not affidavit_form or not isinstance(affidavit_form.payload, dict):
            flash("Complete and download the JBS 2 Affidavit before uploading the stamped copy.", "error")
            return redirect(url_for("mba.student_dashboard"))

    uploaded_file = request.files.get("form_file")
    file_error = _validate_uploaded_pdf(uploaded_file)
    if file_error:
        flash(file_error, "error")
        return redirect(url_for("mba.student_dashboard"))

    try:
        doc = _store_project_document(project, doc_key, uploaded_file)
        db.session.flush()
        if not doc.id:
            raise RuntimeError("Document metadata row was not persisted")
        if doc_key in {"ethics_certificate", "ethics_exemption_form"}:
            from . import routes_forms as _routes_forms

            _routes_forms._maybe_notify_jbs10_intent_released(project)
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Upload failed because metadata was not stored in mba_project_documents.", "error")
        return redirect(url_for("mba.student_dashboard"))

    if doc_key in {
        "supervisor_agreement",
        "ethics_certificate",
        "ethics_exemption_form",
        "global_document",
        "combined_turnitin_ai_report",
        "affidavit_stamped",
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

    flash(f"{MBA_FORM_TEMPLATES[doc_key]['label']} uploaded successfully.", "success")
    return redirect(url_for("mba.student_dashboard"))


def _combined_declaration_ready(project):
    form = MbaForm.query.filter_by(project_id=project.id, form_type="plagiarism_declaration").first()
    payload = form.payload if form and isinstance(form.payload, dict) else {}
    return bool(
        uploaded_doc_for(project, "plagiarism_declaration")
        and payload.get("signature_name")
        and payload.get("signature_date")
    )


def _jbs1_declaration_ready(project):
    return jbs1_declaration_complete(project)


@mba_bp.route("/projects/<int:project_id>/admin-capstone-submission", methods=["POST"])
@login_required
def admin_upload_capstone_submission(project_id):
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    if not assessor_hr_documents_sent(project):
        flash(
            "Send the approved assessor temporary appointment and claim forms to HR before uploading the Capstone Manuscript.",
            "error",
        )
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    if not _jbs1_declaration_ready(project):
        flash(
            "The JBS 1 Declaration must be signed by the student, supervisor, and Program Manager before Admin uploads the Capstone Manuscript.",
            "error",
        )
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    if not _combined_declaration_ready(project):
        flash(
            "The combined plagiarism, Turnitin and AI declaration must be signed by the student before Admin uploads the Capstone Manuscript.",
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
    can_manage_corrections_for_project = supervisor_can_manage_corrections(project, current_user)
    is_supervisor = current_user.id == project.primary_supervisor_id or can_manage_corrections_for_project
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
    is_project_staff = can_manage_corrections_for_project or current_user.id in {
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
    nomination_doc_types = {
        external_examiner_nomination_doc_type(),
        additional_external_examiner_nomination_doc_type(),
    }
    restricted_assessor_doc = doc.doc_type.startswith(
        (
            "assessment_summary",
            "assessor_report_",
            "assessor_detailed_report_",
            "assessor_profile_",
            "assessor_cv_",
            "assessor_highest_qualification_",
            "assessor_banking_",
            "assessor_temp_appointment_",
            "assessor_temp_claim_",
        )
    ) or doc.doc_type in nomination_doc_types
    owner_can_view_released_detailed_report = (
        is_owner
        and doc.doc_type.startswith("assessor_detailed_report_")
        and project_has_active_corrections(project)
        and corrections_released_to_student(project)
    )
    if is_owner and not (is_admin or is_hdc) and restricted_assessor_doc and not owner_can_view_released_detailed_report:
        abort(403)
    is_hdc_results_document = doc.doc_type.startswith(HDC_ASSESSOR_RESULTS_DOCUMENT_PREFIXES)
    supervisor_can_view_forwarded_assessment_docs = (
        is_hdc_results_document
        and can_manage_corrections_for_project
        and assessment_results_forwarded_to_supervisor(project)
    )
    supervisor_can_view_hdc_results = (
        is_hdc_results_document
        and can_manage_corrections_for_project
        and hdc_results_approved(project)
        and results_released_to_supervisor(project)
    )
    if (
        is_supervisor
        and not (is_admin or is_hdc)
        and is_hdc_results_document
        and not (supervisor_can_view_forwarded_assessment_docs or supervisor_can_view_hdc_results)
    ):
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
    if doc.doc_type in nomination_doc_types:
        if not (is_admin or is_hdc or current_user.id == project.primary_supervisor_id):
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

    if supports_exact_form_render(doc.doc_type):
        live_form_response = _live_form_download_response(project, doc)
        if live_form_response:
            return live_form_response

    project_dir = os.path.join(_uploads_dir(), str(project_id))
    _regenerate_generated_document_if_needed(project, doc, project_dir)
    db_response = _project_document_db_response(doc, as_attachment=True)
    if db_response:
        db.session.commit()
        return db_response

    stored_path = os.path.join(project_dir, doc.stored_name or "")
    if _looks_like_html_document(doc) or _file_looks_like_html(stored_path):
        file_pdf_response = _html_file_pdf_response(stored_path, doc, as_attachment=True)
        if file_pdf_response:
            return file_pdf_response
        file_word_response = _html_file_word_response(stored_path, doc, as_attachment=True)
        if file_word_response:
            return file_word_response

    return send_from_directory(project_dir, doc.stored_name, as_attachment=True, download_name=doc.original_name)


@mba_bp.route("/projects/<int:project_id>/documents/<int:doc_id>/view")
@login_required
def view_project_document(project_id, doc_id):
    """Allow permitted users to open a project document inline in the browser."""
    project, doc, redirect_response = _load_project_document_for_current_user(project_id, doc_id)
    if redirect_response:
        return redirect_response

    if supports_exact_form_render(doc.doc_type):
        live_form_response = _live_form_html_response(project, doc)
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
    if not str(doc.original_name or "").lower().endswith(".pdf"):
        return _download_only_view_response(project, doc)
    db_response = _project_document_db_response(doc, as_attachment=False)
    if db_response:
        db.session.commit()
        return db_response
    return send_from_directory(
        project_dir,
        doc.stored_name,
        as_attachment=False,
        download_name=doc.original_name,
        mimetype="application/pdf",
    )
