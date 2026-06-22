from urllib.parse import urlsplit

from flask import Flask, flash, redirect, request, session, url_for
from flask_login import current_user

from config import Config

from .auth import auth_bp, user_has_popia_confirmation
from .cli_commands import register_cli
from .context_processors import inject_auth_flags_factory
from .ethics.routes import ethics_bp
from .extensions import db, limiter, login_manager, migrate, oauth
from .mba.routes import mba_bp
from .oauth_config import configure_microsoft_oauth
from .security import init_csrf
from .security_headers import init_security_headers


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    oauth.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    limiter.init_app(app)
    init_csrf(app)
    init_security_headers(app)

    configure_microsoft_oauth(app)
    register_cli(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(mba_bp, url_prefix="/mba")
    app.register_blueprint(ethics_bp, url_prefix="/ethics")

    app.context_processor(inject_auth_flags_factory(app))

    @app.before_request
    def refresh_idle_session_timeout():
        if not current_user.is_authenticated:
            return None
        if request.endpoint == "auth.session_status":
            return None
        session.permanent = True
        session.modified = True
        return None

    @app.before_request
    def require_popia_confirmation_before_system_access():
        if not current_user.is_authenticated:
            return None
        endpoint = request.endpoint or ""
        if endpoint == "static" or endpoint in {"auth.popia_notice", "auth.logout"}:
            return None
        if user_has_popia_confirmation(current_user):
            return None
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.popia_notice", next=next_url))

    @app.after_request
    def honor_posted_next_redirect(response):
        # A project's standalone "new tab" detail page injects a hidden `next`
        # field into every form (see base.html) so that submitting an action
        # from inside that tab returns to the same detail page instead of the
        # action's normal default (the role's project list).
        if request.method != "POST" or not (300 <= response.status_code < 400):
            return response
        next_path = request.form.get("next")
        if not next_path:
            return response
        parsed = urlsplit(next_path)
        if parsed.scheme or parsed.netloc or not next_path.startswith("/") or next_path.startswith("//"):
            return response
        response.headers["Location"] = next_path
        return response

    @app.route("/")
    def index():
        return redirect(url_for("mba.dashboard"))

    @app.errorhandler(413)
    def _handle_request_entity_too_large(error):
        flash("The file you uploaded is too large. Please upload a smaller file.", "error")
        referrer = request.referrer
        if referrer and referrer.startswith(request.host_url):
            return redirect(referrer)
        return redirect(url_for("mba.dashboard"))

    return app
