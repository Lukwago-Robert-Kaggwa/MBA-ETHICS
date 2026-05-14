from .extensions import oauth


def configure_microsoft_oauth(app):
    if not app.config["MICROSOFT_CLIENT_ID"] or not app.config["MICROSOFT_CLIENT_SECRET"]:
        return

    tenant_id = app.config["MICROSOFT_TENANT_ID"]
    oauth.register(
        name="microsoft",
        client_id=app.config["MICROSOFT_CLIENT_ID"],
        client_secret=app.config["MICROSOFT_CLIENT_SECRET"],
        server_metadata_url=f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile User.Read"},
    )
