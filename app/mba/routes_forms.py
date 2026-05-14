"""
Web form fill routes - replace PDF uploads with fillable HTML forms.

Student forms:  GET/POST /projects/<id>/fill-form/<form_type>
                form_type in: jbs5, jbs10, intent_to_submit, supervisor_agreement,
                corrections_response

Supervisor form: GET/POST /projects/<id>/supervisor-fill-form
                Fills the Supervisor Agreement AND accepts the invitation.
"""
import os
import uuid
from datetime import datetime

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import (
    MbaForm,
    MbaProject,
    MbaProjectComment,
    MbaProjectDocument,
    MbaRole,
    MbaScholarProfile,
    ProjectStatus,
)
from .route_support import (
    FORM_REQUIRED_FIELDS,
    DISSERTATION_CORRECTIONS_CLOSED_STATUSES,
    HDC_ASSESSOR_APPROVED,
    HDC_ASSESSOR_DECLINED,
    _project_has_document,
    _store_project_document,
    _validate_uploaded_pdf,
    INVITATION_ACCEPTED,
    INVITATION_PENDING,
    PRIMARY_ASSESSOR_SLOTS,
    append_comment,
    apply_assessor_suggestions_if_ready,
    assessment_doc_type,
    assessment_result_pack_complete,
    assessor_can_view_student_dissertation,
    assessor_cv_doc_type,
    assessor_highest_qualification_doc_type,
    assessor_narrative_doc_type,
    assessor_profile_doc_type,
    assessor_report_doc_type,
    assessor_slots_for_user,
    assessor_temp_appointment_doc_type,
    assessor_temp_claim_doc_type,
    clear_project_corrections,
    corrections_released_to_student,
    corrections_requested_email_messages,
    all_assessment_results_received,
    document_label,
    build_form_display_html,
    format_project_title,
    generate_form_submission_document_bytes,
    hdc_assessor_nomination_admin_email_messages,
    hdc_assessor_nomination_decision_summary,
    mba_bp,
    mba_admin_notification_emails,
    project_supervisor_notification_emails,
    project_correction_requests,
    project_has_active_corrections,
    project_title_validation_error,
    parse_non_negative_int,
    recommendation_requests_corrections,
    require_mba_role,
    require_mba_user,
    reset_jbs5_review_state,
    role_landing_url,
    set_assessor_hdc_decision,
    sign_student_jbs5_as_supervisor,
    supervisor_approved_corrections,
    submit_project_to_admin_from_jbs5,
    uploaded_doc_for,
    activate_project_corrections,
    activate_additional_assessment,
    primary_assessment_conflict_detected,
)

# Form types a student can fill via web form
STUDENT_FILLABLE_FORMS = {
    "jbs5",
    "jbs10",
    "intent_to_submit",
    "supervisor_agreement",
    "plagiarism_declaration",
    "affidavit",
    "jbs1_declaration",
    "corrections_response",
}

CORRECTIONS_RESPONSE_ROW_LIMITS = {
    "assessor_1": 30,
    "assessor_2": 15,
    "assessor_3": 5,
}


def _uploads_dir():
    from flask import current_app
    return os.path.join(current_app.root_path, "..", "uploads", "mba_forms")


def _save_form_as_document(project, doc_type, form_type, payload, uploaded_by_id=None):
    """
    Persist form data in MbaForm and generate a minimal PDF as MbaProjectDocument.
    Returns the MbaForm instance.
    """
    # Upsert MbaForm
    mba_form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
    if mba_form:
        mba_form.payload = payload
        mba_form.submitted_at = datetime.utcnow()
    else:
        mba_form = MbaForm(
            project_id=project.id,
            form_type=form_type,
            payload=payload,
            submitted_at=datetime.utcnow(),
        )
        db.session.add(mba_form)

    # Write PDF to disk
    project_dir = os.path.join(_uploads_dir(), str(project.id))
    os.makedirs(project_dir, exist_ok=True)
    html_snapshot = str(doc_type or "").startswith("assessor_profile_")
    file_extension = "html" if html_snapshot else "pdf"
    original_name = f"{doc_type}_form.{file_extension}"
    unique_name = f"{doc_type}_{uuid.uuid4().hex[:8]}_form.{file_extension}"
    dest_path = os.path.join(project_dir, unique_name)
    if html_snapshot:
        rendered_html = build_form_display_html(project, form_type, payload)
        if not rendered_html:
            raise RuntimeError(f"Unable to render HTML snapshot for {form_type}")
        pdf_bytes = rendered_html.encode("utf-8")
        mime_type = "text/html; charset=utf-8"
    else:
        pdf_bytes = generate_form_submission_document_bytes(project, form_type, payload)
        mime_type = "application/pdf"
    with open(dest_path, "wb") as fh:
        fh.write(pdf_bytes)

    document_uploaded_by_id = uploaded_by_id or current_user.id
    # Upsert MbaProjectDocument
    existing_doc = MbaProjectDocument.query.filter_by(project_id=project.id, doc_type=doc_type).first()
    if existing_doc:
        old_path = os.path.join(project_dir, existing_doc.stored_name or "")
        if existing_doc.stored_name and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass
        existing_doc.original_name = original_name
        existing_doc.stored_name = unique_name
        existing_doc.file_data = pdf_bytes
        existing_doc.mime_type = mime_type
        existing_doc.file_size = len(pdf_bytes)
        existing_doc.uploaded_by_id = document_uploaded_by_id
        existing_doc.uploaded_at = datetime.utcnow()
    else:
        doc = MbaProjectDocument(
            project_id=project.id,
            doc_type=doc_type,
            original_name=original_name,
            stored_name=unique_name,
            file_data=pdf_bytes,
            mime_type=mime_type,
            file_size=len(pdf_bytes),
            uploaded_by_id=document_uploaded_by_id,
        )
        db.session.add(doc)

    project.comments = append_comment(
        project.comments,
        f"{current_user.email} submitted {document_label(doc_type)} via web form",
    )
    return mba_form


def _sync_project_from_student_form(project, form_type, payload):
    if form_type != "jbs5":
        return

    updated = False
    raw_research_title = (payload.get("research_title") or "").strip()
    title_error = project_title_validation_error(raw_research_title)
    if title_error:
        raise ValueError(title_error)
    research_title = format_project_title(raw_research_title)
    abstract = (payload.get("abstract") or "").strip()

    if raw_research_title and research_title != raw_research_title:
        payload["research_title"] = research_title
        updated = True
    if research_title and research_title != project.project_title:
        project.project_title = research_title
        updated = True
    if abstract and abstract != project.project_description:
        project.project_description = abstract
        updated = True

    if updated:
        project.comments = append_comment(project.comments, "Student updated project details from JBS 5 form")

    if project.supervisor_title_change_requested_at and not project.supervisor_title_change_resolved_at:
        project.supervisor_title_change_resolved_at = datetime.utcnow()
        project.comments = append_comment(
            project.comments,
            "Student submitted an updated JBS 5 form after supervisor title-change request",
        )


def _split_correction_comment_lines(text):
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    return lines or ([str(text).strip()] if str(text or "").strip() else [])


def _build_corrections_response_prefill(project, saved_payload=None):
    prefill = _build_student_prefill(project)
    prefill["student_initials_surname"] = " ".join(
        part for part in [prefill.get("student_initials"), prefill.get("surname")] if part
    ).strip() or prefill.get("full_name", "")
    prefill["department"] = project.discipline_name
    prefill["research_title"] = project.project_title
    prefill.update({key: value for key, value in (saved_payload or {}).items() if value})

    for item in project_correction_requests(project):
        slot = item.get("slot")
        limit = CORRECTIONS_RESPONSE_ROW_LIMITS.get(slot)
        if not limit:
            continue
        comment_lines = _split_correction_comment_lines(item.get("written_assessment"))
        if not comment_lines and item.get("recommendation"):
            comment_lines = [item.get("recommendation")]
        if len(comment_lines) > limit:
            comment_lines = comment_lines[: limit - 1] + ["\n".join(comment_lines[limit - 1 :])]
        for row_index, comment_line in enumerate(comment_lines, start=1):
            prefill.setdefault(f"{slot}_comment_{row_index}", comment_line)
    return prefill


def _corrections_response_missing_rows(payload):
    missing = []
    for slot, limit in CORRECTIONS_RESPONSE_ROW_LIMITS.items():
        slot_label = slot.replace("_", " ").title()
        for row_index in range(1, limit + 1):
            comment = (payload.get(f"{slot}_comment_{row_index}") or "").strip()
            response = (payload.get(f"{slot}_response_{row_index}") or "").strip()
            if comment and not response:
                missing.append(f"{slot_label} row {row_index}")
    return missing


def _send_email_safely(recipient, subject, body):
    from ..mail import send_email

    try:
        send_email(recipient, subject, body)
    except Exception:
        pass


def _notify_admins_form_submitted(project, doc_type):
    for admin_email in mba_admin_notification_emails():
        _send_email_safely(
            admin_email,
            f"Student Submitted {document_label(doc_type)}",
            (
                f"Student {current_user.first_name} ({current_user.email}) "
                f"submitted {document_label(doc_type)} via web form "
                f"for Capstone Project '{project.project_title}'."
            ),
        )


def _notify_admins_jbs5_ready_for_hdc(project, supervisor_signature):
    supervisor_label = (
        f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
        or current_user.email
        or supervisor_signature
    )
    student_label = project.student.email if project.student and project.student.email else "the student"
    for admin_email in mba_admin_notification_emails():
        _send_email_safely(
            admin_email,
            f"JBS5 Ready for HDC: {project.project_title}",
            (
                f"Supervisor {supervisor_label} signed JBS5 for Capstone Project "
                f"'{project.project_title}' for {student_label}. This confirms that no "
                "title changes are required. JBS5 is now locked for student editing and "
                "ready to be forwarded to HDC."
            ),
        )


def _notify_supervisors_form_submitted(project, doc_type):
    for supervisor_email in project_supervisor_notification_emails(project):
        _send_email_safely(
            supervisor_email,
            f"Student Submitted {document_label(doc_type)}",
            (
                f"Student {current_user.first_name} ({current_user.email}) "
                f"submitted {document_label(doc_type)} for Capstone Project '{project.project_title}'. "
                "Please sign in to the MBA system to view the submitted document."
            ),
        )


