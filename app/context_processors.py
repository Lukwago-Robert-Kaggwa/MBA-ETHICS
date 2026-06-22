from pathlib import Path

from flask import request, url_for
from flask_login import current_user
from sqlalchemy import or_
from werkzeug.routing import BuildError

from .models import MbaProject, MbaProjectSupervisorInvitation, MbaRole, ProjectStatus
from .mba.route_support import (
    INVITATION_ACCEPTED,
    additional_assessment_pending,
    additional_assessor_nomination_awaiting_hdc,
    admin_pending_reminder_count,
    assessment_results_forwarded_to_supervisor,
    corrections_released_to_student,
    project_has_active_corrections,
    project_corrections_status,
    supervisor_can_manage_corrections,
)


def inject_auth_flags_factory(app):
    def inject_auth_flags():
        def mba_profile_url():
            try:
                return url_for("mba.profile")
            except BuildError:
                return "/mba/profile"

        def mba_edit_project_url(project_id):
            try:
                return url_for("mba.edit_project", project_id=project_id)
            except BuildError:
                return f"/mba/projects/{project_id}/edit"

        def mba_submit_project_title_url(project_id):
            try:
                return url_for("mba.submit_project_title", project_id=project_id)
            except BuildError:
                return f"/mba/projects/{project_id}/submit-title"

        def mba_corrections_nav():
            nav = {"visible": False, "count": 0, "url": None}
            if not current_user.is_authenticated or getattr(current_user, "system_name", None) != "mba":
                return nav

            active_endpoints = {
                "mba.student_corrections",
                "mba.scholar_corrections",
                "mba.admin_corrections",
            }
            role = getattr(current_user, "role", None)
            endpoint_active = request.endpoint in active_endpoints

            if role == MbaRole.STUDENT.value:
                projects = MbaProject.query.filter_by(student_id=current_user.id).all()
                matches = [
                    project
                    for project in projects
                    if project_has_active_corrections(project) and corrections_released_to_student(project)
                ]
                pending_matches = [
                    project
                    for project in matches
                    if project_corrections_status(project) in {"awaiting_student", "rejected_by_supervisor"}
                ]
                nav.update(
                    {
                        "visible": endpoint_active or bool(matches),
                        "count": len(matches),
                        "pending_count": len(pending_matches),
                        "has_alert": bool(pending_matches),
                        "url": url_for("mba.student_corrections"),
                    }
                )
                return nav

            if role == MbaRole.SCHOLAR.value:
                accepted_invitation_project_ids = [
                    invitation.project_id
                    for invitation in MbaProjectSupervisorInvitation.query.filter_by(
                        supervisor_id=current_user.id,
                        status=INVITATION_ACCEPTED,
                    ).all()
                ]
                projects = MbaProject.query.filter(
                    or_(
                        MbaProject.primary_supervisor_id == current_user.id,
                        MbaProject.id.in_(accepted_invitation_project_ids),
                        MbaProject.co_supervisor_id == current_user.id,
                    )
                ).all()
                matches = [
                    project
                    for project in projects
                    if supervisor_can_manage_corrections(project, current_user)
                    and project_has_active_corrections(project)
                    and assessment_results_forwarded_to_supervisor(project)
                ]
                pending_matches = [
                    project
                    for project in matches
                    if project_corrections_status(project) != "ready_for_admin"
                ]
                nav.update(
                    {
                        "visible": endpoint_active or bool(matches),
                        "count": len(pending_matches),
                        "url": url_for("mba.scholar_corrections"),
                    }
                )
                return nav

            if role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
                projects = MbaProject.query.filter(MbaProject.project_status != ProjectStatus.CREATED.value).all()
                matches = [project for project in projects if project_has_active_corrections(project)]
                pending_matches = [
                    project
                    for project in matches
                    if project_corrections_status(project) != "ready_for_admin"
                ]
                nav.update(
                    {
                        "visible": endpoint_active or bool(matches),
                        "count": len(pending_matches),
                        "url": url_for("mba.admin_corrections"),
                    }
                )
                return nav

            return nav

        def mba_additional_assessment_nav():
            nav = {"visible": False, "count": 0, "url": None}
            if not current_user.is_authenticated or getattr(current_user, "system_name", None) != "mba":
                return nav
            role = getattr(current_user, "role", None)
            if role not in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
                return nav
            endpoint_active = request.endpoint == "mba.admin_additional_assessment"
            projects = MbaProject.query.filter(MbaProject.project_status != ProjectStatus.CREATED.value).all()
            matches = [project for project in projects if additional_assessment_pending(project)]
            nav.update(
                {
                    "visible": True,
                    "count": len(matches),
                    "url": url_for("mba.admin_additional_assessment"),
                    "active": endpoint_active,
                }
            )
            return nav

        def mba_reminders_nav():
            nav = {"visible": False, "count": 0, "url": None, "active": False}
            if not current_user.is_authenticated or getattr(current_user, "system_name", None) != "mba":
                return nav
            role = getattr(current_user, "role", None)
            if role not in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}:
                return nav
            nav.update(
                {
                    "visible": True,
                    "count": admin_pending_reminder_count(),
                    "url": url_for("mba.admin_reminders"),
                    "active": request.endpoint == "mba.admin_reminders",
                }
            )
            return nav

        def mba_hdc_approval_nav():
            nav = {
                "visible": False,
                "active": (request.args.get("approval_set") or "overview").strip().lower(),
                "jbs5": 0,
                "nominations": 0,
                "results": 0,
                "additional_nominations": 0,
                "total": 0,
            }
            if (
                not current_user.is_authenticated
                or getattr(current_user, "system_name", None) != "mba"
                or getattr(current_user, "role", None) != MbaRole.HDC.value
            ):
                return nav
            jbs5_count = MbaProject.query.filter_by(
                project_status=ProjectStatus.JBS5_SUBMITTED_TO_HDC.value
            ).count()
            nomination_count = MbaProject.query.filter_by(
                project_status=ProjectStatus.ADMIN_APPROVED.value
            ).count()
            result_count = MbaProject.query.filter_by(
                project_status=ProjectStatus.RESULTS_SUBMITTED_TO_HDC.value
            ).count()
            additional_nomination_count = sum(
                1
                for project in MbaProject.query.filter(MbaProject.assessor_3_id.isnot(None)).all()
                if additional_assessor_nomination_awaiting_hdc(project)
            )
            nav.update(
                {
                    "visible": True,
                    "jbs5": jbs5_count,
                    "nominations": nomination_count,
                    "results": result_count,
                    "additional_nominations": additional_nomination_count,
                    "total": jbs5_count + nomination_count + result_count + additional_nomination_count,
                }
            )
            return nav

        def asset_version(filename):
            try:
                static_path = Path(app.static_folder or "") / filename
                return str(int(static_path.stat().st_mtime))
            except OSError:
                return "1"

        return {
            "microsoft_login_enabled": bool(
                app.config["MICROSOFT_CLIENT_ID"] and app.config["MICROSOFT_CLIENT_SECRET"]
            ),
            "asset_version": asset_version,
            "mba_profile_url": mba_profile_url,
            "mba_edit_project_url": mba_edit_project_url,
            "mba_submit_project_title_url": mba_submit_project_title_url,
            "mba_corrections_nav": mba_corrections_nav,
            "mba_additional_assessment_nav": mba_additional_assessment_nav,
            "mba_reminders_nav": mba_reminders_nav,
            "mba_hdc_approval_nav": mba_hdc_approval_nav,
        }

    return inject_auth_flags
