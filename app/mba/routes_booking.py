import csv
from datetime import datetime
from io import StringIO

from flask import Response, abort, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy.orm import joinedload

from ..extensions import db
from ..models import (
    MbaBookingDay,
    MbaBookingPanel,
    MbaBookingSettings,
    MbaBookingSlot,
    MbaPanelBooking,
    MbaProject,
    MbaRole,
    MbaUser,
    normalize_email,
)
from .route_support import mba_kpis, mba_bp, require_mba_role, role_landing_url, supervisors_query

BOOKING_STUDENT = "student"
BOOKING_SUPERVISOR = "supervisor"
BOOKING_ACTIVE = "active"
BOOKING_CANCELLED = "cancelled"


def _is_admin():
    return current_user.role in {MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value}


def _booking_settings():
    settings = MbaBookingSettings.query.get(1)
    if settings:
        return settings
    settings = MbaBookingSettings(id=1, is_released=False)
    db.session.add(settings)
    db.session.flush()
    return settings


def _display_name(user):
    if not user:
        return ""
    profile = getattr(user, "student_profile", None) or getattr(user, "scholar_profile", None)
    name = f"{getattr(profile, 'name', '') or user.first_name or ''} {getattr(profile, 'surname', '') or user.last_name or ''}".strip()
    return name or user.email


def _split_name(user):
    profile = getattr(user, "student_profile", None) or getattr(user, "scholar_profile", None)
    first_name = getattr(profile, "name", None) or user.first_name or ""
    surname = getattr(profile, "surname", None) or user.last_name or ""
    if not first_name and not surname and user.email:
        first_name = user.email.split("@", 1)[0]
    return first_name, surname


def _current_booking_role():
    if current_user.role == MbaRole.STUDENT.value:
        return BOOKING_STUDENT
    if current_user.role == MbaRole.SCHOLAR.value and current_user.is_supervisor_role():
        return BOOKING_SUPERVISOR
    return ""


def _student_primary_supervisor_id(user):
    project = (
        MbaProject.query.filter_by(student_id=user.id)
        .filter(MbaProject.primary_supervisor_id.isnot(None))
        .order_by(MbaProject.updated_at.desc())
        .first()
    )
    return project.primary_supervisor_id if project else None


def _supervised_student_ids(supervisor_id):
    return [
        row[0]
        for row in db.session.query(MbaProject.student_id)
        .filter(MbaProject.primary_supervisor_id == supervisor_id)
        .distinct()
        .all()
    ]


def _co_supervisor_from_payload(payload):
    value = payload.get("coSupervisorId") or payload.get("co_supervisor_id") or ""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _serialize_booking(booking):
    return {
        "id": booking.id,
        "userId": booking.user_id,
        "firstName": booking.first_name,
        "surname": booking.surname,
        "name": booking.full_name,
        "email": booking.email,
        "role": booking.role,
        "supervisorId": booking.supervisor_id,
        "supervisor": _display_name(booking.supervisor) if booking.supervisor else "",
        "supervisorName": booking.co_supervisor_name or (_display_name(booking.co_supervisor) if booking.co_supervisor else ""),
        "coSupervisorId": booking.co_supervisor_id,
        "date": booking.day.date.isoformat(),
        "dateDisplay": booking.day.date.strftime("%A %d %b %Y"),
        "panel": booking.panel.name,
        "slot": booking.slot.label,
        "status": booking.status,
        "cancellationReason": booking.cancellation_reason or "",
        "cancelledAt": booking.cancelled_at.isoformat() if booking.cancelled_at else None,
        "bookedAt": booking.booked_at.isoformat() if booking.booked_at else None,
    }


def _active_bookings_query():
    return (
        MbaPanelBooking.query.options(
            joinedload(MbaPanelBooking.day),
            joinedload(MbaPanelBooking.panel),
            joinedload(MbaPanelBooking.slot),
            joinedload(MbaPanelBooking.supervisor),
            joinedload(MbaPanelBooking.co_supervisor),
        )
        .filter(MbaPanelBooking.status == BOOKING_ACTIVE)
        .order_by(MbaPanelBooking.booked_at.desc())
    )


