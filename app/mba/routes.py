from datetime import datetime
from html import escape
import secrets

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import MbaForm, MbaProject, MbaProjectComment, MbaProjectSupervisorInvitation, MbaRole, MbaUser, ProjectStatus, normalize_email
from .grading import project_grade_summary
from .route_support import *  # noqa: F403

# Register modularized route groups
from . import routes_admin as _routes_admin  # noqa: F401
from . import routes_dashboard as _routes_dashboard  # noqa: F401
from . import routes_documents as _routes_documents  # noqa: F401
from . import routes_forms as _routes_forms  # noqa: F401
from . import routes_projects as _routes_projects  # noqa: F401


def user_can_comment_on_project(project):
    if current_user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
        return True
    if current_user.role == MbaRole.SCHOLAR.value:
        if project.primary_supervisor_id == current_user.id:
            return True
        return any(
            invitation.supervisor_id == current_user.id
            and invitation.status in {INVITATION_PENDING, INVITATION_ACCEPTED}
            for invitation in project.supervisor_invitations
        )
    return False


def _jbs5_form_and_payload(project):
    jbs5_form = MbaForm.query.filter_by(project_id=project.id, form_type="jbs5").first()
    payload = jbs5_form.payload if jbs5_form and isinstance(jbs5_form.payload, dict) else {}
    return jbs5_form, payload


def _jbs5_signed_by_student_and_supervisor(project):
    jbs5_form, payload = _jbs5_form_and_payload(project)
    return bool(
        uploaded_doc_for(project, "jbs5")
        and jbs5_form
        and jbs5_form.supervisor_signed
        and payload.get("student_signature")
        and payload.get("student_signature_date")
        and payload.get("supervisor_signature")
        and payload.get("supervisor_signature_date")
    )


def _student_submitted_accepted_supervisor_agreement(project):
    supervisor_agreement_form = MbaForm.query.filter_by(
        project_id=project.id, form_type="supervisor_agreement"
    ).first()
    if supervisor_agreement_form:
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
        )
        if supervisor_signed and student_signed:
            return True
    return any(
        doc.doc_type == "supervisor_agreement" and doc.uploaded_by_id == project.student_id
        for doc in getattr(project, "documents", [])
    )


def _clear_unaccepted_supervisor_agreement(project):
    for form in MbaForm.query.filter_by(project_id=project.id, form_type="supervisor_agreement").all():
        db.session.delete(form)
    for doc in list(getattr(project, "documents", []) or []):
        if doc.doc_type == "supervisor_agreement":
            db.session.delete(doc)
    project.supervisor_title_change_requested_at = None
    project.supervisor_title_change_request = None
    project.supervisor_title_change_resolved_at = None


def _student_detail_lines(project):
    student = project.student
    profile = student.student_profile if student and student.student_profile else None
    full_name = ""
    if profile:
        full_name = f"{profile.name or ''} {profile.surname or ''}".strip()
    if not full_name and student:
        full_name = f"{student.first_name or ''} {student.last_name or ''}".strip()
    return [
        f"Student: {full_name or (student.email if student else 'Unknown')}",
        f"Student email: {student.email if student else 'Unknown'}",
        f"Student number: {(profile.student_number if profile else '') or 'Not captured'}",
        f"Degree: {(profile.degree if profile else '') or 'MBA'}",
        f"Capstone Project: {project.project_title}",
        f"Discipline: {project.discipline_name}",
    ]


def _looks_like_email(email):
    return bool(email and "@" in email and "." in email.rsplit("@", 1)[-1])


def can_request_module_completion_verification(project):
    return (
        project
        and project.project_status == ProjectStatus.HDC_VERIFIED.value
        and all_assessment_results_received(project)
        and not additional_assessment_blocks_hdc_submission(project)
        and not corrections_block_hdc_submission(project)
        and not module_completion_allows_hdc_submission(project)
        and project.module_completion_status != "awaiting_marks_committee"
    )


def module_completion_verification_email(project):
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
    student_lines = _student_detail_lines(project)
    text_body = (
        "Please confirm whether this student has passed all required modules.\n\n"
        + "\n".join(student_lines)
        + "\n\n"
        f"Yes, modules passed: {yes_url}\n"
        f"No, modules not passed: {no_url}\n\n"
        "These links are single-use. Once a response is recorded, both options become invalid."
    )
    escaped_details = "".join(f"<li>{escape(line)}</li>" for line in student_lines)
    button_style = (
        "display:inline-block;padding:10px 14px;border-radius:6px;text-decoration:none;"
        "font-weight:700;margin-right:8px;"
    )
    html_body = (
        "<p>Please confirm whether this student has passed all required modules.</p>"
        f"<ul>{escaped_details}</ul>"
        "<p>"
        f"<a href=\"{escape(yes_url)}\" style=\"{button_style}background:#1f7a3a;color:#fff;\">Yes</a>"
        f"<a href=\"{escape(no_url)}\" style=\"{button_style}background:#b42318;color:#fff;\">No</a>"
        "</p>"
        "<p>These links are single-use. Once a response is recorded, both options become invalid.</p>"
    )
    return {
        "recipient": project.module_completion_marks_email,
        "subject": f"Module Completion Verification: {project.project_title}",
        "body": {"text": text_body, "html": html_body},
    }


def module_completion_not_passed_email_messages(project):
    recipients = []
    if project.student and project.student.email:
        recipients.append(project.student.email)
    recipients.extend(project_supervisor_notification_emails(project))
    recipients.extend(mba_admin_notification_emails())
    recipients = [email for email in dict.fromkeys(recipients) if email]
    student_details = "\n".join(_student_detail_lines(project))
    return [
        {
            "recipient": recipient,
            "subject": f"Module Completion Not Yet Confirmed: {project.project_title}",
            "body": (
                "The Marks Committee indicated that the student has not yet passed all required modules.\n\n"
                f"{student_details}\n\n"
                "The student's results must not be forwarded to HDC until module completion is verified."
            ),
        }
        for recipient in recipients
    ]


def _project_grade_summary(project):
    grade_form_types = [assessment_doc_type(slot) for slot in ALL_ASSESSOR_SLOTS]
    forms = MbaForm.query.filter(
        MbaForm.project_id == project.id,
        MbaForm.form_type.in_(grade_form_types),
    ).all()
    forms_by_project = {project.id: {form.form_type: form for form in forms}}
    return project_grade_summary(project.id, forms_by_project)


def _approved_mark_line(project):
    mark = getattr(project, "results_hdc_approved_mark", None)
    if mark is None:
        return ""
    classification = getattr(project, "results_hdc_approved_classification", None)
    classification_text = f" ({classification})" if classification else ""
    return f"\nFinal mark: {mark:.1f}%{classification_text}"


