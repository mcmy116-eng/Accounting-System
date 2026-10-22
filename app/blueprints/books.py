"""Traceable journal review, controlled adjustments and period closing."""
from datetime import date, timedelta
from pathlib import Path
import hashlib, uuid
from flask import Blueprint, render_template, request, redirect, url_for, abort, current_app, Response
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from sqlalchemy import or_, func
from app.extensions import db
from app.models import (Account, JournalEntry, JournalLine, Document, Invoice, Bill, ExpenseClaim,
                        Payment, PaymentAllocation, BankTransaction, LedgerControl, AuditLog, IntegrationReview, cents)
from app.ledger import create_journal_entry, void_journal_entry, trial_balance
from app import audit

bp = Blueprint('books', __name__, url_prefix='/books')


def diagnostics(as_of):
    issues = []
    entries = JournalEntry.query.filter(JournalEntry.is_posted.is_(True), JournalEntry.date <= as_of).all()
    for entry in entries:
        if not entry.lines or not entry.is_balanced(): issues.append(f'JE#{entry.id}: unbalanced or empty')
    for a in Account.query.filter_by(active=False).all():
        if a.balance_cents(as_of): issues.append(f'Account {a.code}: inactive with a balance (still included in reports)')
    for txn in BankTransaction.query.filter(BankTransaction.date <= as_of, BankTransaction.matched_journal_line_id.isnot(None)).all():
        line = db.session.get(JournalLine, txn.matched_journal_line_id)
        if not line or not txn.bank_account or line.account_id != txn.bank_account.gl_account_id or not line.entry.is_posted or line.debit_cents-line.credit_cents != txn.amount_cents:
            issues.append(f'Bank row#{txn.id}: invalid match')
    duplicates = db.session.query(BankTransaction.matched_journal_line_id, func.count()).filter(BankTransaction.matched_journal_line_id.isnot(None)).group_by(BankTransaction.matched_journal_line_id).having(func.count()>1).all()
    for line_id, count in duplicates: issues.append(f'Journal line#{line_id}: matched to {count} bank rows')
    for pay in Payment.query.filter(Payment.date <= as_of).all():
        if not pay.journal_entry_id or sum(a.amount_cents for a in pay.allocations) != pay.amount_cents:
            issues.append(f'Payment#{pay.id}: allocation / posting mismatch')
    from app.aging import aging_items
    for kind, code in [('invoice','1100'),('bill','2000')]:
        a = Account.query.filter_by(code=code).first()
        items = aging_items(kind, as_of)
        if a and a.balance_cents(as_of) != sum(x[2] for x in items): issues.append(f'{code}: ledger and dated subledger do not agree')
        for obj, _, balance in items:
            if balance < 0: issues.append(f'{obj.number}: unexpected credit balance; check reversals / receipts')
    for review in IntegrationReview.query.filter_by(status='pending').all(): issues.append(f'Sales change#{review.id}: awaiting evidence and adjustment')
    return issues


@bp.route('/')
@login_required
def index():
    q = request.args.get('q', '').strip()
    query = JournalEntry.query
    if q:
        pattern = '%'+q+'%'
        query = query.filter(or_(JournalEntry.memo.ilike(pattern), JournalEntry.reference.ilike(pattern),
                              JournalEntry.lines.any(JournalLine.description.ilike(pattern))))
    for key, op in [('start','ge'),('end','le')]:
        if request.args.get(key):
            dt=date.fromisoformat(request.args[key]);query=query.filter(JournalEntry.date >= dt if op=='ge' else JournalEntry.date <= dt)
    if request.args.get('account_id',type=int):query=query.filter(JournalEntry.lines.any(JournalLine.account_id==request.args.get('account_id',type=int)))
    for key, op in [('min_amount','ge'),('max_amount','le')]:
        if request.args.get(key):
            value=cents(request.args[key]);total=db.session.query(func.sum(JournalLine.debit_cents)).filter(JournalLine.journal_entry_id==JournalEntry.id).correlate(JournalEntry).scalar_subquery()
            query=query.filter(total>=value if op=='ge' else total<=value)
    entries=query.order_by(JournalEntry.date.desc(),JournalEntry.id.desc()).all()
    return render_template('books/index.html', entries=entries, accounts=Account.query.order_by(Account.code).all())


@bp.route('/<int:entry_id>')
@login_required
def detail(entry_id):
    entry=JournalEntry.query.get_or_404(entry_id)
    source=None;endpoint=None;kwargs={};documents=Document.query.filter_by(journal_entry_id=entry.id).all();related=[]
    if entry.source_type in ('invoice','bill','claim'):
        model={'invoice':Invoice,'bill':Bill,'claim':ExpenseClaim}[entry.source_type];source=db.session.get(model,entry.source_id)
        if source:
            endpoint={'invoice':'invoices.detail','bill':'bills.detail','claim':'claims.detail'}[entry.source_type];kwargs={entry.source_type+'_id':source.id}
            if entry.source_type=='bill':documents+=Document.query.filter_by(bill_id=source.id).all()
            if entry.source_type=='claim':documents+=Document.query.filter_by(claim_id=source.id).all()
            if entry.source_type in ('bill','invoice'):
                field=PaymentAllocation.bill_id if entry.source_type=='bill' else PaymentAllocation.invoice_id
                related=[a.payment.journal_entry_id for a in PaymentAllocation.query.filter(field==source.id).all() if a.payment.journal_entry_id]
    elif entry.source_type=='payment':
        payment=db.session.get(Payment,entry.source_id)
        if payment:
            for allocation in payment.allocations:
                obj=allocation.invoice or allocation.bill
                if obj and obj.journal_entry_id:related.append(obj.journal_entry_id)
    elif entry.source_type=='claim_payment':
        claim=db.session.get(ExpenseClaim,entry.source_id)
        if claim and claim.journal_entry_id:related.append(claim.journal_entry_id)
    txns=BankTransaction.query.filter(BankTransaction.matched_journal_line_id.in_([l.id for l in entry.lines])).all()
    logs=AuditLog.query.filter_by(entity_type='journal',entity_id=entry.id).order_by(AuditLog.at).all()
    return render_template('books/detail.html',entry=entry,source_url=url_for(endpoint,**kwargs) if endpoint else None,
                           documents=documents,txns=txns,related=related,logs=logs)


