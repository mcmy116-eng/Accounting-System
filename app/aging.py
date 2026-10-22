"""Historical subledger balances from dated postings and payment allocations."""
from sqlalchemy import func
from app.extensions import db
from app.models import Invoice, Bill, JournalEntry, JournalLine, Account, Payment, PaymentAllocation


def aging_items(kind, as_of):
    model, source, control = (Invoice, 'invoice', '1100') if kind == 'invoice' else (Bill, 'bill', '2000')
    account = Account.query.filter_by(code=control).first()
    if not account:
        return []
    signed = JournalLine.debit_cents - JournalLine.credit_cents
    if kind == 'bill':
        signed = -signed
    movements = dict(db.session.query(JournalEntry.source_id, func.sum(signed))
                     .join(JournalLine).filter(JournalEntry.source_type == source,
                     JournalEntry.is_posted.is_(True), JournalEntry.date <= as_of,
                     JournalLine.account_id == account.id).group_by(JournalEntry.source_id).all())
    allocation_id = PaymentAllocation.invoice_id if kind == 'invoice' else PaymentAllocation.bill_id
    payments = dict(db.session.query(allocation_id, func.sum(PaymentAllocation.amount_cents))
                    .join(Payment).join(JournalEntry, Payment.journal_entry_id == JournalEntry.id)
                    .filter(Payment.date <= as_of, JournalEntry.date <= as_of, JournalEntry.is_posted.is_(True))
                    .group_by(allocation_id).all())
    items = []
    for obj in model.query.filter(model.journal_entry_id.isnot(None)).all():
        balance = int(movements.get(obj.id, 0) or 0) - int(payments.get(obj.id, 0) or 0)
        if balance:
            issue_date = obj.issue_date if kind == 'invoice' else obj.bill_date
            items.append((obj, obj.due_date or issue_date, balance))
    return items
