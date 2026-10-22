import json
from datetime import date, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, flash, make_response
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    Invoice, InvoiceLine, Contact, Account, TaxRate, CostCenter, CompanySettings,
    InvoiceStatus, RecurringInvoice, cents, money,
)
from app.ledger import create_journal_entry, void_journal_entry, LedgerError
from app.system_accounts import get_system_account

bp = Blueprint("invoices", __name__, url_prefix="/invoices")


def _next_invoice_number():
    s = CompanySettings.query.first()
    n = f"{s.invoice_prefix}{s.next_invoice_seq:04d}"
    s.next_invoice_seq += 1
    return n


def _parse_lines(form):
    from app.validation import validate_lines, line_amount
    validate_lines(form)
    descriptions = form.getlist("line_description[]")
    quantities = form.getlist("line_quantity[]")
    prices = form.getlist("line_price[]")
    account_ids = form.getlist("line_account[]")
    tax_ids = form.getlist("line_tax[]")
    cc_ids = form.getlist("line_cost_center[]")
    lines = []
    for i in range(len(descriptions)):
        if not descriptions[i].strip() and not prices[i]:
            continue
        unit_price_cents = cents(prices[i] or 0)
        account = db.session.get(Account, int(account_ids[i]))
        if not account or not account.active or account.type != 'revenue':
            raise LedgerError('Invoice lines require active revenue accounts.')
        qty, line_subtotal = line_amount(quantities[i], unit_price_cents)
        tax_rate_id = int(tax_ids[i]) if tax_ids[i] else None
        tax_rate = TaxRate.query.get(tax_rate_id) if tax_rate_id else None
        tax_amount = round(line_subtotal * (tax_rate.rate / 100)) if tax_rate else 0
        lines.append({
            "description": descriptions[i].strip(),
            "quantity": qty,
            "unit_price_cents": unit_price_cents,
            "account_id": int(account_ids[i]),
            "tax_rate_id": tax_rate_id,
            "cost_center_id": int(cc_ids[i]) if cc_ids[i] else None,
            "amount_cents": line_subtotal,
            "tax_cents": tax_amount,
        })
    return lines


@bp.route("/")
@login_required
def index():
    status_filter = request.args.get("status", "")
    query = Invoice.query
    q = request.args.get('q', '').strip()
    if q:
        from sqlalchemy import or_
        query = query.join(Contact).filter(or_(Invoice.number.ilike('%'+q+'%'), Invoice.notes.ilike('%'+q+'%'), Contact.name.ilike('%'+q+'%')))
    for key, field, comparison in [('start',Invoice.issue_date,'ge'),('end',Invoice.issue_date,'le')]:
        if request.args.get(key):
            dt=date.fromisoformat(request.args[key]);query=query.filter(field>=dt if comparison=='ge' else field<=dt)
    if request.args.get('currency'):query=query.filter(Invoice.currency_code==request.args['currency'].upper())
    for key, comparison in [('min_amount','ge'),('max_amount','le')]:
        if request.args.get(key):
            amount=cents(request.args[key]);query=query.filter(Invoice.total_cents>=amount if comparison=='ge' else Invoice.total_cents<=amount)
    if status_filter:
        query = query.filter_by(status=status_filter)
    invoices = query.order_by(Invoice.issue_date.desc(), Invoice.id.desc()).all()
    return render_template("invoices/index.html", invoices=invoices, statuses=[s.value for s in InvoiceStatus], status_filter=status_filter)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.index"))

    if request.method == "POST":
        lines_data = _parse_lines(request.form)
        if not lines_data:
            flash("Add at least one line item.", "error")
            return redirect(url_for("invoices.new"))

        subtotal = sum(l["amount_cents"] for l in lines_data)
        tax_total = sum(l["tax_cents"] for l in lines_data)

        inv = Invoice(
            number=_next_invoice_number(),
            contact_id=int(request.form["contact_id"]),
            issue_date=date.fromisoformat(request.form["issue_date"]),
            due_date=date.fromisoformat(request.form["due_date"]),
            currency_code=request.form.get("currency_code", "HKD"),
            cost_center_id=int(request.form["cost_center_id"]) if request.form.get("cost_center_id") else None,
            notes=request.form.get("notes", ""),
            subtotal_cents=subtotal,
            tax_cents=tax_total,
            total_cents=subtotal + tax_total,
            status=InvoiceStatus.DRAFT.value,
        )
        db.session.add(inv)
        db.session.flush()
        for l in lines_data:
            db.session.add(InvoiceLine(invoice_id=inv.id, **{k: v for k, v in l.items() if k != "tax_cents"}))
        db.session.commit()
        flash(f"Invoice {inv.number} saved as draft.", "success")
        return redirect(url_for("invoices.detail", invoice_id=inv.id))

    contacts = Contact.query.filter(Contact.active == True, Contact.type.in_(["customer", "both"])).order_by(Contact.name).all()  # noqa: E712
    revenue_accounts = Account.query.filter_by(type="revenue", active=True).order_by(Account.code).all()
    tax_rates = TaxRate.query.filter_by(active=True).all()
    cost_centers = CostCenter.query.filter_by(active=True).all()
    return render_template(
        "invoices/new.html", contacts=contacts, revenue_accounts=revenue_accounts,
        tax_rates=tax_rates, cost_centers=cost_centers,
        default_due=date.today() + timedelta(days=14), today=date.today(),
    )


