from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError

from ..extensions import db
from ..models import MbaForm, MbaProject, MbaRole, MbaScholarProfile, MbaStudentProfile, MbaUser, MbaProjectSupervisorInvitation, ProjectStatus
from .route_support import *  # noqa: F403
from .grading import project_grade_summary


@mba_bp.route("/")
@login_required
def dashboard():
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    return redirect(role_landing_url())


def _missing_required_profile_fields(fields):
    return [label for label, value in fields if not (value or "").strip()]


def _student_profile_missing_fields(profile, student_number):
    return _missing_required_profile_fields(
        [
            ("first name", profile.name),
            ("surname", profile.surname),
            ("contact number", profile.contact),
            ("student number", student_number),
            ("module", profile.module),
            ("block", profile.block_id),
            ("degree", profile.degree),
        ]
    )


def _staff_profile_missing_fields(profile):
    return _missing_required_profile_fields(
        [
            ("first name", profile.name),
            ("surname", profile.surname),
            ("contact number", profile.contact),
            ("department", profile.department),
            ("position", profile.position),
            ("highest qualification", profile.qualification),
            ("affiliation", profile.affiliation),
            ("areas of expertise", profile.skills),
            ("research themes", profile.research_themes),
            ("research interests", profile.research_interests),
            ("research disciplines", profile.research_disciplines),
        ]
    )


def _change_current_user_password():
    current_password = request.form.get("current_password") or ""
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if current_user.password_hash and not current_user.check_password(current_password):
        flash("Current password is incorrect.", "error")
        return False

    if len(new_password) < 8:
        flash("New password must be at least 8 characters.", "error")
        return False

    if new_password != confirm_password:
        flash("New passwords do not match.", "error")
        return False

    if current_user.password_hash and current_user.check_password(new_password):
        flash("New password must be different from the current password.", "error")
        return False

    current_user.set_password(new_password)
    db.session.commit()
    flash("Your password has been updated.", "success")
    return True


@mba_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if not require_mba_user():
        return redirect(url_for("auth.login"))
    is_student = current_user.role == MbaRole.STUDENT.value
    if request.method == "POST" and request.form.get("action") == "change_password":
        _change_current_user_password()
        return redirect(url_for("mba.profile"))
    if is_student:
        profile = current_user.student_profile
        if not profile:
            profile = MbaStudentProfile(user_id=current_user.id)
        if request.method == "POST":
            submitted_student_number = (request.form.get("student_number") or "").strip()
            profile.name = (request.form.get("name") or "").strip() or None
            profile.surname = (request.form.get("surname") or "").strip() or None
            profile.title = (request.form.get("title") or "").strip() or None
            profile.contact = (request.form.get("contact") or "").strip() or None
            profile.secondary_email = (request.form.get("secondary_email") or "").strip() or None
            profile.module = (request.form.get("module") or "").strip() or None
            profile.block_id = (request.form.get("block_id") or "").strip() or None
            profile.degree = ((request.form.get("degree") or "MBA").strip() or "MBA")
            profile.address = (request.form.get("address") or "").strip() or None
            missing_fields = _student_profile_missing_fields(profile, submitted_student_number)
            if missing_fields:
                flash(f"Complete required profile fields: {', '.join(missing_fields)}.", "error")
            elif (
                MbaStudentProfile.query.filter(
                    MbaStudentProfile.student_number == submitted_student_number,
                    MbaStudentProfile.user_id != current_user.id,
                ).first()
                is not None
            ):
                flash("That student number is already linked to another student profile.", "error")
            else:
                profile.student_number = submitted_student_number
                current_user.first_name = profile.name
                current_user.last_name = profile.surname
                current_user.has_profile = True
                try:
                    db.session.add(profile)
                    db.session.commit()
                except IntegrityError:
                    db.session.rollback()
                    flash("That student number is already linked to another student profile.", "error")
                else:
                    flash("Your student profile has been saved.", "success")
                    return redirect(url_for("mba.profile"))
    else:
        profile = current_user.scholar_profile
        if not profile:
            profile = MbaScholarProfile(user_id=current_user.id)
        if request.method == "POST":
            profile.name = (request.form.get("name") or "").strip() or None
            profile.surname = (request.form.get("surname") or "").strip() or None
            profile.title = (request.form.get("title") or "").strip() or None
            profile.contact = (request.form.get("contact") or "").strip() or None
            profile.department = (request.form.get("department") or "").strip() or None
            profile.position = (request.form.get("position") or "").strip() or None
            profile.qualification = (request.form.get("qualification") or "").strip() or None
            profile.affiliation = (request.form.get("affiliation") or "").strip() or None
            profile.skills = (request.form.get("skills") or "").strip() or None
            profile.research_themes = (request.form.get("research_themes") or "").strip() or None
            profile.research_interests = (request.form.get("research_interests") or "").strip() or None
            profile.research_disciplines = (request.form.get("research_disciplines") or "").strip() or None
            profile.selected_publications = (request.form.get("selected_publications") or "").strip() or None
            profile.scholarly_profile_links = (request.form.get("scholarly_profile_links") or "").strip() or None
            profile.address = (request.form.get("address") or "").strip() or None
            profile.students = parse_non_negative_int(request.form.get("students"), profile.students or 0)
            profile.academic_experience = parse_non_negative_int(request.form.get("academic_experience"), profile.academic_experience or 0)
            profile.students_supervised_total = parse_non_negative_int(
                request.form.get("students_supervised_total"),
                profile.students_supervised_total or 0,
            )
            profile.students_assessed_total = parse_non_negative_int(
                request.form.get("students_assessed_total"),
                profile.students_assessed_total or 0,
            )
            profile.publication_count = parse_non_negative_int(
                request.form.get("publication_count"),
                profile.publication_count or 0,
            )
            profile.approved_before = request.form.get("approved_before") == "on"
            profile.international_assessor = request.form.get("international_assessor") == "on"
            missing_fields = _staff_profile_missing_fields(profile)
            if missing_fields:
                flash(f"Complete required profile fields: {', '.join(missing_fields)}.", "error")
            else:
                current_user.first_name = profile.name
                current_user.last_name = profile.surname
                current_user.has_profile = True
                db.session.add(profile)
                db.session.commit()
                flash("Your staff profile has been saved.", "success")
                return redirect(url_for("mba.profile"))
    return render_template("mba/profile.html", profile=profile, is_student=is_student, role_label=profile_role_label(current_user))


