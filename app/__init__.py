from . import auth_loader  # noqa: F401
from .app_factory import create_app
from .cli_commands import register_cli
from .oauth_config import configure_microsoft_oauth
from .seeds import seed_mba_disciplines

__all__ = [
    "configure_microsoft_oauth",
    "create_app",
    "register_cli",
    "seed_mba_disciplines",
]