def _student_supervisor_agreement_document(project):
    return next(
        (
            doc
            for doc in project.documents
            if doc.doc_type == "supervisor_agreement" and doc.uploaded_by_id == project.student_id
        ),
        None,
    )


def _student_supervisor_agreement_signed(project):
    supervisor_agreement_form = MbaForm.query.filter_by(
        project_id=project.id, form_type="supervisor_agreement"
    ).first()
    if not supervisor_agreement_form:
        return False
    payload = supervisor_agreement_form.payload if isinstance(supervisor_agreement_form.payload, dict) else {}
    supervisor_signed = bool(
        supervisor_agreement_form.supervisor_signed
        or (
            payload.get("supervisor_signature")
            and payload.get("supervisor_signature_date")
            and payload.get("supervisor_agreement_declaration")
        )
    )
    student_signed = bool(
        supervisor_agreement_form.student_signed
        or payload.get("student_agreement_declaration")
        or _student_supervisor_agreement_document(project)
    )
    return supervisor_signed and student_signed


def _jbs5_signed_by_supervisor(project):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    return bool(jbs5_form and jbs5_form.supervisor_signed)


def _initials_from_parts(*parts):
    initials = []
    for part in parts:
        cleaned = str(part or "").strip()
        if cleaned:
            initials.append(cleaned[0].upper())
    return "".join(initials)


def _build_student_prefill(project):
    """Build a pre-fill dict from the current user's student profile + project."""
    profile = getattr(current_user, "student_profile", None)
    supervisor_name = ""
    if project.primary_supervisor:
        sp = getattr(project.primary_supervisor, "scholar_profile", None)
        if sp:
            supervisor_name = f"{sp.title or ''} {sp.name or ''} {sp.surname or ''}".strip()
        else:
            supervisor_name = project.primary_supervisor.email or ""
    full_name = f"{profile.name or ''} {profile.surname or ''}".strip() if profile else ""
    qualification = (
        (project.qualification or "").strip()
        or (profile.degree if profile else "")
        or "MBA"
    )
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "full_name": full_name,
        "surname": profile.surname if profile else "",
        "student_number": profile.student_number if profile else "",
        "email": current_user.email or "",
        "contact": profile.contact if profile else "",
        "programme": profile.module if profile else "",
        "block_id": profile.block_id if profile else "",
        "qualification": qualification,
        "student_title": profile.title if profile else "",
        "student_initials": _initials_from_parts(profile.name if profile else "", profile.surname if profile else ""),
        "date_of_first_registration": "",
        "study_type": "Capstone Project",
        "student_is_staff_member": "No",
        "sdg_focus": "",
        "is_4ir_research": "No",
        "has_secondary_focus": "No",
        "secondary_focus": "",
        "assessment_title": project.project_title,
        "course_name": qualification,
        "module_title": (profile.module if profile else "") or qualification,
        "module_lead": supervisor_name,
        "lecturer_name": supervisor_name,
        "submission_date": today,
        "due_date": today,
        "signature_name": full_name,
        "signature_date": today,
        "affidavit_date": today,
        "signing_location": profile.address if profile else "",
        "student_id_number": "",
        "ethical_clearance_number": "",
        "work_type": "Capstone Project",
        "research_title": project.project_title,
        "previous_title": "",
        "amended_title": "",
        "discipline": project.discipline_name,
        "abstract": project.project_description,
        "supervisor_name": supervisor_name,
        "student_signature": full_name,
        "student_signature_date": today,
        "supervisor_signature": supervisor_name,
        "supervisor_signature_name": supervisor_name,
        "supervisor_signature_date": "",
        "co_supervisor_name": "",
        "proposed_supervisor": supervisor_name,
        "proposed_co_supervisors": "",
        "previous_supervisor": "",
        "previous_co_supervisors": "",
        "amended_supervisor": "",
        "amended_co_supervisors": "",
        "supervisor_staff_number": "",
        "co_supervisor_1": "",
        "co_supervisor_1_staff_number": "",
        "co_supervisor_2": "",
        "co_supervisor_2_staff_number": "",
        "previous_supervisor_lineup": "",
        "amended_supervisor_lineup": "",
        "assessor_1_name": "",
        "assessor_1_qualification": "",
        "assessor_1_email": "",
        "assessor_2_name": "",
        "assessor_2_qualification": "",
        "assessor_2_email": "",
    }


def _build_student_supervisor_agreement_prefill(project):
    student_profile = getattr(current_user, "student_profile", None)
    supervisor = project.primary_supervisor
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    supervisor_name = ""
    if supervisor_profile:
        supervisor_name = (
            f"{supervisor_profile.title or ''} "
            f"{supervisor_profile.name or ''} "
            f"{supervisor_profile.surname or ''}"
        ).strip()
    elif supervisor:
        supervisor_name = supervisor.email or ""

    qualification = (
        (project.qualification or "").strip()
        or (student_profile.degree if student_profile else "")
        or "MBA"
    )

    return {
        "supervisor_full_name": supervisor_name,
        "department": supervisor_profile.department if supervisor_profile else "",
        "affiliation": supervisor_profile.affiliation if supervisor_profile else "",
        "position": supervisor_profile.position if supervisor_profile else "",
        "supervisor_surname": supervisor_profile.surname if supervisor_profile else "",
        "supervisor_initials": _initials_from_parts(
            supervisor_profile.name if supervisor_profile else "",
            supervisor_profile.surname if supervisor_profile else "",
        ),
        "student_name": (
            f"{student_profile.name or ''} {student_profile.surname or ''}".strip()
            if student_profile
            else current_user.email
        ),
        "student_surname": student_profile.surname if student_profile else "",
        "student_initials": _initials_from_parts(
            student_profile.name if student_profile else "",
            student_profile.surname if student_profile else "",
        ),
        "student_number": student_profile.student_number if student_profile else "",
        "student_address": student_profile.address if student_profile else "",
        "student_postal_code": "",
        "degree": qualification,
        "research_title": project.project_title,
        "co_supervisor_full_name": "",
        "co_supervisor_department": "",
        "co_supervisor_surname": "",
        "co_supervisor_initials": "",
        "capacity_statement": "",
    }


# ---------------------------------------------------------------------------
# Student form fill route
# ---------------------------------------------------------------------------

