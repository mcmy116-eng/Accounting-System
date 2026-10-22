"""Double-entry ledger posting engine.

Every money-moving action in the app (invoice issued, bill approved, payment
made/received, manual journal) goes through here so that debits always equal
credits and every posted amount is traceable back to its source document.
"""
from datetime import date
from app.extensions import db
from app.models import JournalEntry, JournalLine, Account, AccountType, LedgerControl, JournalReversal


class LedgerError(Exception):
    pass


def create_journal_entry(entry_date, memo, lines, source_type="manual", source_id=None,
                          reference="", created_by_id=None, is_posted=True):
    """lines: list of dicts with keys account_id, debit_cents, credit_cents,
    description (optional), cost_center_id (optional), contact_id (optional)."""
    entry_date = entry_date or date.today()
    control = db.session.get(LedgerControl, 1)
    if is_posted and control and control.locked_through and entry_date <= control.locked_through:
        raise LedgerError(f'Books are locked through {control.locked_through}. Choose an open date or request an audited unlock.')
    for line in lines:
        debit, credit = line.get('debit_cents', 0), line.get('credit_cents', 0)
        if not isinstance(debit, int) or not isinstance(credit, int) or min(debit, credit) < 0 or max(debit, credit) > 2147483647 or (debit and credit):
            raise LedgerError('Each line must contain nonnegative integer cents on exactly one side.')
        account = db.session.get(Account, line['account_id'])
        if not account or not account.active:
            raise LedgerError('Every posting account must exist and be active.')
    total_debit = sum(l.get("debit_cents", 0) for l in lines)
    total_credit = sum(l.get("credit_cents", 0) for l in lines)
    if total_debit != total_credit:
        raise LedgerError(
            f"Journal entry not balanced: debit={total_debit} credit={total_credit}"
        )
    if total_debit == 0:
        raise LedgerError("Journal entry has zero value")

    entry = JournalEntry(
        date=entry_date or date.today(),
        memo=memo,
        reference=reference,
        source_type=source_type,
        source_id=source_id,
        created_by_id=created_by_id,
        is_posted=is_posted,
    )
    db.session.add(entry)
    db.session.flush()

    for l in lines:
        if not l.get("debit_cents") and not l.get("credit_cents"):
            continue
        jl = JournalLine(
            journal_entry_id=entry.id,
            account_id=l["account_id"],
            cost_center_id=l.get("cost_center_id"),
            contact_id=l.get("contact_id"),
            description=l.get("description", ""),
            debit_cents=l.get("debit_cents", 0),
            credit_cents=l.get("credit_cents", 0),
        )
        db.session.add(jl)

    from app import audit
    audit.record('post', 'journal', entry.id, memo[:255])
    return entry


def void_journal_entry(entry: JournalEntry, created_by_id=None, reason='Document void', reversal_date=None):
    """Reverse a posted entry with an equal-and-opposite entry rather than deleting it."""
    if not entry:
        return None
    previous = db.session.get(JournalReversal, entry.id)
    if previous:
        return db.session.get(JournalEntry, previous.reversal_id)
    if not entry.is_posted or JournalReversal.query.filter_by(reversal_id=entry.id).first():
        raise LedgerError('Only an original posted entry can be reversed.')
    if not reason.strip():
        raise LedgerError('A reversal reason is required.')
    reversal_date = reversal_date or max(date.today(), entry.date)
    if reversal_date < entry.date:
        raise LedgerError('A reversal cannot predate the original entry.')
    reversal_lines = []
    for l in entry.lines:
        reversal_lines.append({
            "account_id": l.account_id,
            "cost_center_id": l.cost_center_id,
            "contact_id": l.contact_id,
            "description": f"Reversal: {l.description}",
            "debit_cents": l.credit_cents,
            "credit_cents": l.debit_cents,
        })
    reversal = create_journal_entry(
        reversal_date, f"Reversal of JE#{entry.id} ({entry.memo})"[:255], reversal_lines,
        source_type=entry.source_type, source_id=entry.source_id,
        reference=entry.reference, created_by_id=created_by_id,
    )
    db.session.add(JournalReversal(original_id=entry.id, reversal_id=reversal.id, reason=reason[:500]))
    return reversal


def account_balance_cents(account: Account, as_of=None, cost_center_id=None):
    return account.balance_cents(as_of=as_of, cost_center_id=cost_center_id)


def trial_balance(as_of=None):
    accounts = Account.query.order_by(Account.code).all()
    rows = []
    total_debit = 0
    total_credit = 0
    for a in accounts:
        bal = account_balance_cents(a, as_of=as_of)
        if bal == 0:
            continue
        if a.type in (AccountType.ASSET.value, AccountType.EXPENSE.value):
            debit = bal if bal >= 0 else 0
            credit = -bal if bal < 0 else 0
        else:
            credit = bal if bal >= 0 else 0
            debit = -bal if bal < 0 else 0
        total_debit += debit
        total_credit += credit
        rows.append({"account": a, "debit": debit, "credit": credit})
    return rows, total_debit, total_credit
