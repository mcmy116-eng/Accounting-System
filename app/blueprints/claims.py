import hashlib
from pathlib import Path
from werkzeug.utils import secure_filename
"""Staff expense-claim workflow.

Lifecycle: Draft -> Submitted -> AI Reviewed -> Pending Approval ->
Approved / Rejected (with clarification loop) -> Paid.

Staff users only ever see and act on their own claims. Admins/bookkeepers see
everything and hold the approve / reject / clarify / mark-paid actions.

Accounting: on approval the claim posts a balanced journal entry —
  Dr  each expense account (line amounts)
  Dr  VAT/Tax Receivable (any input tax)
  Cr  Employee Reimbursements Payable (total)
On "mark paid" a second entry clears the payable against a bank account —
  Dr  Employee Reimbursements Payable
  Cr  Bank GL account
"""
import os
import uuid
import json
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    current_app, make_response, abort, Response,
)
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    ExpenseClaim, ExpenseClaimLine, Document, Account, TaxRate, CostCenter,
    BankAccount, CompanySettings, User, ClaimStatus, CLAIM_EDITABLE_STATUSES,
    DocStatus, cents, money,
)
from app.ledger import create_journal_entry, void_journal_entry
from app.system_accounts import get_system_account
from app.ai_extract import extract_bill_from_file, ExtractionError
from app.ai_categorize import suggest_account, learn
from app import audit

bp = Blueprint("claims", __name__, url_prefix="/claims")

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MIME_MAP = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}

# A single expense line above this (in base currency) gets flagged for a second look.
UNUSUAL_AMOUNT_CENTS = 500_000  # 5,000.00


def _next_claim_number():
    s = CompanySettings.query.first()
    # Reuse a simple sequence stored on company settings-adjacent counters; claims
    # get their own CLAIM- prefix based on the highest existing number.
    last = ExpenseClaim.query.order_by(ExpenseClaim.id.desc()).first()
    seq = (last.id + 1) if last else 1
    return f"CLAIM-{seq:04d}"


def _get_claim_or_404(claim_id):
    claim = ExpenseClaim.query.get_or_404(claim_id)
    if current_user.is_staff_only() and claim.staff_id != current_user.id:
        abort(403)
    return claim


def _can_edit_claim(claim):
    """Who may edit lines / add receipts right now.

    * Approvers (admin/bookkeeper) may edit any claim until it is approved or
      paid — the brief requires the master admin to be able to edit claims,
      including ones already submitted for approval.
    * The submitting staffer may edit only while the claim is a draft or has
      been sent back for clarification.
    """
    if claim.status in (ClaimStatus.APPROVED.value, ClaimStatus.PAID.value):
        return False
    if current_user.can_approve_claims():
        return True
    return claim.staff_id == current_user.id and claim.status in CLAIM_EDITABLE_STATUSES


