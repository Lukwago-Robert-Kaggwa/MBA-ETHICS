from datetime import datetime

from flask import abort, flash, redirect, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..mail import send_bulk_emails
from ..models import MbaForm, MbaProject, MbaProjectSupervisorInvitation, MbaRole, MbaUser, ProjectStatus
from .route_support import *  # noqa: F403
from .route_support import (
    _project_has_document,
    _store_project_document,
    _validate_uploaded_pdf,
)

STUDENT_EDITABLE_STATUSES = {
    ProjectStatus.CREATED.value,
    ProjectStatus.ADMIN_DECLINED.value,
    ProjectStatus.JBS5_HDC_DECLINED.value,
}


@mba_bp.route("/projects/new", methods=["POST"])
@login_required
def create_project():
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    if current_user.role != MbaRole.STUDENT.value:
        flash("Only students can create MBA Capstone Projects.", "error")
        return redirect(url_for("mba.dashboard"))
    raw_title = (request.form.get("project_title") or "").strip()
    title_error = project_title_validation_error(raw_title)
    if title_error:
        flash(title_error, "error")
        return redirect(request.referrer or url_for("mba.student_dashboard"))
    title = format_project_title(raw_title)
    description = (request.form.get("project_description") or "").strip()
    discipline = selected_discipline_from_form()
    jbs5_file = request.files.get("jbs5_file") or request.files.get("form_file")
    if not title or not description or not discipline:
        flash("Capstone Project title, description, and a valid discipline are required.", "error")
        return redirect(url_for("mba.dashboard"))
    project = MbaProject(student_id=current_user.id, project_title=title, project_description=description, discipline=discipline.name, discipline_id=discipline.id)
    jbs5_auto_submitted = False
    try:
        db.session.add(project)
        db.session.flush()
        if jbs5_file and jbs5_file.filename:
            file_error = _validate_uploaded_pdf(jbs5_file)
            if file_error:
                flash(f"JBS 5 upload error: {file_error}", "error")
                db.session.rollback()
                return redirect(url_for("mba.student_dashboard"))
            doc = _store_project_document(project, "jbs5", jbs5_file)
            db.session.flush()
            if not doc.id:
                raise RuntimeError("JBS5 metadata row was not persisted")
            jbs5_auto_submitted = submit_project_to_admin_from_jbs5(project)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("mba.student_dashboard"))
    except Exception:
        db.session.rollback()
        flash("Project could not be saved. Please try again.", "error")
        return redirect(url_for("mba.student_dashboard"))
    if raw_title and title != raw_title:
        flash(f"Capstone Project title was automatically formatted as: {title}", "info")
    if not jbs5_file or not jbs5_file.filename:
        flash("Capstone Project created. Fill in your JBS 5 Research Proposal to complete the submission.", "info")
        return redirect(url_for("mba.fill_project_form", project_id=project.id, form_type="jbs5"))
    if jbs5_auto_submitted:
        flash("Capstone Project created and submitted to MBA Admin with JBS 5.", "success")
    else:
        flash("Capstone Project created.", "success")
    return redirect(url_for("mba.student_dashboard"))


@mba_bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"], endpoint="edit_project")
@mba_bp.route("/projects/<int:project_id>/edit/", methods=["GET", "POST"], endpoint="edit_project")
@login_required
def edit_project(project_id):
    if not require_mba_role(MbaRole.STUDENT.value):
        return redirect(role_landing_url())
    if request.method == "GET":
        flash("Open the JBS 5 form from your Student Dashboard to edit before submission.", "error")
        return redirect(url_for("mba.student_dashboard"))
    project = db.session.get(MbaProject, project_id)
    if not project or project.student_id != current_user.id:
        abort(403)
    if project.project_status not in STUDENT_EDITABLE_STATUSES:
        flash("You cannot edit a Capstone Project that is currently under review.", "error")
        return redirect(url_for("mba.student_dashboard"))
    raw_title = (request.form.get("project_title") or "").strip()
    title_error = project_title_validation_error(raw_title)
    if title_error:
        flash(title_error, "error")
        return redirect(url_for("mba.student_dashboard"))
    title = format_project_title(raw_title)
    description = (request.form.get("project_description") or "").strip()
    discipline = selected_discipline_from_form()
    if not title or not description or not discipline:
        flash("Title, description, and a valid discipline are all required.", "error")
        return redirect(url_for("mba.student_dashboard"))
    project.project_title = title
    project.project_description = description
    project.discipline = discipline.name
    project.discipline_id = discipline.id
    project.comments = append_comment(project.comments, "Student updated Capstone Project details")
    db.session.commit()
    if raw_title and title != raw_title:
        flash(f"Capstone Project title was automatically formatted as: {title}", "info")
    flash("Capstone Project updated. You can now resubmit when ready.", "success")
    return redirect(url_for("mba.student_dashboard"))


