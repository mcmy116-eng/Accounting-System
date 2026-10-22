"""Validate the complete statement before committing any rows."""
import csv
import io
import hashlib
import uuid
from pathlib import Path
from datetime import datetime
from flask import current_app
from werkzeug.utils import secure_filename
from app.extensions import db
from app.models import BankImport, BankTransaction, cents
from app.ledger import LedgerError
from app import audit


def import_statement(bank, file):
    raw = file.read()
    digest = hashlib.sha256(raw).hexdigest()
    if BankImport.query.filter_by(bank_account_id=bank.id, sha256=digest).first():
        return 0, 'This exact statement was already imported; existing matches are unchanged.'
    try:
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
        fields = {f.lower().strip(): f for f in reader.fieldnames or []}
        date_key = next((v for k, v in fields.items() if 'date' in k), None)
        desc_key = next((v for k, v in fields.items() if k in ('description','memo','narrative')), None)
        amount_key = fields.get('amount')
        debit_key = next((v for k, v in fields.items() if k in ('debit','withdrawal','out')), None)
        credit_key = next((v for k, v in fields.items() if k in ('credit','deposit','in')), None)
        id_key = next((v for k, v in fields.items() if k in ('transaction id','transaction_id','id')), None)
        if not date_key or not (amount_key or debit_key or credit_key):
            raise LedgerError('CSV requires Date and Amount (or Debit/Credit). Optional Transaction ID distinguishes genuine equal-value transactions.')
        errors, rows, seen, skipped = [], [], set(), 0
        for line, row in enumerate(reader, 2):
            try:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError('incorrect column count')
                value = row[date_key].strip()
                dt = None
                for fmt in ('%Y-%m-%d','%d/%m/%Y','%d-%m-%Y'):
                    try: dt = datetime.strptime(value, fmt).date(); break
                    except ValueError: pass
                if not dt: raise ValueError('invalid date (use YYYY-MM-DD or DD/MM/YYYY)')
                amount = cents(row[amount_key]) if amount_key else cents(row.get(credit_key, '')) - cents(row.get(debit_key, ''))
                if not amount: raise ValueError('zero or missing amount')
                desc = (row.get(desc_key) or '').strip()
                if len(desc) > 255: raise ValueError('description exceeds 255 characters')
                external = (row.get(id_key) or '').strip()
                key = 'id:'+hashlib.sha256(external.encode()).hexdigest() if external else f'file:{digest}:{line}'
                previous = BankTransaction.query.filter_by(bank_account_id=bank.id, source_key=key).first()
                if previous:
                    if (previous.date, previous.amount_cents, previous.description) != (dt, amount, desc):
                        raise ValueError('existing transaction ID has different data')
                    skipped += 1; continue
                if key in seen: raise ValueError('duplicate Transaction ID within this file')
                seen.add(key)
                # Without source IDs, overlap is ambiguous; require review, never silently delete it.
                if not external and BankTransaction.query.filter_by(bank_account_id=bank.id,date=dt,amount_cents=amount,description=desc).first():
                    raise ValueError('possible overlap with an earlier statement; supply the bank Transaction ID after review')
                rows.append(dict(bank_account_id=bank.id,date=dt,description=desc,amount_cents=amount,source_key=key,import_batch=digest[:32],status='unmatched'))
            except (ValueError, ArithmeticError) as exc:
                errors.append(f'Row {line}: {exc}')
        if errors:
            raise LedgerError('Nothing imported. ' + '; '.join(errors[:50]))
        if not rows and not skipped:
            raise LedgerError('The statement has no transaction rows.')
    except UnicodeError:
        raise LedgerError('CSV must be UTF-8. Nothing imported.')
    path = Path(current_app.config['UPLOAD_FOLDER']) / ('bank-'+uuid.uuid4().hex+'.csv')
    path.write_bytes(raw)
    db.session.add(BankImport(bank_account_id=bank.id,sha256=digest,filepath=str(path),filename=secure_filename(file.filename)[:255] or 'statement.csv'))
    for row in rows: db.session.add(BankTransaction(**row))
    audit.record('import', 'bank', bank.id, f'{len(rows)} imported; {skipped} already present', {'sha256':digest})
    db.session.commit()
    return len(rows), f'Imported {len(rows)} transactions; skipped {skipped} existing source IDs.'