def claim_flags(claim):
    """Anomaly flags surfaced for review (missing receipt, unusual amount, possible duplicate)."""
    flags = []
    if not claim.has_receipt():
        flags.append(("missing-receipt", "No receipt attached"))
    if claim.total_cents >= UNUSUAL_AMOUNT_CENTS:
        flags.append(("unusual-amount", "Unusually large amount"))
    dup = ExpenseClaim.query.filter(
        ExpenseClaim.id != claim.id,
        ExpenseClaim.staff_id == claim.staff_id,
        ExpenseClaim.total_cents == claim.total_cents,
        ExpenseClaim.claim_date == claim.claim_date,
        ExpenseClaim.total_cents > 0,
    ).first()
    if dup:
        flags.append(("possible-duplicate", f"Possible duplicate of {dup.number}"))
    return flags


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "")
    staff_filter = request.args.get("staff", "")
    query = ExpenseClaim.query
    if current_user.is_staff_only():
        query = query.filter_by(staff_id=current_user.id)
    elif staff_filter:
        query = query.filter_by(staff_id=int(staff_filter))
    if status_filter:
        query = query.filter_by(status=status_filter)
    claims = query.order_by(ExpenseClaim.created_at.desc(), ExpenseClaim.id.desc()).all()
    staff_users = []
    if current_user.can_approve_claims():
        staff_users = User.query.order_by(User.name).all()
    return render_template(
        "claims/index.html", claims=claims,
        statuses=[s.value for s in ClaimStatus],
        status_filter=status_filter, staff_filter=staff_filter,
        staff_users=staff_users, flags_for=claim_flags,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        claim = ExpenseClaim(
            number=_next_claim_number(),
            staff_id=current_user.id,
            title=request.form.get("title", "").strip(),
            business_purpose=request.form.get("business_purpose", "").strip(),
            claim_date=date.today(),
            status=ClaimStatus.DRAFT.value,
        )
        cc = request.form.get("cost_center_id")
        claim.cost_center_id = int(cc) if cc else None
        db.session.add(claim)
        db.session.flush()
        audit.record("create", "claim", claim.id, f"Created expense claim {claim.number}")
        db.session.commit()
        flash("Claim created. Add your receipts and expense lines below.", "success")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    cost_centers = CostCenter.query.filter_by(active=True).all()
    return render_template("claims/new.html", cost_centers=cost_centers)


@bp.route("/<int:claim_id>")
@login_required
def detail(claim_id):
    claim = _get_claim_or_404(claim_id)
    expense_accounts = Account.query.filter(
        Account.type == "expense", Account.active == True  # noqa: E712
    ).order_by(Account.code).all()
    tax_rates = TaxRate.query.filter_by(active=True).all()
    cost_centers = CostCenter.query.filter_by(active=True).all()
    bank_accounts = BankAccount.query.filter_by(active=True).all()
    editable = _can_edit_claim(claim)
    return render_template(
        "claims/detail.html", claim=claim, expense_accounts=expense_accounts,
        tax_rates=tax_rates, cost_centers=cost_centers, bank_accounts=bank_accounts,
        editable=editable, flags=claim_flags(claim),
        can_approve=current_user.can_approve_claims(),
        ClaimStatus=ClaimStatus,
    )


@bp.route("/<int:claim_id>/save", methods=["POST"])
@login_required
def save(claim_id):
    claim = _get_claim_or_404(claim_id)
    if not _can_edit_claim(claim):
        flash("This claim can no longer be edited.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))

    claim.title = request.form.get("title", "").strip()
    claim.business_purpose = request.form.get("business_purpose", "").strip()
    if request.form.get("claim_date"):
        claim.claim_date = date.fromisoformat(request.form["claim_date"])
    cc = request.form.get("cost_center_id")
    claim.cost_center_id = int(cc) if cc else None

    line_descs = request.form.getlist("line_description[]")
    line_merchants = request.form.getlist("line_merchant[]")
    line_dates = request.form.getlist("line_date[]")
    line_amounts = request.form.getlist("line_amount[]")
    line_accounts = request.form.getlist("line_account[]")
    line_taxes = request.form.getlist("line_tax[]")
    line_ccs = request.form.getlist("line_cost_center[]")
    line_guesses = request.form.getlist("line_guess[]")

    ExpenseClaimLine.query.filter_by(claim_id=claim.id).delete()
    for i in range(len(line_descs)):
        if not (line_descs[i].strip() or (i < len(line_merchants) and line_merchants[i].strip())):
            continue
        amount = cents(line_amounts[i] or 0)
        if amount <= 0:
            continue
        exp_date = None
        if i < len(line_dates) and line_dates[i]:
            try:
                exp_date = date.fromisoformat(line_dates[i])
            except ValueError:
                exp_date = None
        db.session.add(ExpenseClaimLine(
            claim_id=claim.id,
            description=line_descs[i].strip()[:255],
            merchant=(line_merchants[i].strip()[:200] if i < len(line_merchants) else ""),
            expense_date=exp_date,
            amount_cents=amount,
            account_id=int(line_accounts[i]) if (i < len(line_accounts) and line_accounts[i]) else None,
            tax_rate_id=int(line_taxes[i]) if (i < len(line_taxes) and line_taxes[i]) else None,
            cost_center_id=int(line_ccs[i]) if (i < len(line_ccs) and line_ccs[i]) else claim.cost_center_id,
            category_guess=(line_guesses[i][:120] if i < len(line_guesses) else ""),
        ))
    db.session.flush()
    claim.recalc_totals()
    audit.record("update", "claim", claim.id, f"Edited claim {claim.number}")
    db.session.commit()
    flash("Saved.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/upload", methods=["POST"])
@login_required
def upload(claim_id):
    claim = _get_claim_or_404(claim_id)
    if not _can_edit_claim(claim):
        flash("You can't add receipts to this claim.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Choose a receipt image or PDF to upload.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        flash("Unsupported file type. Upload a PDF, PNG, JPG, WEBP or GIF.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))

    stored_name = f"{uuid.uuid4().hex}{ext}"
    filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
    file.save(filepath)
    doc = Document(
        filename=secure_filename(file.filename)[:255] or "receipt", filepath=filepath, sha256=hashlib.sha256(Path(filepath).read_bytes()).hexdigest(), mimetype=MIME_MAP.get(ext, ""),
        uploaded_by_id=current_user.id, extraction_status=DocStatus.PENDING.value,
        claim_id=claim.id,
    )
    db.session.add(doc)
    db.session.flush()

    # AI reads the receipt, then we categorise each line with a confidence score.
    try:
        data = extract_bill_from_file(filepath, MIME_MAP.get(ext))
        doc.extraction_status = DocStatus.EXTRACTED.value
        doc.extracted_json = json.dumps(data)
        merchant = data.get("vendor_name") or ""
        exp_date = None
        if data.get("bill_date"):
            try:
                exp_date = date.fromisoformat(data["bill_date"])
            except ValueError:
                exp_date = None
        if data.get("currency_code"):
            cc = str(data["currency_code"]).strip().upper()
            if len(cc) == 3 and cc.isalpha():   # ignore junk; column is CHAR(3) on Postgres
                claim.currency_code = cc

        line_items = data.get("line_items") or []
        if not line_items and data.get("total") is not None:
            line_items = [{"description": merchant or "Expense", "amount": data.get("total"),
                           "category_guess": ""}]
        confidences = []
        for li in line_items:
            amount = li.get("amount")
            if amount is None:
                qty = float(li.get("quantity") or 1)
                unit = li.get("unit_price") or 0
                amount = qty * unit
            guess = (li.get("category_guess") or "")[:120]
            desc = (li.get("description") or "")[:255]
            acct_id, conf, _reason = suggest_account(merchant, desc, guess)
            confidences.append(conf)
            db.session.add(ExpenseClaimLine(
                claim_id=claim.id, merchant=merchant[:200], description=desc,
                expense_date=exp_date, amount_cents=cents(amount),
                account_id=acct_id, category_guess=guess, ai_confidence=conf,
                cost_center_id=claim.cost_center_id,
            ))
        db.session.flush()
        claim.recalc_totals()
        claim.ai_confidence = (sum(confidences) / len(confidences)) if confidences else None
        claim.ai_summary = f"Read '{merchant or 'receipt'}' — {len(line_items)} line(s), total {money(claim.total_cents)} {claim.currency_code}."
        if claim.status in (ClaimStatus.DRAFT.value, ClaimStatus.CLARIFICATION.value):
            claim.status = ClaimStatus.AI_REVIEWED.value
        audit.record("ai_review", "claim", claim.id,
                     f"AI read receipt for {claim.number} (avg confidence {claim.ai_confidence:.0%})"
                     if claim.ai_confidence is not None else f"AI read receipt for {claim.number}")
        flash("Receipt read. Review the suggested categories, then submit.", "success")
    except ExtractionError as e:
        doc.extraction_status = DocStatus.FAILED.value
        doc.extraction_error = str(e)
        flash(f"Couldn't auto-read that receipt ({e}). Add the expense line manually.", "error")
    except Exception as e:  # noqa: BLE001 — a parsing surprise must never 500 the upload
        doc.extraction_status = DocStatus.FAILED.value
        doc.extraction_error = str(e)[:500]
        flash("Couldn't auto-read that receipt — add the expense line manually.", "error")

    db.session.commit()
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/submit", methods=["POST"])
@login_required
def submit(claim_id):
    claim = _get_claim_or_404(claim_id)
    if not (claim.staff_id == current_user.id or current_user.can_approve_claims()):
        abort(403)
    if claim.status in (ClaimStatus.APPROVED.value, ClaimStatus.PAID.value):
        flash("This claim has already been processed.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    if not claim.lines:
        flash("Add at least one expense line before submitting.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    claim.recalc_totals()
    claim.status = ClaimStatus.PENDING_APPROVAL.value
    claim.submitted_at = datetime.utcnow()
    audit.record("submit", "claim", claim.id, f"Submitted claim {claim.number} for approval ({money(claim.total_cents)})")
    db.session.commit()
    flash("Submitted for approval.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/approve", methods=["POST"])
@login_required
def approve(claim_id):
    if not current_user.can_approve_claims():
        abort(403)
    claim = ExpenseClaim.query.get_or_404(claim_id)
    if claim.status in (ClaimStatus.APPROVED.value, ClaimStatus.PAID.value):
        flash("Already approved.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    if not claim.lines:
        flash("Nothing to approve — the claim has no expense lines.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    if any(l.account_id is None for l in claim.lines):
        flash("Every expense line needs a category (GL account) before approval.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))

    claim.recalc_totals()
    _post_claim(claim)
    # Learn from the human-approved categorisation so future suggestions improve.
    for l in claim.lines:
        learn(l.account_id, merchant=l.merchant, description=l.description,
              category_guess=l.category_guess)
    claim.status = ClaimStatus.APPROVED.value
    claim.decided_at = datetime.utcnow()
    claim.decided_by_id = current_user.id
    audit.record("approve", "claim", claim.id,
                 f"Approved claim {claim.number} ({money(claim.total_cents)}) and posted to the ledger")
    db.session.commit()
    flash(f"Claim {claim.number} approved and posted. Mark it paid once reimbursed.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/reject", methods=["POST"])
@login_required
def reject(claim_id):
    if not current_user.can_approve_claims():
        abort(403)
    claim = ExpenseClaim.query.get_or_404(claim_id)
    if claim.status in (ClaimStatus.APPROVED.value, ClaimStatus.PAID.value):
        flash("Can't reject a claim that's already approved/paid — void the entry instead.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    claim.status = ClaimStatus.REJECTED.value
    claim.reviewer_notes = request.form.get("notes", "").strip()
    claim.decided_at = datetime.utcnow()
    claim.decided_by_id = current_user.id
    audit.record("reject", "claim", claim.id, f"Rejected claim {claim.number}: {claim.reviewer_notes[:200]}")
    db.session.commit()
    flash("Claim rejected.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/clarify", methods=["POST"])
@login_required
def clarify(claim_id):
    if not current_user.can_approve_claims():
        abort(403)
    claim = ExpenseClaim.query.get_or_404(claim_id)
    if claim.journal_entry_id or claim.status in (ClaimStatus.APPROVED.value, ClaimStatus.PAID.value):
        abort(409, 'Posted claims cannot be reopened for editing. Use a documented reversal.')
    claim.status = ClaimStatus.CLARIFICATION.value
    claim.reviewer_notes = request.form.get("notes", "").strip()
    audit.record("clarify", "claim", claim.id, f"Requested clarification on {claim.number}: {claim.reviewer_notes[:200]}")
    db.session.commit()
    flash("Sent back to the claimant for clarification.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


@bp.route("/<int:claim_id>/pay", methods=["POST"])
@login_required
def pay(claim_id):
    if not current_user.can_approve_claims():
        abort(403)
    claim = ExpenseClaim.query.get_or_404(claim_id)
    if claim.status != ClaimStatus.APPROVED.value:
        flash("Only approved claims can be marked paid.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    bank_id = request.form.get("bank_account_id")
    if not bank_id:
        flash("Choose the bank account the reimbursement was paid from.", "error")
        return redirect(url_for("claims.detail", claim_id=claim.id))
    bank = BankAccount.query.get_or_404(int(bank_id))
    _pay_claim(claim, bank)
    claim.status = ClaimStatus.PAID.value
    claim.amount_paid_cents = claim.total_cents
    claim.paid_at = datetime.utcnow()
    audit.record("pay", "claim", claim.id,
                 f"Marked {claim.number} paid ({money(claim.total_cents)}) from {bank.name}")
    db.session.commit()
    flash(f"Claim {claim.number} marked paid.", "success")
    return redirect(url_for("claims.detail", claim_id=claim.id))


def _post_claim(claim):
    from app.controls import require_base_currency
    require_base_currency(claim.currency_code)
    if claim.journal_entry_id:
        abort(409, 'This claim already has a posted entry.')
    reimb = get_system_account("2550")
    tax_receivable = get_system_account("1500")
    je_lines = []
    tax_total = 0
    for l in claim.lines:
        account=db.session.get(Account,l.account_id)
        if not account or not account.active or account.type!='expense':
            abort(409,'Claim lines require active expense accounts.')
        je_lines.append({
            "account_id": l.account_id,
            "debit_cents": l.amount_cents,
            "credit_cents": 0,
            "description": (l.merchant + " — " if l.merchant else "") + (l.description or "expense"),
            "cost_center_id": l.cost_center_id or claim.cost_center_id,
        })
        tax_total += l.tax_cents()
    if tax_total:
        je_lines.append({
            "account_id": tax_receivable.id, "debit_cents": tax_total, "credit_cents": 0,
            "description": f"Input tax on claim {claim.number}",
            "cost_center_id": claim.cost_center_id,
        })
    je_lines.append({
        "account_id": reimb.id, "debit_cents": 0, "credit_cents": claim.total_cents,
        "description": f"Reimbursement owed to {claim.staff.name}",
        "cost_center_id": claim.cost_center_id,
    })
    entry = create_journal_entry(
        claim.claim_date, f"Expense claim {claim.number} — {claim.staff.name}", je_lines,
        source_type="claim", source_id=claim.id, reference=claim.number,
        created_by_id=current_user.id,
    )
    claim.journal_entry_id = entry.id


def _pay_claim(claim, bank):
    if not bank.active or bank.currency_code != claim.currency_code:
        abort(409, 'Choose an active bank in the claim currency.')
    reimb = get_system_account("2550")
    je_lines = [
        {"account_id": reimb.id, "debit_cents": claim.total_cents, "credit_cents": 0,
         "description": f"Reimbursement paid to {claim.staff.name} ({claim.number})"},
        {"account_id": bank.gl_account_id, "debit_cents": 0, "credit_cents": claim.total_cents,
         "description": f"Reimbursement {claim.number} from {bank.name}"},
    ]
    entry = create_journal_entry(
        date.today(), f"Reimbursement paid — claim {claim.number}", je_lines,
        source_type="claim_payment", source_id=claim.id, reference=claim.number,
        created_by_id=current_user.id,
    )
    claim.payment_journal_entry_id = entry.id


@bp.route("/documents/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    doc = Document.query.get_or_404(doc_id)
    from app.controls import document_response
    return document_response(doc)


@bp.route("/export.csv")
@login_required
def export_csv():
    import csv
    import io
    query = ExpenseClaim.query
    if current_user.is_staff_only():
        query = query.filter_by(staff_id=current_user.id)
    claims = query.order_by(ExpenseClaim.claim_date).all()
    buf = io.StringIO()
    from app.report_export import safe_cell
    class SafeWriter:
        def writerow(self, row): writer.writerow([safe_cell(x) for x in row])
    writer = csv.writer(buf)
    w = SafeWriter()
    w.writerow(["Claim", "Staff", "Date", "Title", "Purpose", "Line merchant",
                "Line description", "Category (GL)", "Amount", "Tax", "Currency", "Status"])
    for c in claims:
        if not c.lines:
            w.writerow([c.number, c.staff.name if c.staff else "", c.claim_date, c.title,
                        c.business_purpose, "", "", "", money(c.total_cents), money(c.tax_cents),
                        c.currency_code, c.status])
        for l in c.lines:
            w.writerow([
                c.number, c.staff.name if c.staff else "", c.claim_date, c.title,
                c.business_purpose, l.merchant, l.description,
                (l.account.name if l.account else ""), money(l.amount_cents),
                money(l.tax_cents()), c.currency_code, c.status,
            ])
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=expense_claims.csv"
    return resp
