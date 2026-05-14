import secrets

from flask import abort, current_app, has_request_context, request, session

CSRF_SESSION_KEY = "_csrf_token"
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


def _csrf_enabled():
    return current_app.config.get("WTF_CSRF_ENABLED", True)


def generate_csrf_token():
    if not has_request_context():
        return ""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def validate_csrf_request():
    if not _csrf_enabled():
        return
    if request.method in SAFE_HTTP_METHODS:
        return

    expected = session.get(CSRF_SESSION_KEY)
    provided = (
        request.form.get("_csrf_token")
        or request.headers.get("X-CSRF-Token")
        or request.headers.get("X-CSRFToken")
    )
    if not expected or not provided or not secrets.compare_digest(str(expected), str(provided)):
        abort(400, description="Missing or invalid CSRF token.")


def init_csrf(app):
    @app.before_request
    def _csrf_before_request():
        validate_csrf_request()

    @app.context_processor
    def _inject_csrf_token():
        return {"csrf_token": generate_csrf_token}
