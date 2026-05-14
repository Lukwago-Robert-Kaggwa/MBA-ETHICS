import os

from app import create_app

app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "False").lower() in {"1", "true", "yes"}
    app.run(debug=debug, use_reloader=False)
