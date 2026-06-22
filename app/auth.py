import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from .extensions import db, limiter, oauth
from .models import (
    EthicsActivityLog,
    EthicsRole,
    EthicsUser,
    MbaStudentProfile,
    MbaRole,
    MbaScholarRole,
    MbaUser,
    UJ_STUDENT_EMAIL_RE,
    is_uj_student_email,
    normalize_email,
    student_email_for,
)
from .mail import send_email
from .password_policy import validate_password_strength
from .supervisor_sync import sync_ethics_supervisor_from_mba

auth_bp = Blueprint("auth", __name__)
POPIA_NOTICE_VERSION = "2026-05-12"
RESET_TOKEN_TTL_MINUTES = 30


def find_registered_user(email, system=None):
    clean_email = normalize_email(email)
    if system == "mba":
        return MbaUser.find_by_email(clean_email)
    if system == "ethics":
        return EthicsUser.find_by_email(clean_email)
    return MbaUser.find_by_email(clean_email) or EthicsUser.find_by_email(clean_email)


def find_mba_profile_by_student_number(student_number):
    clean_number = (student_number or "").strip()
    if not clean_number:
        return None
    return MbaStudentProfile.query.filter_by(student_number=clean_number).first()


def looks_like_email(email):
    return bool(email and "@" in email and "." in email.rsplit("@", 1)[-1])


def _eligible_mba_password_reset_user(user):
    if not user or not user.is_active:
        return False
    if user.role in {
        MbaRole.STUDENT.value,
        MbaRole.EXAMINER.value,
        MbaRole.HDC.value,
        MbaRole.ADMIN.value,
        MbaRole.MAIN_ADMIN.value,
    }:
        return True
    return user.role == MbaRole.SCHOLAR.value and user.scholar_role in {
        MbaScholarRole.EXAMINER.value,
        MbaScholarRole.SUPERVISOR.value,
        MbaScholarRole.BOTH.value,
    }


def _eligible_ethics_password_reset_user(user):
    if not user or not user.is_active:
        return False
    return user.role in {
        EthicsRole.STUDENT.value,
        EthicsRole.SUPERVISOR.value,
        EthicsRole.ADMIN.value,
        EthicsRole.SUPER_ADMIN.value,
    }


def _password_reset_users(email):
    clean_email = normalize_email(email)
    users = []

    mba_user = MbaUser.find_by_email(clean_email)
    if _eligible_mba_password_reset_user(mba_user):
        users.append(mba_user)

    ethics_user = EthicsUser.find_by_email(clean_email)
    if _eligible_ethics_password_reset_user(ethics_user):
        users.append(ethics_user)

    return users


def _hash_reset_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _issue_reset_token(user):
    raw_token = secrets.token_urlsafe(32)
    user.reset_token_hash = _hash_reset_token(raw_token)
    user.reset_token_expires_at = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    return raw_token


def _find_user_by_reset_token(raw_token):
    if not raw_token:
        return None
    token_hash = _hash_reset_token(raw_token)
    for model in (MbaUser, EthicsUser):
        user = model.query.filter_by(reset_token_hash=token_hash).first()
        if user and user.reset_token_expires_at and user.reset_token_expires_at >= datetime.utcnow():
            return user
    return None


def _password_reset_email_body(email, raw_tokens_by_user):
    lines = [
        "Hello,",
        "",
        f"You requested a password reset. The link(s) below expire in {RESET_TOKEN_TTL_MINUTES} minutes and can only be used once.",
        "",
    ]
    for user, raw_token in raw_tokens_by_user.items():
        reset_url = url_for("auth.reset_password", token=raw_token, _external=True)
        system_label = "MBA" if user.system_name == "mba" else "Ethics"
        lines.append(f"{system_label} account ({email}): {reset_url}")
    lines += ["", "If you did not request this, please ignore this email or contact support."]
    return "\n".join(lines)


def user_has_popia_confirmation(user):
    return bool(getattr(user, "popia_confirmed_at", None))


def _safe_internal_next_url(target):
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return None
    if target.startswith(url_for("auth.popia_notice")) or target.startswith(url_for("auth.logout")):
        return None
    return target


def post_login_url(user):
    if user.system_name == "ethics":
        return url_for("ethics.dashboard")
    return url_for("mba.dashboard")


def post_login_redirect(user):
    if not user_has_popia_confirmation(user):
        return redirect(url_for("auth.popia_notice"))
    return redirect(post_login_url(user))


def log_ethics_auth_activity(user, action, details=None):
    if user and user.system_name == "ethics":
        db.session.add(EthicsActivityLog(user_id=user.id, action=action, details=details))


