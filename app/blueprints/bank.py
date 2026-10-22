import csv
import io
import uuid
from datetime import date, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import BankAccount, BankTransaction, Account, AccountType, JournalLine, JournalEntry, cents
from app.ledger import create_journal_entry, LedgerError
from app.system_accounts import get_system_account

bp = Blueprint("bank", __name__, url_prefix="/bank")


@bp.route("/")
@login_required
def index():
    accounts = BankAccount.query.filter_by(active=True).all()
    balances = {a.id: a.gl_account.balance_cents(as_of=date.today()) for a in accounts}
    return render_template("bank/index.html", accounts=accounts, balances=balances)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bank.index"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("bank.new"))
        code_base = 1010
        existing_codes = {int(a.code) for a in Account.query.filter(Account.code.between("1000", "1099")).all() if a.code.isdigit()}
        code = code_base
        while str(code) in {str(c) for c in existing_codes}:
            code += 1
        gl = Account(code=str(code), name=f"{name} (Bank)", type=AccountType.ASSET.value, is_bank=True)
        db.session.add(gl)
        db.session.flush()

        opening = cents(request.form.get("opening_balance") or 0)
        bank = BankAccount(
            name=name, account_number=request.form.get("account_number", ""),
            currency_code=request.form.get("currency_code", "HKD"),
            gl_account_id=gl.id, opening_balance_cents=opening,
        )
        db.session.add(bank)
        db.session.flush()

        from app.controls import require_base_currency
        require_base_currency(bank.currency_code)
        if opening:
            equity = Account.query.filter_by(code="3000").first()
            create_journal_entry(
                date.fromisoformat(request.form.get("opening_date") or date.today().isoformat()), f"Opening balance for {name}",
                [
                    {"account_id": gl.id, "debit_cents": max(opening, 0), "credit_cents": max(-opening, 0), "description": "Opening balance"},
                    {"account_id": equity.id, "debit_cents": max(-opening, 0), "credit_cents": max(opening, 0), "description": "Opening balance"},
                ],
                source_type="manual", created_by_id=current_user.id,
            )
        db.session.commit()
        flash(f"Bank account {name} created.", "success")
        return redirect(url_for("bank.index"))
    return render_template("bank/new.html")


@bp.route("/<int:bank_id>")
@login_required
def detail(bank_id):
    bank = BankAccount.query.get_or_404(bank_id)
    transactions = BankTransaction.query.filter_by(bank_account_id=bank_id).order_by(BankTransaction.date.desc()).all()
    return render_template("bank/detail.html", bank=bank, transactions=transactions)


@bp.route("/<int:bank_id>/import", methods=["POST"])
@login_required
def import_csv(bank_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bank.detail", bank_id=bank_id))
    bank = BankAccount.query.get_or_404(bank_id)
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("bank.detail", bank_id=bank_id))

    from app.bank_import import import_statement
    if not bank.active:
        abort(409, 'Bank account is inactive.')
    count, message = import_statement(bank, file)
    flash(message, 'success')
    return redirect(url_for('bank.detail', bank_id=bank_id))


@bp.route("/<int:bank_id>/reconcile")
@login_required
def reconcile(bank_id):
    bank = BankAccount.query.get_or_404(bank_id)
    unmatched_txns = BankTransaction.query.filter_by(bank_account_id=bank_id, status="unmatched").order_by(BankTransaction.date).all()
    matched_line_ids = {jl.id for jl in JournalLine.query.filter_by(account_id=bank.gl_account_id).all()}
    already_linked = {bt.matched_journal_line_id for bt in BankTransaction.query.filter(BankTransaction.matched_journal_line_id.isnot(None)).all()}
    candidate_lines = (JournalLine.query.join(JournalEntry)
                        .filter(JournalLine.account_id == bank.gl_account_id, JournalEntry.is_posted == True)  # noqa: E712
                        .order_by(JournalEntry.date.desc()).all())
    candidate_lines = [l for l in candidate_lines if l.id not in already_linked]
    all_accounts = Account.query.filter_by(active=True).order_by(Account.code).all()
    return render_template("bank/reconcile.html", bank=bank, unmatched_txns=unmatched_txns,
                            candidate_lines=candidate_lines, all_accounts=all_accounts)


