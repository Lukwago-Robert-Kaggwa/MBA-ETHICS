import os
import mimetypes
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, send_from_directory, url_for
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from ..extensions import db
from .form_definitions import FORM_DEFINITIONS, form_definition, iter_fields
from ..models import (
    EthicsActivityLog,
    EthicsFormDraft,
    EthicsFormRequirement,
    EthicsFormSubmission,
    EthicsReviewerAssignment,
    EthicsRole,
    EthicsSubmissionStatus,
    EthicsSubmissionFile,
    EthicsUser,
)
from ..supervisor_sync import sync_ethics_supervisor_from_mba

ethics_bp = Blueprint("ethics", __name__, template_folder="../templates")

UPLOAD_FOLDER = os.path.join("uploads", "ethics")
LEGACY_UPLOAD_FOLDER = os.path.join("app", "static", "uploads", "ethics")
ALLOWED_UPLOAD_EXTENSIONS = {".pdf", ".doc", ".docx"}


def _ethics_upload_dir():
    if os.path.isabs(UPLOAD_FOLDER):
        return UPLOAD_FOLDER
    return os.path.abspath(os.path.join(current_app.root_path, "..", UPLOAD_FOLDER))


def _legacy_ethics_upload_dir():
    return os.path.abspath(os.path.join(current_app.root_path, "static", "uploads", "ethics"))


def _mime_type_for(filename, fallback="application/octet-stream"):
    guessed, _encoding = mimetypes.guess_type(filename or "")
    return guessed or fallback


def require_ethics_user():
    if not current_user.is_authenticated or current_user.system_name != "ethics":
        flash("Please log in with an Ethics account.", "error")
        return False
    return True


def require_ethics_admin():
    if not require_ethics_user():
        return False
    if current_user.role not in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value}:
        flash("Only Ethics Admin can access this page.", "error")
        return False
    return True


def require_ethics_super_admin():
    if not require_ethics_user():
        return False
    if current_user.role != EthicsRole.SUPER_ADMIN.value:
        flash("Only Ethics Super Admin can access analytics.", "error")
        return False
    return True


def log_activity(action, target=None, details=None):
    db.session.add(
        EthicsActivityLog(
            user_id=current_user.id,
            action=action,
            target_type=target.__class__.__name__ if target else None,
            target_id=str(target.id) if target else None,
            details=details,
        )
    )


def latest_student_submissions(student_id):
    return (
        EthicsFormSubmission.query.filter_by(student_id=student_id)
        .order_by(EthicsFormSubmission.created_at.desc())
        .all()
    )


def latest_student_draft(student_id, form_type):
    return EthicsFormDraft.query.filter_by(student_id=student_id, form_type=(form_type or "").upper()).first()


def draft_values_by_name(draft):
    values = {}
    if not draft:
        return values
    for section in draft.payload.get("sections", []):
        for answer in section.get("answers", []):
            values[answer.get("name")] = answer.get("value")
    return values


def role_landing_url():
    role = current_user.role
    if role == EthicsRole.STUDENT.value:
        return url_for("ethics.student_dashboard")
    if role == EthicsRole.SUPERVISOR.value:
        return url_for("ethics.supervisor_dashboard")
    if role == EthicsRole.REVIEWER.value:
        return url_for("ethics.review_dashboard")
    if role == EthicsRole.REC.value:
        return url_for("ethics.rec_dashboard")
    if role in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        return url_for("ethics.chair_landing")
    return url_for("ethics.dashboard")


def redirect_to_role_landing():
    return redirect(role_landing_url())


def ethics_kpis():
    return {
        "drafts": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.DRAFT.value).count(),
        "supervisor": EthicsFormSubmission.query.filter_by(
            status=EthicsSubmissionStatus.AWAITING_SUPERVISOR.value
        ).count(),
        "admin": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.AWAITING_ADMIN.value).count(),
        "review": EthicsFormSubmission.query.filter(
            EthicsFormSubmission.status.in_(
                [EthicsSubmissionStatus.AWAITING_REVIEWERS.value, EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value]
            )
        ).count(),
        "rec": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.AWAITING_REC.value).count(),
        "certificates": EthicsFormSubmission.query.filter_by(
            status=EthicsSubmissionStatus.CERTIFICATE_ISSUED.value
        ).count(),
    }


def terminal_statuses():
    return {
        EthicsSubmissionStatus.APPROVED.value,
        EthicsSubmissionStatus.APPROVED_WITH_MINOR_CHANGES.value,
        EthicsSubmissionStatus.RESUBMISSION_REQUIRED.value,
        EthicsSubmissionStatus.REJECTED.value,
        EthicsSubmissionStatus.CERTIFICATE_ISSUED.value,
    }


