from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Account, AccountType, JournalLine, JournalEntry

bp = Blueprint("accounts", __name__, url_prefix="/accounts")


@bp.route("/")
@login_required
def index():
    accounts = Account.query.order_by(Account.code).all()
    balances = {a.id: a.balance_cents(as_of=date.today()) for a in accounts}
    return render_template("accounts/index.html", accounts=accounts, balances=balances, types=[t.value for t in AccountType])


@bp.route("/new", methods=["POST"])
@login_required
def new():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("accounts.index"))
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    type_ = request.form.get("type")
    subtype = request.form.get("subtype", "").strip() or None
    is_bank = bool(request.form.get("is_bank"))
    if not code or not name or type_ not in [t.value for t in AccountType]:
        flash("Code, name and a valid type are required.", "error")
        return redirect(url_for("accounts.index"))
    if Account.query.filter_by(code=code).first():
        flash("An account with that code already exists.", "error")
        return redirect(url_for("accounts.index"))
    a = Account(code=code, name=name, type=type_, subtype=subtype, is_bank=is_bank)
    db.session.add(a)
    db.session.commit()
    flash(f"Account {code} - {name} created.", "success")
    return redirect(url_for("accounts.index"))


@bp.route("/<int:account_id>/toggle", methods=["POST"])
@login_required
def toggle(account_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("accounts.index"))
    a = Account.query.get_or_404(account_id)
    if a.system_locked:
        flash("This is a system account and cannot be deactivated.", "error")
        return redirect(url_for("accounts.index"))
    if a.active and a.balance_cents() != 0:
        from flask import abort
        abort(409, 'An account with a balance cannot be deactivated. Reconcile it first.')
    a.active = not a.active
    from app import audit
    audit.record('activate' if a.active else 'deactivate', 'account', a.id, f'Account {a.code}')
    db.session.commit()
    return redirect(url_for("accounts.index"))


@bp.route("/<int:account_id>")
@login_required
def ledger(account_id):
    a = Account.query.get_or_404(account_id)
    start = date.fromisoformat(request.args['start']) if request.args.get('start') else None
    end = date.fromisoformat(request.args['end']) if request.args.get('end') else None
    cc = request.args.get('cost_center_id',type=int)
    query = JournalLine.query.join(JournalEntry).filter(JournalLine.account_id==account_id,JournalEntry.is_posted.is_(True))
    if start:query=query.filter(JournalEntry.date>=start)
    if end:query=query.filter(JournalEntry.date<=end)
    if cc:query=query.filter(JournalLine.cost_center_id==cc)
    lines=query.order_by(JournalEntry.date,JournalEntry.id).all()
    running = a.balance_cents(start-timedelta(days=1),cc) if start else 0
    rows = []
    debit_normal = a.type in (AccountType.ASSET.value, AccountType.EXPENSE.value)
    for l in lines:
        delta = (l.debit_cents - l.credit_cents) if debit_normal else (l.credit_cents - l.debit_cents)
        running += delta
        rows.append({"line": l, "running": running})
    return render_template("accounts/ledger.html", account=a, rows=rows, start=start, end=end, cost_center_id=cc)