@bp.route('/new',methods=['GET','POST'])
@login_required
def new():
    if not current_user.can_edit():abort(403)
    if request.method=='POST':
        fields=[request.form.getlist(x) for x in ['account_id[]','debit[]','credit[]','description[]']]
        if len(set(map(len,fields)))!=1 or not 2<=len(fields[0])<=500:abort(400,'Provide at least two complete journal lines.')
        lines=[]
        for aid,debit,credit,desc in zip(*fields):
            if not aid:continue
            account=Account.query.get_or_404(int(aid))
            if account.code in ('1100','2000','2550'):abort(409,'Use the invoice/bill/claim workflow for control accounts so the subledger stays reconciled.')
            lines.append(dict(account_id=account.id,debit_cents=cents(debit),credit_cents=cents(credit),description=desc[:255]))
        memo=request.form.get('memo','').strip();file=request.files.get('file');drive=request.form.get('drive_file_id','').strip()
        if not memo or not file or not file.filename:abort(400,'Explain the adjustment and attach the supporting calculation or receipt.')
        entry=create_journal_entry(date.fromisoformat(request.form['date']),memo[:255],lines,reference=request.form.get('reference','')[:100],created_by_id=current_user.id)
        filename=secure_filename(file.filename)[:255] or 'evidence.bin';data=file.read();path=Path(current_app.config['UPLOAD_FOLDER'])/('journal-'+uuid.uuid4().hex+'-'+filename)
        path.write_bytes(data)
        db.session.add(Document(filename=filename,filepath=str(path),mimetype=file.mimetype,uploaded_by_id=current_user.id,journal_entry_id=entry.id,sha256=hashlib.sha256(data).hexdigest(),drive_file_id=drive[:200] or None))
        db.session.commit();return redirect(url_for('books.detail',entry_id=entry.id))
    accounts=Account.query.filter(Account.active.is_(True),Account.code.notin_(['1100','2000','2550'])).order_by(Account.code).all()
    return render_template('books/new.html',accounts=accounts,today=date.today())


@bp.route('/<int:entry_id>/reverse',methods=['POST'])
@login_required
def reverse(entry_id):
    if not current_user.can_edit():abort(403)
    entry=JournalEntry.query.get_or_404(entry_id)
    if entry.source_type!='manual':abort(409,'Use the source document workflow to reverse this entry.')
    reason=request.form.get('reason','').strip()
    reversal=void_journal_entry(entry,current_user.id,reason,date.fromisoformat(request.form['date']))
    audit.record('reverse','journal',entry.id,reason,{'reversal_id':reversal.id})
    db.session.commit();return redirect(url_for('books.detail',entry_id=reversal.id))


@bp.route('/close',methods=['GET','POST'])
@login_required
def close():
    control=db.session.get(LedgerControl,1)
    as_of=date.fromisoformat(request.values.get('as_of') or date.today().isoformat())
    issues=diagnostics(as_of)
    if request.method=='POST':
        if not current_user.is_admin():abort(403)
        reason=request.form.get('reason','').strip()
        if not reason:abort(400,'A close/unlock explanation is required.')
        before=control.locked_through
        if request.form.get('action')=='unlock':
            control.locked_through=None
        else:
            if as_of>date.today():abort(400,'Cannot close a future period.')
            if before and as_of<before:abort(409,'Use the audited unlock action before moving the lock backwards.')
            if issues:abort(409,'Resolve the listed accounting exceptions before closing.')
            if not request.form.get('reviewed'):abort(400,'Confirm source documents and external workpapers were reviewed.')
            control.locked_through=as_of
        audit.record('period_lock','books',1,reason,{'before':str(before),'after':str(control.locked_through)})
        db.session.commit();return redirect(url_for('books.close',as_of=as_of))
    return render_template('books/close.html',control=control,issues=issues,as_of=as_of)


@bp.route('/documents/<int:doc_id>')
@login_required
def document(doc_id):
    from app.controls import document_response
    return document_response(Document.query.get_or_404(doc_id))


@bp.route('/annual-package',methods=['POST'])
@login_required
def export_package():
    if not current_user.can_edit():abort(403)
    start=date.fromisoformat(request.form['start']);end=date.fromisoformat(request.form['end'])
    if start>end:abort(400,'Start date must not follow end date.')
    from app.archive import annual_package
    data=annual_package(start,end)
    audit.record('export','annual_package',None,f'{start} to {end}',{'sha256':hashlib.sha256(data).hexdigest()})
    db.session.commit()
    return Response(data,mimetype='application/zip',headers={'Content-Disposition':f'attachment; filename=accounting-{start}-{end}.zip'})