@mba_bp.route("/projects/<int:project_id>/submit-title", methods=["POST"], endpoint="submit_project_title")
@login_required
def submit_project_title(project_id):
    if not require_mba_role(MbaRole.STUDENT.value):
        return redirect(role_landing_url())
    project = db.session.get(MbaProject, project_id)
    if not project or project.student_id != current_user.id:
        abort(403)
    if project.project_status not in STUDENT_EDITABLE_STATUSES:
        flash("This project is already in review.", "error")
        return redirect(url_for("mba.student_dashboard"))
    if not _project_has_document(project.id, "jbs5"):
        flash("Complete your JBS 5 form before submitting this Capstone Project.", "error")
        return redirect(url_for("mba.student_dashboard"))
    try:
        submitted = submit_project_to_admin_from_jbs5(project)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("mba.student_dashboard"))
    if submitted:
        flash("Capstone Project submitted to MBA Admin from JBS 5.", "success")
    else:
        flash("This Capstone Project is already in review.", "info")
    return redirect(url_for("mba.student_dashboard"))


@mba_bp.route("/projects/<int:project_id>/select-supervisor-project", methods=["POST"])
@login_required
def select_supervisor_project(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())
    if not current_user.is_supervisor_role():
        flash("Only supervisors can choose an available Capstone Project title.", "error")
        return redirect(url_for("mba.scholar_dashboard"))

    project = MbaProject.query.filter_by(id=project_id).with_for_update().first()
    if not project:
        abort(404)
    if not project_available_for_supervisor_pool(project):
        flash("This Capstone Project title is no longer available for supervisor selection.", "error")
        return redirect(url_for("mba.scholar_dashboard", _anchor="available-supervisor-projects"))

    selected_at = datetime.utcnow()
    project.supervisor_invitations.clear()
    invitation = MbaProjectSupervisorInvitation(
        project=project,
        supervisor=current_user,
        status=INVITATION_PENDING,
        invited_at=selected_at,
    )
    project.supervisor_invitations.append(invitation)
    project.primary_supervisor_id = current_user.id
    project.primary_supervisor_invitation_status = INVITATION_PENDING
    project.supervisor_confirmed = False
    project.supervisor_accepted_at = None
    project.assignment_confirmed = False
    set_invitations_sent(project)
    mark_supervisor_invitations_sent(project, sent_at=selected_at, invitations=[invitation])
    project.comments = append_comment(
        project.comments,
        f"{current_user.email}: selected this released JBS5 project title for supervision.",
    )
    email_result = send_bulk_emails(
        [
            {
                "recipient": admin_email,
                "subject": f"Supervisor Selected Capstone Project: {project.project_title}",
                "body": (
                    f"{current_user.email} selected the released JBS5 project title "
                    f"'{project.project_title}' for supervision.\n\n"
                    "The supervisor must now complete the Supervisor Agreement before reviewing and signing JBS5."
                ),
            }
            for admin_email in mba_admin_notification_emails()
        ]
    )
    project.comments = append_comment(
        project.comments,
        f"Supervisor self-selection admin email result: delivered={len(email_result['delivered'])}, failed={len(email_result['failed'])}",
    )
    db.session.commit()
    flash("Project selected. Complete the Supervisor Agreement to confirm supervision.", "success")
    return redirect(url_for("mba.supervisor_fill_form", project_id=project.id))