def _booking_rule_violation(user_id, role, day_id, panel_id):
    if role == BOOKING_STUDENT:
        supervisor_id = _student_primary_supervisor_id(current_user)
        if not supervisor_id:
            return False
        return MbaPanelBooking.query.filter_by(
            user_id=supervisor_id,
            role=BOOKING_SUPERVISOR,
            day_id=day_id,
            panel_id=panel_id,
            status=BOOKING_ACTIVE,
        ).first() is not None

    if role == BOOKING_SUPERVISOR:
        student_ids = _supervised_student_ids(user_id)
        if not student_ids:
            return False
        return MbaPanelBooking.query.filter(
            MbaPanelBooking.user_id.in_(student_ids),
            MbaPanelBooking.role == BOOKING_STUDENT,
            MbaPanelBooking.day_id == day_id,
            MbaPanelBooking.panel_id == panel_id,
            MbaPanelBooking.status == BOOKING_ACTIVE,
        ).first() is not None

    return False


def _parse_csv_values(value):
    return [item.strip() for item in (value or "").replace("\r", "\n").replace(",", "\n").split("\n") if item.strip()]


@mba_bp.route("/booking")
@login_required
def booking_page():
    if not require_mba_role(MbaRole.STUDENT.value, MbaRole.SCHOLAR.value, MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        return redirect(role_landing_url())
    if current_user.role == MbaRole.SCHOLAR.value and not current_user.is_supervisor_role():
        return redirect(role_landing_url())
    return render_template("mba/booking.html", kpis=mba_kpis())


@mba_bp.route("/booking/api/me")
@login_required
def booking_me():
    if current_user.system_name != "mba":
        abort(403)
    first_name, surname = _split_name(current_user)
    role = _current_booking_role()
    supervisor_id = _student_primary_supervisor_id(current_user) if role == BOOKING_STUDENT else None
    supervisor = MbaUser.query.get(supervisor_id) if supervisor_id else None
    settings = _booking_settings()
    return jsonify(
        {
            "id": current_user.id,
            "firstName": first_name,
            "surname": surname,
            "email": current_user.email,
            "role": role,
            "isAdmin": _is_admin(),
            "isReleased": bool(settings.is_released),
            "supervisorId": supervisor_id,
            "supervisorName": _display_name(supervisor) if supervisor else "",
            "canBook": role in {BOOKING_STUDENT, BOOKING_SUPERVISOR},
        }
    )


@mba_bp.route("/booking/api/schedule")
@login_required
def booking_schedule():
    if current_user.system_name != "mba":
        abort(403)
    days = (
        MbaBookingDay.query.options(joinedload(MbaBookingDay.panels), joinedload(MbaBookingDay.slots))
        .order_by(MbaBookingDay.date.asc())
        .all()
    )
    payload = []
    for day in days:
        slots = sorted(day.slots, key=lambda item: (item.role, item.sort_order, item.label))
        payload.append(
            {
                "id": day.id,
                "date": day.date.isoformat(),
                "displayDate": day.date.strftime("%A %d %b %Y"),
                "panels": [panel.name for panel in sorted(day.panels, key=lambda item: (item.sort_order, item.name))],
                "studentSlots": [slot.label for slot in slots if slot.role == BOOKING_STUDENT],
                "supervisorSlots": [slot.label for slot in slots if slot.role == BOOKING_SUPERVISOR],
            }
        )
    return jsonify(payload)


@mba_bp.route("/booking/api/bookings", methods=["GET", "POST", "DELETE"])
@login_required
def booking_bookings():
    if current_user.system_name != "mba":
        abort(403)
    settings = _booking_settings()
    if request.method == "GET":
        return jsonify([_serialize_booking(booking) for booking in _active_bookings_query().all()])

    if request.method == "DELETE":
        if not _is_admin():
            abort(403)
        count = MbaPanelBooking.query.delete()
        db.session.commit()
        return jsonify({"deleted": count})

    if not settings.is_released and not _is_admin():
        return jsonify({"message": "Panel booking is locked until MBA Admin releases the page."}), 403

    payload = request.get_json(silent=True) or {}
    role = _current_booking_role()
    if _is_admin():
        role = (payload.get("role") or role).strip().lower()
    if role not in {BOOKING_STUDENT, BOOKING_SUPERVISOR}:
        return jsonify({"message": "Your MBA role cannot create panel bookings."}), 403

    date_value = (payload.get("date") or "").strip()
    panel_name = (payload.get("panel") or "").strip()
    slot_label = (payload.get("slot") or "").strip()
    first_name, surname = _split_name(current_user)
    email = normalize_email(current_user.email)

    if not all([date_value, panel_name, slot_label]):
        return jsonify({"message": "Choose a date, panel and slot."}), 400

    try:
        selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"message": "Select a valid booking date."}), 400
    day = MbaBookingDay.query.filter_by(date=selected_date).first()
    if not day:
        return jsonify({"message": "Select a valid booking date."}), 400
    panel = MbaBookingPanel.query.filter_by(day_id=day.id, name=panel_name).first()
    if not panel:
        return jsonify({"message": "Select a valid panel."}), 400
    slot = MbaBookingSlot.query.filter_by(day_id=day.id, role=role, label=slot_label).first()
    if not slot:
        return jsonify({"message": "Choose a valid slot."}), 400

    if MbaPanelBooking.query.filter_by(day_id=day.id, panel_id=panel.id, role=role, slot_id=slot.id, status=BOOKING_ACTIVE).first():
        return jsonify({"message": "Slot taken."}), 400

    if role == BOOKING_STUDENT and MbaPanelBooking.query.filter_by(user_id=current_user.id, role=role, status=BOOKING_ACTIVE).first():
        return jsonify({"message": "Students may only have one active booking."}), 400
    if role == BOOKING_SUPERVISOR and MbaPanelBooking.query.filter_by(
        user_id=current_user.id,
        role=role,
        day_id=day.id,
        panel_id=panel.id,
        status=BOOKING_ACTIVE,
    ).first():
        return jsonify({"message": "You are already booked on this panel. Supervisors may book on other panels."}), 400

    if _booking_rule_violation(current_user.id, role, day.id, panel.id):
        return jsonify({"message": "Supervisors cannot be on the same panel as their own student."}), 400

    supervisor_id = _student_primary_supervisor_id(current_user) if role == BOOKING_STUDENT else None
    co_supervisor_id = _co_supervisor_from_payload(payload)
    co_supervisor = MbaUser.query.get(co_supervisor_id) if co_supervisor_id else None
    booking = MbaPanelBooking(
        user_id=current_user.id,
        first_name=first_name or "MBA",
        surname=surname or "User",
        email=email,
        role=role,
        supervisor_id=supervisor_id,
        co_supervisor_id=co_supervisor.id if co_supervisor else None,
        co_supervisor_name=_display_name(co_supervisor) if co_supervisor else (payload.get("coSupervisorName") or "").strip() or None,
        day_id=day.id,
        panel_id=panel.id,
        slot_id=slot.id,
    )
    db.session.add(booking)
    db.session.commit()
    return jsonify(_serialize_booking(booking)), 201


