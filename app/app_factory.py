from flask import Flask, redirect, request, url_for
from flask_login import current_user

from config import Config

from .auth import auth_bp, user_has_popia_confirmation
from .cli_commands import register_cli
from .context_processors import inject_auth_flags_factory
from .ethics.routes import ethics_bp
from .extensions import db, login_manager, migrate, oauth
from .mba.routes import mba_bp
from .oauth_config import configure_microsoft_oauth
from .security import init_csrf


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    oauth.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    init_csrf(app)

    configure_microsoft_oauth(app)
    register_cli(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(mba_bp, url_prefix="/mba")
    app.register_blueprint(ethics_bp, url_prefix="/ethics")

    app.context_processor(inject_auth_flags_factory(app))

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

    @app.route("/")
    def index():
        return redirect(url_for("mba.dashboard"))

    return app