def user_can_access_submission(submission):
    if not submission or not current_user.is_authenticated or current_user.system_name != "ethics":
        return False
    if current_user.role in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        return True
    if current_user.role == EthicsRole.STUDENT.value:
        return submission.student_id == current_user.id
    if current_user.role == EthicsRole.SUPERVISOR.value:
        return submission.supervisor_id == current_user.id
    if current_user.role == EthicsRole.REVIEWER.value:
        return any(assignment.reviewer_id == current_user.id for assignment in submission.reviewer_assignments)
    if current_user.role == EthicsRole.REC.value:
        return submission.submitted_to_rec
    return False


def submission_timeline(submission):
    logs = (
        EthicsActivityLog.query.filter_by(target_type="EthicsFormSubmission", target_id=str(submission.id))
        .order_by(EthicsActivityLog.created_at.asc())
        .all()
    )
    return logs


@ethics_bp.route("/")
@login_required
def dashboard():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    return redirect_to_role_landing()


@ethics_bp.route("/student-dashboard", methods=["GET"])
@login_required
def student_dashboard():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.STUDENT.value:
        return redirect_to_role_landing()

    if sync_ethics_supervisor_from_mba(current_user):
        log_activity("sync_mba_supervisor", current_user.supervisor, "MBA supervisor linked in Ethics")
        db.session.commit()
        flash("Your MBA supervisor is already linked in Ethics.", "success")

    supervisors = EthicsUser.query.filter_by(role=EthicsRole.SUPERVISOR.value, is_active=True).order_by(
        EthicsUser.email
    )
    submissions = latest_student_submissions(current_user.id)
    active_submissions = [item for item in submissions if item.status not in terminal_statuses()]
    old_submissions = [item for item in submissions if item.status in terminal_statuses()]
    return render_template(
        "ethics/student_dashboard.html",
        supervisors=supervisors,
        active_submissions=active_submissions,
        old_submissions=old_submissions,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/pack", methods=["GET"])
@login_required
def ethics_pack():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.STUDENT.value:
        return redirect_to_role_landing()
    if sync_ethics_supervisor_from_mba(current_user):
        log_activity("sync_mba_supervisor", current_user.supervisor, "MBA supervisor linked in Ethics")
        db.session.commit()
        flash("Your MBA supervisor is already linked in Ethics.", "success")
    if not current_user.supervisor_id:
        flash("Choose a supervisor before starting your ethics pack.", "error")
        return redirect_to_role_landing()
    return render_template("ethics/ethics_pack.html", form_definitions=FORM_DEFINITIONS)


@ethics_bp.route("/forms/new/<form_type>", methods=["GET"])
@login_required
def new_ethics_form(form_type):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.STUDENT.value:
        return redirect_to_role_landing()
    if not current_user.supervisor_id:
        flash("Choose a supervisor before starting your ethics pack.", "error")
        return redirect_to_role_landing()

    definition = form_definition(form_type)
    if not definition:
        flash("Choose Form A, Form B, or Form C.", "error")
        return redirect(url_for("ethics.ethics_pack"))
    draft = latest_student_draft(current_user.id, form_type)
    return render_template(
        "ethics/form_pack.html",
        form_type=form_type.upper(),
        definition=definition,
        draft_values=draft_values_by_name(draft),
    )


@ethics_bp.route("/supervisor-dashboard", methods=["GET"])
@login_required
def supervisor_dashboard():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role not in {EthicsRole.SUPERVISOR.value, EthicsRole.REVIEWER.value}:
        return redirect_to_role_landing()

    all_submissions = (
        EthicsFormSubmission.query.filter_by(supervisor_id=current_user.id)
        .order_by(EthicsFormSubmission.updated_at.desc())
        .all()
    )
    active_submissions = [
        item for item in all_submissions if item.status == EthicsSubmissionStatus.AWAITING_SUPERVISOR.value
    ]
    old_submissions = [item for item in all_submissions if item.status != EthicsSubmissionStatus.AWAITING_SUPERVISOR.value]
    return render_template(
        "ethics/supervisor_dashboard.html",
        active_submissions=active_submissions,
        old_submissions=old_submissions,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/supervisor-dashboard/previous/<int:student_id>", methods=["GET"])
@login_required
def supervisor_previous_forms(student_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.SUPERVISOR.value:
        return redirect_to_role_landing()

    student = db.session.get(EthicsUser, student_id)
    submissions = (
        EthicsFormSubmission.query.filter_by(student_id=student_id, supervisor_id=current_user.id)
        .order_by(EthicsFormSubmission.created_at.desc())
        .all()
    )
    return render_template(
        "ethics/previous_forms.html",
        page_title="Student Previous Forms",
        student=student,
        submissions=submissions,
    )


@ethics_bp.route("/review-dashboard", methods=["GET"])
@login_required
def review_dashboard():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.REVIEWER.value:
        return redirect_to_role_landing()

    assignments = (
        EthicsReviewerAssignment.query.filter_by(reviewer_id=current_user.id)
        .join(EthicsFormSubmission)
        .order_by(EthicsReviewerAssignment.created_at.desc())
        .all()
    )
    active_assignments = [item for item in assignments if not item.completed_at]
    old_assignments = [item for item in assignments if item.completed_at]
    return render_template(
        "ethics/review_dashboard.html",
        active_assignments=active_assignments,
        old_assignments=old_assignments,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/chair-landing", methods=["GET"])
@login_required
def chair_landing():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role not in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        return redirect_to_role_landing()

    active_statuses = [
        EthicsSubmissionStatus.AWAITING_ADMIN.value,
        EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value,
        EthicsSubmissionStatus.AWAITING_REC.value,
        EthicsSubmissionStatus.APPROVED.value,
        EthicsSubmissionStatus.APPROVED_WITH_MINOR_CHANGES.value,
    ]
    active_submissions = (
        EthicsFormSubmission.query.filter(EthicsFormSubmission.status.in_(active_statuses))
        .order_by(EthicsFormSubmission.updated_at.desc())
        .all()
    )
    old_submissions = (
        EthicsFormSubmission.query.filter(~EthicsFormSubmission.status.in_(active_statuses))
        .order_by(EthicsFormSubmission.updated_at.desc())
        .limit(50)
        .all()
    )
    reviewers = EthicsUser.query.filter_by(role=EthicsRole.REVIEWER.value, is_active=True).order_by(EthicsUser.email)
    return render_template(
        "ethics/chair_landing.html",
        active_submissions=active_submissions,
        old_submissions=old_submissions,
        reviewers=reviewers,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/admin/activity-logs", methods=["GET"])
@login_required
def activity_logs():
    if not require_ethics_admin():
        return redirect_to_role_landing()

    logs = EthicsActivityLog.query.order_by(EthicsActivityLog.created_at.desc()).limit(300).all()
    return render_template("ethics/activity_logs.html", logs=logs, kpis=ethics_kpis())


@ethics_bp.route("/admin/monitoring", methods=["GET"])
@login_required
def monitoring_forms():
    if not require_ethics_admin():
        return redirect_to_role_landing()

    submissions = EthicsFormSubmission.query.order_by(EthicsFormSubmission.updated_at.desc()).all()
    return render_template("ethics/monitoring.html", submissions=submissions, kpis=ethics_kpis())


@ethics_bp.route("/admin/reassign-reviewers", methods=["GET", "POST"])
@login_required
def reassign_reviewers():
    if not require_ethics_admin():
        return redirect_to_role_landing()

    reviewers = EthicsUser.query.filter_by(role=EthicsRole.REVIEWER.value, is_active=True).order_by(EthicsUser.email)
    submissions = EthicsFormSubmission.query.order_by(EthicsFormSubmission.updated_at.desc()).all()

    if request.method == "POST":
        submission_id = request.form.get("submission_id", type=int)
        reviewer_ids = [int(value) for value in request.form.getlist("reviewer_ids") if value.isdigit()]
        submission = db.session.get(EthicsFormSubmission, submission_id)
        selected_reviewers = (
            EthicsUser.query.filter(EthicsUser.id.in_(reviewer_ids[:2]), EthicsUser.role == EthicsRole.REVIEWER.value)
            .order_by(EthicsUser.email)
            .all()
        )

        if not submission or not selected_reviewers:
            flash("Choose a submission and at least one reviewer.", "error")
            return redirect(url_for("ethics.reassign_reviewers"))

        old_reviewers = [assignment.reviewer.email for assignment in submission.reviewer_assignments]
        selected_ids = {reviewer.id for reviewer in selected_reviewers}
        for assignment in list(submission.reviewer_assignments):
            if not assignment.completed_at and assignment.reviewer_id not in selected_ids:
                db.session.delete(assignment)

        for reviewer in selected_reviewers:
            exists = EthicsReviewerAssignment.query.filter_by(
                submission_id=submission.id,
                reviewer_id=reviewer.id,
            ).first()
            if not exists:
                db.session.add(
                    EthicsReviewerAssignment(
                        submission_id=submission.id,
                        reviewer_id=reviewer.id,
                        assigned_by_id=current_user.id,
                    )
                )

        submission.status = EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value
        submission.submitted_to_reviewers = True
        details = (
            f"Reviewers changed from {', '.join(old_reviewers) or 'none'} "
            f"to {', '.join(reviewer.email for reviewer in selected_reviewers)}"
        )
        log_activity("reassign_reviewers", submission, details)
        db.session.commit()
        flash("Reviewer reassignment saved.", "success")
        return redirect(url_for("ethics.reassign_reviewers"))

    return render_template(
        "ethics/reassign_reviewers.html",
        submissions=submissions,
        reviewers=reviewers,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/admin/reassign-supervisors", methods=["GET", "POST"])
@login_required
def reassign_supervisors():
    if not require_ethics_admin():
        return redirect_to_role_landing()

    supervisors = EthicsUser.query.filter_by(role=EthicsRole.SUPERVISOR.value, is_active=True).order_by(
        EthicsUser.email
    )
    submissions = EthicsFormSubmission.query.order_by(EthicsFormSubmission.updated_at.desc()).all()

    if request.method == "POST":
        submission_id = request.form.get("submission_id", type=int)
        supervisor_id = request.form.get("supervisor_id", type=int)
        submission = db.session.get(EthicsFormSubmission, submission_id)
        supervisor = EthicsUser.query.filter_by(id=supervisor_id, role=EthicsRole.SUPERVISOR.value).first()

        if not submission or not supervisor:
            flash("Choose a submission and a valid supervisor.", "error")
            return redirect(url_for("ethics.reassign_supervisors"))

        old_supervisor = submission.supervisor.email if submission.supervisor else "none"
        submission.supervisor_id = supervisor.id
        if submission.student:
            submission.student.supervisor_id = supervisor.id
        log_activity(
            "reassign_supervisor",
            submission,
            f"Supervisor changed from {old_supervisor} to {supervisor.email}",
        )
        db.session.commit()
        flash("Supervisor reassignment saved.", "success")
        return redirect(url_for("ethics.reassign_supervisors"))

    return render_template(
        "ethics/reassign_supervisors.html",
        submissions=submissions,
        supervisors=supervisors,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/admin/analytics", methods=["GET"])
@login_required
def analytics():
    if not require_ethics_super_admin():
        return redirect_to_role_landing()

    form_counts = dict(
        db.session.query(EthicsFormSubmission.form_type, db.func.count(EthicsFormSubmission.id))
        .group_by(EthicsFormSubmission.form_type)
        .all()
    )
    status_counts = dict(
        db.session.query(EthicsFormSubmission.status, db.func.count(EthicsFormSubmission.id))
        .group_by(EthicsFormSubmission.status)
        .all()
    )
    risk_counts = dict(
        db.session.query(EthicsFormSubmission.risk_level, db.func.count(EthicsFormSubmission.id))
        .group_by(EthicsFormSubmission.risk_level)
        .all()
    )
    reviewer_stats = (
        db.session.query(
            EthicsUser.email,
            db.func.count(EthicsReviewerAssignment.id),
            db.func.count(EthicsReviewerAssignment.completed_at),
        )
        .outerjoin(EthicsReviewerAssignment, EthicsReviewerAssignment.reviewer_id == EthicsUser.id)
        .filter(EthicsUser.role == EthicsRole.REVIEWER.value)
        .group_by(EthicsUser.id)
        .order_by(EthicsUser.email)
        .all()
    )
    totals = {
        "submissions": EthicsFormSubmission.query.count(),
        "students": EthicsUser.query.filter_by(role=EthicsRole.STUDENT.value).count(),
        "supervisors": EthicsUser.query.filter_by(role=EthicsRole.SUPERVISOR.value).count(),
        "reviewers": EthicsUser.query.filter_by(role=EthicsRole.REVIEWER.value).count(),
    }
    return render_template(
        "ethics/analytics.html",
        form_counts=form_counts,
        status_counts=status_counts,
        risk_counts=risk_counts,
        reviewer_stats=reviewer_stats,
        totals=totals,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/rec-dashboard", methods=["GET"])
@login_required
def rec_dashboard():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.REC.value:
        return redirect_to_role_landing()

    rec_submissions = (
        EthicsFormSubmission.query.filter_by(submitted_to_rec=True)
        .order_by(EthicsFormSubmission.updated_at.desc())
        .all()
    )
    active_submissions = [
        item for item in rec_submissions if item.status == EthicsSubmissionStatus.AWAITING_REC.value
    ]
    old_submissions = [item for item in rec_submissions if item.status != EthicsSubmissionStatus.AWAITING_REC.value]
    return render_template(
        "ethics/rec_dashboard.html",
        active_submissions=active_submissions,
        old_submissions=old_submissions,
        kpis=ethics_kpis(),
    )


@ethics_bp.route("/submissions/<int:submission_id>", methods=["GET"])
@login_required
def submission_detail(submission_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    submission = db.session.get(EthicsFormSubmission, submission_id)
    if not user_can_access_submission(submission):
        abort(403)

    reviewers = EthicsUser.query.filter_by(role=EthicsRole.REVIEWER.value, is_active=True).order_by(EthicsUser.email)
    current_assignment = None
    if current_user.role == EthicsRole.REVIEWER.value:
        current_assignment = EthicsReviewerAssignment.query.filter_by(
            submission_id=submission.id, reviewer_id=current_user.id
        ).first()

    return render_template(
        "ethics/submission_detail.html",
        submission=submission,
        reviewers=reviewers,
        current_assignment=current_assignment,
        timeline=submission_timeline(submission),
    )


@ethics_bp.route("/submissions/<int:submission_id>/files/<stored_name>", methods=["GET"])
@login_required
def download_submission_file(submission_id, stored_name):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))

    submission = db.session.get(EthicsFormSubmission, submission_id)
    if not user_can_access_submission(submission):
        abort(403)

    if os.path.basename(stored_name) != stored_name:
        abort(404)

    file_record = payload_file_record(submission.payload, stored_name)
    if not file_record:
        abort(404)

    db_file = EthicsSubmissionFile.query.filter_by(
        submission_id=submission.id,
        stored_name=stored_name,
    ).first()
    if not db_file:
        db_file = EthicsSubmissionFile.query.filter_by(
            student_id=submission.student_id,
            stored_name=stored_name,
        ).first()
        if db_file and db_file.submission_id is None:
            db_file.submission_id = submission.id
            db.session.commit()
    if db_file and db_file.file_data:
        return send_file(
            BytesIO(db_file.file_data),
            mimetype=db_file.mime_type or _mime_type_for(db_file.original_name),
            as_attachment=True,
            download_name=db_file.original_name or file_record.get("filename") or stored_name,
        )

    for directory in (_ethics_upload_dir(), _legacy_ethics_upload_dir()):
        file_path = os.path.join(directory, stored_name)
        if os.path.isfile(file_path):
            return send_from_directory(
                directory,
                stored_name,
                as_attachment=True,
                download_name=file_record.get("filename") or stored_name,
            )

    abort(404)


@ethics_bp.route("/dashboard-legacy")
@login_required
def dashboard_legacy():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))

    role = current_user.role
    supervisors = EthicsUser.query.filter_by(role=EthicsRole.SUPERVISOR.value, is_active=True).order_by(
        EthicsUser.email
    )
    reviewers = EthicsUser.query.filter_by(role=EthicsRole.REVIEWER.value, is_active=True).order_by(EthicsUser.email)

    student_submissions = latest_student_submissions(current_user.id) if role == EthicsRole.STUDENT.value else []
    supervisor_queue = []
    admin_queue = []
    reviewer_queue = []
    rec_queue = []

    if role == EthicsRole.SUPERVISOR.value:
        supervisor_queue = (
            EthicsFormSubmission.query.filter_by(supervisor_id=current_user.id)
            .filter(EthicsFormSubmission.status.in_([EthicsSubmissionStatus.AWAITING_SUPERVISOR.value]))
            .order_by(EthicsFormSubmission.submitted_at.desc())
            .all()
        )

    if role in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        admin_queue = (
            EthicsFormSubmission.query.filter(
                EthicsFormSubmission.status.in_(
                    [
                        EthicsSubmissionStatus.AWAITING_ADMIN.value,
                        EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value,
                        EthicsSubmissionStatus.AWAITING_REC.value,
                        EthicsSubmissionStatus.APPROVED.value,
                    ]
                )
            )
            .order_by(EthicsFormSubmission.updated_at.desc())
            .all()
        )

    if role == EthicsRole.REVIEWER.value:
        reviewer_queue = (
            EthicsReviewerAssignment.query.filter_by(reviewer_id=current_user.id)
            .join(EthicsFormSubmission)
            .order_by(EthicsReviewerAssignment.created_at.desc())
            .all()
        )

    if role == EthicsRole.REC.value:
        rec_queue = (
            EthicsFormSubmission.query.filter_by(submitted_to_rec=True)
            .filter(EthicsFormSubmission.status == EthicsSubmissionStatus.AWAITING_REC.value)
            .order_by(EthicsFormSubmission.updated_at.desc())
            .all()
        )

    kpis = {
        "drafts": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.DRAFT.value).count(),
        "supervisor": EthicsFormSubmission.query.filter_by(
            status=EthicsSubmissionStatus.AWAITING_SUPERVISOR.value
        ).count(),
        "admin": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.AWAITING_ADMIN.value).count(),
        "review": EthicsFormSubmission.query.filter(
            EthicsFormSubmission.status.in_(
                [EthicsSubmissionStatus.AWAITING_REVIEWERS.value, EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value]
            )
        ).count(),
        "rec": EthicsFormSubmission.query.filter_by(status=EthicsSubmissionStatus.AWAITING_REC.value).count(),
        "certificates": EthicsFormSubmission.query.filter_by(
            status=EthicsSubmissionStatus.CERTIFICATE_ISSUED.value
        ).count(),
    }

    return render_template(
        "ethics/dashboard.html",
        supervisors=supervisors,
        reviewers=reviewers,
        student_submissions=student_submissions,
        supervisor_queue=supervisor_queue,
        admin_queue=admin_queue,
        reviewer_queue=reviewer_queue,
        rec_queue=rec_queue,
        kpis=kpis,
    )


@ethics_bp.route("/choose-supervisor", methods=["POST"])
@login_required
def choose_supervisor():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.STUDENT.value:
        flash("Only students can choose a supervisor.", "error")
        return redirect_to_role_landing()

    supervisor_id = request.form.get("supervisor_id", type=int)
    supervisor = EthicsUser.query.filter_by(id=supervisor_id, role=EthicsRole.SUPERVISOR.value).first()
    if not supervisor:
        flash("Choose a valid supervisor.", "error")
        return redirect_to_role_landing()

    if current_user.supervisor_id and not request.form.get("replace_supervisor"):
        flash("You already have a supervisor. Use replace supervisor if you want to change them.", "error")
        return redirect_to_role_landing()

    old_supervisor = current_user.supervisor
    current_user.supervisor_id = supervisor.id
    current_user.authenticated_student = True
    if old_supervisor:
        details = f"Student replaced supervisor {old_supervisor.email} with {supervisor.email}"
        action = "replace_supervisor"
        message = "Supervisor replaced."
    else:
        details = f"Student selected supervisor {supervisor.email}"
        action = "choose_supervisor"
        message = "Supervisor selected."
    log_activity(action, supervisor, details)
    db.session.commit()
    flash(message, "success")
    return redirect_to_role_landing()


def collect_pack_payload(definition):
    payload = {"sections": []}
    for section in definition["sections"]:
        section_payload = {"title": section["title"], "answers": []}
        for field in section["fields"]:
            name = field["name"]
            if field["type"] == "file":
                value = save_uploaded_files(name)
            elif field["type"] == "table":
                value = collect_table_rows(field)
            elif field["type"] == "checkboxes":
                value = request.form.getlist(name)
            else:
                value = (request.form.get(name) or "").strip()
            section_payload["answers"].append({"label": field["label"], "name": name, "value": value})
        payload["sections"].append(section_payload)
    return payload


def collect_draft_payload(definition):
    payload = {"sections": []}
    for section in definition["sections"]:
        section_payload = {"title": section["title"], "answers": []}
        for field in section["fields"]:
            name = field["name"]
            if field["type"] == "file":
                continue
            if field["type"] == "table":
                value = collect_table_rows(field)
            elif field["type"] == "checkboxes":
                value = request.form.getlist(name)
            else:
                value = (request.form.get(name) or "").strip()
            section_payload["answers"].append({"label": field["label"], "name": name, "value": value})
        payload["sections"].append(section_payload)
    return payload


def collect_table_rows(field):
    column_names = [column["name"] for column in field.get("columns", [])]
    columns = {name: request.form.getlist(f"{name}[]") for name in column_names}
    row_count = max([len(values) for values in columns.values()] or [0])
    rows = []
    for index in range(row_count):
        row = {}
        for name in column_names:
            values = columns.get(name, [])
            row[name] = (values[index] if index < len(values) else "").strip()
        if any(row.values()):
            rows.append(row)
    return rows


@ethics_bp.route("/forms/autosave/<form_type>", methods=["POST"])
@login_required
def autosave_ethics_form(form_type):
    if not require_ethics_user():
        return jsonify({"success": False, "error": "Not logged in"}), 401
    if current_user.role != EthicsRole.STUDENT.value:
        return jsonify({"success": False, "error": "Only students can autosave forms"}), 403

    definition = form_definition(form_type)
    if not definition:
        return jsonify({"success": False, "error": "Unknown form type"}), 400

    form_type = form_type.upper()
    draft = latest_student_draft(current_user.id, form_type)
    if not draft:
        draft = EthicsFormDraft(student_id=current_user.id, form_type=form_type, payload={})
        db.session.add(draft)
    draft.payload = collect_draft_payload(definition)
    db.session.commit()
    return jsonify({"success": True, "updated_at": draft.updated_at.isoformat()})


def save_uploaded_files(field_name):
    files = [item for item in request.files.getlist(field_name) if item and item.filename]
    if not files:
        return []

    upload_dir = _ethics_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)
    saved_files = []
    for uploaded_file in files:
        original_name = secure_filename(uploaded_file.filename)
        extension = os.path.splitext(original_name)[1].lower()
        if extension not in ALLOWED_UPLOAD_EXTENSIONS:
            continue
        stored_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}_{original_name}"
        uploaded_file.seek(0)
        file_bytes = uploaded_file.read()
        uploaded_file.seek(0)
        stored_path = os.path.join(upload_dir, stored_name)
        with open(stored_path, "wb") as fh:
            fh.write(file_bytes)
        db.session.add(
            EthicsSubmissionFile(
                student_id=current_user.id,
                field_name=field_name,
                original_name=original_name,
                stored_name=stored_name,
                file_data=file_bytes,
                mime_type=uploaded_file.mimetype or _mime_type_for(original_name),
                file_size=len(file_bytes),
            )
        )
        saved_files.append(
            {
                "filename": original_name,
                "stored_name": stored_name,
            }
        )
    return saved_files


def link_payload_files_to_submission(submission):
    if not submission:
        return
    for section in (submission.payload or {}).get("sections", []):
        for answer in section.get("answers", []):
            field_name = answer.get("name")
            value = answer.get("value")
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict) or not item.get("stored_name"):
                    continue
                file_record = EthicsSubmissionFile.query.filter_by(
                    student_id=submission.student_id,
                    stored_name=item["stored_name"],
                ).first()
                if file_record:
                    file_record.submission_id = submission.id
                    file_record.field_name = file_record.field_name or field_name