@mba_bp.route("/projects/<int:project_id>/fill-form/<form_type>", methods=["GET", "POST"])
@login_required
def fill_project_form(project_id, form_type):
    """Student fills in a project form via browser instead of uploading a PDF."""
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    if current_user.role != MbaRole.STUDENT.value:
        flash("Only students can fill in project forms.", "error")
        return redirect(role_landing_url())

    if form_type not in STUDENT_FILLABLE_FORMS:
        abort(404)

    project = db.session.get(MbaProject, project_id)
    if not project or project.student_id != current_user.id:
        abort(403)

    # Access guards (mirror upload_project_form guards)
    if form_type in {"supervisor_agreement", "jbs10", "intent_to_submit"}:
        if not project.supervisor_accepted_at:
            flash("This form is available after a supervisor accepts the invitation.", "error")
            return redirect(url_for("mba.student_dashboard"))
    if form_type in {"jbs10", "intent_to_submit"}:
        if not _student_supervisor_agreement_document(project):
            flash("Submit the supervisor agreement before filling in this form.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not _jbs5_signed_by_supervisor(project):
            flash("This form is available after the supervisor signs JBS5.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not project.jbs5_hdc_approved_at:
            flash("JBS10 and Intent to Submit are available only after HDC approves JBS5.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not (
            _project_has_document(project.id, "ethics_certificate")
            or _project_has_document(project.id, "ethics_exemption_form")
        ):
            flash(
                "Upload the Ethics Certificate or the Ethics Exemption Form before filling in this form.",
                "error",
            )
            return redirect(url_for("mba.student_dashboard"))
    if form_type in {"plagiarism_declaration", "affidavit", "jbs1_declaration"}:
        if not project.jbs5_hdc_approved_at:
            flash("JBS5 must be approved by HDC before these supporting forms become available.", "error")
            return redirect(url_for("mba.student_dashboard"))
        if not _project_has_document(project.id, "jbs10") or not _project_has_document(project.id, "intent_to_submit"):
            flash("These Capstone Project supporting forms become available after JBS10 and Intent to Submit are submitted.", "error")
            return redirect(url_for("mba.student_dashboard"))
    if form_type == "corrections_response":
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

    # Pre-fill
    existing_form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
    saved_payload = existing_form.payload if existing_form and isinstance(existing_form.payload, dict) else {}
    if form_type == "jbs5" and existing_form and existing_form.supervisor_signed:
        flash("JBS5 has already been signed by the supervisor and can no longer be edited.", "error")
        return redirect(url_for("mba.student_dashboard"))

    if form_type == "corrections_response":
        prefill = _build_corrections_response_prefill(project, saved_payload)
    else:
        prefill = _build_student_prefill(project)
    if form_type == "supervisor_agreement":
        prefill.update(_build_student_supervisor_agreement_prefill(project))
        prefill.update({key: value for key, value in (saved_payload or {}).items() if value})
    elif form_type != "corrections_response":
        prefill.update(saved_payload)  # saved data takes priority over defaults
    if form_type == "jbs5" and not (existing_form and existing_form.supervisor_signed):
        prefill["supervisor_signature"] = ""
        prefill["supervisor_signature_date"] = ""

    template_name = f"mba/form_fill_{form_type}.html"
    template_context = {
        "project": project,
        "prefill": prefill,
        "student_acceptance": form_type == "supervisor_agreement",
    }

    if request.method == "POST":
        assessor_suggestions_created = False
        jbs5_auto_submitted = False
        payload = {
            k: (request.form.get(k) or "").strip()
            for k in request.form
            if k not in {"csrf_token", "_csrf_token"}
        }
        title_was_formatted = False
        if form_type == "jbs5":
            raw_research_title = payload.get("research_title", "")
            title_error = project_title_validation_error(raw_research_title)
            if title_error:
                flash(title_error, "error")
                template_context["prefill"] = payload
                return render_template(template_name, **template_context)
            formatted_research_title = format_project_title(raw_research_title)
            if raw_research_title and formatted_research_title != raw_research_title:
                payload["research_title"] = formatted_research_title
                title_was_formatted = True
            payload["supervisor_signature"] = ""
            payload["supervisor_signature_date"] = ""
            payload.pop("supervisor_signature_user_id", None)
            payload.pop("supervisor_signature_email", None)
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)
            if existing_form and existing_form.supervisor_signed and isinstance(saved_payload, dict):
                for signature_field in ("supervisor_signature", "supervisor_signature_date"):
                    if saved_payload.get(signature_field):
                        payload[signature_field] = saved_payload.get(signature_field)
        if form_type == "supervisor_agreement":
            payload["_student_acceptance"] = "1"
            payload["student_agreement_declaration"] = "1" if request.form.get("student_agreement_declaration") else ""
            if (
                isinstance(saved_payload, dict)
                and saved_payload.get("supervisor_agreement_declaration")
            ) or (existing_form and existing_form.supervisor_signed):
                payload["supervisor_agreement_declaration"] = saved_payload.get("supervisor_agreement_declaration") or "1"
        if form_type == "jbs10":
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)

        consent_messages = {
            "supervisor_agreement": (
                "student_agreement_declaration",
                "You must confirm the student supervisor agreement declaration before submitting.",
            ),
            "jbs10": (
                "declaration",
                "You must confirm the JBS 10 declaration before submitting.",
            ),
            "plagiarism_declaration": (
                "plagiarism_consent",
                "You must confirm the combined plagiarism, Turnitin and AI declaration before submitting.",
            ),
            "affidavit": (
                "affidavit_consent",
                "You must confirm the affidavit before submitting.",
            ),
            "jbs1_declaration": (
                "jbs1_consent",
                "You must confirm the JBS 1 declaration before submitting.",
            ),
        }
        consent_field = consent_messages.get(form_type, (None, None))[0]
        required = set(FORM_REQUIRED_FIELDS.get(form_type, {"full_name", "student_number", "research_title"}))
        if consent_field:
            required.discard(consent_field)

        missing = [f for f in sorted(required) if not payload.get(f)]
        if missing:
            flash(f"Required fields missing: {', '.join(missing)}", "error")
            template_context["prefill"] = payload
            return render_template(template_name, **template_context)

        if consent_field and not request.form.get(consent_field):
            flash(consent_messages[form_type][1], "error")
            template_context["prefill"] = payload
            return render_template(template_name, **template_context)

        corrected_dissertation_file = None
        corrections_turnitin_file = None
        if form_type == "corrections_response":
            missing_response_rows = _corrections_response_missing_rows(payload)
            if missing_response_rows:
                flash(
                    "Student responses are required for assessor comment rows: "
                    + ", ".join(missing_response_rows[:8])
                    + ("." if len(missing_response_rows) <= 8 else ", ..."),
                    "error",
                )
                template_context["prefill"] = payload
                return render_template(template_name, **template_context)

            corrected_dissertation_file = request.files.get("corrected_dissertation_file")
            corrected_dissertation_error = _validate_uploaded_pdf(corrected_dissertation_file)
            if corrected_dissertation_error:
                if corrected_dissertation_error == "No file selected.":
                    corrected_dissertation_error = "Corrected Capstone Manuscript is required."
                else:
                    corrected_dissertation_error = f"Corrected Capstone Manuscript: {corrected_dissertation_error}"
                flash(corrected_dissertation_error, "error")
                template_context["prefill"] = payload
                return render_template(template_name, **template_context)

            corrections_turnitin_file = request.files.get("corrections_turnitin_report_file")
            turnitin_error = _validate_uploaded_pdf(corrections_turnitin_file)
            if turnitin_error:
                if turnitin_error == "No file selected.":
                    turnitin_error = "Resubmitted Turnitin Report is required."
                else:
                    turnitin_error = f"Resubmitted Turnitin Report: {turnitin_error}"
                flash(turnitin_error, "error")
                template_context["prefill"] = payload
                return render_template(template_name, **template_context)

        try:
            saved_form = _save_form_as_document(project, form_type, form_type, payload)
            if form_type in {"jbs5", "supervisor_agreement", "plagiarism_declaration"}:
                saved_form.student_signed = True
            if form_type == "jbs5":
                reset_jbs5_review_state(project, clear_supervisor_signature=True, clear_hdc_signature=True)
            if form_type == "corrections_response":
                saved_form.student_signed = True
                corrected_doc = _store_project_document(
                    project,
                    "corrected_dissertation",
                    corrected_dissertation_file,
                )
                turnitin_doc = _store_project_document(
                    project,
                    "corrections_turnitin_report",
                    corrections_turnitin_file,
                )
                project.corrections_student_resubmitted_at = datetime.utcnow()
                project.corrections_supervisor_approved_at = None
                project.corrections_supervisor_comments = None
                project.corrections_supervisor_rejected_at = None
                project.corrections_supervisor_rejection_comments = None
                project.comments = append_comment(
                    project.comments,
                    (
                        f"{current_user.email}: submitted Response to Assessors' Comments "
                        "via web form with the corrected Capstone Manuscript and resubmitted "
                        "Turnitin report for supervisor review."
                    ),
                )
                db.session.flush()
                response_doc = MbaProjectDocument.query.filter_by(
                    project_id=project.id,
                    doc_type="corrections_response",
                ).first()
                from .routes_documents import corrections_response_supervisor_email_messages

                email_result = send_bulk_emails(
                    corrections_response_supervisor_email_messages(
                        project,
                        response_doc,
                        turnitin_doc,
                        corrected_doc,
                    )
                )
                project.comments = append_comment(
                    project.comments,
                    (
                        "Corrections response supervisor email result: "
                        f"delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}"
                    ),
                )
            if form_type == "plagiarism_declaration":
                saved_form.supervisor_signed = bool(
                    payload.get("supervisor_signature_name")
                    and payload.get("supervisor_signature_date")
                )
            _sync_project_from_student_form(project, form_type, payload)
            if form_type == "jbs5":
                db.session.flush()
                jbs5_auto_submitted = submit_project_to_admin_from_jbs5(project)
            if form_type in {"jbs10", "intent_to_submit"}:
                db.session.flush()
                assessor_suggestions_created = bool(apply_assessor_suggestions_if_ready(project))
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            template_context["prefill"] = payload
            return render_template(template_name, **template_context)
        except Exception:
            db.session.rollback()
            flash("Form could not be saved. Please try again.", "error")
            template_context["prefill"] = payload
            return render_template(template_name, **template_context)

        if form_type == "jbs5" and jbs5_auto_submitted:
            _notify_admins_form_submitted(project, form_type)
        elif form_type in {
            "jbs10",
            "intent_to_submit",
            "supervisor_agreement",
            "plagiarism_declaration",
            "affidavit",
            "jbs1_declaration",
        }:
            _notify_admins_form_submitted(project, form_type)
        if form_type == "supervisor_agreement":
            _notify_supervisors_form_submitted(project, form_type)

        if title_was_formatted:
            flash(f"Capstone Project title was automatically formatted as: {payload['research_title']}", "info")
        if jbs5_auto_submitted:
            flash("Capstone Project submitted to MBA Admin from the JBS 5 form.", "success")
        if assessor_suggestions_created:
            flash("Assessor suggestions were generated for MBA Admin.", "info")
        if form_type == "corrections_response":
            flash(
                "Corrected Capstone Manuscript, Response to Assessors' Comments, and resubmitted Turnitin report submitted for supervisor review.",
                "success",
            )
            return redirect(url_for("mba.student_corrections"))
        flash(f"{document_label(form_type)} submitted.", "success")
        return redirect(url_for("mba.student_dashboard"))

    return render_template(template_name, **template_context)


def _hdc_signature_name():
    return (
        f"{current_user.first_name or ''} {current_user.last_name or ''}".strip()
        or current_user.email
        or "HDC"
    )


def _jbs5_signed_by_student_and_supervisor(project):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if not jbs5_form or not isinstance(jbs5_form.payload, dict):
        return False
    payload = dict(jbs5_form.payload or {})
    student_signed = bool(
        jbs5_form.student_signed
        or (payload.get("student_signature") and payload.get("student_signature_date"))
    )
    supervisor_signed = bool(
        jbs5_form.supervisor_signed
        or (payload.get("supervisor_signature") and payload.get("supervisor_signature_date"))
    )
    return student_signed and supervisor_signed


def _send_hdc_nomination_admin_alert(project):
    admin_messages = hdc_assessor_nomination_admin_email_messages(project, current_user.email)
    if admin_messages:
        email_result = send_bulk_emails(admin_messages)
        project.comments = append_comment(
            project.comments,
            (
                "System: HDC assessor nomination decision admin alert email result: "
                f"delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}"
            ),
        )
    else:
        project.comments = append_comment(
            project.comments,
            "System: HDC assessor nomination decision recorded; no MBA Admin email recipients are configured.",
        )


