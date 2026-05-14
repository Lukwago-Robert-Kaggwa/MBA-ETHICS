from .extensions import db, login_manager
from .models import EthicsUser, MbaUser


@login_manager.user_loader
def load_user(user_id):
    try:
        system, raw_id = user_id.split(":", 1)
        model = MbaUser if system == "mba" else EthicsUser if system == "ethics" else None
        return db.session.get(model, int(raw_id)) if model else None
    except (TypeError, ValueError):
        return None