def iter_uploaded_payload_files(payload):
    for section in (payload or {}).get("sections", []):
        for answer in section.get("answers", []):
            value = answer.get("value")
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, dict) and item.get("stored_name"):
                    yield item


def payload_file_record(payload, stored_name):
    for item in iter_uploaded_payload_files(payload):
        if item.get("stored_name") == stored_name:
            return item
    return None


def first_payload_value(payload, *names):
    for section in payload.get("sections", []):
        for answer in section.get("answers", []):
            if answer.get("name") in names and answer.get("value"):
                value = answer["value"]
                if isinstance(value, list):
                    if value and isinstance(value[0], dict):
                        return value[0].get("filename")
                    return value[0] if value else None
                return value
    return None


def normalize_risk(value):
    clean_value = (value or "low").lower()
    if "high" in clean_value:
        return "high"
    if "medium" in clean_value:
        return "medium"
    return "low"


@ethics_bp.route("/submissions/new", methods=["POST"])
@login_required
def create_submission():
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.STUDENT.value:
        flash("Only students can create ethics submissions.", "error")
        return redirect_to_role_landing()
    if not current_user.supervisor_id:
        flash("Choose a supervisor before starting your ethics pack.", "error")
        return redirect_to_role_landing()

    form_type = (request.form.get("form_type") or "A").upper()
    definition = form_definition(form_type)
    if not definition:
        flash("Choose Form A, Form B, or Form C.", "error")
        return redirect_to_role_landing()

    payload = collect_pack_payload(definition)
    title = (
        first_payload_value(payload, "study_title", "project_title", "summary_title", "title_provision")
        or (request.form.get("title") or "").strip()
    )
    summary = (
        first_payload_value(payload, "abstract", "project_description", "executive_summary", "research_purpose")
        or (request.form.get("summary") or "").strip()
    )
    risk_level = normalize_risk(
        first_payload_value(payload, "risk_rating", "risk_level") or request.form.get("risk_level")
    )
    if not title:
        flash("Study title is required.", "error")
        return redirect(url_for("ethics.new_ethics_form", form_type=form_type))

    requirement = EthicsFormRequirement(
        student_id=current_user.id,
        form_type=form_type,
        needs_permission=first_payload_value(payload, "need_permission") in {"Yes", "Pending"},
        has_clearance=first_payload_value(payload, "has_clearance") == "Yes",
        has_ethics_evidence=bool(first_payload_value(payload, "prior_clearance", "prior_clearance_path")),
        company_requires_jbs=first_payload_value(payload, "company_requires_jbs") == "Yes",
        proposal_filename=first_payload_value(payload, "proposal_path", "proposal") or None,
        permission_letter_filename=first_payload_value(payload, "permission_letter", "permission_letter_path") or None,
        research_tools_filename=first_payload_value(payload, "research_tools_path") or None,
        impact_assessment_filename=first_payload_value(payload, "impact_assessment_path") or None,
        participation_info_filename=first_payload_value(payload, "participation_info_sheet") or None,
        pending_note_filename=first_payload_value(payload, "pending_note", "pending_note_path") or None,
        payload=payload,
    )
    db.session.add(requirement)
    db.session.flush()

    submission = EthicsFormSubmission(
        student_id=current_user.id,
        supervisor_id=current_user.supervisor_id,
        requirement_id=requirement.id,
        form_type=form_type,
        title=title,
        summary=summary,
        department=first_payload_value(payload, "department") or None,
        degree=first_payload_value(payload, "degree") or "MBA",
        risk_level=risk_level,
        status=EthicsSubmissionStatus.AWAITING_SUPERVISOR.value,
        payload=payload,
        submitted_at=datetime.utcnow(),
    )
    db.session.add(submission)
    db.session.flush()
    link_payload_files_to_submission(submission)
    draft = latest_student_draft(current_user.id, form_type)
    if draft:
        db.session.delete(draft)
    log_activity("submit_ethics_pack", submission, f"Form {form_type} submitted to supervisor")
    db.session.commit()
    flash(f"Form {form_type} submitted to supervisor.", "success")
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))


