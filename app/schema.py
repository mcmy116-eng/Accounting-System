"""Lightweight, idempotent schema bootstrap.

The project has no Alembic migration chain — historically the schema was created
once by ``seed.py`` via ``db.create_all()``. As the model grows we still need
new tables and the occasional new column to appear on already-deployed
databases (SQLite locally, Postgres in production) without a manual migration.

``ensure_schema()`` is safe to run on every app start:
  * ``create_all()`` creates any missing tables (never touches existing ones).
  * ``_ensure_columns()`` adds simple nullable columns that create_all won't add
    to a pre-existing table.
  * ``_ensure_reimbursement_account()`` makes sure the staff-reimbursement
    liability account the claims workflow posts against exists.

Only additive, non-destructive operations belong here.
"""
from sqlalchemy import inspect, text

from app.extensions import db

# table -> list of (column_name, DDL type) that may be missing on older DBs.
_ADDITIVE_COLUMNS = {
    "documents": [
        ("claim_id", "INTEGER"),
        ("journal_entry_id", "INTEGER"),
        ("sha256", "VARCHAR(64)"),
        ("drive_file_id", "VARCHAR(200)"),
    ],
    "bank_transactions": [("source_key", "VARCHAR(128)")],
    "synced_orders": [("source_snapshot", "TEXT")],
}

# Liability account the reimbursement workflow credits when a claim is approved.
REIMBURSEMENT_ACCOUNT_CODE = "2550"
REIMBURSEMENT_ACCOUNT_NAME = "Employee Reimbursements Payable"


def _ensure_columns():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in existing_tables:
            continue  # create_all will have made it with the full, current shape
        have = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl_type in columns:
            if name not in have:
                db.session.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl_type}'))
    db.session.commit()


def _ensure_reimbursement_account():
    from app.models import Account
    if not Account.query.filter_by(code=REIMBURSEMENT_ACCOUNT_CODE).first():
        db.session.add(Account(
            code=REIMBURSEMENT_ACCOUNT_CODE,
            name=REIMBURSEMENT_ACCOUNT_NAME,
            type="liability",
            system_locked=True,
        ))
        db.session.commit()


def _bootstrap_schema(app):
    with app.app_context():
        db.create_all()
        _ensure_columns()
        from app.models import LedgerControl
        if not db.session.get(LedgerControl, 1):
            db.session.add(LedgerControl(id=1))
            db.session.commit()
        # Only create the reimbursement account if the chart already exists
        # (i.e. the DB has been seeded); otherwise seed.py will add it.
        from app.models import Account
        if Account.query.first():
            _ensure_reimbursement_account()


def ensure_schema(app):
    # Gunicorn workers can start together. Serialize additive PostgreSQL DDL
    # with a session advisory lock; use a separate connection for its lifetime.
    with app.app_context():
        if db.engine.dialect.name == 'postgresql':
            with db.engine.connect() as conn:
                conn.execute(text('SELECT pg_advisory_lock(87260422)'))
                try:
                    _bootstrap_schema(app)
                finally:
                    conn.execute(text('SELECT pg_advisory_unlock(87260422)'))
        else:
            _bootstrap_schema(app)
