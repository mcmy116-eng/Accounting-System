import hashlib
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import abort
import os
import uuid
import json
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    Bill, BillLine, Contact, Account, TaxRate, CostCenter, CompanySettings, Document,
    BillStatus, ContactType, DocStatus, cents,
)
from app.ledger import create_journal_entry, void_journal_entry, LedgerError
from app.system_accounts import get_system_account
from app.ai_extract import extract_bill_from_file, ExtractionError

bp = Blueprint("bills", __name__, url_prefix="/bills")

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
MIME_MAP = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif",
}


def _next_bill_number():
    s = CompanySettings.query.first()
    n = f"{s.bill_prefix}{s.next_bill_seq:04d}"
    s.next_bill_seq += 1
    return n


def _guess_vendor_contact(name):
    if not name:
        return None
    return Contact.query.filter(Contact.name.ilike(f"%{name.strip()}%")).first()


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "")
    query = Bill.query
    if status_filter:
        query = query.filter_by(status=status_filter)
    bills = query.order_by(Bill.bill_date.desc(), Bill.id.desc()).all()
    return render_template("bills/index.html", bills=bills, statuses=[s.value for s in BillStatus], status_filter=status_filter)


@bp.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bills.index"))

    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("bills.upload"))
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_EXT:
            flash("Unsupported file type. Upload a PDF, PNG, JPG, WEBP or GIF.", "error")
            return redirect(url_for("bills.upload"))

        stored_name = f"{uuid.uuid4().hex}{ext}"
        filepath = os.path.join(current_app.config["UPLOAD_FOLDER"], stored_name)
        file.save(filepath)

        bill = Bill(number=_next_bill_number(), status=BillStatus.AWAITING_REVIEW.value,
                    bill_date=date.today(), due_date=date.today() + timedelta(days=14))
        db.session.add(bill)
        db.session.flush()

        doc = Document(
            filename=secure_filename(file.filename)[:255] or "receipt", filepath=filepath, sha256=hashlib.sha256(Path(filepath).read_bytes()).hexdigest(), mimetype=MIME_MAP.get(ext, ""),
            uploaded_by_id=current_user.id, extraction_status=DocStatus.PENDING.value,
            bill_id=bill.id,
        )
        db.session.add(doc)
        db.session.flush()

        try:
            data = extract_bill_from_file(filepath, MIME_MAP.get(ext))
            doc.extraction_status = DocStatus.EXTRACTED.value
            doc.extracted_json = json.dumps(data)

            if data.get("bill_date"):
                try:
                    bill.bill_date = date.fromisoformat(data["bill_date"])
                except ValueError:
                    pass
            if data.get("due_date"):
                try:
                    bill.due_date = date.fromisoformat(data["due_date"])
                except ValueError:
                    pass
            if data.get("currency_code"):
                cc = str(data["currency_code"]).strip().upper()
                if len(cc) == 3 and cc.isalpha():   # ignore junk; column is CHAR(3) on Postgres
                    bill.currency_code = cc
            if data.get("vendor_reference"):
                bill.vendor_ref = str(data["vendor_reference"]).strip()[:80]

            vendor_contact = _guess_vendor_contact(data.get("vendor_name"))
            if vendor_contact:
                bill.contact_id = vendor_contact.id
            bill.notes = f"Vendor (from document): {data.get('vendor_name', 'unknown')}"

            subtotal = 0
            for li in data.get("line_items", []):
                qty = float(li.get("quantity") or 1)
                amount = li.get("amount")
                unit_price = li.get("unit_price")
                if amount is None and unit_price is not None:
                    amount = qty * unit_price
                if unit_price is None and amount is not None:
                    unit_price = amount / qty if qty else amount
                amount = amount or 0
                unit_price = unit_price or 0
                line = BillLine(
                    bill_id=bill.id, description=li.get("description", "")[:255],
                    quantity=qty, unit_price_cents=cents(unit_price),
                    amount_cents=cents(amount), category_guess=(li.get("category_guess") or "")[:120],
                )
                db.session.add(line)
                subtotal += cents(amount)
            bill.subtotal_cents = subtotal
            bill.tax_cents = cents(data.get("tax") or 0)
            bill.total_cents = cents(data.get("total")) if data.get("total") is not None else subtotal + bill.tax_cents
        except ExtractionError as e:
            doc.extraction_status = DocStatus.FAILED.value
            doc.extraction_error = str(e)
            flash(f"AI extraction failed ({e}). You can still fill this bill in manually below.", "error")
        except Exception as e:  # noqa: BLE001 — a parsing surprise must never 500 the upload
            doc.extraction_status = DocStatus.FAILED.value
            doc.extraction_error = str(e)[:500]
            flash("Couldn't read this document automatically — fill in the details manually below.", "error")

        db.session.commit()
        return redirect(url_for("bills.review", bill_id=bill.id))

    return render_template("bills/upload.html")


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    """Manual bill entry, no document upload."""
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bills.index"))
    if request.method == "POST":
        bill = Bill(number=_next_bill_number(), status=BillStatus.AWAITING_REVIEW.value,
                    bill_date=date.today(), due_date=date.today() + timedelta(days=14))
        db.session.add(bill)
        db.session.commit()
        return redirect(url_for("bills.review", bill_id=bill.id))
    return redirect(url_for("bills.upload"))