@auth_bp.route("/login", methods=["GET", "POST"])
@limiter.limit("10/minute")
def login():
    if current_user.is_authenticated:
        return post_login_redirect(current_user)

    if request.method == "POST":
        system = request.form.get("system", "mba").lower()
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password") or ""
        user = find_registered_user(email, system)

        if not user or not user.check_password(password):
            flash("Invalid email or password.", "error")
            return render_template("auth/login.html", email=email, system=system)

        if not user.is_active:
            flash("This account is inactive. Contact an administrator.", "error")
            return render_template("auth/login.html", email=email, system=system)

        login_user(user)
        session.permanent = True
        log_ethics_auth_activity(user, "login", "Email and password sign-in")
        db.session.commit()
        return post_login_redirect(user)

    return render_template("auth/login.html")


@auth_bp.route("/forgot-password", methods=["POST"])
@limiter.limit("5/hour")
def forgot_password():
    if current_user.is_authenticated:
        return post_login_redirect(current_user)

    generic_message = "If an account with that email exists, you will receive a password reset email."
    email = normalize_email(request.form.get("reset_email") or "")

    if looks_like_email(email):
        users = _password_reset_users(email)
        if users:
            raw_tokens = {user: _issue_reset_token(user) for user in users}
            for user in users:
                log_ethics_auth_activity(user, "password_reset_requested", "Password reset link issued")

            try:
                sent = send_email(email, "Reset Your Password", _password_reset_email_body(email, raw_tokens))
                if not sent:
                    raise RuntimeError("Mail delivery is not configured.")
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Password reset email failed for %s", email)

    flash(generic_message, "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("20/hour")
def reset_password(token):
    if current_user.is_authenticated:
        return post_login_redirect(current_user)

    user = _find_user_by_reset_token(token)
    if not user:
        flash("This password reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if new_password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("auth/reset_password.html", token=token)

        policy_error = validate_password_strength(new_password, email=user.email)
        if policy_error:
            flash(policy_error, "error")
            return render_template("auth/reset_password.html", token=token)

        user.set_password(new_password)
        user.reset_token_hash = None
        user.reset_token_expires_at = None
        log_ethics_auth_activity(user, "password_reset_completed", "Password reset completed via emailed link")
        db.session.commit()
        flash("Your password has been updated. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)


@auth_bp.route("/popia-notice", methods=["GET", "POST"])
@login_required
def popia_notice():
    next_url = _safe_internal_next_url(request.values.get("next"))
    if user_has_popia_confirmation(current_user):
        return redirect(next_url or post_login_url(current_user))

    if request.method == "POST":
        if request.form.get("popia_confirmed") != "yes":
            flash("Please confirm the POPIA notice before continuing.", "error")
            return render_template(
                "auth/popia_notice.html",
                notice_version=POPIA_NOTICE_VERSION,
                next_url=next_url,
            )

        forwarded_for = request.headers.get("X-Forwarded-For", "")
        remote_ip = (forwarded_for.split(",", 1)[0].strip() or request.remote_addr or "")[:64]
        current_user.popia_confirmed_at = datetime.utcnow()
        current_user.popia_notice_version = POPIA_NOTICE_VERSION
        current_user.popia_confirmed_ip = remote_ip
        current_user.popia_confirmed_user_agent = (request.headers.get("User-Agent") or "")[:255]
        log_ethics_auth_activity(current_user, "popia_confirmed", f"POPIA notice {POPIA_NOTICE_VERSION} confirmed")
        db.session.commit()
        flash("POPIA notice confirmed. Thank you.", "success")
        return redirect(next_url or post_login_url(current_user))

    return render_template(
        "auth/popia_notice.html",
        notice_version=POPIA_NOTICE_VERSION,
        next_url=next_url,
    )


@auth_bp.route("/register", methods=["GET", "POST"])
@limiter.limit("10/hour")
def register():
    if current_user.is_authenticated:
        return post_login_redirect(current_user)

    system = "mba"

    if request.method == "POST":
        submitted_student_email = normalize_email(request.form.get("student_email") or "")
        legacy_student_number = request.form.get("student_number", "").strip()
        if submitted_student_email:
            email = submitted_student_email
            match = UJ_STUDENT_EMAIL_RE.match(email)
            student_number = match.group("number") if match else ""
        else:
            student_number = legacy_student_number
            email = student_email_for(student_number)
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not email:
            flash("Student email address is required.", "error")
            return render_template("auth/register.html", system=system)

        if not looks_like_email(email):
            flash("Enter a valid student email address.", "error")
            return render_template("auth/register.html", system=system, student_email=submitted_student_email)

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return render_template("auth/register.html", system=system, student_email=email)

        policy_error = validate_password_strength(password, email=email)
        if policy_error:
            flash(policy_error, "error")
            return render_template("auth/register.html", system=system, student_email=email)

        if find_registered_user(email, system):
            flash("An account already exists for that student email.", "error")
            return render_template("auth/register.html", system=system, student_email=email)

        if system == "mba" and find_mba_profile_by_student_number(student_number):
            flash("An MBA account already exists for that student email address.", "error")
            return render_template("auth/register.html", system=system, student_email=email)

        user = (
            MbaUser(email=email)
            if system == "mba"
            else EthicsUser(email=email, student_number=student_number or None, authenticated_student=True)
        )
        user.set_password(password)
        db.session.add(user)
        db.session.flush()

        if system == "mba":
            db.session.add(MbaStudentProfile(user_id=user.id, student_number=student_number or None))

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An account already exists for that student email address.", "error")
            return render_template("auth/register.html", system=system, student_email=email)
        login_user(user)
        session.permanent = True
        log_ethics_auth_activity(user, "register", "Student registered an Ethics account")
        db.session.commit()
        return post_login_redirect(user)

    return render_template("auth/register.html", system=system)


@auth_bp.route("/auth/microsoft")
def microsoft_login():
    if "microsoft" not in oauth._clients:
        flash("Microsoft sign-in is currently unavailable. Use email and password to sign in.", "error")
        return redirect(url_for("auth.login"))

    system = request.args.get("system", "mba")
    session["microsoft_login_system"] = system if system in {"mba", "ethics"} else "mba"
    redirect_uri = current_app.config["MICROSOFT_REDIRECT_URI"] or url_for("auth.microsoft_callback", _external=True)
    return oauth.microsoft.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/microsoft/callback")
def microsoft_callback():
    if "microsoft" not in oauth._clients:
        flash("Microsoft login is not configured.", "error")
        return redirect(url_for("auth.login"))

    token = oauth.microsoft.authorize_access_token()
    userinfo = token.get("userinfo") or oauth.microsoft.userinfo(token=token)
    email = normalize_email(userinfo.get("email") or userinfo.get("preferred_username") or userinfo.get("upn"))
    subject = userinfo.get("sub") or userinfo.get("oid")

    if not email:
        flash("Microsoft did not return an email address.", "error")
        return redirect(url_for("auth.login"))

    requested_system = session.pop("microsoft_login_system", "mba")
    user = find_registered_user(email, requested_system)
    if not user and requested_system == "mba" and is_uj_student_email(email):
        student_number = email.split("@", 1)[0]
        if find_mba_profile_by_student_number(student_number):
            flash("An MBA account already exists for that student email address.", "error")
            return redirect(url_for("auth.login"))
        user = MbaUser(
            email=email,
            microsoft_subject=subject,
            role=MbaRole.STUDENT.value,
            has_profile=True,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(MbaStudentProfile(user_id=user.id, student_number=student_number))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An MBA account already exists for that student email address.", "error")
            return redirect(url_for("auth.login"))

    if not user:
        flash("Your Microsoft account email is not registered in this system.", "error")
        return redirect(url_for("auth.login"))

    if not user.is_active:
        flash("This account is inactive. Contact an administrator.", "error")
        return redirect(url_for("auth.login"))

    user.microsoft_subject = subject
    log_ethics_auth_activity(user, "login", "Microsoft sign-in")
    db.session.commit()
    login_user(user)
    session.permanent = True
    return post_login_redirect(user)


@auth_bp.route("/switch/ethics")
@login_required
def switch_to_ethics():
    if current_user.system_name == "ethics":
        return redirect(url_for("ethics.dashboard"))

    email = normalize_email(current_user.email)
    mba_user = current_user
    ethics_user = EthicsUser.find_by_email(email)
    if not ethics_user:
        if current_user.role != MbaRole.STUDENT.value:
            flash("Only student MBA accounts can be automatically linked into Ethics.", "error")
            return redirect(url_for("mba.dashboard"))
        student_profile = getattr(current_user, "student_profile", None)
        student_number = student_profile.student_number if student_profile else None
        ethics_user = EthicsUser(email=email, student_number=student_number, authenticated_student=True)
        db.session.add(ethics_user)

    synced_supervisor = sync_ethics_supervisor_from_mba(ethics_user, mba_user)
    db.session.commit()
    if synced_supervisor:
        flash("Your MBA supervisor is already linked in Ethics.", "success")

    logout_user()
    login_user(ethics_user)
    session.permanent = True
    log_ethics_auth_activity(ethics_user, "switch_from_mba", "User opened Ethics from MBA")
    db.session.commit()
    return redirect(url_for("ethics.dashboard"))


@auth_bp.route("/logout")
def logout():
    if current_user.is_authenticated and current_user.system_name == "ethics":
        log_ethics_auth_activity(current_user, "logout", "User signed out")
        db.session.commit()
    session.clear()
    logout_user()
    return redirect(url_for("auth.login"))


@auth_bp.route("/session-status")
def session_status():
    return jsonify(authenticated=current_user.is_authenticated)