@mba_bp.route("/booking/api/system-counts")
@login_required
def booking_system_counts():
    if current_user.system_name != "mba":
        abort(403)
    return jsonify(
        {
            "students": MbaUser.query.filter_by(role=MbaRole.STUDENT.value, is_active=True).count(),
            "supervisors": supervisors_query().count(),
        }
    )


@mba_bp.route("/booking/api/supervisors/search")
@login_required
def booking_search_supervisors():
    if current_user.system_name != "mba":
        abort(403)
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:
        return jsonify({"results": []})
    like = f"%{query}%"
    supervisors = supervisors_query().filter(
        (MbaUser.email.ilike(like)) | (MbaUser.first_name.ilike(like)) | (MbaUser.last_name.ilike(like))
    ).limit(20).all()
    results = [{"id": user.id, "name": _display_name(user), "email": user.email} for user in supervisors]
    return jsonify({"results": results})


@mba_bp.route("/booking/export")
@login_required
def booking_export():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        abort(403)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["First Name", "Surname", "Email", "Role", "Supervisor", "Co-Supervisor", "Date", "Panel", "Slot", "Booked At", "Status"])
    for booking in _active_bookings_query().all():
        writer.writerow(
            [
                booking.first_name,
                booking.surname,
                booking.email,
                booking.role,
                _display_name(booking.supervisor) if booking.supervisor else "",
                booking.co_supervisor_name or (_display_name(booking.co_supervisor) if booking.co_supervisor else ""),
                booking.day.date.isoformat(),
                booking.panel.name,
                booking.slot.label,
                booking.booked_at.isoformat() if booking.booked_at else "",
                booking.status,
            ]
        )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=mba_panel_bookings.csv"},
    )


