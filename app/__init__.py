import os
from flask import Flask
from app.extensions import db, login_manager


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    if os.environ.get('RENDER') and (not os.environ.get('SECRET_KEY') or os.environ['SECRET_KEY']=='dev-secret-change-me'):
        raise RuntimeError('Production requires a private SECRET_KEY in service Secrets.')
    basedir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    db_path = os.environ.get("DATABASE_PATH", os.path.join(basedir, "instance", "accounting.db"))
    database_url = os.environ.get("DATABASE_URL", f"sqlite:///{db_path}")
    # Railway/Render/Heroku-style providers hand out "postgres://", but SQLAlchemy 2.x requires "postgresql://"
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["UPLOAD_FOLDER"] = os.environ.get(
        "UPLOAD_FOLDER", os.path.join(basedir, "uploads")
    )
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB uploads

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        user = db.session.get(User, int(user_id))
        return user if user and user.active else None

    from app.blueprints.auth import bp as auth_bp
    from app.blueprints.dashboard import bp as dashboard_bp
    from app.blueprints.accounts import bp as accounts_bp
    from app.blueprints.contacts import bp as contacts_bp
    from app.blueprints.invoices import bp as invoices_bp
    from app.blueprints.bills import bp as bills_bp
    from app.blueprints.payments import bp as payments_bp
    from app.blueprints.bank import bp as bank_bp
    from app.blueprints.reports import bp as reports_bp
    from app.blueprints.settings import bp as settings_bp
    from app.blueprints.claims import bp as claims_bp
    from app.blueprints.integrations import bp as integrations_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(contacts_bp)
    app.register_blueprint(invoices_bp)
    app.register_blueprint(bills_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(bank_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(claims_bp)
    app.register_blueprint(integrations_bp)
    from app.blueprints.books import bp as books_bp
    app.register_blueprint(books_bp)

    from app.controls import configure_controls
    configure_controls(app)

    from app.models import money as money_fn
    app.jinja_env.filters["money"] = money_fn

    @app.context_processor
    def inject_globals():
        from app.models import CompanySettings
        settings = CompanySettings.query.first()
        return {"company_settings": settings}

    # Idempotently bring the schema up to date (new tables / columns) so deploys
    # don't need a manual migration step. Safe to run on every start.
    from app.schema import ensure_schema
    ensure_schema(app)

    return app