def hdc_results_release_supervisor_email_messages(project):
    recipients = project_supervisor_notification_emails(project)
    if not recipients:
        return []
    mark_line = _approved_mark_line(project)
    dashboard_url = url_for("mba.scholar_dashboard", _external=True)
    reviewed_line = (
        f"\nHDC reviewed at: {project.results_hdc_reviewed_at.strftime('%d %b %Y %H:%M')}"
        if project.results_hdc_reviewed_at
        else ""
    )
    comments_line = f"\nHDC comments:\n{project.results_hdc_comments}" if project.results_hdc_comments else ""
    body = (
        f"MBA Admin has released the HDC-approved assessment results for '{project.project_title}' to you.\n\n"
        f"Student: {project.student.email if project.student else 'Unknown'}\n"
        f"Discipline: {project.discipline_name}{mark_line}{reviewed_line}{comments_line}\n\n"
        "Please sign in to the MBA system to view the approved results. "
        "Do not release the HDC-approved results to the student from the supervisor workspace.\n\n"
        f"Supervisor dashboard: {dashboard_url}"
    )
    return [
        {
            "recipient": recipient,
            "subject": f"HDC-Approved Results Released: {project.project_title}",
            "body": body,
        }
        for recipient in recipients
    ]


def moodle_manuscript_submission_email_messages(project):
    if not project.student or not project.student.email:
        return []

    dashboard_url = url_for("mba.student_dashboard", _external=True)
    student_name = (
        f"{project.student.first_name or ''} {project.student.last_name or ''}".strip()
        or project.student.email
    )
    return [
        {
            "recipient": project.student.email,
            "subject": f"Submit Your Capstone Manuscript on Moodle: {project.project_title}",
            "body": (
                f"Dear {student_name},\n\n"
                f"MBA Admin requests that you submit the Capstone Manuscript for "
                f"'{project.project_title}' through Moodle.\n\n"
                "Do not upload the Capstone Manuscript in the MBA system. In the MBA system, "
                "upload the supporting documents only, including the Global Document and the "
                "combined Turnitin-AI report where required.\n\n"
                "After you submit on Moodle, MBA Admin will download the Capstone Manuscript "
                "from Moodle and upload it in the MBA system for the assessment workflow.\n\n"
                f"MBA dashboard: {dashboard_url}"
            ),
        }
    ]


@mba_bp.route("/module-completion/<token>/<decision>")
def module_completion_verification_response(token, decision):
    decision = (decision or "").strip().lower()
    if decision not in {"yes", "no"}:
        abort(404)
    project = MbaProject.query.filter_by(module_completion_verification_token=token).first()
    if not project:
        return (
            render_template(
                "mba/module_completion_response.html",
                state="invalid",
                title="Verification Link Unavailable",
                message="This module completion verification link is invalid, expired, or has been replaced.",
                project=None,
                student_details=[],
            ),
            404,
        )
    if project.module_completion_responded_at:
        return render_template(
            "mba/module_completion_response.html",
            state="already",
            title="Verification Already Recorded",
            message="This Marks Committee response has already been submitted. The link can no longer be used.",
            project=project,
            student_details=_student_detail_lines(project),
        )

    project.module_completion_responded_at = datetime.utcnow()
    project.module_completion_response = decision
    if decision == "yes":
        project.module_completion_status = "completed"
        project.comments = append_comment(
            project.comments,
            f"Marks Committee confirmed module completion via {project.module_completion_marks_email}.",
        )
        db.session.commit()
        return render_template(
            "mba/module_completion_response.html",
            state="confirmed",
            title="Module Completion Confirmed",
            message="Thank you. The MBA Admin team can now forward the student's results to HDC once all other requirements are complete.",
            project=project,
            student_details=_student_detail_lines(project),
        )

    project.module_completion_status = "modules_incomplete"
    project.comments = append_comment(
        project.comments,
        f"Marks Committee reported modules incomplete via {project.module_completion_marks_email}.",
    )
    email_result = send_bulk_emails(module_completion_not_passed_email_messages(project))
    project.comments = append_comment(
        project.comments,
        f"Module incomplete notification result: delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}",
    )
    db.session.commit()
    return render_template(
        "mba/module_completion_response.html",
        state="not-confirmed",
        title="Module Completion Not Confirmed",
        message="Thank you. The student, supervisor, and MBA Admin team have been notified that results cannot be forwarded to HDC yet.",
        project=project,
        student_details=_student_detail_lines(project),
    )


@mba_bp.route("/projects/<int:project_id>/comments", methods=["POST"])
@login_required
def project_comment(project_id):
    if not require_mba_user():
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    if not user_can_comment_on_project(project):
        abort(403)

    comment = (request.form.get("comment") or "").strip()
    if comment:
        db.session.add(
            MbaProjectComment(
                project_id=project.id,
                author_id=current_user.id,
                comment=comment,
            )
        )
        project.comments = append_comment(project.comments, f"{current_user.email}: {comment}")
        db.session.commit()
        flash("Comment saved.", "success")
    return redirect(request.referrer or role_landing_url())


def corrections_rejection_student_email_messages(project, rejection_comment):
    if not project.student or not project.student.email:
        return []
    supervisor_label = (
        f"{(current_user.first_name or '').strip()} {(current_user.last_name or '').strip()}".strip()
        or current_user.email
    )
    correction_url = url_for("mba.student_corrections", _external=True)
    return [
        {
            "recipient": project.student.email,
            "subject": f"Corrections Returned for Revision: {project.project_title}",
            "body": (
                f"{supervisor_label} ({current_user.email}) reviewed your corrected response pack for "
                f"'{project.project_title}' and returned it for revision.\n\n"
                f"Supervisor comments:\n{rejection_comment}\n\n"
                "Please sign in to the MBA system, open the Response to Assessors' Comments section, "
                "revise the corrected Capstone Manuscript and response form, and upload a resubmitted "
                f"Turnitin report with the revised pack.\n\nCorrection queue: {correction_url}"
            ),
        }
    ]