@mba_bp.route("/booking/admin/schedule", methods=["POST"])
@login_required
def booking_admin_schedule():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        abort(403)
    date_value = (request.form.get("date") or "").strip()
    try:
        date_obj = datetime.strptime(date_value, "%Y-%m-%d").date()
    except ValueError:
        abort(400, description="Enter a valid date.")

    panels = _parse_csv_values(request.form.get("panels")) or ["Panel 1"]
    student_slots = _parse_csv_values(request.form.get("student_slots")) or ["09:00"]
    supervisor_slots = _parse_csv_values(request.form.get("supervisor_slots")) or ["Slot 1"]
    day = MbaBookingDay.query.filter_by(date=date_obj).first()
    if not day:
        day = MbaBookingDay(date=date_obj, created_by_id=current_user.id)
        db.session.add(day)
        db.session.flush()

    existing_panels = {panel.name.lower(): panel for panel in day.panels}
    for index, name in enumerate(panels, start=1):
        panel = existing_panels.get(name.lower())
        if panel:
            panel.sort_order = index
        else:
            db.session.add(MbaBookingPanel(day_id=day.id, name=name, sort_order=index))

    existing_slots = {(slot.role, slot.label.lower()): slot for slot in day.slots}
    for role, labels in ((BOOKING_STUDENT, student_slots), (BOOKING_SUPERVISOR, supervisor_slots)):
        for index, label in enumerate(labels, start=1):
            slot = existing_slots.get((role, label.lower()))
            if slot:
                slot.sort_order = index
            else:
                db.session.add(MbaBookingSlot(day_id=day.id, role=role, label=label, sort_order=index))

    db.session.commit()
    return redirect(url_for("mba.booking_page"))


@mba_bp.route("/booking/admin/release", methods=["POST"])
@login_required
def booking_admin_release():
    if not require_mba_role(MbaRole.ADMIN.value, MbaRole.MAIN_ADMIN.value):
        abort(403)
    settings = _booking_settings()
    settings.is_released = request.form.get("is_released") == "1"
    settings.released_at = datetime.utcnow() if settings.is_released else None
    settings.released_by_id = current_user.id if settings.is_released else None
    db.session.commit()
    return redirect(url_for("mba.booking_page"))


@mba_bp.route("/booking/<int:booking_id>/cancel", methods=["POST"])
@login_required
def booking_cancel(booking_id):
    if current_user.system_name != "mba":
        abort(403)
    booking = MbaPanelBooking.query.get_or_404(booking_id)
    if not _is_admin() and booking.user_id != current_user.id:
        abort(403)
    booking.status = BOOKING_CANCELLED
    payload = request.get_json(silent=True) or {} if request.is_json else {}
    booking.cancellation_reason = (payload.get("reason") if request.is_json else request.form.get("reason")) or ""
    booking.cancelled_at = datetime.utcnow()
    db.session.commit()
    return jsonify(_serialize_booking(booking)) if request.is_json else redirect(url_for("mba.booking_page"))