@bp.route("/<int:bill_id>/review", methods=["GET", "POST"])
@login_required
def review(bill_id):
    bill = Bill.query.get_or_404(bill_id)
    if bill.status not in (BillStatus.AWAITING_REVIEW.value, BillStatus.DRAFT.value):
        return redirect(url_for("bills.detail", bill_id=bill.id))

    if request.method == "POST":
        if not current_user.can_edit():
            flash("You don't have permission to do that.", "error")
            return redirect(url_for("bills.review", bill_id=bill.id))
        from app.validation import validate_lines, line_amount
        validate_lines(request.form)

        vendor_name_new = request.form.get("new_vendor_name", "").strip()
        contact_id = request.form.get("contact_id")
        if vendor_name_new:
            c = Contact(name=vendor_name_new, type=ContactType.VENDOR.value)
            db.session.add(c)
            db.session.flush()
            bill.contact_id = c.id
        elif contact_id:
            bill.contact_id = int(contact_id)

        bill.bill_date = date.fromisoformat(request.form["bill_date"])
        bill.due_date = date.fromisoformat(request.form["due_date"])
        bill.vendor_ref = request.form.get("vendor_ref", "")
        bill.cost_center_id = int(request.form["cost_center_id"]) if request.form.get("cost_center_id") else None
        bill.notes = request.form.get("notes", "")

        line_ids = request.form.getlist("line_id[]")
        descriptions = request.form.getlist("line_description[]")
        quantities = request.form.getlist("line_quantity[]")
        prices = request.form.getlist("line_price[]")
        account_ids = request.form.getlist("line_account[]")
        tax_ids = request.form.getlist("line_tax[]")
        cc_ids = request.form.getlist("line_cost_center[]")

        # wipe and recreate lines to keep this simple and robust
        BillLine.query.filter_by(bill_id=bill.id).delete()
        subtotal = 0
        tax_total = 0
        missing_account = False
        for i in range(len(descriptions)):
            if not descriptions[i].strip():
                continue
            if not account_ids[i]:
                missing_account = True
                continue
            unit_price_cents = cents(prices[i] or 0)
            account = db.session.get(Account, int(account_ids[i]))
            if not account or not account.active or account.type not in ('asset','expense') or account.is_bank or account.code in ('1000','1030','1100','1500'):
                raise LedgerError('Bill lines require an active expense, inventory, prepaid or fixed-asset account.')
            qty, amount = line_amount(quantities[i], unit_price_cents)
            tax_rate_id = int(tax_ids[i]) if tax_ids[i] else None
            tax_rate = TaxRate.query.get(tax_rate_id) if tax_rate_id else None
            tax_amount = round(amount * (tax_rate.rate / 100)) if tax_rate else 0
            db.session.add(BillLine(
                bill_id=bill.id, description=descriptions[i].strip(), quantity=qty,
                unit_price_cents=unit_price_cents, amount_cents=amount,
                account_id=int(account_ids[i]), tax_rate_id=tax_rate_id,
                cost_center_id=int(cc_ids[i]) if cc_ids[i] else None,
            ))
            subtotal += amount
            tax_total += tax_amount

        bill.subtotal_cents = subtotal
        bill.tax_cents = tax_total
        bill.total_cents = subtotal + tax_total

        action = request.form.get("action", "save")
        if action == "approve":
            from sqlalchemy import func
            others = Bill.query.filter(Bill.id != bill.id, Bill.contact_id == bill.contact_id,
                                       Bill.status.in_(['approved', 'partial', 'paid']))
            if bill.vendor_ref.strip():
                duplicate = others.filter(func.lower(func.trim(Bill.vendor_ref)) == bill.vendor_ref.strip().lower()).first()
            else:
                duplicate = others.filter(Bill.bill_date == bill.bill_date, Bill.total_cents == bill.total_cents).first()
            if duplicate:
                abort(409, f'Possible duplicate of {duplicate.number}. Check the original; use a distinct vendor reference for a separate genuine bill.')
            if missing_account:
                flash("Every line needs a GL account before you can approve & post.", "error")
                db.session.commit()
                return redirect(url_for("bills.review", bill_id=bill.id))
            if not bill.contact_id:
                flash("Select or create a vendor before approving.", "error")
                db.session.commit()
                return redirect(url_for("bills.review", bill_id=bill.id))
            db.session.flush()
            _post_bill(bill)
            db.session.commit()
            flash(f"Bill {bill.number} approved and posted to the ledger.", "success")
            return redirect(url_for("bills.detail", bill_id=bill.id))

        db.session.commit()
        flash("Draft saved.", "success")
        return redirect(url_for("bills.review", bill_id=bill.id))

    vendors = Contact.query.filter(Contact.active == True, Contact.type.in_(["vendor", "both"])).order_by(Contact.name).all()  # noqa: E712
    expense_accounts = Account.query.filter(Account.type.in_(["expense", "asset"]), Account.active.is_(True), Account.is_bank.is_(False), Account.code.notin_(["1000","1030","1100","1500"])).order_by(Account.code).all()  # noqa: E712
    tax_rates = TaxRate.query.filter_by(active=True).all()
    cost_centers = CostCenter.query.filter_by(active=True).all()
    document = Document.query.filter_by(bill_id=bill.id).order_by(Document.id.desc()).first()
    return render_template(
        "bills/review.html", bill=bill, vendors=vendors, expense_accounts=expense_accounts,
        tax_rates=tax_rates, cost_centers=cost_centers, document=document,
    )