@mba_bp.route("/student-dashboard")
@login_required
def student_dashboard():
    if not require_mba_role(MbaRole.STUDENT.value):
        return redirect(role_landing_url())
    projects = MbaProject.query.filter_by(student_id=current_user.id).order_by(MbaProject.created_at.desc()).all()
    disciplines = disciplines_query().all()
    pending_correction_projects = [
        project
        for project in projects
        if project_has_active_corrections(project)
        and corrections_released_to_student(project)
        and project_corrections_status(project) in {"awaiting_student", "rejected_by_supervisor"}
    ]
    return render_template(
        "mba/student_dashboard.html",
        projects=projects,
        disciplines=disciplines,
        kpis=mba_kpis(),
        document_label=document_label,
        uploaded_doc_for=uploaded_doc_for,
        student_has_uploaded_doc=student_has_uploaded_doc,
        project_activity_entries=project_activity_entries,
        project_status_label=public_project_status_label,
        project_status_badge_class=public_project_status_badge_class,
        project_has_active_corrections=project_has_active_corrections,
        project_corrections_status=project_corrections_status,
        corrections_status_label=corrections_status_label,
        corrections_released_to_student=corrections_released_to_student,
        pending_correction_projects=pending_correction_projects,
    )


@mba_bp.route("/student-corrections")
@login_required
def student_corrections():
    if not require_mba_role(MbaRole.STUDENT.value):
        return redirect(role_landing_url())
    projects = (
        MbaProject.query.options(joinedload(MbaProject.student).joinedload(MbaUser.student_profile))
        .filter_by(student_id=current_user.id)
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
    correction_projects = [
        project
        for project in projects
        if project_has_active_corrections(project, forms_by_project=forms_by_project)
        and corrections_released_to_student(project)
    ]
    return render_template(
        "mba/student_corrections.html",
        projects=correction_projects,
        forms_by_project=forms_by_project,
        project_correction_requests=project_correction_requests,
        project_corrections_status=project_corrections_status,
        corrections_status_label=corrections_status_label,
        uploaded_doc_for=uploaded_doc_for,
        student_submitted_corrections_response=student_submitted_corrections_response,
        student_submitted_corrections_pack=student_submitted_corrections_pack,
        supervisor_approved_corrections=supervisor_approved_corrections,
        supervisor_rejected_corrections=supervisor_rejected_corrections,
        correction_request_reference_time=correction_request_reference_time,
        document_label=document_label,
        project_status_label=public_project_status_label,
        project_status_badge_class=public_project_status_badge_class,
        kpis=mba_kpis(),
    )


@mba_bp.route("/scholar-dashboard")
@login_required
def scholar_dashboard():
    if not require_mba_role(MbaRole.SCHOLAR.value):
        return redirect(role_landing_url())
    supervisor_status = (request.args.get("supervisor_status") or "all").strip().lower()
    assessor_status = (request.args.get("assessor_status") or "all").strip().lower()
    allowed_filter_statuses = {"all", "accepted", "expired"}
    if supervisor_status not in allowed_filter_statuses:
        supervisor_status = "all"
    if assessor_status not in allowed_filter_statuses:
        assessor_status = "all"
    supervisor_student_number = (request.args.get("supervisor_student_number") or "").strip()
    assessor_student_number = (request.args.get("assessor_student_number") or "").strip()
    supervisor_page = parse_positive_int(request.args.get("supervisor_page"), 1)
    supervisor_per_page = parse_page_size(request.args.get("supervisor_per_page"), 5)
    assessor_page = parse_positive_int(request.args.get("assessor_page"), 1)
    assessor_per_page = parse_page_size(request.args.get("assessor_per_page"), 5)
    available_page = parse_positive_int(request.args.get("available_page"), 1)
    available_per_page = parse_page_size(request.args.get("available_per_page"), 10)

    def project_student_number_matches(project, search_value):
        if not search_value:
            return True
        student = project.student
        profile = student.student_profile if student else None
        student_number = profile.student_number if profile else ""
        search_value = search_value.lower()
        return search_value in (student_number or "").lower()

    def supervisor_project_state(project):
        invitation = next(
            (
                item
                for item in project.supervisor_invitations
                if item.supervisor_id == current_user.id
            ),
            None,
        )
        if invitation:
            if not supervisor_invitation_has_been_sent(project, invitation):
                return "all"
            if invitation.status == "expired":
                return "expired"
            if invitation.status == INVITATION_ACCEPTED:
                return "accepted"
            return invitation.status
        if (
            project.primary_supervisor_id == current_user.id
            and effective_supervisor_invitation_status(project) == INVITATION_ACCEPTED
            and project.supervisor_accepted_at is not None
        ):
            return "accepted"
        return "all"

    def assessor_project_states(project):
        states = []
        for slot in ALL_ASSESSOR_SLOTS:
            if getattr(project, f"{slot}_id") == current_user.id:
                states.append(getattr(project, f"{slot}_invitation_status") or INVITATION_PENDING)
        return states

    def status_counts(projects, state_func):
        return {
            "all": len(projects),
            "accepted": sum(1 for project in projects if "accepted" in state_func(project)),
            "expired": sum(1 for project in projects if "expired" in state_func(project)),
        }

    supervisor_invitation_rows = (
        MbaProjectSupervisorInvitation.query.options(joinedload(MbaProjectSupervisorInvitation.project))
        .filter(
            MbaProjectSupervisorInvitation.supervisor_id == current_user.id,
            MbaProjectSupervisorInvitation.status.in_([INVITATION_PENDING, INVITATION_ACCEPTED, "expired"]),
        )
        .all()
    )
    invited_project_ids = [
        inv.project_id
        for inv in supervisor_invitation_rows
        if supervisor_invitation_has_been_sent(inv.project, inv)
    ]
    supervised_projects_query = (
        MbaProject.query.options(
            joinedload(MbaProject.supervisor_invitations),
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
        )
        .filter(((MbaProject.primary_supervisor_id == current_user.id) | (MbaProject.id.in_(invited_project_ids))),)
        .order_by(MbaProject.updated_at.desc())
    )
    if supervisor_student_number:
        supervised_projects_query = supervised_projects_query.join(
            MbaStudentProfile,
            MbaStudentProfile.user_id == MbaProject.student_id,
        ).filter(MbaStudentProfile.student_number.ilike(f"%{supervisor_student_number}%"))
    supervised_projects_all = supervised_projects_query.all()
    examiner_projects_query = (
        MbaProject.query.options(joinedload(MbaProject.student).joinedload(MbaUser.student_profile))
        .filter(
            (MbaProject.assessor_1_id == current_user.id)
            | (MbaProject.assessor_2_id == current_user.id)
            | (MbaProject.assessor_3_id == current_user.id)
        )
        .order_by(MbaProject.updated_at.desc())
    )
    if assessor_student_number:
        examiner_projects_query = examiner_projects_query.join(
            MbaStudentProfile,
            MbaStudentProfile.user_id == MbaProject.student_id,
        ).filter(MbaStudentProfile.student_number.ilike(f"%{assessor_student_number}%"))
    examiner_projects_all = examiner_projects_query.all()

    supervised_projects_by_student = [
        project for project in supervised_projects_all if project_student_number_matches(project, supervisor_student_number)
    ]
    examiner_projects_by_student = [
        project for project in examiner_projects_all if project_student_number_matches(project, assessor_student_number)
    ]
    if current_user.is_supervisor_role():
        available_supervisor_projects_all = (
            MbaProject.query.options(
                joinedload(MbaProject.documents),
                joinedload(MbaProject.supervisor_invitations),
                joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            )
            .filter(
                MbaProject.project_status == ProjectStatus.ADMIN_SUBMITTED.value,
                MbaProject.supervisor_pool_released_at.isnot(None),
            )
            .order_by(MbaProject.supervisor_pool_released_at.desc(), MbaProject.updated_at.desc())
            .all()
        )
        available_supervisor_projects_all = [
            project for project in available_supervisor_projects_all if project_available_for_supervisor_pool(project)
        ]
    else:
        available_supervisor_projects_all = []
    supervisor_project_states = {
        project.id: supervisor_project_state(project) for project in supervised_projects_by_student
    }
    supervisor_filter_counts = status_counts(
        supervised_projects_by_student, lambda project: [supervisor_project_states.get(project.id, "all")]
    )
    assessor_filter_counts = status_counts(examiner_projects_by_student, assessor_project_states)

    supervised_projects = [
        project
        for project in supervised_projects_by_student
        if supervisor_status == "all" or supervisor_project_states.get(project.id, "all") == supervisor_status
    ]
    examiner_projects = [
        project
        for project in examiner_projects_by_student
        if assessor_status == "all" or assessor_status in assessor_project_states(project)
    ]
    supervisor_pagination_args = request_query_args({"supervisor_page", "supervisor_per_page"})
    assessor_pagination_args = request_query_args({"assessor_page", "assessor_per_page"})
    supervised_projects, supervisor_pagination = paginate_list(
        supervised_projects,
        supervisor_page,
        supervisor_per_page,
        "mba.scholar_dashboard",
        page_param="supervisor_page",
        per_page_param="supervisor_per_page",
        base_args=supervisor_pagination_args,
        anchor="supervisor-projects",
    )
    examiner_projects, assessor_pagination = paginate_list(
        examiner_projects,
        assessor_page,
        assessor_per_page,
        "mba.scholar_dashboard",
        page_param="assessor_page",
        per_page_param="assessor_per_page",
        base_args=assessor_pagination_args,
        anchor="assessor-projects",
    )
    available_pagination_args = request_query_args({"available_page", "available_per_page"})
    available_supervisor_projects, available_pagination = paginate_list(
        available_supervisor_projects_all,
        available_page,
        available_per_page,
        "mba.scholar_dashboard",
        page_param="available_page",
        per_page_param="available_per_page",
        base_args=available_pagination_args,
        anchor="available-supervisor-projects",
    )

    return render_template(
        "mba/scholar_dashboard.html",
        supervised_projects=supervised_projects,
        examiner_projects=examiner_projects,
        available_supervisor_projects=available_supervisor_projects,
        invitation_status_for_user=invitation_status_for_user,
        assessor_can_view_project_documents=assessor_can_view_project_documents,
        assessor_can_view_student_dissertation=assessor_can_view_student_dissertation,
        assessment_doc_type=assessment_doc_type,
        assessor_profile_doc_type=assessor_profile_doc_type,
        assessor_cv_doc_type=assessor_cv_doc_type,
        assessor_highest_qualification_doc_type=assessor_highest_qualification_doc_type,
        assessor_temp_appointment_doc_type=assessor_temp_appointment_doc_type,
        assessor_temp_claim_doc_type=assessor_temp_claim_doc_type,
        uploaded_doc_for=uploaded_doc_for,
        document_label=document_label,
        kpis=mba_kpis(),
        supervisor_status=supervisor_status,
        assessor_status=assessor_status,
        supervisor_student_number=supervisor_student_number,
        assessor_student_number=assessor_student_number,
        supervisor_project_states=supervisor_project_states,
        supervisor_filter_counts=supervisor_filter_counts,
        assessor_filter_counts=assessor_filter_counts,
        supervisor_pagination=supervisor_pagination,
        assessor_pagination=assessor_pagination,
        available_pagination=available_pagination,
        project_status_label=public_project_status_label,
        project_status_badge_class=public_project_status_badge_class,
        project_has_active_corrections=project_has_active_corrections,
        project_corrections_status=project_corrections_status,
        corrections_status_label=corrections_status_label,
        assessment_results_forwarded_to_supervisor=assessment_results_forwarded_to_supervisor,
        results_released_to_supervisor=results_released_to_supervisor,
        assessment_result_pack_complete=assessment_result_pack_complete,
        additional_assessment_required=additional_assessment_required,
        additional_assessment_stage=additional_assessment_stage,
        additional_assessment_status_label=additional_assessment_status_label,
    )


@mba_bp.route("/scholar-corrections")
@login_required
def scholar_corrections():
    if not require_mba_role(MbaRole.SCHOLAR.value):
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
        )
        .filter(MbaProject.primary_supervisor_id == current_user.id)
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
        if supervisor_can_manage_corrections(project, current_user)
        and project_has_active_corrections(project, forms_by_project=forms_by_project)
        and assessment_results_forwarded_to_supervisor(project)
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
        "mba/scholar_corrections.html",
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
        project_status_label=public_project_status_label,
        project_status_badge_class=public_project_status_badge_class,
        assessment_results_forwarded_to_supervisor=assessment_results_forwarded_to_supervisor,
        corrections_released_to_student=corrections_released_to_student,
        kpis=mba_kpis(),
    )