@ethics_bp.route("/submissions/<int:submission_id>/supervisor", methods=["POST"])
@login_required
def supervisor_action(submission_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    submission = db.session.get(EthicsFormSubmission, submission_id)
    if not submission or submission.supervisor_id != current_user.id:
        flash("Submission not found in your supervisor queue.", "error")
        return redirect_to_role_landing()

    decision = request.form.get("decision")
    comments = (request.form.get("comments") or "").strip()
    submission.supervisor_comments = comments
    submission.supervisor_reviewed_at = datetime.utcnow()
    submission.supervisor_decision = decision

    if decision == "send_back":
        submission.status = EthicsSubmissionStatus.SENT_BACK_BY_SUPERVISOR.value
        flash("Submission sent back to the student.", "success")
    else:
        submission.status = EthicsSubmissionStatus.AWAITING_ADMIN.value
        submission.submitted_to_admin = True
        flash("Submission sent to Ethics Admin.", "success")

    log_activity("supervisor_decision", submission, decision)
    db.session.commit()
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))


@ethics_bp.route("/submissions/<int:submission_id>/assign-reviewers", methods=["POST"])
@login_required
def assign_reviewers(submission_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role not in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        flash("Only Ethics Admin can assign reviewers.", "error")
        return redirect_to_role_landing()

    submission = db.session.get(EthicsFormSubmission, submission_id)
    reviewer_ids = [int(value) for value in request.form.getlist("reviewer_ids") if value.isdigit()]
    if not submission or not reviewer_ids:
        flash("Choose at least one reviewer.", "error")
        return redirect_to_role_landing()

    for reviewer_id in reviewer_ids[:2]:
        reviewer = EthicsUser.query.filter_by(id=reviewer_id, role=EthicsRole.REVIEWER.value).first()
        if reviewer:
            exists = EthicsReviewerAssignment.query.filter_by(
                submission_id=submission.id, reviewer_id=reviewer.id
            ).first()
            if not exists:
                db.session.add(
                    EthicsReviewerAssignment(
                        submission_id=submission.id,
                        reviewer_id=reviewer.id,
                        assigned_by_id=current_user.id,
                    )
                )

    submission.status = EthicsSubmissionStatus.REVIEW_IN_PROGRESS.value
    submission.submitted_to_reviewers = True
    log_activity("assign_reviewers", submission, ",".join(map(str, reviewer_ids[:2])))
    db.session.commit()
    flash("Reviewer assignment saved.", "success")
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))


