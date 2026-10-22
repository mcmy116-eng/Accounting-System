"""One authorised, labelled sample bundle for the empty HAKUGEN Render ledger.

No external APIs, messages or real payments. Run at service startup, after the
persistent upload disk is mounted. Never import into an occupied ledger or reset
an existing bundle. All DB rows commit together under the accounting lock.
"""
import csv
import hashlib
import io
import json
import shutil
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import current_app
from sqlalchemy import inspect

from app import audit
from app.controls import lock_books
from app.extensions import db
from app.ledger import create_journal_entry, trial_balance
from app.models import (
    Account, BankAccount, BankImport, BankTransaction, Bill, BillLine,
    CompanySettings, Contact, CostCenter, Currency, Document, ExpenseClaim,
    Invoice, InvoiceLine, JournalEntry, JournalLine, LedgerControl, Payment,
    PaymentAllocation, RecurringInvoice, SampleDataBundle, SyncedOrder,
)

BUNDLE_KEY = 'hakugen-dummy-20261022-v1'
TARGET_SERVICE = 'srv-d9ne0vm417fc73d5vms0'
MARK = 'Dummy/testing 數'
NOTICE = 'Dummy/testing data included. Synthetic examples only; not actual company transactions.'
START, END = date(2026, 10, 1), date(2026, 10, 31)
EXPECTED = {
    'Revenue': 1200000, 'Expenses': 525000, 'Profit': 675000,
    'Bank': 775000, 'Accounts receivable': 700000,
    'Accounts payable': 600000, 'Inventory': 300000,
    'Trial balance debit': 2300000, 'Trial balance credit': 2300000,
}


def installed_bundle():
    return SampleDataBundle.query.filter_by(key=BUNDLE_KEY, status='installed').first()


def current_snapshot():
    """Whole-ledger October values for comparison with the initial sample."""
    balances = {a.code: a.balance_cents(END) for a in Account.query.all()}
    revenue = sum(a.balance_cents(END) - a.balance_cents(date(2026, 9, 30))
                  for a in Account.query.filter_by(type='revenue').all())
    expenses = sum(a.balance_cents(END) - a.balance_cents(date(2026, 9, 30))
                   for a in Account.query.filter_by(type='expense').all())
    _, debit, credit = trial_balance(END)
    return {'Revenue': revenue, 'Expenses': expenses, 'Profit': revenue - expenses,
            'Bank': balances.get('DUMMY-BANK-20261022', 0),
            'Accounts receivable': balances.get('1100', 0),
            'Accounts payable': balances.get('2000', 0),
            'Inventory': balances.get('1200', 0),
            'Trial balance debit': debit, 'Trial balance credit': credit}


def _pdf(title, lines):
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from xml.sax.saxutils import escape
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    story = [Paragraph('Dummy/testing - SYNTHETIC DOCUMENT', styles['Title']),
             Paragraph('NOT A REAL INVOICE, RECEIPT OR PAYMENT. NOT FOR TAX FILING.', styles['Normal']),
             Spacer(1, 15), Paragraph(escape(title), styles['Heading2'])]
    story.extend(Paragraph(escape(str(line)), styles['Normal']) for line in lines)
    SimpleDocTemplate(buf, title='Dummy/testing - ' + title).build(story)
    return buf.getvalue()