@mba_bp.route("/examiner-dashboard")
@login_required
def examiner_dashboard():
    if not require_mba_role(MbaRole.EXAMINER.value):
        return redirect(role_landing_url())
    examiner_page = parse_positive_int(request.args.get("examiner_page"), 1)
    examiner_per_page = parse_page_size(request.args.get("examiner_per_page"), 5)
    query = (
        MbaProject.query.options(
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            joinedload(MbaProject.documents),
        ).filter(
            (
                (MbaProject.assessor_1_id == current_user.id)
                | (MbaProject.assessor_2_id == current_user.id)
                | (MbaProject.assessor_3_id == current_user.id)
            ),
            MbaProject.invitations_sent_at.isnot(None),
        )
        .order_by(MbaProject.updated_at.desc())
    )
    examiner_pagination_args = request_query_args({"examiner_page", "examiner_per_page"})
    projects, examiner_pagination = paginate_query(
        query,
        examiner_page,
        examiner_per_page,
        "mba.examiner_dashboard",
        page_param="examiner_page",
        per_page_param="examiner_per_page",
        base_args=examiner_pagination_args,
        anchor="assigned-projects",
    )
    return render_template(
        "mba/examiner_dashboard.html",
        projects=projects,
        examiner_pagination=examiner_pagination,
        invitation_status_for_user=invitation_status_for_user,
        assessor_can_view_project_documents=assessor_can_view_project_documents,
        assessor_can_view_student_dissertation=assessor_can_view_student_dissertation,
        assessment_doc_type=assessment_doc_type,
        assessor_profile_doc_type=assessor_profile_doc_type,
        assessor_cv_doc_type=assessor_cv_doc_type,
        assessor_highest_qualification_doc_type=assessor_highest_qualification_doc_type,
        assessor_temp_appointment_doc_type=assessor_temp_appointment_doc_type,
        assessor_temp_claim_doc_type=assessor_temp_claim_doc_type,
        uploaded_doc_for=uploaded_doc_for,
        document_label=document_label,
        kpis=mba_kpis(),
        project_status_label=public_project_status_label,
        project_status_badge_class=public_project_status_badge_class,
        assessment_result_pack_complete=assessment_result_pack_complete,
        additional_assessment_required=additional_assessment_required,
        additional_assessment_stage=additional_assessment_stage,
        additional_assessment_status_label=additional_assessment_status_label,
    )