@ethics_bp.route("/assignments/<int:assignment_id>/review", methods=["POST"])
@login_required
def reviewer_action(assignment_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    assignment = db.session.get(EthicsReviewerAssignment, assignment_id)
    if not assignment or assignment.reviewer_id != current_user.id:
        flash("Review assignment not found.", "error")
        return redirect_to_role_landing()

    recommendation = request.form.get("recommendation") or "approved"
    comments = (request.form.get("comments") or "").strip()
    assignment.recommendation = recommendation
    assignment.comments = comments
    assignment.completed_at = datetime.utcnow()

    submission = assignment.submission
    completed = [a for a in submission.reviewer_assignments if a.completed_at]
    if recommendation == "resubmission_required":
        submission.status = EthicsSubmissionStatus.RESUBMISSION_REQUIRED.value
    elif len(completed) >= max(1, len(submission.reviewer_assignments)):
        if submission.risk_level in {"medium", "high"}:
            submission.status = EthicsSubmissionStatus.AWAITING_REC.value
            submission.submitted_to_rec = True
        elif any(a.recommendation == "approved_with_minor_changes" for a in completed):
            submission.status = EthicsSubmissionStatus.APPROVED_WITH_MINOR_CHANGES.value
        else:
            submission.status = EthicsSubmissionStatus.APPROVED.value

    log_activity("reviewer_decision", submission, recommendation)
    db.session.commit()
    flash("Review submitted.", "success")
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))