def _post_bill(bill: Bill):
    from app.controls import require_base_currency
    require_base_currency(bill.currency_code)
    ap_account = get_system_account("2000")
    tax_receivable = get_system_account("1500")

    je_lines = [{
        "account_id": ap_account.id,
        "debit_cents": 0,
        "credit_cents": bill.total_cents,
        "description": f"Bill {bill.number} from {bill.contact.name}",
        "contact_id": bill.contact_id,
        "cost_center_id": bill.cost_center_id,
    }]
    for l in bill.lines:
        je_lines.append({
            "account_id": l.account_id,
            "debit_cents": l.amount_cents,
            "credit_cents": 0,
            "description": l.description,
            "cost_center_id": l.cost_center_id or bill.cost_center_id,
        })
    if bill.tax_cents:
        je_lines.append({
            "account_id": tax_receivable.id,
            "debit_cents": bill.tax_cents,
            "credit_cents": 0,
            "description": f"Tax on bill {bill.number}",
            "cost_center_id": bill.cost_center_id,
        })

    entry = create_journal_entry(
        bill.bill_date, f"Bill {bill.number} from {bill.contact.name}", je_lines,
        source_type="bill", source_id=bill.id, reference=bill.number, created_by_id=current_user.id,
    )
    bill.journal_entry_id = entry.id
    bill.status = BillStatus.APPROVED.value


@bp.route("/<int:bill_id>")
@login_required
def detail(bill_id):
    bill = Bill.query.get_or_404(bill_id)
    return render_template("bills/detail.html", bill=bill)


@bp.route("/<int:bill_id>/void", methods=["POST"])
@login_required
def void(bill_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    bill = Bill.query.get_or_404(bill_id)
    if bill.status == BillStatus.VOID.value:
        return redirect(url_for("bills.detail", bill_id=bill.id))
    if bill.amount_paid_cents:
        flash("Cannot void a bill that has payments applied.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))
    if bill.journal_entry_id:
        from app.models import JournalEntry
        entry = JournalEntry.query.get(bill.journal_entry_id)
        void_journal_entry(entry, created_by_id=current_user.id, reason=request.form.get("reason", "Bill void"))
    bill.status = BillStatus.VOID.value
    db.session.commit()
    flash(f"Bill {bill.number} voided.", "success")
    return redirect(url_for("bills.detail", bill_id=bill_id))


@bp.route("/documents/<int:doc_id>/file")
@login_required
def document_file(doc_id):
    doc = Document.query.get_or_404(doc_id)
    from app.controls import document_response
    return document_response(doc)