@mba_bp.route("/hdc-dashboard")
@login_required
def hdc_dashboard():
    if not require_mba_role(MbaRole.HDC.value):
        return redirect(role_landing_url())
    approval_set = (request.args.get("approval_set") or "overview").strip().lower()
    allowed_approval_sets = {"overview", "jbs5", "nominations", "results"}
    if approval_set not in allowed_approval_sets:
        approval_set = "overview"
    review_status = (
        request.args.get("review_status")
        or request.args.get("nomination_status")
        or "pending_review"
    ).strip().lower()
    allowed_review_statuses = {"all", "pending_review", "approved", "rejected"}
    if review_status not in allowed_review_statuses:
        review_status = "pending_review"
    hdc_student_number = (request.args.get("student_number") or "").strip()
    hdc_page = parse_positive_int(request.args.get("hdc_page"), 1)
    hdc_per_page = parse_page_size(request.args.get("hdc_per_page"), 5)

    approval_status_groups = {
        "overview": {
            "all": [
                ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
                ProjectStatus.JBS5_HDC_APPROVED.value,
                ProjectStatus.JBS5_HDC_DECLINED.value,
                ProjectStatus.ADMIN_APPROVED.value,
                ProjectStatus.HDC_VERIFIED.value,
                ProjectStatus.HDC_DECLINED.value,
                ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
                ProjectStatus.RESULTS_APPROVED.value,
                ProjectStatus.RESULTS_DECLINED.value,
                ProjectStatus.GRADUATED.value,
            ],
            "pending_review": [
                ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
                ProjectStatus.ADMIN_APPROVED.value,
                ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
            ],
            "approved": [
                ProjectStatus.JBS5_HDC_APPROVED.value,
                ProjectStatus.HDC_VERIFIED.value,
                ProjectStatus.RESULTS_APPROVED.value,
                ProjectStatus.GRADUATED.value,
            ],
            "rejected": [
                ProjectStatus.JBS5_HDC_DECLINED.value,
                ProjectStatus.HDC_DECLINED.value,
                ProjectStatus.RESULTS_DECLINED.value,
            ],
        },
        "jbs5": {
            "all": [
                ProjectStatus.JBS5_SUBMITTED_TO_HDC.value,
                ProjectStatus.JBS5_HDC_APPROVED.value,
                ProjectStatus.JBS5_HDC_DECLINED.value,
            ],
            "pending_review": [ProjectStatus.JBS5_SUBMITTED_TO_HDC.value],
            "approved": [ProjectStatus.JBS5_HDC_APPROVED.value],
            "rejected": [ProjectStatus.JBS5_HDC_DECLINED.value],
        },
        "nominations": {
            "all": [
                ProjectStatus.ADMIN_APPROVED.value,
                ProjectStatus.HDC_VERIFIED.value,
                ProjectStatus.HDC_DECLINED.value,
            ],
            "pending_review": [ProjectStatus.ADMIN_APPROVED.value],
            "approved": [ProjectStatus.HDC_VERIFIED.value],
            "rejected": [ProjectStatus.HDC_DECLINED.value],
        },
        "results": {
            "all": [
                ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value,
                ProjectStatus.RESULTS_APPROVED.value,
                ProjectStatus.RESULTS_DECLINED.value,
                ProjectStatus.GRADUATED.value,
            ],
            "pending_review": [ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value],
            "approved": [ProjectStatus.RESULTS_APPROVED.value, ProjectStatus.GRADUATED.value],
            "rejected": [ProjectStatus.RESULTS_DECLINED.value],
        },
    }
    approval_labels = {
        "overview": "All HDC Approvals",
        "jbs5": "JBS5 Approvals",
        "nominations": "JBS10 & Assessor Nomination Approvals",
        "results": "Result Approvals",
    }

    queue_scope = (
        MbaProject.query.options(
            joinedload(MbaProject.student).joinedload(MbaUser.student_profile),
            joinedload(MbaProject.primary_supervisor).joinedload(MbaUser.scholar_profile),
            joinedload(MbaProject.assessor_1).joinedload(MbaUser.scholar_profile),
            joinedload(MbaProject.assessor_2).joinedload(MbaUser.scholar_profile),
            joinedload(MbaProject.assessor_3).joinedload(MbaUser.scholar_profile),
            joinedload(MbaProject.documents),
        )
        .order_by(MbaProject.updated_at.desc())
    )
    if approval_set == "jbs5":
        queue_scope = queue_scope.filter(
            or_(
                MbaProject.project_status.in_(approval_status_groups[approval_set]["all"]),
                MbaProject.jbs5_hdc_approved_at.isnot(None),
            )
        )
    else:
        queue_scope = queue_scope.filter(MbaProject.project_status.in_(approval_status_groups[approval_set]["all"]))
    if hdc_student_number:
        queue_scope = queue_scope.join(
            MbaStudentProfile,
            MbaStudentProfile.user_id == MbaProject.student_id,
        ).filter(MbaStudentProfile.student_number.ilike(f"%{hdc_student_number}%"))

    def apply_review_status_filter(base_query, selected_status):
        if selected_status == "all":
            return base_query
        if approval_set == "jbs5" and selected_status == "approved":
            return base_query.filter(MbaProject.jbs5_hdc_approved_at.isnot(None))
        return base_query.filter(
            MbaProject.project_status.in_(approval_status_groups[approval_set][selected_status])
        )

    review_filter_counts = {
        "all": queue_scope.order_by(None).count(),
        "pending_review": apply_review_status_filter(queue_scope, "pending_review").order_by(None).count(),
        "approved": apply_review_status_filter(queue_scope, "approved").order_by(None).count(),
        "rejected": apply_review_status_filter(queue_scope, "rejected").order_by(None).count(),
    }
    queue_query = apply_review_status_filter(queue_scope, review_status)

    hdc_pending_counts = {
        "jbs5": MbaProject.query.filter_by(project_status=ProjectStatus.JBS5_SUBMITTED_TO_HDC.value).count(),
        "nominations": MbaProject.query.filter_by(project_status=ProjectStatus.ADMIN_APPROVED.value).count(),
        "results": MbaProject.query.filter_by(project_status=ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value).count(),
    }
    hdc_pending_counts["total"] = sum(hdc_pending_counts.values())
    hdc_pagination_args = request_query_args({"hdc_page", "hdc_per_page"})
    queue, hdc_pagination = paginate_query(
        queue_query,
        hdc_page,
        hdc_per_page,
        "mba.hdc_dashboard",
        page_param="hdc_page",
        per_page_param="hdc_per_page",
        base_args=hdc_pagination_args,
        anchor="hdc-queue",
    )

    project_ids = [p.id for p in queue]
    def hdc_visible_documents(project):
        return [
            doc for doc in project.documents
            if hdc_can_access_document(project, doc.doc_type)
        ]
    documents_by_project = {
        p.id: hdc_visible_documents(p)
        for p in queue
    }

    from ..models import MbaForm
    all_forms = MbaForm.query.filter(MbaForm.project_id.in_(project_ids)).all() if project_ids else []
    forms_by_project = {}
    for form in all_forms:
        forms_by_project.setdefault(form.project_id, {})[form.form_type] = form

    grade_summaries = {p.id: project_grade_summary(p.id, forms_by_project) for p in queue}

    return render_template(
        "mba/hdc_dashboard.html",
        projects=queue,
        documents_by_project=documents_by_project,
        forms_by_project=forms_by_project,
        grade_summaries=grade_summaries,
        document_label=document_label,
        all_assessment_results_received=all_assessment_results_received,
        required_hdc_results_documents_missing=required_hdc_results_documents_missing,
        kpis=mba_kpis(),
        approval_set=approval_set,
        approval_labels=approval_labels,
        hdc_pending_counts=hdc_pending_counts,
        review_status=review_status,
        hdc_student_number=hdc_student_number,
        review_filter_counts=review_filter_counts,
        hdc_pagination=hdc_pagination,
        project_status_label=project_status_label,
        assessor_hdc_decision=assessor_hdc_decision,
        assessor_hdc_decision_label=assessor_hdc_decision_label,
    )