@ethics_bp.route("/submissions/<int:submission_id>/rec", methods=["POST"])
@login_required
def rec_action(submission_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role != EthicsRole.REC.value:
        flash("Only REC users can submit REC decisions.", "error")
        return redirect_to_role_landing()

    submission = db.session.get(EthicsFormSubmission, submission_id)
    if not submission:
        flash("Submission not found.", "error")
        return redirect_to_role_landing()

    decision = request.form.get("decision") or "approved"
    submission.rec_status = decision
    submission.rec_comments = (request.form.get("comments") or "").strip()
    submission.rec_decided_at = datetime.utcnow()
    submission.status = (
        EthicsSubmissionStatus.APPROVED.value
        if decision == "approved"
        else EthicsSubmissionStatus.RESUBMISSION_REQUIRED.value
    )
    log_activity("rec_decision", submission, decision)
    db.session.commit()
    flash("REC decision saved.", "success")
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))


@ethics_bp.route("/submissions/<int:submission_id>/certificate", methods=["POST"])
@login_required
def issue_certificate(submission_id):
    if not require_ethics_user():
        return redirect(url_for("auth.login"))
    if current_user.role not in {EthicsRole.ADMIN.value, EthicsRole.SUPER_ADMIN.value, EthicsRole.DEAN.value}:
        flash("Only Ethics Admin can issue certificates.", "error")
        return redirect_to_role_landing()

    submission = db.session.get(EthicsFormSubmission, submission_id)
    if not submission:
        flash("Submission not found.", "error")
        return redirect_to_role_landing()

    years = request.form.get("valid_years", type=int) or 1
    issued = datetime.utcnow()
    submission.certificate_code = f"JBSREC-{issued.year}-{submission.id:05d}"
    submission.certificate_issued_at = issued
    submission.certificate_valid_years = years
    submission.certificate_end_date = issued + timedelta(days=365 * years)
    submission.certificate_issuer = current_user.email
    submission.certificate_heading = request.form.get("heading") or "Ethical Clearance Certificate"
    submission.status = EthicsSubmissionStatus.CERTIFICATE_ISSUED.value
    log_activity("issue_certificate", submission, submission.certificate_code)
    db.session.commit()
    flash("Certificate issued.", "success")
    return redirect(url_for("ethics.submission_detail", submission_id=submission.id))