@bp.route("/<int:bank_id>/match", methods=["POST"])
@login_required
def match(bank_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bank.reconcile", bank_id=bank_id))
    txn_id = int(request.form["txn_id"])
    line_id = int(request.form["line_id"])
    txn = BankTransaction.query.get_or_404(txn_id)
    bank = BankAccount.query.get_or_404(bank_id)
    line = JournalLine.query.get_or_404(line_id)
    if (not bank.active or txn.bank_account_id != bank.id or txn.status != 'unmatched'
            or txn.matched_journal_line_id is not None or not line.entry.is_posted
            or line.account_id != bank.gl_account_id
            or line.debit_cents - line.credit_cents != txn.amount_cents
            or BankTransaction.query.filter_by(matched_journal_line_id=line.id).first()):
        abort(409, 'Match rejected: bank, amount, posting or existing match does not agree.')
    from app import audit
    audit.record('match', 'bank_transaction', txn.id, f'Matched journal line {line.id}')
    txn.matched_journal_line_id = line_id
    txn.status = "matched"
    db.session.commit()
    flash("Transaction matched.", "success")
    return redirect(url_for("bank.reconcile", bank_id=bank_id))


@bp.route("/<int:bank_id>/quick-entry", methods=["POST"])
@login_required
def quick_entry(bank_id):
    """For bank transactions with no matching source doc (e.g. bank fees, interest) -
    post a two-sided journal entry directly and mark the transaction matched."""
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bank.reconcile", bank_id=bank_id))
    bank = BankAccount.query.get_or_404(bank_id)
    txn_id = int(request.form["txn_id"])
    contra_account_id = int(request.form["contra_account_id"])
    txn = BankTransaction.query.get_or_404(txn_id)

    if (not bank.active or txn.bank_account_id != bank.id or txn.status != 'unmatched'
            or txn.matched_journal_line_id is not None
            or JournalEntry.query.filter_by(source_type='bank', source_id=txn.id).first()):
        abort(409, 'This bank transaction is already posted/matched or belongs to another bank.')
    from app.controls import require_base_currency
    require_base_currency(bank.currency_code)
    contra = Account.query.get_or_404(contra_account_id)
    if not contra.active or contra.id == bank.gl_account_id:
        abort(409, 'Choose a different active contra account.')
    amount = abs(txn.amount_cents)
    if txn.amount_cents >= 0:
        lines = [
            {"account_id": bank.gl_account_id, "debit_cents": amount, "credit_cents": 0, "description": txn.description},
            {"account_id": contra_account_id, "debit_cents": 0, "credit_cents": amount, "description": txn.description},
        ]
    else:
        lines = [
            {"account_id": contra_account_id, "debit_cents": amount, "credit_cents": 0, "description": txn.description},
            {"account_id": bank.gl_account_id, "debit_cents": 0, "credit_cents": amount, "description": txn.description},
        ]
    try:
        entry = create_journal_entry(txn.date, txn.description or "Bank transaction", lines,
                                      source_type="bank", source_id=txn.id, created_by_id=current_user.id)
    except LedgerError as e:
        db.session.rollback()
        flash(str(e), "error")
        return redirect(url_for("bank.reconcile", bank_id=bank_id))

    bank_line = next(l for l in entry.lines if l.account_id == bank.gl_account_id)
    txn.matched_journal_line_id = bank_line.id
    txn.status = "matched"
    db.session.commit()
    flash("Entry posted and matched.", "success")
    return redirect(url_for("bank.reconcile", bank_id=bank_id))


@bp.route("/<int:bank_id>/reconcile-line/<int:line_id>", methods=["POST"])
@login_required
def reconcile_line(bank_id, line_id):
    if not current_user.can_edit():
        abort(403)
    bank = BankAccount.query.get_or_404(bank_id)
    line = JournalLine.query.get_or_404(line_id)
    txn = BankTransaction.query.filter_by(bank_account_id=bank_id, matched_journal_line_id=line_id).first()
    if (not txn or line.account_id != bank.gl_account_id or not line.entry.is_posted
            or txn.amount_cents != line.debit_cents - line.credit_cents):
        abort(409, 'The bank match must be valid before reconciliation.')
    if txn.status == 'matched':
        txn.status = 'reconciled'
        from app import audit
        audit.record('reconcile', 'bank_transaction', txn.id, f'Journal line {line.id}')
        db.session.commit()
    return redirect(url_for('bank.reconcile', bank_id=bank_id))