def install_dummy_data(service_id, today=None):
    """Standalone transaction; return persisted scope/result, never overwrite."""
    if service_id != TARGET_SERVICE:
        return {'status': 'not_applicable', 'reason': 'Different service; no sample data written.'}
    today = today or datetime.now(ZoneInfo('Asia/Hong_Kong')).date()
    root = None
    committed = False
    try:
        lock_books()
        existing = db.session.get(SampleDataBundle, BUNDLE_KEY)
        if existing:
            result = json.loads(existing.manifest_json)
            db.session.rollback()
            return dict(result, repeated=True)

        def blocked(reason):
            result = {'status': 'blocked', 'reason': reason, 'bundle': BUNDLE_KEY}
            db.session.add(SampleDataBundle(key=BUNDLE_KEY, status='blocked',
                                           manifest_json=json.dumps(result)))
            audit.record('sample_import_blocked', 'sample_data', summary=MARK + ': ' + reason)
            db.session.commit()
            return result

        # This request is bounded to this deployment window, not future empty books.
        if not date(2026, 10, 22) <= today <= date(2026, 10, 29):
            return blocked('The authorised sample import window has expired or not started.')
        models = (JournalEntry, JournalLine, Invoice, Bill, ExpenseClaim, Payment,
                  PaymentAllocation, BankTransaction, BankImport, Document,
                  SyncedOrder, RecurringInvoice)
        schema = inspect(db.engine)
        for model in models:
            if not schema.has_table(model.__tablename__):
                return blocked('Required accounting schema is unavailable.')
        counts = {m.__tablename__: m.query.count() for m in models}
        if any(counts.values()):
            return blocked('Existing accounting records found; no dummy transactions were added.')
        control = db.session.get(LedgerControl, 1)
        if control.locked_through:
            return blocked('A period is already locked; no automatic unlock was attempted.')
        settings = CompanySettings.query.first()
        if not settings or not db.session.get(Currency, settings.base_currency):
            return blocked('Company and base currency must already be configured.')
        ccy = settings.base_currency
        account_types = {'1100': 'asset', '1200': 'asset', '2000': 'liability',
                         '3000': 'equity', '4000': 'revenue', '5000': 'expense',
                         '6010': 'expense', '6050': 'expense', '6080': 'expense'}
        accounts = {a.code: a for a in Account.query.filter(Account.code.in_(account_types)).all()}
        if any(code not in accounts or not accounts[code].active or accounts[code].type != kind
               for code, kind in account_types.items()):
            return blocked('Required active chart accounts do not match the sample.')
        if (Account.query.filter_by(code='DUMMY-BANK-20261022').first() or
                CostCenter.query.filter_by(code='DUMMY-20261022').first() or
                Contact.query.filter(Contact.name.like(MARK + '%')).first()):
            return blocked('Reserved dummy identifiers already exist; review before importing.')

        destination = Path(current_app.config['UPLOAD_FOLDER']).resolve() / BUNDLE_KEY
        if destination.exists():
            return blocked('The reserved evidence folder already exists; nothing was overwritten.')
        destination.mkdir(mode=0o700)
        root = destination  # Only remove a folder created by this attempt on rollback.
        segment = CostCenter(code='DUMMY-20261022', name=MARK)
        customer = Contact(type='customer', name=MARK + ' | Customer A', currency_code=ccy, notes=MARK)
        vendor = Contact(type='vendor', name=MARK + ' | Supplier A', currency_code=ccy, notes=MARK)
        bank_gl = Account(code='DUMMY-BANK-20261022', name=MARK + ' | Training bank',
                          type='asset', is_bank=True, description=NOTICE)
        db.session.add_all([segment, customer, vendor, bank_gl])
        db.session.flush()
        bank = BankAccount(name=MARK + ' | Training bank', account_number='NOT-A-REAL-BANK-ACCOUNT',
                           currency_code=ccy, gl_account_id=bank_gl.id, opening_balance_cents=0)
        db.session.add(bank)
        db.session.flush()
        entries, docs, cash, invoices, bills, payments = [], [], [], [], [], []

        def posting(day, ref, purpose, debit, credit, amount, source='manual', source_id=None):
            entry = create_journal_entry(date(2026, 10, day), MARK + ' | ' + purpose,
                [{'account_id': debit.id, 'debit_cents': amount, 'cost_center_id': segment.id,
                  'description': MARK + ' | ' + ref},
                 {'account_id': credit.id, 'credit_cents': amount, 'cost_center_id': segment.id,
                  'description': MARK + ' | ' + ref}],
                source_type=source, source_id=source_id, reference=ref)
            db.session.flush()
            entries.append(entry)
            if bank_gl.id in (debit.id, credit.id):
                cash.append((entry, amount if debit.id == bank_gl.id else -amount))
            return entry

        def evidence(entry, purpose, amount, bill=None):
            data = _pdf(entry.reference, [purpose, f'Date: {entry.date}',
                        f'Amount: {ccy} {amount / 100:,.2f}',
                        'All parties, transactions and bank movements are fabricated for testing.'])
            path = root / (entry.reference + '.pdf')
            path.write_bytes(data)
            doc = Document(filename=path.name, filepath=str(path), mimetype='application/pdf',
                           sha256=hashlib.sha256(data).hexdigest(),
                           bill_id=bill.id if bill else None,
                           journal_entry_id=None if bill else entry.id)
            db.session.add(doc)
            docs.append(doc)

        entry = posting(1, 'DUMMY-CAPITAL-01', 'Capital deposit', bank_gl, accounts['3000'], 500000)
        evidence(entry, 'Synthetic capital contribution to training bank; no actual transfer.', 500000)
        for number, day, amount, draft in [('DUMMY-INV-001', 2, 1000000, False),
                                            ('DUMMY-INV-002', 3, 200000, False),
                                            ('DUMMY-INV-DRAFT', 20, 50000, True)]:
            inv = Invoice(number=number, contact_id=customer.id, issue_date=date(2026, 10, day),
                          due_date=date(2026, 10, 30), currency_code=ccy,
                          cost_center_id=segment.id, subtotal_cents=amount, total_cents=amount,
                          notes=MARK + ' | Synthetic invoice, not actual revenue.',
                          status='draft' if draft else 'sent')
            db.session.add(inv)
            db.session.flush()
            db.session.add(InvoiceLine(invoice_id=inv.id, description=MARK + ' | Demo services',
                quantity=1, unit_price_cents=amount, amount_cents=amount,
                account_id=accounts['4000'].id, cost_center_id=segment.id))
            if not draft:
                entry = posting(day, number, 'Invoice', accounts['1100'], accounts['4000'],
                                amount, 'invoice', inv.id)
                inv.journal_entry_id = entry.id
                evidence(entry, 'Synthetic sales invoice to DUMMY Customer A.', amount)
            invoices.append(inv)

        for number, day, amount, code, purpose in [
                ('DUMMY-BILL-001', 4, 120000, '6010', 'Software subscription'),
                ('DUMMY-BILL-002', 5, 300000, '6050', 'Office supplies'),
                ('DUMMY-BILL-003', 6, 400000, '1200', 'Inventory purchase')]:
            bill = Bill(number=number, vendor_ref=number, contact_id=vendor.id,
                bill_date=date(2026, 10, day), due_date=date(2026, 10, 30), currency_code=ccy,
                cost_center_id=segment.id, subtotal_cents=amount, total_cents=amount,
                notes=MARK + ' | ' + purpose + '; synthetic only.', status='approved')
            db.session.add(bill)
            db.session.flush()
            db.session.add(BillLine(bill_id=bill.id, description=MARK + ' | ' + purpose,
                quantity=1, unit_price_cents=amount, amount_cents=amount,
                account_id=accounts[code].id, cost_center_id=segment.id))
            entry = posting(day, number, purpose, accounts[code], accounts['2000'], amount, 'bill', bill.id)
            bill.journal_entry_id = entry.id
            evidence(entry, 'Synthetic supplier bill: ' + purpose, amount, bill)
            bills.append(bill)

        for obj, day, amount, direction, ref in [
                (invoices[0], 12, 300000, 'in', 'DUMMY-RECEIPT-001'),
                (invoices[1], 10, 100000, 'in', 'DUMMY-RECEIPT-002'),
                (invoices[1], 15, 100000, 'in', 'DUMMY-RECEIPT-003'),
                (bills[0], 12, 120000, 'out', 'DUMMY-PAYMENT-001'),
                (bills[1], 16, 100000, 'out', 'DUMMY-PAYMENT-002')]:
            incoming = direction == 'in'
            pay = Payment(direction=direction, contact_id=obj.contact_id, date=date(2026, 10, day),
                amount_cents=amount, currency_code=ccy, bank_account_id=bank.id,
                method=MARK, reference=ref)
            db.session.add(pay)
            db.session.flush()
            allocation = PaymentAllocation(payment_id=pay.id, amount_cents=amount)
            if incoming:
                allocation.invoice_id = obj.id
            else:
                allocation.bill_id = obj.id
            db.session.add(allocation)
            entry = posting(day, ref, 'Receipt' if incoming else 'Payment',
                bank_gl if incoming else accounts['2000'],
                accounts['1100'] if incoming else bank_gl, amount, 'payment', pay.id)
            pay.journal_entry_id = entry.id
            obj.amount_paid_cents += amount
            obj.status = 'paid' if obj.amount_paid_cents == obj.total_cents else 'partial'
            evidence(entry, 'Synthetic payment allocation to ' + obj.number + '; no real money moved.', amount)
            payments.append(pay)

        entry = posting(18, 'DUMMY-COGS-001', 'Inventory cost adjustment', accounts['5000'], accounts['1200'], 100000)
        evidence(entry, 'Synthetic external stock workpaper: 4,000 purchased less 1,000 COGS = 3,000 remaining. No automatic SKU costing.', 100000)
        entry = posting(19, 'DUMMY-FEE-001', 'Bank fee', accounts['6080'], bank_gl, 5000)
        evidence(entry, 'Synthetic bank fee; no real bank charge.', 5000)

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(['Date', 'Description', 'Amount', 'Transaction ID'])
        for entry, amount in sorted(cash, key=lambda item: (item[0].date, item[0].id)):
            writer.writerow([entry.date, entry.memo, f'{amount / 100:.2f}', entry.reference])
        data = buf.getvalue().encode('utf-8-sig')
        path = root / 'DUMMY-bank-statement.csv'
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        db.session.add(BankImport(bank_account_id=bank.id, sha256=digest, filepath=str(path), filename=path.name))
        for entry, amount in cash:
            line = next(line for line in entry.lines if line.account_id == bank_gl.id)
            db.session.add(BankTransaction(bank_account_id=bank.id, date=entry.date,
                description=entry.memo, amount_cents=amount, import_batch=digest[:32],
                source_key='id:' + hashlib.sha256(entry.reference.encode()).hexdigest(),
                status='matched', matched_journal_line_id=line.id))
        db.session.flush()
        if current_snapshot() != EXPECTED or any(not e.is_balanced() for e in entries):
            raise ValueError('Sample accounting self-check failed; entire import rolled back.')
        for doc in docs:
            if hashlib.sha256(Path(doc.filepath).read_bytes()).hexdigest() != doc.sha256:
                raise ValueError('Sample evidence readback failed.')
        result = {'status': 'installed', 'bundle': BUNDLE_KEY, 'remark': MARK,
                  'currency': ccy, 'start': str(START), 'end': str(END),
                  'expected_cents': EXPECTED, 'self_check': 'PASSED',
                  'bank_id': bank.id, 'cost_center_id': segment.id,
                  'invoice_ids': [x.id for x in invoices], 'bill_ids': [x.id for x in bills],
                  'payment_ids': [x.id for x in payments],
                  'journal_ids': [x.id for x in entries], 'document_ids': [x.id for x in docs],
                  'bank_rows': len(cash), 'counts_before': counts,
                  'authorization': 'Mic requested Dummy/testing transactions on 2026-10-22.'}
        db.session.add(SampleDataBundle(key=BUNDLE_KEY, status='installed', manifest_json=json.dumps(result)))
        audit.record('sample_import', 'sample_data', summary=MARK + ' | Authorised synthetic sample bundle', detail=result)
        db.session.commit()
        committed = True
        db.session.expire_all()
        saved = db.session.get(SampleDataBundle, BUNDLE_KEY)
        if (json.loads(saved.manifest_json) != result or
                JournalEntry.query.filter(JournalEntry.id.in_(result['journal_ids'])).count() != 13 or
                Document.query.filter(Document.id.in_(result['document_ids'])).count() != 13):
            raise ValueError('Committed sample readback needs review; do not reimport.')
        db.session.rollback()
        return dict(result, readback_verified=True)
    except Exception:
        db.session.rollback()
        if root and not committed:
            shutil.rmtree(root)
        raise
