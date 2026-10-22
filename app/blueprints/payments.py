from datetime import date
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import (
    Payment, PaymentAllocation, Invoice, Bill, BankAccount, InvoiceStatus, BillStatus, cents,
)
from app.ledger import create_journal_entry, LedgerError
from app.system_accounts import get_system_account

bp = Blueprint("payments", __name__, url_prefix="/payments")


@bp.route("/")
@login_required
def index():
    payments = Payment.query.order_by(Payment.date.desc(), Payment.id.desc()).all()
    return render_template("payments/index.html", payments=payments)


def _refresh_invoice_status(inv: Invoice):
    if inv.amount_paid_cents <= 0:
        inv.status = InvoiceStatus.SENT.value if inv.status != InvoiceStatus.VOID.value else inv.status
    elif inv.amount_paid_cents >= inv.total_cents:
        inv.status = InvoiceStatus.PAID.value
    else:
        inv.status = InvoiceStatus.PARTIAL.value


def _refresh_bill_status(bill: Bill):
    if bill.amount_paid_cents <= 0:
        bill.status = BillStatus.APPROVED.value if bill.status != BillStatus.VOID.value else bill.status
    elif bill.amount_paid_cents >= bill.total_cents:
        bill.status = BillStatus.PAID.value
    else:
        bill.status = BillStatus.PARTIAL.value


@bp.route("/receive/<int:invoice_id>", methods=["GET", "POST"])
@login_required
def receive(invoice_id):
    inv = Invoice.query.get_or_404(invoice_id)
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    if request.method == "POST":
        if inv.status not in ('sent', 'partial', 'overdue') or not inv.journal_entry_id:
            abort(409, 'Only posted, outstanding invoices can receive payment.')
        amount = cents(request.form.get("amount"))
        bank_account_id = int(request.form["bank_account_id"])
        pay_date = date.fromisoformat(request.form["date"])
        method = request.form.get("method", "")
        reference = request.form.get("reference", "")

        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return redirect(url_for("payments.receive", invoice_id=invoice_id))
        if amount > inv.balance_due_cents():
            flash("Amount exceeds the outstanding balance.", "error")
            return redirect(url_for("payments.receive", invoice_id=invoice_id))

        bank = BankAccount.query.get_or_404(bank_account_id)
        from app.controls import require_base_currency
        require_base_currency(inv.currency_code)
        if not bank.active or bank.currency_code != inv.currency_code or pay_date < inv.issue_date:
            abort(409, 'Use an active bank in the invoice currency and a date on/after issue.')
        if reference and Payment.query.join(PaymentAllocation).filter(PaymentAllocation.invoice_id == inv.id, Payment.reference == reference, Payment.bank_account_id == bank.id).first():
            abort(409, 'This receipt reference is already allocated to this invoice.')
        ar_account = get_system_account("1100")

        payment = Payment(direction="in", contact_id=inv.contact_id, date=pay_date,
                           amount_cents=amount, currency_code=inv.currency_code,
                           bank_account_id=bank.id, method=method, reference=reference)
        db.session.add(payment)
        db.session.flush()
        db.session.add(PaymentAllocation(payment_id=payment.id, invoice_id=inv.id, amount_cents=amount))

        try:
            entry = create_journal_entry(
                pay_date, f"Payment received for invoice {inv.number}",
                [
                    {"account_id": bank.gl_account_id, "debit_cents": amount, "credit_cents": 0,
                     "description": f"Payment for {inv.number}", "contact_id": inv.contact_id},
                    {"account_id": ar_account.id, "debit_cents": 0, "credit_cents": amount,
                     "description": f"Payment for {inv.number}", "contact_id": inv.contact_id},
                ],
                source_type="payment", source_id=payment.id, reference=reference,
                created_by_id=current_user.id,
            )
        except LedgerError as e:
            db.session.rollback()
            flash(f"Could not post payment: {e}", "error")
            return redirect(url_for("payments.receive", invoice_id=invoice_id))

        payment.journal_entry_id = entry.id
        inv.amount_paid_cents += amount
        _refresh_invoice_status(inv)
        db.session.commit()
        flash(f"Payment of {inv.currency_code} {amount/100:,.2f} recorded against {inv.number}.", "success")
        return redirect(url_for("invoices.detail", invoice_id=invoice_id))

    bank_accounts = BankAccount.query.filter_by(active=True).all()
    return render_template("payments/receive.html", invoice=inv, bank_accounts=bank_accounts, today=date.today())


@bp.route("/pay/<int:bill_id>", methods=["GET", "POST"])
@login_required
def pay(bill_id):
    bill = Bill.query.get_or_404(bill_id)
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    if request.method == "POST":
        if bill.status not in ('approved', 'partial') or not bill.journal_entry_id:
            abort(409, 'Only approved, outstanding bills can be paid.')
        amount = cents(request.form.get("amount"))
        bank_account_id = int(request.form["bank_account_id"])
        pay_date = date.fromisoformat(request.form["date"])
        method = request.form.get("method", "")
        reference = request.form.get("reference", "")

        if amount <= 0:
            flash("Amount must be greater than zero.", "error")
            return redirect(url_for("payments.pay", bill_id=bill_id))
        if amount > bill.balance_due_cents():
            flash("Amount exceeds the outstanding balance.", "error")
            return redirect(url_for("payments.pay", bill_id=bill_id))

        bank = BankAccount.query.get_or_404(bank_account_id)
        from app.controls import require_base_currency
        require_base_currency(bill.currency_code)
        if not bank.active or bank.currency_code != bill.currency_code or pay_date < bill.bill_date:
            abort(409, 'Use an active bank in the bill currency and a date on/after the bill.')
        if reference and Payment.query.join(PaymentAllocation).filter(PaymentAllocation.bill_id == bill.id, Payment.reference == reference, Payment.bank_account_id == bank.id).first():
            abort(409, 'This payment reference is already allocated to this bill.')
        ap_account = get_system_account("2000")

        payment = Payment(direction="out", contact_id=bill.contact_id, date=pay_date,
                           amount_cents=amount, currency_code=bill.currency_code,
                           bank_account_id=bank.id, method=method, reference=reference)
        db.session.add(payment)
        db.session.flush()
        db.session.add(PaymentAllocation(payment_id=payment.id, bill_id=bill.id, amount_cents=amount))

        try:
            entry = create_journal_entry(
                pay_date, f"Payment made for bill {bill.number}",
                [
                    {"account_id": ap_account.id, "debit_cents": amount, "credit_cents": 0,
                     "description": f"Payment for {bill.number}", "contact_id": bill.contact_id},
                    {"account_id": bank.gl_account_id, "debit_cents": 0, "credit_cents": amount,
                     "description": f"Payment for {bill.number}", "contact_id": bill.contact_id},
                ],
                source_type="payment", source_id=payment.id, reference=reference,
                created_by_id=current_user.id,
            )
        except LedgerError as e:
            db.session.rollback()
            flash(f"Could not post payment: {e}", "error")
            return redirect(url_for("payments.pay", bill_id=bill_id))

        payment.journal_entry_id = entry.id
        bill.amount_paid_cents += amount
        _refresh_bill_status(bill)
        db.session.commit()
        flash(f"Payment of {bill.currency_code} {amount/100:,.2f} recorded against {bill.number}.", "success")
        return redirect(url_for("bills.detail", bill_id=bill_id))

    bank_accounts = BankAccount.query.filter_by(active=True).all()
    return render_template("payments/pay.html", bill=bill, bank_accounts=bank_accounts, today=date.today())