@mba_bp.route("/projects/<int:project_id>/hdc-sign-form/<form_type>", methods=["GET", "POST"])
@login_required
def hdc_sign_project_form(project_id, form_type):
    if not require_mba_role(MbaRole.HDC.value):
        return redirect(role_landing_url())
    if form_type not in {"jbs5", "jbs10"}:
        abort(404)

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    allowed_statuses = {
        "jbs5": {
            ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
            ProjectStatus.JBS5_HDC_DECLINED.value,
        },
        "jbs10": {
            ProjectStatus.ADMIN_APPROVED.value,
            ProjectStatus.HDC_DECLINED.value,
        },
    }
    if project.project_status not in allowed_statuses[form_type]:
        flash(f"{document_label(form_type)} is not waiting for HDC review.", "error")
        return redirect(url_for("mba.hdc_dashboard"))

    form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
    if not form or not isinstance(form.payload, dict):
        flash(f"{document_label(form_type)} is not available for HDC review.", "error")
        return redirect(url_for("mba.hdc_dashboard"))

    payload = dict(form.payload or {})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    prefill = dict(payload)
    prefill.setdefault("jbs_hdc_signature", _hdc_signature_name())
    prefill.setdefault("jbs_hdc_signature_date", today)
    template_name = f"mba/form_fill_{form_type}.html"
    template_context = {
        "project": project,
        "prefill": prefill,
        "hdc_signature_mode": True,
    }

    if request.method == "POST":
        decision = (request.form.get("decision") or "").strip()
        comment = (request.form.get("comment") or "").strip()
        signature = (request.form.get("jbs_hdc_signature") or "").strip()
        signature_date = (request.form.get("jbs_hdc_signature_date") or "").strip()
        if decision not in {"approve", "decline"}:
            flash("Choose an HDC decision before submitting.", "error")
            return render_template(template_name, **template_context)
        if decision == "approve" and (not signature or not signature_date):
            flash("HDC signature and date are required before approval.", "error")
            prefill["jbs_hdc_signature"] = signature
            prefill["jbs_hdc_signature_date"] = signature_date
            template_context["prefill"] = prefill
            return render_template(template_name, **template_context)
        if decision == "decline" and not comment:
            flash("Add HDC feedback before returning the document.", "error")
            return render_template(template_name, **template_context)

        if decision == "approve":
            payload["jbs_hdc_signature"] = signature
            payload["jbs_hdc_signature_date"] = signature_date
        else:
            payload.pop("jbs_hdc_signature", None)
            payload.pop("jbs_hdc_signature_date", None)

        try:
            _save_form_as_document(
                project,
                form_type,
                form_type,
                payload,
                uploaded_by_id=project.student_id,
            )
            if form_type == "jbs5":
                if decision == "approve":
                    if not _jbs5_signed_by_student_and_supervisor(project):
                        raise ValueError("JBS5 must be signed by both the student and supervisor before HDC can approve it.")
                    project.project_status = ProjectStatus.JBS5_HDC_APPROVED.value
                    project.title_approved = True
                    project.jbs5_hdc_approved_at = datetime.utcnow()
                    project.comments = append_comment(
                        project.comments,
                        f"{current_user.email}: signed and approved JBS5 for HDC.",
                    )
                    message = "JBS5 signed and approved by HDC."
                else:
                    reset_jbs5_review_state(project, clear_supervisor_signature=True, clear_hdc_signature=True)
                    project.project_status = ProjectStatus.JBS5_HDC_DECLINED.value
                    project.comments = append_comment(
                        project.comments,
                        f"{current_user.email}: returned JBS5 from HDC review.",
                    )
                    message = "JBS5 returned with HDC feedback."
                if comment:
                    project.jbs5_hdc_comments = append_comment(
                        project.jbs5_hdc_comments,
                        f"{current_user.email}: {comment}",
                    )
            else:
                if not project.jbs5_hdc_approved_at:
                    raise ValueError("HDC must approve JBS5 before JBS10 nominations can be reviewed.")
                decision_value = HDC_ASSESSOR_APPROVED if decision == "approve" else HDC_ASSESSOR_DECLINED
                for slot in PRIMARY_ASSESSOR_SLOTS:
                    set_assessor_hdc_decision(project, slot, decision_value)
                if decision == "approve":
                    project.project_status = ProjectStatus.HDC_VERIFIED.value
                    project.nomination_form_approved = True
                    project.comments = append_comment(
                        project.comments,
                        f"{current_user.email}: signed JBS10 and approved assessor nominations.",
                    )
                    message = "JBS10 signed and assessor nominations approved by HDC."
                else:
                    project.project_status = ProjectStatus.HDC_DECLINED.value
                    project.nomination_form_approved = False
                    project.comments = append_comment(
                        project.comments,
                        f"{current_user.email}: returned JBS10 assessor nominations from HDC review.",
                    )
                    message = "JBS10 nominations returned with HDC feedback."
                if comment:
                    project.hdc_comments = append_comment(project.hdc_comments, f"{current_user.email}: {comment}")
                decision_summary = hdc_assessor_nomination_decision_summary(project)
                project.comments = append_comment(
                    project.comments,
                    f"{current_user.email}: HDC assessor nomination decision recorded - {decision_summary}.",
                )
                _send_hdc_nomination_admin_alert(project)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(template_name, **template_context)
        except Exception:
            db.session.rollback()
            flash("HDC review could not be saved. Please try again.", "error")
            return render_template(template_name, **template_context)

        flash(message, "success")
        return redirect(url_for("mba.hdc_dashboard"))

    return render_template(template_name, **template_context)