@mba_bp.route("/projects/<int:project_id>/supervisor-approve-corrections", methods=["POST"])
@login_required
def supervisor_approve_corrections(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    if not supervisor_can_manage_corrections(project, current_user):
        abort(403)
    if not project_has_active_corrections(project):
        flash("There are no active assessor comments on this Capstone Project.", "error")
        return redirect(url_for("mba.scholar_corrections"))
    if not student_submitted_corrections_pack(project):
        flash("The student must submit the corrected Capstone Manuscript, Response to Assessors' Comments form, and resubmitted Turnitin report first.", "error")
        return redirect(url_for("mba.scholar_corrections"))

    comment = (request.form.get("comment") or "").strip()
    project.corrections_supervisor_approved_at = datetime.utcnow()
    project.corrections_supervisor_comments = comment or None
    project.corrections_supervisor_rejected_at = None
    project.corrections_supervisor_rejection_comments = None
    project.comments = append_comment(
        project.comments,
        f"{current_user.email}: approved the student's response to assessors' comments.",
    )
    response_doc = uploaded_doc_for(project, "corrections_response")
    turnitin_doc = uploaded_doc_for(project, "corrections_turnitin_report")
    corrected_doc = uploaded_doc_for(project, "corrected_dissertation")
    from .routes_documents import corrections_approval_admin_email_messages

    email_result = send_bulk_emails(
        corrections_approval_admin_email_messages(project, response_doc, turnitin_doc, corrected_doc)
    )
    project.comments = append_comment(
        project.comments,
        (
            "Corrections approval admin email result: "
            f"delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}"
        ),
    )
    db.session.commit()
    flash("Response pack approved. MBA Admin has been notified for HDC submission review.", "success")
    return redirect(url_for("mba.scholar_corrections"))


@mba_bp.route("/projects/<int:project_id>/supervisor-reject-corrections", methods=["POST"])
@login_required
def supervisor_reject_corrections(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    if not supervisor_can_manage_corrections(project, current_user):
        abort(403)
    if not project_has_active_corrections(project):
        flash("There are no active assessor comments on this Capstone Project.", "error")
        return redirect(url_for("mba.scholar_corrections"))
    if not student_submitted_corrections_pack(project):
        flash("The student must submit the corrected response pack before it can be returned.", "error")
        return redirect(url_for("mba.scholar_corrections"))
    if supervisor_approved_corrections(project):
        flash("The response pack has already been approved.", "info")
        return redirect(url_for("mba.scholar_corrections"))

    rejection_comment = (request.form.get("rejection_comment") or "").strip()
    if not rejection_comment:
        flash("Enter comments for the student before returning the corrections.", "error")
        return redirect(url_for("mba.scholar_corrections"))

    project.corrections_supervisor_approved_at = None
    project.corrections_supervisor_comments = None
    project.corrections_supervisor_rejected_at = datetime.utcnow()
    project.corrections_supervisor_rejection_comments = rejection_comment
    project.comments = append_comment(
        project.comments,
        f"{current_user.email}: returned the student's corrections for revision.",
    )
    email_result = send_bulk_emails(
        corrections_rejection_student_email_messages(project, rejection_comment)
    )
    project.comments = append_comment(
        project.comments,
        (
            "Corrections rejection student email result: "
            f"delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}"
        ),
    )
    db.session.commit()
    flash("Corrections returned to the student. The student has been notified.", "success")
    return redirect(url_for("mba.scholar_corrections"))


@mba_bp.route("/projects/<int:project_id>/supervisor-release-corrections", methods=["POST"])
@login_required
def supervisor_release_corrections(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    if not supervisor_can_manage_corrections(project, current_user):
        abort(403)
    if not assessment_results_forwarded_to_supervisor(project):
        flash("MBA Admin must forward the assessment summary before comments can be released to the student.", "error")
        return redirect(url_for("mba.scholar_corrections"))
    if not project_has_active_corrections(project):
        flash("There are no assessor comments to release.", "error")
        return redirect(url_for("mba.scholar_corrections"))

    project.corrections_released_to_student_at = datetime.utcnow()
    project.comments = append_comment(
        project.comments,
        f"{current_user.email}: released assessor comments to the student.",
    )
    db.session.commit()

    messages = []
    if project.student and project.student.email:
        messages.append(
            {
                "recipient": project.student.email,
                "subject": f"Response to Assessors' Comments Required: {project.project_title}",
                "body": (
                    f"Your supervisor has released assessor comments for your MBA Capstone Project "
                    f"'{project.project_title}'.\n\n"
                    "Please sign in to the MBA system, open the Response to Assessors' Comments section, "
                    "upload the corrected Capstone Manuscript, fill the Response to Assessors' Comments form, "
                    "and upload the resubmitted Turnitin report in the MBA system."
                ),
            }
        )
    send_bulk_emails(messages)
    flash("Assessor comments released to the student.", "success")
    return redirect(url_for("mba.scholar_corrections"))


@mba_bp.route("/projects/<int:project_id>/admin-action", methods=["POST"])
@login_required
def admin_project_action(project_id):
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    action = request.form.get("action")
    comment = (request.form.get("comment") or "").strip()
    retired_admin_actions = {
        "apply_suggestions",
        "send_invitations",
        "unlock_supervisor",
        "override_supervisor",
        "revise_supervisors",
        "multi_invite_supervisors",
        "unlock_assessors",
        "confirm_assessors",
        "override_assessors",
        "invite_single_assessor_1",
        "invite_single_assessor_2",
        "decline",
        "reopen_dissertation_submission",
        "mark_modules_completed",
        "set_marks_committee_awaiting",
        "set_marks_committee_response_received",
    }
    if action in retired_admin_actions:
        flash("That older admin action has been retired. Use the visible workflow controls on the project card.", "info")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    supervisor_change_locked = _student_submitted_accepted_supervisor_agreement(project)
    supervisor_change_actions = {
        "invite_selected_supervisors",
        "invite_suggested_supervisors",
    }
    if action in supervisor_change_actions and supervisor_change_locked:
        flash(
            "Supervisor invitations can no longer be changed because the student has submitted the accepted Supervisor Agreement.",
            "error",
        )
        return redirect(url_for("mba.admin_dashboard", panel="projects"))
    assessor_invite_actions = {"invite_selected_assessors", "invite_suggested_assessors"}
    assessor_change_locked = (
        accepted_assessor_count(project) >= len(PRIMARY_ASSESSOR_SLOTS)
        and not hdc_declined_assessor_nomination(project)
        and not hdc_declined_assessor_slots(project)
    )
    if action in assessor_invite_actions and assessor_change_locked:
        flash(
            "Assessor invitations can no longer be changed because both assessors have accepted their invitations.",
            "error",
        )
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    selected_supervisor_ids = []
    for supervisor_id in request.form.getlist("supervisor_ids"):
        try:
            selected_supervisor_ids.append(int(supervisor_id))
        except (TypeError, ValueError):
            continue
    suggested_supervisor_ids = []
    for supervisor_id in request.form.getlist("suggested_supervisor_ids"):
        try:
            suggested_supervisor_ids.append(int(supervisor_id))
        except (TypeError, ValueError):
            continue
    assessor_ids = (
        [request.form.get(f"assessor_{index}_id", type=int) for index in range(1, 3)]
        if action == "invite_selected_assessors"
        else [project.assessor_1_id, project.assessor_2_id]
    )
    assessor_suggested_ids = (
        [request.form.get(f"assessor_{index}_suggested_id", type=int) for index in range(1, 3)]
        if action == "invite_suggested_assessors"
        else assessor_ids
    )
    valid_examiner_ids = None

    def _valid_examiner_ids():
        nonlocal valid_examiner_ids
        if valid_examiner_ids is None:
            valid_examiner_ids = {user.id for user in examiners_query().all()}
        return valid_examiner_ids

    previous_assessor_assignments = {
        f"{slot}_id": getattr(project, f"{slot}_id")
        for slot in PRIMARY_ASSESSOR_SLOTS
    }
    previous_hdc_declined_assessor_slots = hdc_declined_assessor_slots(project)
    hdc_rejection_without_slot_decisions = hdc_rejection_without_slot_decisions_requires_replacement(project)

    def _projects_redirect():
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    def _assessor_invitation_prerequisite_error():
        if not project.jbs5_hdc_approved_at:
            return "HDC must approve JBS5 before assessor invitations can be sent."
        if not student_submitted_assessor_prerequisite_docs(project):
            return "Both JBS10 and Intent to Submit must be submitted by the student before sending assessor invitations."
        if not (project.supervisor_confirmed or project.supervisor_accepted_at):
            return "Supervisor must be confirmed before assigning assessors."
        return None

    def _assessor_slot_has_active_invitation(slot):
        return (
            previous_assessor_assignments.get(f"{slot}_id")
            and getattr(project, f"{slot}_invitation_status") in {INVITATION_PENDING, INVITATION_ACCEPTED}
            and not assessor_hdc_decline_requires_replacement(project, slot)
            and not hdc_rejection_without_slot_decisions
        )

    def _invite_assessor_pairs(slot_id_pairs, source_label):
        prerequisite_error = _assessor_invitation_prerequisite_error()
        if prerequisite_error:
            flash(prerequisite_error, "error")
            return _projects_redirect()

        selected_pairs = [(slot, assessor_id) for slot, assessor_id in slot_id_pairs if assessor_id]
        selected_ids = [assessor_id for _, assessor_id in selected_pairs]
        if not 1 <= len(selected_ids) <= len(PRIMARY_ASSESSOR_SLOTS):
            flash("Select one or two assessors to invite.", "error")
            return _projects_redirect()
        if len(selected_ids) != len(set(selected_ids)):
            flash("Each selected assessor must be different.", "error")
            return _projects_redirect()

        invalid_ids = [assessor_id for assessor_id in selected_ids if assessor_id not in _valid_examiner_ids()]
        if invalid_ids:
            flash("One or more selected assessors are invalid.", "error")
            return _projects_redirect()

        active_slots = [slot for slot in PRIMARY_ASSESSOR_SLOTS if _assessor_slot_has_active_invitation(slot)]
        slots_to_invite = []
        blocked_replacement = False
        unchanged_declined_slot = None
        for slot, assessor_id in selected_pairs:
            current_id = previous_assessor_assignments.get(f"{slot}_id")
            current_status = getattr(project, f"{slot}_invitation_status")
            unchanged = current_id == assessor_id
            slot_rejected_by_hdc = (
                assessor_hdc_decline_requires_replacement(project, slot)
                or hdc_rejection_without_slot_decisions
            )

            if (
                current_status in {INVITATION_PENDING, INVITATION_ACCEPTED}
                and not slot_rejected_by_hdc
            ):
                if unchanged:
                    continue
                blocked_replacement = True
                break
            if unchanged and (
                current_status == INVITATION_DECLINED
                or slot_rejected_by_hdc
            ):
                unchanged_declined_slot = slot
                break
            slots_to_invite.append(slot)

        if blocked_replacement:
            flash("An invited assessor cannot be replaced unless that assessor declines the invitation.", "error")
            return _projects_redirect()
        if unchanged_declined_slot:
            label = INVITATION_SLOTS[unchanged_declined_slot]["label"]
            flash(f"Choose a replacement for the declined {label} before sending a new invitation.", "error")
            return _projects_redirect()
        if not slots_to_invite:
            if len(active_slots) >= len(PRIMARY_ASSESSOR_SLOTS):
                flash("Two assessor invitations are already active. Wait for one assessor to decline before inviting another assessor.", "error")
            else:
                flash("Select an empty or declined assessor slot before sending a new invitation.", "error")
            return _projects_redirect()
        if len(active_slots) + len(slots_to_invite) > len(PRIMARY_ASSESSOR_SLOTS):
            flash("Two assessor invitations are already active. Wait for one assessor to decline before inviting another assessor.", "error")
            return _projects_redirect()

        changed_slots = [
            slot
            for slot in slots_to_invite
            if previous_assessor_assignments.get(f"{slot}_id") != dict(selected_pairs)[slot]
        ]
        if changed_slots:
            if hdc_rejection_without_slot_decisions:
                for slot in changed_slots:
                    if previous_assessor_assignments.get(f"{slot}_id"):
                        set_assessor_hdc_decision(project, slot, HDC_ASSESSOR_DECLINED)
            reset_assessor_invitation_tracking(
                project,
                changed_slots,
                clear_hdc_decisions=not (
                    previous_hdc_declined_assessor_slots
                    or hdc_rejection_without_slot_decisions
                ),
            )
            clear_additional_assessment(project)
            if previous_hdc_declined_assessor_slots or hdc_rejection_without_slot_decisions:
                project.project_status = ProjectStatus.HDC_DECLINED.value
                project.nomination_form_approved = False

        selected_by_slot = dict(selected_pairs)
        for slot in slots_to_invite:
            setattr(project, f"{slot}_id", selected_by_slot[slot])
            setattr(project, f"{slot}_invitation_status", INVITATION_PENDING)
        project.assessor_3_id = None
        project.assessor_3_invitation_status = None
        project.assessor_3_invited_at = None
        project.assessor_3_reminder_sent_at = None
        project.assessors_confirmed = True
        project.assessors_nominated_at = datetime.utcnow()
        project.nomination_form_submitted = True
        project.nomination_form_approved = False
        mark_assessor_invitations_sent(project, slots=slots_to_invite)
        email_result = send_bulk_emails(
            invitation_email_messages(
                project,
                include_supervisors=False,
                assessor_slots=slots_to_invite,
            )
        )
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        assessor_emails = ", ".join(
            getattr(project, slot).email
            for slot in slots_to_invite
            if getattr(project, slot, None) and getattr(project, slot).email
        )
        project.comments = append_comment(
            project.comments,
            (
                f"{current_user.email}: invited {source_label}: "
                f"{assessor_emails or 'none'}; delivered={delivered_count}; failed={failed_count}"
            ),
        )
        db.session.commit()
        if delivered_count and not failed_count:
            flash("Assessor invitation(s) sent.", "success")
        elif delivered_count and failed_count:
            flash(f"Assessor invitation(s) recorded. Email sent to {delivered_count}; {failed_count} failed.", "warning")
        else:
            flash("Assessor invitation(s) recorded. Email delivery is not configured or failed.", "warning")
        return _projects_redirect()

    if action == "invite_selected_supervisors":
        if not 1 <= len(selected_supervisor_ids) <= 2:
            flash("Select one or two supervisors to invite.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if len(selected_supervisor_ids) != len(set(selected_supervisor_ids)):
            flash("Each selected supervisor must be different.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        selected_supervisors = supervisors_query().filter(MbaUser.id.in_(selected_supervisor_ids)).all()
        supervisors_by_id = {supervisor.id: supervisor for supervisor in selected_supervisors}
        if set(selected_supervisor_ids) != set(supervisors_by_id):
            flash("One or more selected supervisors are invalid.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        _clear_unaccepted_supervisor_agreement(project)
        project.supervisor_invitations.clear()
        invited_at = datetime.utcnow()
        for supervisor_id in selected_supervisor_ids:
            project.supervisor_invitations.append(
                MbaProjectSupervisorInvitation(
                    project=project,
                    supervisor=supervisors_by_id[supervisor_id],
                    status=INVITATION_PENDING,
                    invited_at=invited_at,
                )
            )
        project.primary_supervisor_id = None
        project.project_status = ProjectStatus.ADMIN_SUBMITTED.value
        project.primary_supervisor_invitation_status = INVITATION_PENDING
        project.supervisor_confirmed = True
        project.supervisor_accepted_at = None
        project.assignment_confirmed = True
        project.assessors_confirmed = False
        project.assessors_nominated_at = None
        project.nomination_form_submitted = False
        reset_assessor_invitation_tracking(project)
        clear_additional_assessment(project)
        set_invitations_sent(project)
        mark_supervisor_invitations_sent(project)
        email_result = send_bulk_emails(invitation_email_messages(project, include_assessors=False))
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        supervisor_emails = ", ".join(supervisors_by_id[supervisor_id].email for supervisor_id in selected_supervisor_ids)
        project.comments = append_comment(
            project.comments,
            f"Admin invited selected supervisors: {supervisor_emails}; delivered={delivered_count}; failed={failed_count}",
        )
        db.session.commit()
        if delivered_count and not failed_count:
            flash("Selected supervisor invitation(s) sent.", "success")
        elif delivered_count and failed_count:
            flash(f"Selected supervisor invitation(s) recorded. Email sent to {delivered_count}; {failed_count} failed.", "warning")
        else:
            flash("Selected supervisor invitation(s) recorded. Email delivery is not configured or failed.", "warning")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    elif action == "invite_suggested_supervisors":
        supervisors = supervisors_query().all()
        supervisor_ids = suggested_supervisor_ids[:SUPERVISOR_SUGGESTION_LIMIT]
        if len(supervisor_ids) < SUPERVISOR_SUGGESTION_LIMIT:
            recommendations = match_recommendations(
                project,
                supervisors,
                examiners_query().all(),
                supervisor_workload_by_user_id=supervisor_workload_counts(exclude_project_id=project.id),
                assessor_workload_by_user_id=assessor_workload_counts(exclude_project_id=project.id),
            )
            supervisor_ids = [
                item["user"].id
                for item in recommendations["ranked_supervisors"][:SUPERVISOR_SUGGESTION_LIMIT]
                if item.get("user")
            ]
        if (
            len(supervisor_ids) != SUPERVISOR_SUGGESTION_LIMIT
            or len(supervisor_ids) != len(set(supervisor_ids))
        ):
            flash("Two supervisor suggestions are required before inviting suggested supervisors.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        supervisors_by_id = {supervisor.id: supervisor for supervisor in supervisors if supervisor.id in supervisor_ids}
        if set(supervisor_ids) != set(supervisors_by_id):
            flash("One or more suggested supervisors are invalid.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        _clear_unaccepted_supervisor_agreement(project)
        project.supervisor_invitations.clear()
        invited_at = datetime.utcnow()
        for supervisor_id in supervisor_ids:
            project.supervisor_invitations.append(
                MbaProjectSupervisorInvitation(
                    project=project,
                    supervisor=supervisors_by_id[supervisor_id],
                    status=INVITATION_PENDING,
                    invited_at=invited_at,
                )
            )
        project.primary_supervisor_id = None
        project.project_status = ProjectStatus.ADMIN_SUBMITTED.value
        project.supervisor_accepted_at = None
        project.assignment_confirmed = True
        project.supervisor_confirmed = True
        project.assessors_confirmed = False
        project.assessors_nominated_at = None
        project.nomination_form_submitted = False
        reset_assessor_invitation_tracking(project)
        clear_additional_assessment(project)
        set_invitations_sent(project)
        for invitation in project.supervisor_invitations:
            invitation.status = INVITATION_PENDING
        mark_supervisor_invitations_sent(project)
        project.primary_supervisor_invitation_status = (
            INVITATION_PENDING if project.supervisor_invitations else None
        )
        email_result = send_bulk_emails(invitation_email_messages(project, include_assessors=False))
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        if delivered_count and not failed_count:
            message = "Invitations sent to supervisor(s) by email."
        elif delivered_count and failed_count:
            message = f"Invitations recorded. Email sent to {delivered_count} recipient(s); {failed_count} failed."
        else:
            message = "Invitations recorded in the system. Email delivery is not configured or failed."
        supervisor_emails = ", ".join(invitation.supervisor.email for invitation in project.supervisor_invitations if invitation.supervisor and invitation.supervisor.email)
        project.comments = append_comment(
            project.comments,
            f"Admin invited suggested supervisors: {supervisor_emails}; delivered={delivered_count}; failed={failed_count}",
        )
        db.session.commit()
        flash(message, "success")
        return redirect(url_for("mba.admin_dashboard", panel="projects"))

    if action == "invite_selected_assessors":
        return _invite_assessor_pairs(
            list(zip(PRIMARY_ASSESSOR_SLOTS, assessor_ids)),
            "selected assessor(s)",
        )

    if action == "invite_suggested_assessors":
        examiners = examiners_query().all()
        assessor_invitations_started = any(
            getattr(project, f"{slot}_invitation_status") in {
                INVITATION_PENDING,
                INVITATION_ACCEPTED,
                INVITATION_DECLINED,
            }
            for slot in PRIMARY_ASSESSOR_SLOTS
        )
        if (
            assessor_invitations_started
            or project.project_status == ProjectStatus.HDC_DECLINED.value
            or previous_hdc_declined_assessor_slots
        ):
            flash("Use Invite Selected Assessors to add or replace one assessor after invitations have started.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        suggested_assessor_ids = [assessor_id for assessor_id in assessor_suggested_ids if assessor_id]
        if len(suggested_assessor_ids) < len(PRIMARY_ASSESSOR_SLOTS):
            recommendations = match_recommendations(
                project,
                supervisors_query().all(),
                examiners,
                supervisor_workload_by_user_id=supervisor_workload_counts(exclude_project_id=project.id),
                assessor_workload_by_user_id=assessor_workload_counts(exclude_project_id=project.id),
            )
            suggested_assessor_ids = [
                assessor.id
                for assessor in recommendations["assessors"][: len(PRIMARY_ASSESSOR_SLOTS)]
                if assessor
            ]
        if (
            len(suggested_assessor_ids) != len(PRIMARY_ASSESSOR_SLOTS)
            or len(suggested_assessor_ids) != len(set(suggested_assessor_ids))
        ):
            flash("Two assessor suggestions are required before inviting suggested assessors.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        return _invite_assessor_pairs(
            list(zip(PRIMARY_ASSESSOR_SLOTS, suggested_assessor_ids[: len(PRIMARY_ASSESSOR_SLOTS)])),
            "suggested assessor(s)",
        )

    if action == "forward_jbs5_to_hdc":
        if project.jbs5_hdc_approved_at:
            flash("JBS5 has already been approved by HDC.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if project.project_status == ProjectStatus.JBS5_SUBMITTED_TO_HDC.value:
            flash("JBS5 has already been forwarded to HDC and is waiting for approval.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not _jbs5_signed_by_student_and_supervisor(project):
            flash("JBS5 must be signed by both the student and supervisor before it can be forwarded to HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.project_status = ProjectStatus.JBS5_SUBMITTED_TO_HDC.value
        project.title_approved = False
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: forwarded supervisor-signed JBS5 to HDC for approval.",
        )
        message = "JBS5 forwarded to HDC for approval."
    elif action == "approve_to_hdc":
        if project.project_status == ProjectStatus.ADMIN_APPROVED.value:
            flash("Assessor nominations have already been forwarded to HDC and are awaiting review.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if project.project_status in NOMINATION_FORWARDING_UNAVAILABLE_STATUSES:
            flash("HDC has already approved the JBS10 and nominated assessors, so nominations cannot be forwarded again.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not project.jbs5_hdc_approved_at:
            flash("Forward JBS5 to HDC and wait for HDC approval before forwarding assessor nominations.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not student_submitted_assessor_prerequisite_docs(project):
            flash("JBS10 and Intent to Submit must be submitted by the student before nominations can be forwarded to HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        declined_hdc_slots = hdc_declined_assessor_slots(project)
        if declined_hdc_slots:
            declined_labels = ", ".join(INVITATION_SLOTS[slot]["label"] for slot in declined_hdc_slots)
            flash(f"Replace the assessor nomination declined by HDC before forwarding again: {declined_labels}.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        resolved_declined_slots = hdc_resolved_declined_assessor_slots(project)
        if resolved_declined_slots:
            reset_assessor_hdc_decisions(project, resolved_declined_slots)
        if accepted_assessor_count(project) < len(PRIMARY_ASSESSOR_SLOTS):
            flash("Forward nominations to HDC is only available after two assessors have accepted their invitations.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        invitation_state = project_invitation_snapshot(project)
        if not invitation_state["can_approve_to_hdc"]:
            flash("Forward nominations to HDC is only available after all invitations are accepted and each assessor has submitted the acceptance pack, external examiner nomination form, CV, and highest qualification document.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not _jbs5_signed_by_student_and_supervisor(project):
            flash("JBS5 must be signed by both the student and supervisor before it is sent to HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        supervisor_agreement_form = MbaForm.query.filter_by(project_id=project.id, form_type="supervisor_agreement").first()
        if not (
            uploaded_doc_for(project, "supervisor_agreement")
            and supervisor_agreement_form
            and supervisor_agreement_form.student_signed
            and supervisor_agreement_form.supervisor_signed
        ):
            flash("The Supervisor Agreement must be signed by both the student and supervisor before the Capstone Project is sent to HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.project_status = ProjectStatus.ADMIN_APPROVED.value
        project.nomination_form_approved = False
        message = "Assessor nominations have been sent to HDC for approval."
    elif action == "request_moodle_manuscript_submission":
        if not project.student or not project.student.email:
            flash("The student does not have an email address on file.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if uploaded_doc_for(project, "dissertation"):
            flash("The Capstone Manuscript has already been uploaded from Moodle.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not student_submitted_assessor_prerequisite_docs(project):
            flash("Ask the student to submit the Capstone Manuscript only after JBS10 and Intent to Submit are on file and JBS5 is approved by HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))

        project.dissertation_moodle_request_sent_at = datetime.utcnow()
        email_result = send_bulk_emails(moodle_manuscript_submission_email_messages(project))
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        project.comments = append_comment(
            project.comments,
            (
                f"{current_user.email}: requested student to submit the Capstone Manuscript through Moodle; "
                f"delivered={delivered_count}; failed={failed_count}"
            ),
        )
        if delivered_count and not failed_count:
            message = "Student was asked to submit the Capstone Manuscript through Moodle."
        elif delivered_count and failed_count:
            message = f"Moodle submission request recorded. Email sent; {failed_count} failed."
        else:
            message = "Moodle submission request recorded. Email delivery is not configured or failed."
    elif action == "release_dissertation_to_assessors":
        if not assessor_can_view_project_documents(project):
            flash("Release the nomination stage to assessors before releasing the Capstone Manuscript.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        dissertation_doc = uploaded_doc_for(project, "dissertation")
        if not dissertation_doc:
            flash("The Admin-uploaded Capstone Manuscript is not on file yet.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if project.dissertation_released_to_assessors:
            flash("The Capstone Manuscript has already been released to assessors.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))

        from .routes_documents import dissertation_assessor_email_messages

        project.dissertation_released_to_assessors = True
        project.dissertation_released_at = datetime.utcnow()
        email_result = send_bulk_emails(dissertation_assessor_email_messages(project, dissertation_doc))
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        project.comments = append_comment(
            project.comments,
            f"MBA Admin released the Capstone Manuscript to assessors: delivered={delivered_count}; failed={failed_count}",
        )
        if delivered_count and not failed_count:
            message = "Capstone Manuscript released to assessors and email notifications sent."
        elif delivered_count and failed_count:
            message = f"Capstone Manuscript released to assessors. Email sent to {delivered_count}; {failed_count} failed."
        elif failed_count:
            message = "Capstone Manuscript released to assessors, but assessor email delivery failed."
        else:
            message = "Capstone Manuscript released to assessors. No accepted assessor email recipients were available yet."
    elif action == "assign_additional_assessor":
        if project.project_status not in {ProjectStatus.HDC_VERIFIED.value, ProjectStatus.RESULTS_DECLINED.value}:
            flash("Additional assessment can only be managed while the results are still with MBA Admin.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))
        if not additional_assessment_required(project):
            flash("This project does not currently require an additional assessment.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))
        if not all(assessment_result_pack_complete(project, slot) for slot in PRIMARY_ASSESSOR_SLOTS):
            flash("Both primary assessor result packs must be submitted before assigning the third assessor.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))

        additional_assessor_id = request.form.get("additional_assessor_id", type=int)
        if not additional_assessor_id:
            flash("Select the third assessor to invite.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))

        valid_examiner_ids = {user.id for user in examiners_query().all()}
        if additional_assessor_id not in valid_examiner_ids:
            flash("Selected additional assessor is invalid.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))
        if additional_assessor_id in {project.primary_supervisor_id, project.assessor_1_id, project.assessor_2_id}:
            flash("The additional assessor must be different from the supervisor and the two original assessors.", "error")
            return redirect(url_for("mba.admin_additional_assessment"))

        suggested_assessor = suggested_additional_assessor(project, examiners_query().all())
        previous_additional_assessor_id = project.assessor_3_id
        if previous_additional_assessor_id and previous_additional_assessor_id != additional_assessor_id:
            reset_assessor_slot_artifacts(project, ADDITIONAL_ASSESSOR_SLOT)

        activate_additional_assessment(project)
        project.assessor_3_id = additional_assessor_id
        project.assessor_3_invitation_status = INVITATION_PENDING
        mark_assessor_invitations_sent(project, slots=[ADDITIONAL_ASSESSOR_SLOT])

        additional_assessor = db.session.get(MbaUser, additional_assessor_id)
        was_override = bool(suggested_assessor and suggested_assessor.id != additional_assessor_id)
        email_result = send_bulk_emails(
            [
                {
                    "recipient": additional_assessor.email,
                    "subject": f"MBA Additional Assessment Invitation: {project.project_title}",
                    "body": (
                        f"You have been invited to serve as Additional Assessor for the MBA Capstone Project '{project.project_title}'.\n\n"
                        f"Student: {project.student.email if project.student else 'Unknown'}\n"
                        f"Discipline: {project.discipline_name}\n\n"
                        "This additional assessment was requested because the first two assessor outcomes conflict. "
                        "Please sign in to the MBA system to complete the acceptance pack and submit your assessment."
                    ),
                }
            ]
        )
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        project.comments = append_comment(
            project.comments,
            (
                f"Admin assigned additional assessor: {additional_assessor.email}; "
                f"override={'yes' if was_override else 'no'}; delivered={delivered_count}; failed={failed_count}"
            ),
        )
        if delivered_count and not failed_count:
            message = "Additional assessor assigned and invitation sent."
        elif delivered_count and failed_count:
            message = f"Additional assessor assigned. Email sent to {delivered_count}; {failed_count} failed."
        else:
            message = "Additional assessor assigned. Email delivery is not configured or failed."
    elif action == "request_module_completion_verification":
        if not can_request_module_completion_verification(project):
            flash("Module completion verification is not available for this Capstone Project right now.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        marks_email = normalize_email(request.form.get("marks_committee_email"))
        if not _looks_like_email(marks_email):
            flash("Enter a valid Marks Committee email address.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.module_completion_status = "awaiting_marks_committee"
        project.module_completion_marks_email = marks_email
        project.module_completion_verification_token = secrets.token_urlsafe(32)
        project.module_completion_requested_at = datetime.utcnow()
        project.module_completion_responded_at = None
        project.module_completion_response = None
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: requested module completion verification from {marks_email}.",
        )
        email_result = send_bulk_emails([module_completion_verification_email(project)])
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        project.comments = append_comment(
            project.comments,
            f"Module completion verification email result: delivered={delivered_count}, failed={failed_count}",
        )
        if delivered_count and not failed_count:
            message = "Module completion verification request sent to the Marks Committee."
        elif failed_count:
            message = "Module completion verification request recorded, but email delivery failed or is not configured."
        else:
            message = "Module completion verification request recorded."
    elif action == "forward_results_to_supervisor":
        if not all_assessment_results_received(project):
            flash("All assessor results must be received before forwarding the assessment summary to the supervisor.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if project.assessment_results_forwarded_to_supervisor_at:
            flash("Assessment summary has already been forwarded to the supervisor.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.assessment_results_forwarded_to_supervisor_at = datetime.utcnow()
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: forwarded the assessment summary to the supervisor.",
        )
        messages = []
        for supervisor_email in project_supervisor_notification_emails(project):
            messages.append(
                {
                    "recipient": supervisor_email,
                    "subject": f"Assessment Summary Ready for Supervisor Review: {project.project_title}",
                    "body": (
                        f"MBA Admin has forwarded the assessment summary for the Capstone Project "
                        f"'{project.project_title}'.\n\n"
                        "If assessors requested corrections or raised comments, only the supervisor may release "
                        "those comments to the student."
                    ),
                }
            )
        email_result = send_bulk_emails(messages)
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        if delivered_count and not failed_count:
            message = "Assessment summary forwarded to the supervisor."
        elif delivered_count and failed_count:
            message = f"Assessment summary forwarded. Email sent to {delivered_count}; {failed_count} failed."
        else:
            message = "Assessment summary forwarded. Email delivery is not configured or failed."
    elif action == "release_hdc_results_to_supervisor":
        if not hdc_results_approved(project):
            flash("Only HDC-approved results can be released to the supervisor.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if results_released_to_supervisor(project):
            flash("The HDC-approved results have already been released to the supervisor.", "info")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.results_released_to_supervisor_at = datetime.utcnow()
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: released HDC-approved assessment results to the supervisor.",
        )
        email_result = send_bulk_emails(hdc_results_release_supervisor_email_messages(project))
        delivered_count = len(email_result["delivered"])
        failed_count = len(email_result["failed"])
        project.comments = append_comment(
            project.comments,
            f"HDC-approved results supervisor release email result: delivered={delivered_count}, failed={failed_count}",
        )
        if delivered_count and not failed_count:
            message = "HDC-approved results released to the supervisor."
        elif delivered_count and failed_count:
            message = f"HDC-approved results released. Email sent to {delivered_count}; {failed_count} failed."
        else:
            message = "HDC-approved results release recorded. Email delivery is not configured or failed."
    elif action == "submit_results_to_hdc":
        if project.project_status not in RESULTS_HDC_SUBMISSION_STATUSES:
            flash("Results can only be sent to HDC after nominations are approved, or after HDC has returned the results for resubmission.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        resubmitting_results = project.project_status == ProjectStatus.RESULTS_DECLINED.value
        if not all_assessment_results_received(project):
            flash("Both assessor results must be uploaded before sending results to HDC.", "error")
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if additional_assessment_blocks_hdc_submission(project):
            flash(
                "Results cannot be sent to HDC while the additional assessment is still pending.",
                "error",
            )
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if corrections_block_hdc_submission(project):
            flash(
                "Results cannot be sent to HDC while assessor-requested corrections are still open. "
                "Wait for the student's corrected Capstone Manuscript, Response to Assessors' Comments, resubmitted Turnitin report, and supervisor approval.",
                "error",
            )
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        if not module_completion_allows_hdc_submission(project):
            flash(
                "Verify module completion before sending results to HDC. If modules are incomplete, set the status to Awaiting Response from the Marks Committee first.",
                "error",
            )
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        missing_hdc_docs = required_hdc_results_documents_missing(project)
        if missing_hdc_docs:
            flash(
                "Results cannot be sent to HDC until these documents are on file: "
                + ", ".join(document_label(doc_key) for doc_key in missing_hdc_docs),
                "error",
            )
            return redirect(url_for("mba.admin_dashboard", panel="projects"))
        project.project_status = ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value
        project.results_submitted_to_hdc_at = datetime.utcnow()
        project.results_hdc_decision = "pending"
        project.results_hdc_reviewed_at = None
        project.results_hdc_approved_mark = None
        project.results_hdc_approved_classification = None
        project.results_released_to_supervisor_at = None
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: {'resubmitted' if resubmitting_results else 'submitted'} assessment results and supporting documents to HDC.",
        )
        message = (
            "Assessment results and supporting documents have been resent to HDC."
            if resubmitting_results
            else "Assessment results and supporting documents have been sent to HDC."
        )
    else:
        message = "Assignment updated."

    if comment:
        project.comments = append_comment(project.comments, f"{current_user.email}: {comment}")
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("mba.admin_dashboard"))


@mba_bp.route("/projects/<int:project_id>/hdc-action", methods=["POST"])
@login_required
def hdc_project_action(project_id):
    if not require_mba_role(MbaRole.HDC.value):
        return redirect(role_landing_url())

    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)

    action = request.form.get("action")
    comment = (request.form.get("comment") or "").strip()
    notify_admin_nomination_decision = False
    notify_admin_results_decision = False
    single_assessor_nomination_actions = {
        "approve_assessor_1_nomination": ("assessor_1", HDC_ASSESSOR_APPROVED),
        "decline_assessor_1_nomination": ("assessor_1", HDC_ASSESSOR_DECLINED),
        "approve_assessor_2_nomination": ("assessor_2", HDC_ASSESSOR_APPROVED),
        "decline_assessor_2_nomination": ("assessor_2", HDC_ASSESSOR_DECLINED),
    }
    if action == "verify":
        if project.project_status == ProjectStatus.JBS5_SUBMITTED_TO_HDC.value:
            flash("Open JBS5 and complete the HDC signature section before approving it.", "info")
            return redirect(url_for("mba.hdc_sign_project_form", project_id=project.id, form_type="jbs5"))
        elif project.project_status == ProjectStatus.ADMIN_APPROVED.value:
            flash("Open JBS10 and complete the HDC signature section before approving all nominations.", "info")
            return redirect(url_for("mba.hdc_sign_project_form", project_id=project.id, form_type="jbs10"))
        elif project.project_status == ProjectStatus.JBS5_HDC_DECLINED.value:
            flash("JBS5 has been returned. Wait for the student, supervisor, and MBA Admin to resubmit it before another HDC decision.", "info")
            return redirect(url_for("mba.hdc_dashboard"))
        elif project.project_status == ProjectStatus.HDC_DECLINED.value:
            flash("Assessor nominations have been returned. Wait for MBA Admin to replace and resubmit the nomination set before another HDC decision.", "info")
            return redirect(url_for("mba.hdc_dashboard"))
        else:
            flash("This Capstone Project is not waiting for HDC approval.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
    elif action == "decline":
        if project.project_status == ProjectStatus.JBS5_SUBMITTED_TO_HDC.value:
            flash("Open JBS5 before returning it with HDC feedback.", "info")
            return redirect(url_for("mba.hdc_sign_project_form", project_id=project.id, form_type="jbs5"))
        elif project.project_status == ProjectStatus.ADMIN_APPROVED.value:
            flash("Open JBS10 before returning all nominations with HDC feedback.", "info")
            return redirect(url_for("mba.hdc_sign_project_form", project_id=project.id, form_type="jbs10"))
        elif project.project_status == ProjectStatus.JBS5_HDC_DECLINED.value:
            flash("JBS5 has already been returned. Wait for a corrected resubmission before another HDC decision.", "info")
            return redirect(url_for("mba.hdc_dashboard"))
        elif project.project_status == ProjectStatus.HDC_DECLINED.value:
            flash("Assessor nominations have already been returned. Wait for MBA Admin to resubmit them before another HDC decision.", "info")
            return redirect(url_for("mba.hdc_dashboard"))
        else:
            flash("This Capstone Project is not waiting for HDC approval.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
    elif action in single_assessor_nomination_actions:
        if project.project_status != ProjectStatus.ADMIN_APPROVED.value:
            flash("This Capstone Project is not waiting for HDC nomination review.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
        if not project.jbs5_hdc_approved_at:
            flash("HDC must approve JBS5 before assessor nominations can be reviewed.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
        slot, decision = single_assessor_nomination_actions[action]
        if not getattr(project, f"{slot}_id", None):
            flash(f"{INVITATION_SLOTS[slot]['label']} is not assigned.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
        set_assessor_hdc_decision(project, slot, decision)
        review_status = sync_hdc_assessor_nomination_status(project)
        notify_admin_nomination_decision = True
        assessor_label = INVITATION_SLOTS[slot]["label"]
        decision_label = assessor_hdc_decision_label(decision).lower()
        if review_status == "approved":
            message = "Both assessor nominations approved by HDC."
        elif review_status == "signature_pending":
            message = "Both assessor nominations approved. Open JBS10 and complete the HDC signature section to finish approval."
        elif review_status == "signature_pending_declined":
            message = (
                f"{assessor_label} nomination {decision_label} by HDC. "
                "Both assessor nominations have been reviewed. Open JBS10 and complete the HDC signature section."
            )
        elif review_status == "declined":
            message = f"{assessor_label} nomination {decision_label} by HDC. Rejected assessor nomination(s) have been returned to MBA Admin."
        else:
            message = f"{assessor_label} nomination {decision_label} by HDC. Review the remaining assessor nomination."
    elif action == "approve_results":
        if project.project_status != ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value or not all_assessment_results_received(project):
            flash("Assessment results are not ready for HDC approval.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
        missing_hdc_docs = required_hdc_results_documents_missing(project)
        if missing_hdc_docs:
            flash(
                "HDC cannot approve results until these documents are available for review: "
                + ", ".join(document_label(doc_key) for doc_key in missing_hdc_docs),
                "error",
            )
            return redirect(url_for("mba.hdc_dashboard"))
        project.project_status = ProjectStatus.RESULTS_APPROVED.value
        project.results_hdc_decision = "approved"
        project.results_hdc_reviewed_at = datetime.utcnow()
        grade_summary = _project_grade_summary(project)
        project.results_hdc_approved_mark = grade_summary.get("final")
        project.results_hdc_approved_classification = grade_summary.get("classification") or None
        project.results_released_to_supervisor_at = None
        notify_admin_results_decision = True
        message = "Assessment results approved by HDC."
    elif action == "decline_results":
        if project.project_status != ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value:
            flash("Assessment results are not ready for HDC review.", "error")
            return redirect(url_for("mba.hdc_dashboard"))
        project.project_status = ProjectStatus.RESULTS_DECLINED.value
        project.results_hdc_decision = "declined"
        project.results_hdc_reviewed_at = datetime.utcnow()
        project.results_hdc_approved_mark = None
        project.results_hdc_approved_classification = None
        project.results_released_to_supervisor_at = None
        notify_admin_results_decision = True
        message = "Assessment results declined by HDC."
    else:
        message = "HDC comment saved."

    if comment:
        if action in {"approve_results", "decline_results"}:
            project.results_hdc_comments = append_comment(project.results_hdc_comments, f"{current_user.email}: {comment}")
        else:
            project.hdc_comments = append_comment(project.hdc_comments, f"{current_user.email}: {comment}")
    if notify_admin_nomination_decision:
        decision_summary = hdc_assessor_nomination_decision_summary(project)
        project.comments = append_comment(
            project.comments,
            f"{current_user.email}: HDC assessor nomination decision recorded - {decision_summary}.",
        )
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
    if notify_admin_results_decision:
        admin_messages = hdc_results_admin_email_messages(project, current_user.email)
        if admin_messages:
            email_result = send_bulk_emails(admin_messages)
            project.comments = append_comment(
                project.comments,
                (
                    "System: HDC results decision admin alert email result: "
                    f"delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}"
                ),
            )
        else:
            project.comments = append_comment(
                project.comments,
                "System: HDC results decision recorded; no MBA Admin email recipients are configured.",
            )
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("mba.hdc_dashboard"))
