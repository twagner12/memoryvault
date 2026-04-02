"""MemoryVault Web UI — Flask app factory."""

from pathlib import Path

from flask import Flask, g

from memoryvault.database import Database
from memoryvault.web.tasks import TaskManager


def create_app(db_path="memoryvault.db"):
    app = Flask(__name__,
                static_folder="static",
                template_folder="templates")
    app.secret_key = "memoryvault-local-desktop"
    app.config["DB_PATH"] = Path(db_path).resolve()
    app.config["THUMB_DIR"] = Path(db_path).resolve().parent / ".thumbnails"
    app.config["THUMB_DIR"].mkdir(exist_ok=True)
    app.config["TASK_MANAGER"] = TaskManager()

    from .blueprints.dashboard import bp as dashboard_bp
    from .blueprints.sources import bp as sources_bp
    from .blueprints.battle import bp as battle_bp
    from .blueprints.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(sources_bp, url_prefix="/sources")
    app.register_blueprint(battle_bp, url_prefix="/battle")
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.teardown_appcontext
    def close_db(exc):
        db = g.pop("db", None)
        if db:
            db.close()

    return app


def get_db():
    """Get a Database instance for the current request."""
    if "db" not in g:
        from flask import current_app
        g.db = Database(current_app.config["DB_PATH"])
    return g.db