@mba_bp.route("/projects/<int:project_id>/supervisor-title-change-request", methods=["POST"])
@login_required
def supervisor_title_change_request(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    if project.primary_supervisor_id != current_user.id:
        pending_invitation = next(
            (
                inv
                for inv in project.supervisor_invitations
                if inv.supervisor_id == current_user.id and inv.status == INVITATION_PENDING
            ),
            None,
        )
        if pending_invitation:
            flash("Accept the supervisor invitation before requesting title changes.", "error")
            return redirect(url_for("mba.scholar_dashboard"))
        abort(403)

    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if not jbs5_form or not isinstance(jbs5_form.payload, dict):
        flash("The student-submitted JBS5 form is not available yet.", "error")
        return redirect(url_for("mba.scholar_dashboard"))
    if jbs5_form and jbs5_form.supervisor_signed:
        flash("JBS5 has already been signed by the supervisor and can no longer be edited.", "error")
        return redirect(url_for("mba.scholar_dashboard"))
    if not _student_supervisor_agreement_signed(project):
        flash(
            "The student must sign and submit the supervisor-signed Supervisor Agreement before you can request title changes.",
            "error",
        )
        return redirect(url_for("mba.scholar_dashboard"))

    comment = (request.form.get("title_change_comment") or "").strip()
    if not comment:
        flash("Add the title changes you want the student to make.", "error")
        return redirect(url_for("mba.scholar_dashboard"))

    project.supervisor_title_change_requested_at = datetime.utcnow()
    project.supervisor_title_change_request = comment
    project.supervisor_title_change_resolved_at = None
    db.session.add(
        MbaProjectComment(
            project_id=project.id,
            author_id=current_user.id,
            comment=f"Title change request: {comment}",
        )
    )
    project.comments = append_comment(
        project.comments,
        f"{current_user.email} requested title changes: {comment}",
    )
    db.session.commit()

    if project.student and project.student.email:
        _send_email_safely(
            project.student.email,
            f"Supervisor Requested Title Changes: {project.project_title}",
            (
                f"Your supervisor requested title changes for '{project.project_title}'.\n\n"
                f"Requested changes:\n{comment}\n\n"
                "Please sign in to the MBA system, review the Capstone Project comments, and update your JBS 5 form."
            ),
        )
    for admin_email in mba_admin_notification_emails():
        _send_email_safely(
            admin_email,
            f"Supervisor Requested Title Changes: {project.project_title}",
            (
                f"Supervisor {current_user.first_name} ({current_user.email}) requested title changes "
                f"for Capstone Project '{project.project_title}'.\n\nRequested changes:\n{comment}"
            ),
        )

    flash("Title change request sent to the student and MBA Admin.", "success")
    return redirect(url_for("mba.scholar_dashboard"))


# ---------------------------------------------------------------------------
# Supervisor JBS5 signature route
# ---------------------------------------------------------------------------

@mba_bp.route("/projects/<int:project_id>/supervisor-sign-jbs5", methods=["GET", "POST"])
@login_required
def supervisor_sign_jbs5(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    pending_invitation = next(
        (
            inv
            for inv in project.supervisor_invitations
            if inv.supervisor_id == current_user.id and inv.status == INVITATION_PENDING
        ),
        None,
    )
    if project.primary_supervisor_id != current_user.id:
        if pending_invitation:
            flash("Accept the supervisor invitation before signing JBS5.", "error")
            return redirect(url_for("mba.scholar_dashboard"))
        abort(403)

    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    if not jbs5_form or not isinstance(jbs5_form.payload, dict):
        flash("The student-submitted JBS5 form is not available yet.", "error")
        return redirect(url_for("mba.scholar_dashboard"))

    if jbs5_form.supervisor_signed:
        flash("JBS5 has already been signed by the supervisor.", "info")
        return redirect(url_for("mba.scholar_dashboard"))

    if project.supervisor_title_change_requested_at and not project.supervisor_title_change_resolved_at:
        flash("Wait for the student to update JBS5 before signing it.", "error")
        return redirect(url_for("mba.scholar_dashboard"))
    if not _student_supervisor_agreement_signed(project):
        flash(
            "The student must sign and submit the supervisor-signed Supervisor Agreement before you can sign JBS5.",
            "error",
        )
        return redirect(url_for("mba.scholar_dashboard"))

    profile = getattr(current_user, "scholar_profile", None)
    default_name = (
        f"{profile.title or ''} {profile.name or ''} {profile.surname or ''}".strip()
        if profile
        else ""
    ) or f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
    template_name = "mba/form_fill_jbs5.html"
    payload = dict(jbs5_form.payload or {})
    prefill = dict(payload)
    prefill.setdefault(
        "student_name",
        payload.get("full_name") or (project.student.email if project.student else ""),
    )
    prefill.setdefault("student_number", "")
    prefill.setdefault("research_title", project.project_title)
    prefill.setdefault("discipline", project.discipline_name)
    prefill.setdefault("abstract", project.project_description)
    prefill["supervisor_signature"] = default_name
    prefill["supervisor_signature_date"] = datetime.utcnow().strftime("%Y-%m-%d")
    template_context = {
        "project": project,
        "prefill": prefill,
        "supervisor_signature_mode": True,
    }

    if request.method == "POST":
        supervisor_signature = (request.form.get("supervisor_signature") or "").strip()
        supervisor_signature_date = (request.form.get("supervisor_signature_date") or "").strip()
        if not supervisor_signature or not supervisor_signature_date:
            flash("Supervisor signature and date are required.", "error")
            prefill["supervisor_signature"] = supervisor_signature
            prefill["supervisor_signature_date"] = supervisor_signature_date
            return render_template(template_name, **template_context)
        if not request.form.get("jbs5_supervisor_declaration"):
            flash("Confirm that signing JBS5 means no title changes are required and the form will be locked.", "error")
            prefill["supervisor_signature"] = supervisor_signature
            prefill["supervisor_signature_date"] = supervisor_signature_date
            return render_template(template_name, **template_context)

        try:
            sign_student_jbs5_as_supervisor(
                project,
                supervisor_signature,
                supervisor_signature_date,
                supervisor_user=current_user,
            )
            db.session.commit()
            _notify_admins_jbs5_ready_for_hdc(project, supervisor_signature)
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(template_name, **template_context)
        except Exception:
            db.session.rollback()
            flash("JBS5 could not be signed. Please try again.", "error")
            return render_template(template_name, **template_context)

        flash("JBS5 signed and locked. MBA Admin has been notified that it is ready to be forwarded to HDC.", "success")
        return redirect(url_for("mba.scholar_dashboard"))

    return render_template(template_name, **template_context)


# ---------------------------------------------------------------------------
# Supervisor fill form route (also accepts the invitation)
# ---------------------------------------------------------------------------

@mba_bp.route("/projects/<int:project_id>/supervisor-fill-form", methods=["GET", "POST"])
@login_required
def supervisor_fill_form(project_id):
    """
    Supervisor fills the Supervisor Agreement via web form.
    On submit, the invitation is automatically accepted.
    """
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    # Find the pending invitation for this supervisor
    invitation = next(
        (
            inv
            for inv in project.supervisor_invitations
            if inv.supervisor_id == current_user.id and inv.status == INVITATION_PENDING
        ),
        None,
    )
    if not invitation:
        flash("No pending supervisor invitation found for this Capstone Project.", "error")
        return redirect(url_for("mba.scholar_dashboard"))

    # Pre-fill
    profile = getattr(current_user, "scholar_profile", None)
    student_profile = (
        getattr(project.student, "student_profile", None) if project.student else None
    )
    existing_form = MbaForm.query.filter_by(
        project_id=project.id, form_type="supervisor_agreement"
    ).first()
    saved_payload = existing_form.payload if existing_form and isinstance(existing_form.payload, dict) else {}

    prefill = {
        "supervisor_full_name": (
            f"{profile.title or ''} {profile.name or ''} {profile.surname or ''}".strip()
            if profile
            else ""
        ),
        "department": profile.department if profile else "",
        "affiliation": profile.affiliation if profile else "",
        "position": profile.position if profile else "",
        "supervisor_surname": profile.surname if profile else "",
        "supervisor_initials": _initials_from_parts(
            profile.name if profile else "",
            profile.surname if profile else "",
        ),
        "student_name": (
            f"{student_profile.name or ''} {student_profile.surname or ''}".strip()
            if student_profile
            else (project.student.email if project.student else "")
        ),
        "student_surname": student_profile.surname if student_profile else "",
        "student_initials": _initials_from_parts(
            student_profile.name if student_profile else "",
            student_profile.surname if student_profile else "",
        ),
        "student_number": student_profile.student_number if student_profile else "",
        "student_address": student_profile.address if student_profile else "",
        "student_postal_code": "",
        "degree": (
            (project.qualification or "").strip()
            or (student_profile.degree if student_profile else "")
            or "MBA"
        ),
        "research_title": project.project_title,
        "co_supervisor_full_name": "",
        "co_supervisor_department": "",
        "co_supervisor_surname": "",
        "co_supervisor_initials": "",
        "capacity_statement": "",
    }
    prefill.update(saved_payload)

    if request.method == "POST":
        payload = {
            k: (request.form.get(k) or "").strip()
            for k in request.form
            if k not in {"csrf_token", "_csrf_token"}
        }

        if not payload.get("supervisor_full_name") or not payload.get("research_title"):
            flash("Supervisor name and research title are required.", "error")
            return render_template(
                "mba/form_fill_supervisor_agreement.html",
                project=project,
                prefill=payload,
                invitation=invitation,
            )

        if not request.form.get("supervisor_agreement_declaration"):
            flash("You must confirm the supervisor agreement declaration to accept.", "error")
            return render_template(
                "mba/form_fill_supervisor_agreement.html",
                project=project,
                prefill=payload,
                invitation=invitation,
            )

        try:
            payload["supervisor_agreement_declaration"] = "1"
            if (
                isinstance(saved_payload, dict)
                and saved_payload.get("student_agreement_declaration")
            ) or (existing_form and existing_form.student_signed):
                payload["student_agreement_declaration"] = saved_payload.get("student_agreement_declaration") or "1"
            supervisor_form = _save_form_as_document(project, "supervisor_agreement", "supervisor_agreement", payload)
            supervisor_form.supervisor_signed = True

            # Accept the invitation
            project.primary_supervisor_id = current_user.id
            project.primary_supervisor_invitation_status = INVITATION_ACCEPTED
            project.supervisor_confirmed = True
            project.supervisor_accepted_at = datetime.utcnow()
            project.project_status = ProjectStatus.SUPERVISOR_ACCEPTED.value
            invitation.status = "accepted"
            invitation.responded_at = datetime.utcnow()

            # Expire remaining pending invitations
            for other in project.supervisor_invitations:
                if other.id != invitation.id and other.status == INVITATION_PENDING:
                    other.status = "expired"
                    other.responded_at = datetime.utcnow()

            project.comments = append_comment(
                project.comments,
                f"Supervisor agreement submitted and invitation accepted by {current_user.email}",
            )
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(
                "mba/form_fill_supervisor_agreement.html",
                project=project,
                prefill=payload,
                invitation=invitation,
            )
        except Exception:
            db.session.rollback()
            flash("Form submission failed. Please try again.", "error")
            return render_template(
                "mba/form_fill_supervisor_agreement.html",
                project=project,
                prefill=payload,
                invitation=invitation,
            )

        # Notify admin
        _notify_admins_form_submitted(project, "supervisor_agreement")

        # Notify student
        if project.student and project.student.email:
            from ..mail import send_email

            try:
                send_email(
                    project.student.email,
                    "Supervisor Accepted: JBS5 Under Review",
                    (
                        f"Your supervisor has accepted the invitation and submitted the "
                        f"supervisor agreement for '{project.project_title}'. "
                        "They will now review JBS5 and either request title changes or sign it."
                    ),
                )
            except Exception:
                pass

        flash("Supervisor Agreement submitted. Invitation accepted. You can now review JBS5.", "success")
        return redirect(url_for("mba.scholar_dashboard"))

    return render_template(
        "mba/form_fill_supervisor_agreement.html",
        project=project,
        prefill=prefill,
        invitation=invitation,
    )


# ---------------------------------------------------------------------------
# Assessor grade form route
# ---------------------------------------------------------------------------


def _initials_from_name_parts(*parts):
    letters = []
    for part in parts:
        for token in str(part or "").replace(".", " ").split():
            if token:
                letters.append(token[0].upper())
    return "".join(letters)


def _yes_no_from_bool(value):
    return "Yes" if value else "No"


def _sync_scholar_profile_from_assessor_payload(payload, cv_uploaded=False):
    profile = current_user.scholar_profile or MbaScholarProfile(user_id=current_user.id)
    if not current_user.scholar_profile:
        db.session.add(profile)

    first_names = (payload.get("assessor_first_names") or "").strip()
    surname = (payload.get("assessor_surname") or "").strip()
    profile.name = first_names or profile.name
    profile.surname = surname or profile.surname
    profile.title = (payload.get("assessor_title") or "").strip() or profile.title
    profile.contact = (payload.get("assessor_contact") or "").strip() or profile.contact
    profile.department = (
        (payload.get("assessor_department") or "").strip()
        or (payload.get("department_unit_centre") or "").strip()
        or (payload.get("uj_department_division") or "").strip()
        or profile.department
    )
    profile.position = (payload.get("assessor_position") or "").strip() or profile.position
    profile.qualification = (payload.get("highest_qualification") or "").strip() or profile.qualification
    profile.affiliation = (
        (payload.get("assessor_affiliation") or "").strip()
        or (payload.get("current_university_affiliation") or "").strip()
        or (payload.get("qualification_institution") or "").strip()
        or profile.affiliation
    )
    profile.skills = (payload.get("skills") or "").strip() or profile.skills
    profile.research_themes = (payload.get("research_themes") or "").strip() or profile.research_themes
    profile.research_interests = (payload.get("research_interests") or "").strip() or profile.research_interests
    profile.research_disciplines = (payload.get("research_disciplines") or "").strip() or profile.research_disciplines
    profile.selected_publications = (payload.get("selected_publications") or "").strip() or profile.selected_publications
    profile.scholarly_profile_links = (payload.get("scholarly_profile_links") or "").strip() or profile.scholarly_profile_links
    profile.address = (
        (payload.get("assessor_address") or "").strip()
        or (payload.get("home_address") or "").strip()
        or (payload.get("postal_address") or "").strip()
        or profile.address
    )
    profile.students = parse_non_negative_int(payload.get("current_student_load"), profile.students or 0)
    profile.academic_experience = parse_non_negative_int(
        payload.get("academic_experience_years"),
        profile.academic_experience or 0,
    )
    profile.students_supervised_total = parse_non_negative_int(
        payload.get("students_supervised_total"),
        profile.students_supervised_total or 0,
    )
    profile.students_assessed_total = parse_non_negative_int(
        payload.get("students_assessed_total"),
        profile.students_assessed_total or 0,
    )
    profile.publication_count = parse_non_negative_int(
        payload.get("publication_count"),
        profile.publication_count or 0,
    )
    profile.approved_before = (payload.get("approved_before") or "").strip().lower() == "yes"
    profile.international_assessor = (payload.get("international_assessor") or "").strip().lower() == "yes"
    current_user.first_name = profile.name
    current_user.last_name = profile.surname
    current_user.has_profile = True
    if cv_uploaded:
        current_user.has_cv = True
    db.session.add(profile)


def _assessor_acceptance_prefill(project, slot):
    profile = getattr(current_user, "scholar_profile", None)
    student_profile = getattr(project.student, "student_profile", None) if project.student else None
    supervisor = getattr(project, "primary_supervisor", None)
    supervisor_profile = getattr(supervisor, "scholar_profile", None) if supervisor else None
    first_name = getattr(profile, "name", None) or current_user.first_name or ""
    last_name = getattr(profile, "surname", None) or current_user.last_name or ""
    full_name = " ".join(part for part in [profile.title if profile else "", first_name, last_name] if part).strip()
    qualification = profile.qualification if profile and profile.qualification else ""
    affiliation = profile.affiliation if profile and profile.affiliation else ""
    department = profile.department if profile and profile.department else ""
    profile_address = profile.address if profile else ""
    project_qualification = project.qualification or (student_profile.degree if student_profile else "") or "MBA"
    study_type = ""
    for form_name in ("jbs10", "jbs5"):
        saved_form = MbaForm.query.filter_by(project_id=project.id, form_type=form_name).first()
        if saved_form and isinstance(saved_form.payload, dict):
            study_type = (saved_form.payload.get("study_type") or "").strip()
            if study_type:
                break
    student_initials = _initials_from_name_parts(
        student_profile.name if student_profile else "",
        student_profile.surname if student_profile else "",
    )
    supervisor_name = ""
    if supervisor_profile:
        supervisor_name = " ".join(
            part
            for part in [supervisor_profile.title, supervisor_profile.name, supervisor_profile.surname]
            if part
        ).strip()
    elif supervisor:
        supervisor_name = supervisor.email or ""
    degree_registered = (
        "MBA Master of Business Administration"
        if str(project_qualification).strip().upper() == "MBA"
        else project_qualification
    )
    today = datetime.utcnow().strftime("%Y-%m-%d")
    prefill = {
        "assessor_name": full_name or current_user.email or f"{first_name} {last_name}".strip(),
        "assessor_surname": last_name,
        "assessor_first_names": first_name,
        "assessor_title": profile.title if profile else "",
        "employee_number": "",
        "new_employee": "Yes",
        "employed_at_uj": "Yes" if "university of johannesburg" in (affiliation or "").lower() or "uj" in (affiliation or "").lower() else "No",
        "uj_department_division": department,
        "appointed_as": "External Assessor",
        "identity_passport_number": "",
        "date_of_birth": "",
        "work_visa_number": "",
        "gender": "",
        "marital_status": "",
        "sa_citizen": "Yes",
        "nationality": "",
        "employed_outside_uj": "No",
        "home_language": "",
        "income_tax_number": "",
        "care_of_intermediary": "None",
        "home_address": profile_address or "",
        "postal_address": profile_address or "",
        "home_postal_code": "",
        "postal_code": "",
        "home_tel": "",
        "assessor_contact": profile.contact if profile else "",
        "assessor_email": current_user.email or "",
        "work_tel": "",
        "disability_status": "No",
        "disability_nature": "",
        "race": "",
        "assessor_department": department,
        "assessor_position": profile.position if profile else "",
        "assessor_affiliation": affiliation,
        "assessor_address": profile_address or "",
        "academic_experience_years": str(profile.academic_experience if profile else 0),
        "current_student_load": str(profile.students if profile else 0),
        "students_supervised_total": str(profile.students_supervised_total if profile else 0),
        "students_assessed_total": str(profile.students_assessed_total if profile else 0),
        "publication_count": str(profile.publication_count if profile else 0),
        "selected_publications": profile.selected_publications if profile else "",
        "scholarly_profile_links": profile.scholarly_profile_links if profile else "",
        "skills": profile.skills if profile else "",
        "research_themes": profile.research_themes if profile else "",
        "research_interests": profile.research_interests if profile else "",
        "research_disciplines": profile.research_disciplines if profile else "",
        "approved_before": _yes_no_from_bool(profile.approved_before) if profile else "No",
        "international_assessor": _yes_no_from_bool(profile.international_assessor) if profile else "No",
        "qualification_institution": affiliation,
        "highest_qualification": qualification,
        "qualification_awarded_date": "",
        "qualification_status": "Completed" if qualification else "",
        "bank_changed": "No",
        "bank_account_holder": full_name or current_user.email,
        "bank_name": "",
        "bank_branch_name": "",
        "bank_branch_code": "",
        "bank_account_number": "",
        "bank_account_type": "",
        "bank_account_ownership": "Own",
        "employment_group": "Academic",
        "appointment_category": project_qualification,
        "appointment_start_date": today,
        "appointment_end_date": today,
        "temporary_employment_reason": "Services will not exceed 3 months",
        "appointment_reason_other": "",
        "appointment_motivation": f"External assessor appointment for Capstone Project '{project.project_title}'.",
        "rate_per_month": "N/A",
        "rate_per_hour": "1341.35",
        "other_rate_basis": "",
        "total_units": "1.53",
        "actual_hours": "10",
        "full_cost_centre_string": "05 05 046904 20 31330",
        "permanent_post_number": "N/A",
        "appointed_against_permanent_position": "No",
        "position_number": "",
        "total_budget_for_appointment": "2062.28",
        "conflict_of_interest_details": "None",
        "employee_signature_name": full_name or current_user.email,
        "employee_signature_date": today,
        "faculty_division": "Johannesburg Business School",
        "department_unit_centre": department or "Johannesburg Business School",
        "month_of_claim": datetime.utcnow().strftime("%B %Y"),
        "alternate_contact_number": "",
        "alternate_email_address": "",
        "requestor_extension": "",
        "requestor_email": "",
        "claim_unit_basis": "Per Hour",
        "contract_eit_number": "",
        "claim_total_units": "1.53",
        "claim_rate": "1341.35",
        "claim_currency": "ZAR",
        "amount_claimed": "2062.28",
        "claim_cost_centre_number": "05 05 046904 20 31330",
        "total_claimed": "2062.28",
        "claim_signature_name": full_name or current_user.email,
        "claim_signature_date": today,
        "project_title": project.project_title,
        "student_name": (
            f"{student_profile.name or ''} {student_profile.surname or ''}".strip()
            if student_profile
            else (project.student.email if project.student else "")
        ),
        "student_number": student_profile.student_number if student_profile else "",
        "student_initials_surname": " ".join(
            part for part in [student_initials, student_profile.surname if student_profile else ""] if part
        ).strip(),
        "current_degree_registered": degree_registered,
        "qualification_description": study_type or "Capstone Project",
        "supervisor_name": supervisor_name,
        "supervisor_department": supervisor_profile.department if supervisor_profile else "Johannesburg Business School",
        "supervisor_phone": supervisor_profile.contact if supervisor_profile else "",
        "supervisor_email": supervisor.email if supervisor else "",
        "co_supervisor_name": "",
        "co_supervisor_department": "",
        "co_supervisor_phone": "",
        "co_supervisor_email": "",
        "assessor_telephone_number": "",
        "current_university_affiliation": affiliation,
        "supervisor_signature_name": "",
        "supervisor_signature_date": "",
        "hod_signature_name": "",
        "hod_signature_date": "",
        "executive_dean_signature_name": "",
        "executive_dean_signature_date": "",
        "slot_label": slot.replace("_", " ").title(),
        "cv_filename": "",
        "assessor_profile_date": today,
        "assessor_signature_name": full_name or current_user.email,
    }
    for saved_form_type in [
        assessor_temp_appointment_doc_type(slot),
        assessor_temp_claim_doc_type(slot),
        assessor_profile_doc_type(slot),
    ]:
        saved_form = MbaForm.query.filter_by(project_id=project.id, form_type=saved_form_type).first()
        if (
            saved_form
            and isinstance(saved_form.payload, dict)
            and str(saved_form.payload.get("_submitted_by") or "") == str(current_user.id)
        ):
            prefill.update(saved_form.payload)
    current_cv_doc = next(
        (
            doc for doc in project.documents
            if doc.doc_type == assessor_cv_doc_type(slot) and doc.uploaded_by_id == current_user.id
        ),
        None,
    )
    if current_cv_doc and not prefill.get("cv_filename"):
        prefill["cv_filename"] = current_cv_doc.original_name
    return prefill


@mba_bp.route("/projects/<int:project_id>/assessor-acceptance-form/<slot>", methods=["GET", "POST"])
@login_required
def assessor_acceptance_form(project_id, slot):
    if not require_mba_role(MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    valid_slots = assessor_slots_for_user(project, current_user.id)
    if slot not in valid_slots:
        abort(403)

    current_status = getattr(project, f"{slot}_invitation_status")
    if current_status not in {INVITATION_PENDING, INVITATION_ACCEPTED}:
        flash("This assessor invitation is no longer available for response.", "error")
        return redirect(role_landing_url())

    slot_label = slot.replace("_", " ").title()
    prefill = _assessor_acceptance_prefill(project, slot)
    reason_options = [
        "Services will not exceed 3 months",
        "Specific project for limited time and clear deliverable",
        "Temporary increase in volume of work, less than 12 months",
        "Seasonal increase in volume of work, less than 12 months",
        "Position funded by external (non UJ) funds for limited time",
        "Other",
    ]
    yes_no_options = ["Yes", "No"]
    gender_options = ["Male", "Female", "Other", "Prefer not to say"]
    marital_status_options = ["Single", "Married", "Divorced", "Widowed", "Other"]
    account_type_options = ["Cheque", "Savings", "Current", "Transmission", "Other"]
    account_ownership_options = ["Own", "Joint"]
    race_options = ["African", "Coloured", "Indian", "White", "Chinese", "Other", "Prefer not to say"]
    existing_cv_doc = next(
        (
            doc for doc in project.documents
            if doc.doc_type == assessor_cv_doc_type(slot) and doc.uploaded_by_id == current_user.id
        ),
        None,
    )
    existing_highest_qualification_doc = next(
        (
            doc for doc in project.documents
            if doc.doc_type == assessor_highest_qualification_doc_type(slot)
            and doc.uploaded_by_id == current_user.id
        ),
        None,
    )

    def _render_acceptance_form(current_prefill):
        return render_template(
            "mba/form_fill_assessor_acceptance.html",
            project=project,
            prefill=current_prefill,
            slot=slot,
            slot_label=slot_label,
            reason_options=reason_options,
            yes_no_options=yes_no_options,
            gender_options=gender_options,
            marital_status_options=marital_status_options,
            account_type_options=account_type_options,
            account_ownership_options=account_ownership_options,
            race_options=race_options,
            existing_cv_doc=existing_cv_doc,
            existing_highest_qualification_doc=existing_highest_qualification_doc,
        )

    if request.method == "POST":
        payload = {
            key: (request.form.get(key) or "").strip()
            for key in request.form
            if key not in {"csrf_token", "_csrf_token"}
        }
        payload["assessor_name"] = " ".join(
            part for part in [
                payload.get("assessor_title", ""),
                payload.get("assessor_first_names", ""),
                payload.get("assessor_surname", ""),
            ] if part
        ).strip() or payload.get("assessor_name", "")
        uploaded_cv = request.files.get("cv_file")
        uploaded_highest_qualification = request.files.get("highest_qualification_file")
        required_messages = {
            "new_employee": "Choose whether this is a new employee appointment.",
            "employed_at_uj": "Choose whether you are employed at UJ.",
            "appointed_as": "Appointed as is required.",
            "assessor_surname": "Surname is required.",
            "assessor_title": "Title is required.",
            "assessor_first_names": "First names are required.",
            "identity_passport_number": "Identity / passport number is required.",
            "assessor_email": "Email address is required.",
            "assessor_contact": "Cell / mobile number is required.",
            "highest_qualification": "Highest qualification is required.",
            "bank_account_holder": "Account holder name is required.",
            "bank_name": "Bank name is required.",
            "bank_branch_name": "Branch name is required.",
            "bank_branch_code": "Branch code is required.",
            "bank_account_number": "Account number is required.",
            "bank_account_type": "Account type is required.",
            "appointment_category": "Appointment category is required.",
            "appointment_start_date": "Appointment start date is required.",
            "appointment_end_date": "Appointment end date is required.",
            "temporary_employment_reason": "Reason for temporary employment is required.",
            "rate_per_hour": "Rate per hour is required.",
            "actual_hours": "Actual hours are required.",
            "full_cost_centre_string": "Full cost centre string is required.",
            "employee_signature_name": "Employee signature / full name is required.",
            "employee_signature_date": "Employee signature date is required.",
            "faculty_division": "Faculty / Division is required.",
            "department_unit_centre": "Department / Unit / Centre is required.",
            "month_of_claim": "Month of claim is required.",
            "contract_eit_number": "Contract EIT number is required.",
            "claim_total_units": "Claim total units are required.",
            "claim_rate": "Claim rate is required.",
            "claim_currency": "Claim currency is required.",
            "amount_claimed": "Amount claimed is required.",
            "claim_cost_centre_number": "Claim cost centre number is required.",
            "total_claimed": "Total claimed is required.",
            "claim_signature_name": "Claim signature / full name is required.",
            "claim_signature_date": "Claim signature date is required.",
            "assessor_name": "Assessor full name is required for the nomination form.",
            "highest_qualification": "Qualification is required for the nomination form.",
            "assessor_affiliation": "Affiliation is required for the nomination form.",
            "assessor_address": "Street address is required for the nomination form.",
            "assessor_contact": "Cell number is required for the nomination form.",
            "assessor_email": "Email address is required for the nomination form.",
            "students_supervised_total": "The approximate number of postgraduate students supervised to completion is required.",
            "current_university_affiliation": "Current affiliation with a university is required for the nomination form.",
            "publication_count": "Approximate number of publications is required for the nomination form.",
            "international_assessor": "Please indicate whether this is an international assessor.",
        }
        for key, message in required_messages.items():
            if not payload.get(key):
                flash(message, "error")
                return _render_acceptance_form(payload)

        if payload.get("temporary_employment_reason") == "Other" and not payload.get("appointment_reason_other"):
            flash("Please specify the other reason for temporary employment.", "error")
            return _render_acceptance_form(payload)

        if not request.form.get("appointment_declaration"):
            flash("You must confirm the temporary appointment declaration before accepting.", "error")
            return _render_acceptance_form(payload)

        if not request.form.get("claim_declaration"):
            flash("You must confirm the claim declaration before accepting.", "error")
            return _render_acceptance_form(payload)

        if not request.form.get("assessor_profile_declaration"):
            flash("You must confirm the external examiner nomination form declaration before accepting.", "error")
            return _render_acceptance_form(payload)

        if uploaded_cv and uploaded_cv.filename:
            cv_error = _validate_uploaded_pdf(uploaded_cv)
            if cv_error:
                flash(f"Curriculum Vitae: {cv_error}", "error")
                return _render_acceptance_form(payload)
        elif not existing_cv_doc:
            flash("Upload the assessor Curriculum Vitae PDF before accepting.", "error")
            return _render_acceptance_form(payload)

        if uploaded_highest_qualification and uploaded_highest_qualification.filename:
            qualification_error = _validate_uploaded_pdf(uploaded_highest_qualification)
            if qualification_error:
                flash(f"Highest Qualification document: {qualification_error}", "error")
                return _render_acceptance_form(payload)
        elif not existing_highest_qualification_doc:
            flash("Upload the assessor Highest Qualification document PDF before accepting.", "error")
            return _render_acceptance_form(payload)

        appointment_payload = {
            key: payload.get(key, "")
            for key in [
                "new_employee",
                "employee_number",
                "employed_at_uj",
                "uj_department_division",
                "appointed_as",
                "assessor_surname",
                "assessor_title",
                "assessor_first_names",
                "identity_passport_number",
                "date_of_birth",
                "work_visa_number",
                "gender",
                "marital_status",
                "sa_citizen",
                "nationality",
                "employed_outside_uj",
                "home_language",
                "income_tax_number",
                "care_of_intermediary",
                "home_address",
                "postal_address",
                "home_postal_code",
                "postal_code",
                "home_tel",
                "assessor_contact",
                "assessor_email",
                "work_tel",
                "disability_status",
                "disability_nature",
                "race",
                "qualification_institution",
                "highest_qualification",
                "qualification_awarded_date",
                "qualification_status",
                "bank_changed",
                "bank_account_holder",
                "bank_name",
                "bank_branch_name",
                "bank_branch_code",
                "bank_account_number",
                "bank_account_type",
                "bank_account_ownership",
                "employment_group",
                "appointment_category",
                "appointment_start_date",
                "appointment_end_date",
                "temporary_employment_reason",
                "appointment_reason_other",
                "appointment_motivation",
                "rate_per_month",
                "rate_per_hour",
                "other_rate_basis",
                "total_units",
                "actual_hours",
                "full_cost_centre_string",
                "permanent_post_number",
                "total_budget_for_appointment",
                "conflict_of_interest_details",
                "employee_signature_name",
                "employee_signature_date",
                "appointment_declaration",
            ]
        }
        appointment_payload["_submitted_by"] = str(current_user.id)
        claim_payload = {
            key: payload.get(key, "")
            for key in [
                "employed_at_uj",
                "employed_outside_uj",
                "faculty_division",
                "department_unit_centre",
                "employee_number",
                "month_of_claim",
                "assessor_surname",
                "assessor_title",
                "assessor_first_names",
                "assessor_contact",
                "assessor_email",
                "alternate_contact_number",
                "alternate_email_address",
                "requestor_extension",
                "requestor_email",
                "appointment_start_date",
                "appointment_end_date",
                "appointed_as",
                "claim_unit_basis",
                "rate_per_hour",
                "actual_hours",
                "full_cost_centre_string",
                "appointed_against_permanent_position",
                "position_number",
                "total_budget_for_appointment",
                "contract_eit_number",
                "claim_total_units",
                "claim_rate",
                "claim_currency",
                "amount_claimed",
                "claim_cost_centre_number",
                "total_claimed",
                "bank_changed",
                "bank_account_holder",
                "bank_name",
                "bank_branch_name",
                "bank_branch_code",
                "bank_account_number",
                "bank_account_type",
                "bank_account_ownership",
                "claim_signature_name",
                "claim_signature_date",
                "claim_declaration",
            ]
        }
        claim_payload["_submitted_by"] = str(current_user.id)
        dossier_payload = {
            key: payload.get(key, "")
            for key in [
                "project_title",
                "student_name",
                "student_number",
                "student_initials_surname",
                "current_degree_registered",
                "qualification_description",
                "supervisor_name",
                "supervisor_department",
                "supervisor_phone",
                "supervisor_email",
                "co_supervisor_name",
                "co_supervisor_department",
                "co_supervisor_phone",
                "co_supervisor_email",
                "slot_label",
                "assessor_name",
                "assessor_telephone_number",
                "assessor_email",
                "assessor_contact",
                "assessor_affiliation",
                "assessor_address",
                "highest_qualification",
                "students_supervised_total",
                "current_university_affiliation",
                "international_assessor",
                "publication_count",
                "supervisor_signature_name",
                "supervisor_signature_date",
                "hod_signature_name",
                "hod_signature_date",
                "executive_dean_signature_name",
                "executive_dean_signature_date",
                "assessor_profile_date",
                "assessor_profile_declaration",
            ]
        }
        dossier_payload["cv_filename"] = uploaded_cv.filename if uploaded_cv and uploaded_cv.filename else (existing_cv_doc.original_name if existing_cv_doc else "")
        dossier_payload["_submitted_by"] = str(current_user.id)

        status_before_submit = getattr(project, f"{slot}_invitation_status")
        try:
            _save_form_as_document(project, assessor_temp_appointment_doc_type(slot), assessor_temp_appointment_doc_type(slot), appointment_payload)
            _save_form_as_document(project, assessor_temp_claim_doc_type(slot), assessor_temp_claim_doc_type(slot), claim_payload)
            _save_form_as_document(project, assessor_profile_doc_type(slot), assessor_profile_doc_type(slot), dossier_payload)
            if uploaded_cv and uploaded_cv.filename:
                _store_project_document(project, assessor_cv_doc_type(slot), uploaded_cv)
            if uploaded_highest_qualification and uploaded_highest_qualification.filename:
                _store_project_document(project, assessor_highest_qualification_doc_type(slot), uploaded_highest_qualification)
            _sync_scholar_profile_from_assessor_payload(
                {
                    **payload,
                    "cv_filename": dossier_payload["cv_filename"],
                },
                cv_uploaded=bool((uploaded_cv and uploaded_cv.filename) or existing_cv_doc),
            )
            if status_before_submit == INVITATION_PENDING:
                setattr(project, f"{slot}_invitation_status", INVITATION_ACCEPTED)
                project.comments = append_comment(
                    project.comments,
                    f"{slot_label} onboarding pack submitted and invitation accepted by {current_user.email}",
                )
                if assessor_can_view_student_dissertation(project):
                    dissertation_doc = uploaded_doc_for(project, "dissertation")
                    if dissertation_doc:
                        from .routes_documents import dissertation_assessor_email_messages

                        email_result = send_bulk_emails(
                            dissertation_assessor_email_messages(
                                project,
                                dissertation_doc,
                                assessor_user_ids={current_user.id},
                            )
                        )
                        delivered_count = len(email_result["delivered"])
                        failed_count = len(email_result["failed"])
                        if delivered_count or failed_count:
                            project.comments = append_comment(
                                project.comments,
                                f"Capstone Project release email after assessor acceptance pack: delivered={delivered_count}; failed={failed_count}",
                            )
            else:
                project.comments = append_comment(
                    project.comments,
                    f"{slot_label} onboarding pack updated by {current_user.email}",
                )
            db.session.commit()
        except Exception:
            current_app.logger.exception(
                "Assessor acceptance pack submission failed for project %s slot %s user %s",
                project.id,
                slot,
                current_user.id,
            )
            db.session.rollback()
            flash("Assessor acceptance pack submission failed. Please try again.", "error")
            return _render_acceptance_form(payload)

        if status_before_submit == INVITATION_PENDING:
            flash("Assessor acceptance pack, external examiner nomination form, and CV submitted. Invitation accepted.", "success")
        else:
            flash("Assessor acceptance pack, external examiner nomination form, and CV updated.", "success")
        return redirect(role_landing_url())

    return _render_acceptance_form(prefill)


@mba_bp.route("/projects/<int:project_id>/assessor-grade-form/<slot>", methods=["GET", "POST"])
@login_required
def assessor_grade_form(project_id, slot):
    """Assessor submits a numeric grade and written assessment via web form."""
    if not require_mba_role(MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    valid_slots = assessor_slots_for_user(project, current_user.id)
    if slot not in valid_slots:
        abort(403)

    if getattr(project, f"{slot}_invitation_status") != INVITATION_ACCEPTED:
        flash("Accept the assessor invitation before submitting a grade.", "error")
        return redirect(role_landing_url())

    if not assessor_can_view_student_dissertation(project):
        flash("Assessor pack submission opens after MBA Admin releases the Capstone Manuscript to assessors.", "error")
        return redirect(role_landing_url())

    if project.project_status not in {
        ProjectStatus.HDC_VERIFIED.value,
        ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
        ProjectStatus.RESULTS_DECLINED.value,
    }:
        flash("Assessor pack submission opens after HDC verifies the assessor nominations.", "error")
        return redirect(role_landing_url())

    if assessment_result_pack_complete(project, slot):
        flash("Assessment results have already been submitted and can no longer be edited.", "info")
        return redirect(role_landing_url())

    form_type = assessment_doc_type(slot)
    report_form_type = assessor_report_doc_type(slot)
    narrative_form_type = assessor_narrative_doc_type(slot)
    appointment_form_type = assessor_temp_appointment_doc_type(slot)
    claim_form_type = assessor_temp_claim_doc_type(slot)
    profile = getattr(current_user, "scholar_profile", None)
    student_profile = getattr(project.student, "student_profile", None) if project.student else None
    slot_label = slot.replace("_", " ").title()
    assessor_first_name = getattr(profile, "name", None) or current_user.first_name or ""
    assessor_last_name = getattr(profile, "surname", None) or current_user.last_name or ""
    student_first_name = getattr(student_profile, "name", None) or ""
    student_last_name = getattr(student_profile, "surname", None) or ""
    assessor_display_name = (
        f"{profile.title or ''} {assessor_first_name} {assessor_last_name}".strip()
        if profile else current_user.email
    )
    if not assessor_display_name:
        assessor_display_name = current_user.email or f"{assessor_first_name} {assessor_last_name}".strip()

    recommendation_options = [
        "Accept as the research stands",
        "Accept subject to minor revisions to the satisfaction of the Supervisor / Head of School",
        "Accept subject to major revisions to the satisfaction of the Supervisor / Head of School",
        "Major revisions and re-examination by the same assessor",
        "Outright rejection",
    ]
    yes_no_options = ["Yes", "No"]

    prefill = {
        "assessor_name": assessor_display_name,
        "affiliation": profile.affiliation if profile else "",
        "assessor_email": current_user.email or "",
        "assessor_contact": profile.contact if profile else "",
        "student_name": (
            f"{student_first_name} {student_last_name}".strip()
            if student_profile
            else (project.student.email if project.student else "")
        ),
        "student_number": student_profile.student_number if student_profile else "",
        "research_title": project.project_title,
        "grade": "",
        "recommendation": "",
        "consent_name_disclosure": "",
        "written_assessment": "",
        "assessor_signature_name": assessor_display_name,
        "certification_date": datetime.utcnow().strftime("%Y-%m-%d"),
    }
    for saved_form_type in [appointment_form_type, claim_form_type, form_type, report_form_type, narrative_form_type]:
        saved_form = MbaForm.query.filter_by(project_id=project.id, form_type=saved_form_type).first()
        if (
            saved_form
            and isinstance(saved_form.payload, dict)
            and (
                not saved_form_type.startswith(("assessor_temp_appointment_", "assessor_temp_claim_"))
                or str(saved_form.payload.get("_submitted_by") or "") == str(current_user.id)
            )
        ):
            prefill.update(saved_form.payload)

    template = "mba/form_fill_assessor_grade.html"

    def _render_grade_form(current_prefill):
        return render_template(
            template,
            project=project,
            prefill=current_prefill,
            slot=slot,
            slot_label=slot_label,
            recommendation_options=recommendation_options,
            yes_no_options=yes_no_options,
        )

    if request.method == "POST":
        existing_assessment_form = MbaForm.query.filter_by(project_id=project.id, form_type=form_type).first()
        previous_assessment_payload = (
            dict(existing_assessment_form.payload or {})
            if existing_assessment_form and isinstance(existing_assessment_form.payload, dict)
            else {}
        )
        payload = {
            key: (request.form.get(key) or "").strip()
            for key in request.form
            if key not in {"csrf_token", "_csrf_token"}
        }

        try:
            grade_val = int(payload.get("grade", ""))
            if not (0 <= grade_val <= 100):
                raise ValueError
        except (ValueError, TypeError):
            flash("Final mark must be a whole number between 0 and 100.", "error")
            return _render_grade_form(payload)

        if not payload.get("recommendation"):
            flash("An examination outcome recommendation is required.", "error")
            return _render_grade_form(payload)

        required_messages = {
            "assessor_name": "Assessor name is required.",
            "student_name": "Candidate name is required.",
            "student_number": "Student number is required.",
            "research_title": "Research title is required.",
            "affiliation": "Institutional affiliation is required.",
            "assessor_email": "Assessor email address is required.",
            "assessor_contact": "Assessor contact number is required.",
            "consent_name_disclosure": "Choose whether your name may be divulged to a successful candidate.",
            "written_assessment": "The examiner's detailed report is required.",
            "assessor_signature_name": "External assessor signature / full name is required.",
            "certification_date": "Date is required.",
        }
        for key, message in required_messages.items():
            if not payload.get(key):
                flash(message, "error")
                return _render_grade_form(payload)

        if not request.form.get("declaration"):
            flash("You must confirm the declaration before submitting.", "error")
            return _render_grade_form(payload)

        assessment_payload = {
            "assessor_name": payload.get("assessor_name", ""),
            "affiliation": payload.get("affiliation", ""),
            "assessor_email": payload.get("assessor_email", ""),
            "assessor_contact": payload.get("assessor_contact", ""),
            "student_name": payload.get("student_name", ""),
            "student_number": payload.get("student_number", ""),
            "research_title": payload.get("research_title", ""),
            "grade": payload.get("grade", ""),
            "recommendation": payload.get("recommendation", ""),
            "consent_name_disclosure": payload.get("consent_name_disclosure", ""),
            "written_assessment": payload.get("written_assessment", ""),
            "assessor_signature_name": payload.get("assessor_signature_name", ""),
            "certification_date": payload.get("certification_date", ""),
            "declaration": payload.get("declaration", ""),
        }
        report_payload = dict(assessment_payload)
        narrative_payload = dict(assessment_payload)
        correction_request_triggered = (
            recommendation_requests_corrections(assessment_payload.get("recommendation"))
            and not recommendation_requests_corrections(previous_assessment_payload.get("recommendation"))
        )

        try:
            _save_form_as_document(project, form_type, form_type, assessment_payload)
            _save_form_as_document(project, report_form_type, report_form_type, report_payload)
            _save_form_as_document(project, narrative_form_type, narrative_form_type, narrative_payload)
            if primary_assessment_conflict_detected(project):
                activate_additional_assessment(project)
            if recommendation_requests_corrections(assessment_payload.get("recommendation")):
                activate_project_corrections(project)
            elif not project_correction_requests(project):
                clear_project_corrections(project)
            if all_assessment_results_received(project):
                project.comments = append_comment(project.comments, "All assessor submission packs received.")
            db.session.commit()
        except Exception:
            current_app.logger.exception(
                "Assessor grade submission failed for project %s slot %s user %s",
                project.id,
                slot,
                current_user.id,
            )
            db.session.rollback()
            flash("Grade submission failed. Please try again.", "error")
            return _render_grade_form(payload)

        if correction_request_triggered:
            send_bulk_emails(
                corrections_requested_email_messages(
                    project,
                    {
                        "slot": slot,
                        "slot_label": slot_label,
                        "assessor_name": assessment_payload.get("assessor_name", ""),
                        "recommendation": assessment_payload.get("recommendation", ""),
                    },
                )
            )

        flash(f"Grade and capstone assessor forms submitted for {slot_label}.", "success")
        return redirect(role_landing_url())

    return _render_grade_form(prefill)