# Updated invitation response for multi-supervisor invitations (first-accept logic)
@mba_bp.route("/projects/<int:project_id>/invitation-response", methods=["POST"])
@login_required
def invitation_response(project_id):
    if not require_mba_role(MbaRole.SCHOLAR.value, MbaRole.EXAMINER.value):
        return redirect(role_landing_url())
    project = db.session.get(MbaProject, project_id)
    if not project:
        abort(404)
    supervisor_invitation_id = request.form.get("supervisor_invitation_id")
    decision = (request.form.get("decision") or "").strip().lower()
    if supervisor_invitation_id:
        invitation = next((inv for inv in project.supervisor_invitations if str(inv.id) == str(supervisor_invitation_id)), None)
        if not invitation or invitation.supervisor_id != current_user.id:
            abort(403)
        if invitation.status != "pending":
            flash("This invitation has already been responded to.", "error")
            return redirect(role_landing_url())
        if decision not in {INVITATION_ACCEPTED, INVITATION_DECLINED}:
            flash("Invalid invitation response.", "error")
            return redirect(role_landing_url())
        if decision == INVITATION_ACCEPTED:
            uploaded_file = request.files.get("form_file")
            file_error = _validate_uploaded_pdf(uploaded_file)
            if file_error:
                flash("You must upload a signed Supervisor Agreement PDF to accept the invitation.", "error")
                return redirect(url_for("mba.scholar_dashboard"))

            _store_project_document(project, "supervisor_agreement", uploaded_file)
            db.session.flush()
            supervisor_name = f"{current_user.first_name or ''} {current_user.last_name or ''}".strip() or current_user.email
            signature_date = datetime.utcnow().strftime("%Y-%m-%d")
            supervisor_agreement_form = MbaForm.query.filter_by(project_id=project.id, form_type="supervisor_agreement").first()
            if supervisor_agreement_form:
                agreement_payload = dict(supervisor_agreement_form.payload or {})
                supervisor_agreement_form.payload = {
                    **agreement_payload,
                    "supervisor_full_name": agreement_payload.get("supervisor_full_name") or supervisor_name,
                    "supervisor_signature": supervisor_name,
                    "supervisor_signature_date": signature_date,
                    "supervisor_agreement_declaration": "1",
                }
                supervisor_agreement_form.submitted_at = datetime.utcnow()
            else:
                supervisor_agreement_form = MbaForm(
                    project_id=project.id,
                    form_type="supervisor_agreement",
                    payload={
                        "student_name": f"{project.student.first_name or ''} {project.student.last_name or ''}".strip() if project.student else "",
                        "student_number": project.student.student_profile.student_number if project.student and project.student.student_profile else "",
                        "research_title": project.project_title,
                        "supervisor_full_name": supervisor_name,
                        "supervisor_signature": supervisor_name,
                        "supervisor_signature_date": signature_date,
                        "supervisor_agreement_declaration": "1",
                    },
                    submitted_at=datetime.utcnow(),
                )
                db.session.add(supervisor_agreement_form)
            supervisor_agreement_form.supervisor_signed = True
            project.primary_supervisor_id = current_user.id
            project.primary_supervisor_invitation_status = INVITATION_ACCEPTED
            project.supervisor_confirmed = True
            project.supervisor_accepted_at = datetime.utcnow()
            project.project_status = ProjectStatus.SUPERVISOR_ACCEPTED.value
            invitation.status = "accepted"
            invitation.responded_at = datetime.utcnow()
            for other in project.supervisor_invitations:
                if other.id != invitation.id and other.status == "pending":
                    other.status = "expired"
                    other.responded_at = datetime.utcnow()
            project.comments = append_comment(project.comments, f"Supervisor invitation accepted by {current_user.email}")
            from ..mail import send_email

            admin_users = MbaUser.query.filter(MbaUser.role.in_([MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value])).all()
            admin_emails = [admin.email for admin in admin_users if admin.email]
            for admin_email in admin_emails:
                try:
                    send_email(admin_email, f"Supervisor Accepted for Capstone Project: {project.project_title}", f"Supervisor {current_user.first_name} ({current_user.email}) has accepted the invitation for Capstone Project '{project.project_title}'.")
                except Exception:
                    pass
            if project.student and project.student.email:
                try:
                    send_email(
                        project.student.email,
                        "Supervisor Accepted: JBS5 Under Review",
                        (
                            f"Your supervisor has accepted the invitation for your Capstone Project "
                            f"'{project.project_title}'. They will now review JBS5 and either request "
                            "title changes or sign it."
                        ),
                    )
                except Exception:
                    pass
            flash("Invitation accepted. You can now review JBS5.", "success")
        else:
            invitation.status = "declined"
            invitation.responded_at = datetime.utcnow()
            remaining_pending = any(other.status == INVITATION_PENDING for other in project.supervisor_invitations)
            project.primary_supervisor_invitation_status = (INVITATION_PENDING if remaining_pending else INVITATION_DECLINED)
            if not remaining_pending and project.primary_supervisor_id == current_user.id:
                project.primary_supervisor_id = None
                project.supervisor_confirmed = False
                project.assignment_confirmed = False
            project.comments = append_comment(project.comments, f"Supervisor invitation declined by {current_user.email}")
            flash("Invitation declined.", "success")
        db.session.commit()
        return redirect(url_for("mba.scholar_dashboard"))
    slot = (request.form.get("slot") or "").strip()
    meta = INVITATION_SLOTS.get(slot)
    if not meta or decision not in {INVITATION_ACCEPTED, INVITATION_DECLINED}:
        flash("Invalid invitation response.", "error")
        return redirect(role_landing_url())
    if getattr(project, meta["id_field"]) != current_user.id:
        abort(403)
    if not project_has_sent_invitations(project):
        flash("Invitation has not been sent yet.", "error")
        return redirect(role_landing_url())
    current_status = getattr(project, meta["status_field"]) or INVITATION_PENDING
    if current_status != INVITATION_PENDING:
        flash("This invitation has already been responded to.", "error")
        return redirect(role_landing_url())
    if decision == INVITATION_ACCEPTED and slot in ALL_ASSESSOR_SLOTS:
        if not assessor_acceptance_pack_complete(project, slot):
            flash(
                "Complete the assessor acceptance pack, external examiner nomination form, and CV before accepting the assessor invitation.",
                "error",
            )
            return redirect(url_for("mba.assessor_acceptance_form", project_id=project.id, slot=slot))
    setattr(project, meta["status_field"], decision)
    project.comments = append_comment(project.comments, f"{meta['label']} invitation {decision} by {current_user.email}")
    if (
        decision == INVITATION_ACCEPTED
        and slot in ALL_ASSESSOR_SLOTS
        and assessor_can_view_student_dissertation(project)
    ):
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
                    f"Capstone Project release email after assessor acceptance: delivered={delivered_count}; failed={failed_count}",
                )
    db.session.commit()
    flash(f"Invitation {decision}.", "success")
    return redirect(role_landing_url())
