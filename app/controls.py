"""Shared authorization, CSRF and serialized, replay-safe mutations."""
import hashlib
import secrets
from pathlib import Path
from flask import abort, current_app, redirect, request, session, url_for, flash
from flask_login import current_user, logout_user
from sqlalchemy import text
from app.extensions import db


def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


def request_id():
    return secrets.token_urlsafe(24)


def lock_books():
    """All application writes share this DB transaction lock, including close/export.

    SQLite uses its writer lock; PostgreSQL locks the singleton control row.
    Never commit the lock separately from the business transaction.
    """
    from app.models import LedgerControl
    if db.engine.dialect.name == 'sqlite':
        db.session.execute(text('BEGIN IMMEDIATE'))
    else:
        db.session.query(LedgerControl).filter_by(id=1).with_for_update().one()


def configure_controls(app):
    app.jinja_env.globals.update(csrf_token=csrf_token, request_id=request_id)
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=bool(__import__('os').environ.get('RENDER')))

    @app.before_request
    def guard():
        from app.models import MutationReceipt
        if current_user.is_authenticated and not current_user.active:
            logout_user()
            session.clear()
            return redirect(url_for('auth.login'))
        if current_user.is_authenticated:
            if current_user.is_staff_only() and request.blueprint not in ('claims', 'auth', 'static'):
                if request.path == '/':
                    return redirect(url_for('claims.index'))
                abort(403)
            if (request.method not in ('GET', 'HEAD', 'OPTIONS') and
                    not current_user.can_edit() and request.blueprint != 'auth'):
                if not (current_user.is_staff_only() and request.blueprint == 'claims'):
                    abort(403)
            if request.method == 'GET' and request.blueprint in ('reports','books','accounts'):
                # All balances within a rendered statement observe one write-free
                # interval, including SQLite (whose default SELECT is autocommit).
                lock_books()
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            supplied = request.form.get('_csrf') or request.headers.get('X-CSRF-Token', '')
            expected = session.get('_csrf', '')
            if not supplied or not expected or not secrets.compare_digest(supplied, expected):
                abort(400, 'Form expired or CSRF token missing. Reload the form and try again.')
            if current_user.is_authenticated and request.endpoint not in ('auth.login', 'auth.logout'):
                key = request.form.get('_request_id') or request.headers.get('Idempotency-Key', '')
                if not key or len(key) > 128:
                    abort(400, 'A form request ID is required. Reload the form.')
                lock_books()
                db.session.refresh(current_user._get_current_object())
                if not current_user.active:
                    abort(403, 'This account was deactivated. No change was saved.')
                digest = hashlib.sha256(f'{current_user.id}:{request.endpoint}:{key}'.encode()).hexdigest()
                if db.session.get(MutationReceipt, digest):
                    abort(409, 'This request was already processed. Refresh to see the saved result.')
                # The route commits this receipt atomically with its business changes.
                db.session.add(MutationReceipt(key=digest, user_id=current_user.id, endpoint=request.endpoint))
                db.session.flush()

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(409)
    def expected_error(error):
        from flask import render_template
        db.session.rollback()
        return render_template('error.html', message=error.description, code=error.code), error.code

    @app.errorhandler(ValueError)
    @app.errorhandler(ArithmeticError)
    def bad_input(error):
        db.session.rollback()
        from flask import render_template
        return render_template('error.html', code=400, message='Invalid date, amount or selection. Please check the form.'), 400

    from sqlalchemy.exc import IntegrityError
    @app.errorhandler(IntegrityError)
    def conflict(error):
        db.session.rollback()
        from flask import render_template
        return render_template('error.html', code=409, message='This conflicts with an existing record. Refresh and review before retrying.'), 409

    from sqlalchemy.exc import DataError
    @app.errorhandler(DataError)
    def invalid_storage_value(error):
        db.session.rollback()
        from flask import render_template
        return render_template('error.html',code=400,message='A field exceeds its supported size or format. Check the entered values.'),400

    from app.ledger import LedgerError
    @app.errorhandler(LedgerError)
    def ledger_error(error):
        db.session.rollback()
        from flask import render_template
        return render_template('error.html', code=409, message=str(error)), 409

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'DENY'
        if current_user.is_authenticated:
            response.headers['Cache-Control'] = 'no-store'
        return response


def document_response(doc):
    """One policy for every document URL; never trust a different route's owner."""
    from flask import send_file
    from app.models import ExpenseClaim
    if current_user.is_staff_only():
        claim = db.session.get(ExpenseClaim, doc.claim_id) if doc.claim_id else None
        if not claim or claim.staff_id != current_user.id:
            abort(403)
    root = Path(current_app.config['UPLOAD_FOLDER']).resolve()
    path = Path(doc.filepath).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    return send_file(path, mimetype=doc.mimetype or 'application/octet-stream',
                     as_attachment=True, download_name=doc.filename)


def require_base_currency(code):
    from app.models import CompanySettings
    from app.ledger import LedgerError
    base = CompanySettings.query.first().base_currency
    if code != base:
        raise LedgerError(f'Posting in {code} requires a verified FX conversion. Use {base} or a reviewed base-currency adjustment; no 1:1 conversion is assumed.')