@bp.route("/<int:invoice_id>")
@login_required
def detail(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    return render_template("invoices/detail.html", invoice=inv)


@bp.route("/<int:invoice_id>/send", methods=["POST"])
@login_required
def send(invoice_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    inv = Invoice.query.get_or_404(invoice_id)
    from app.controls import require_base_currency
    require_base_currency(inv.currency_code)
    if inv.status != InvoiceStatus.DRAFT.value:
        flash("Only draft invoices can be sent/posted.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    ar_account = get_system_account("1100")
    tax_payable = get_system_account("2200")

    je_lines = [{
        "account_id": ar_account.id,
        "debit_cents": inv.total_cents,
        "credit_cents": 0,
        "description": f"Invoice {inv.number} to {inv.contact.name}",
        "contact_id": inv.contact_id,
        "cost_center_id": inv.cost_center_id,
    }]
    for l in inv.lines:
        je_lines.append({
            "account_id": l.account_id,
            "debit_cents": 0,
            "credit_cents": l.amount_cents,
            "description": l.description,
            "cost_center_id": l.cost_center_id or inv.cost_center_id,
        })
    if inv.tax_cents:
        je_lines.append({
            "account_id": tax_payable.id,
            "debit_cents": 0,
            "credit_cents": inv.tax_cents,
            "description": f"Tax on invoice {inv.number}",
            "cost_center_id": inv.cost_center_id,
        })

    try:
        entry = create_journal_entry(
            inv.issue_date, f"Invoice {inv.number} issued to {inv.contact.name}", je_lines,
            source_type="invoice", source_id=inv.id, reference=inv.number, created_by_id=current_user.id,
        )
    except LedgerError as e:
        flash(f"Could not post invoice: {e}", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    inv.journal_entry_id = entry.id
    inv.status = InvoiceStatus.SENT.value
    db.session.commit()
    flash(f"Invoice {inv.number} posted and marked as sent.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/void", methods=["POST"])
@login_required
def void(invoice_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    inv = Invoice.query.get_or_404(invoice_id)
    if inv.status == InvoiceStatus.VOID.value:
        return redirect(url_for('invoices.detail', invoice_id=inv.id))
    if inv.amount_paid_cents:
        flash("Cannot void an invoice that has payments applied. Refund/unapply first.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))
    if inv.journal_entry_id:
        from app.models import JournalEntry
        entry = JournalEntry.query.get(inv.journal_entry_id)
        void_journal_entry(entry, created_by_id=current_user.id, reason=request.form.get('reason', 'Invoice void'))
    inv.status = InvoiceStatus.VOID.value
    db.session.commit()
    flash(f"Invoice {inv.number} voided.", "success")
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


@bp.route("/<int:invoice_id>/pdf")
@login_required
def pdf(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    from app.pdf_utils import invoice_pdf
    buf = invoice_pdf(inv)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f"inline; filename={inv.number}.pdf"
    return resp


# --- Recurring invoices ---

@bp.route("/recurring")
@login_required
def recurring_index():
    rules = RecurringInvoice.query.order_by(RecurringInvoice.next_run_date).all()
    return render_template("invoices/recurring.html", rules=rules)


@bp.route("/recurring/new", methods=["GET", "POST"])
@login_required
def recurring_new():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.recurring_index"))
    if request.method == "POST":
        lines_data = _parse_lines(request.form)
        rule = RecurringInvoice(
            contact_id=int(request.form["contact_id"]),
            frequency=request.form.get("frequency", "monthly"),
            next_run_date=date.fromisoformat(request.form["start_date"]),
            currency_code=request.form.get("currency_code", "HKD"),
            due_days=int(request.form.get("due_days", 14)),
            cost_center_id=int(request.form["cost_center_id"]) if request.form.get("cost_center_id") else None,
            template_json=json.dumps({"lines": lines_data, "notes": request.form.get("notes", "")}),
        )
        db.session.add(rule)
        db.session.commit()
        flash("Recurring invoice schedule created.", "success")
        return redirect(url_for("invoices.recurring_index"))

    contacts = Contact.query.filter(Contact.active == True, Contact.type.in_(["customer", "both"])).order_by(Contact.name).all()  # noqa: E712
    revenue_accounts = Account.query.filter_by(type="revenue", active=True).order_by(Account.code).all()
    tax_rates = TaxRate.query.filter_by(active=True).all()
    cost_centers = CostCenter.query.filter_by(active=True).all()
    return render_template(
        "invoices/recurring_new.html", contacts=contacts, revenue_accounts=revenue_accounts,
        tax_rates=tax_rates, cost_centers=cost_centers, today=date.today(),
    )


@bp.route("/recurring/<int:rule_id>/run", methods=["POST"])
@login_required
def recurring_run(rule_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.recurring_index"))
    rule = RecurringInvoice.query.get_or_404(rule_id)
    data = json.loads(rule.template_json)
    lines_data = data["lines"]
    subtotal = sum(l["amount_cents"] for l in lines_data)
    tax_total = sum(l["tax_cents"] for l in lines_data)
    inv = Invoice(
        number=_next_invoice_number(),
        contact_id=rule.contact_id,
        issue_date=rule.next_run_date,
        due_date=rule.next_run_date + timedelta(days=rule.due_days),
        currency_code=rule.currency_code,
        cost_center_id=rule.cost_center_id,
        notes=data.get("notes", ""),
        subtotal_cents=subtotal,
        tax_cents=tax_total,
        total_cents=subtotal + tax_total,
        status=InvoiceStatus.DRAFT.value,
        recurring_rule_id=rule.id,
    )
    db.session.add(inv)
    db.session.flush()
    for l in lines_data:
        db.session.add(InvoiceLine(invoice_id=inv.id, **{k: v for k, v in l.items() if k != "tax_cents"}))

    from app.dateutil_lite import advance_date
    rule.next_run_date = advance_date(rule.next_run_date, rule.frequency)
    db.session.commit()
    flash(f"Generated invoice {inv.number} from recurring schedule.", "success")
    return redirect(url_for("invoices.detail", invoice_id=inv.id))
