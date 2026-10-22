"""Consistent, indexed accounting exports; never silently omit missing evidence."""
import io, json, hashlib, zipfile, html
from pathlib import Path
from datetime import date, datetime, timezone
from flask import current_app
from app.extensions import db
from app.models import Document, Invoice, Bill, ExpenseClaim, JournalEntry, BankImport
from app.report_builder import BUILDERS
from app.report_export import to_csv


def annual_package(start, end):
    files={};missing=[];index=[]
    for slug,(builder,kind) in BUILDERS.items():
        report=builder(end) if kind.startswith('as_of') else builder(start,end)
        files[f'reports/{slug}.csv']=to_csv(report)
    entries=JournalEntry.query.filter(JournalEntry.date>=start,JournalEntry.date<=end,JournalEntry.is_posted.is_(True)).order_by(JournalEntry.date,JournalEntry.id).all()
    from app.report_builder import Report
    ledger=Report('General Ledger',meta=[f'{start} to {end}'],header=['Journal ID','Date','Reference','Memo','Account','Description','Debit','Credit','Source type','Source ID'])
    for e in entries:
        for l in e.lines:ledger.add([e.id,str(e.date),e.reference,e.memo,l.account.code,l.description,l.debit_cents/100,l.credit_cents/100,e.source_type,e.source_id])
    files['reports/general-ledger.csv']=to_csv(ledger)
    root=Path(current_app.config['UPLOAD_FOLDER']).resolve()
    for d in Document.query.order_by(Document.id).all():
        linked=[];dt=None
        if d.bill_id:
            obj=db.session.get(Bill,d.bill_id)
            if obj:dt=obj.bill_date;linked.append({'type':'bill','id':obj.id,'number':obj.number,'journal_id':obj.journal_entry_id})
        if d.claim_id:
            obj=db.session.get(ExpenseClaim,d.claim_id)
            if obj:dt=obj.claim_date;linked.append({'type':'claim','id':obj.id,'number':obj.number,'journal_id':obj.journal_entry_id})
        if d.journal_entry_id:
            obj=db.session.get(JournalEntry,d.journal_entry_id)
            if obj:dt=obj.date;linked.append({'type':'journal','id':obj.id})
        if not dt or not start<=dt<=end:continue
        path=Path(d.filepath).resolve()
        item={'document_id':d.id,'original_name':d.filename,'uploaded_at':str(d.uploaded_at),'drive_file_id':d.drive_file_id,'sources':linked}
        if not path.is_relative_to(root) or not path.is_file():
            item['status']='MISSING';missing.append(item)
        else:
            data=path.read_bytes();suffix=path.suffix.lower();name=f'originals/document-{d.id}{suffix}'
            files[name]=data;item.update(status='INCLUDED',path=name,sha256=hashlib.sha256(data).hexdigest())
        index.append(item)
    # Include system-generated invoice originals for this period.
    from app.pdf_utils import invoice_pdf
    for obj in Invoice.query.filter(Invoice.issue_date>=start,Invoice.issue_date<=end,Invoice.journal_entry_id.isnot(None)).all():
        name=f'invoices/invoice-{obj.id}.pdf'
        try:files[name]=invoice_pdf(obj).getvalue();index.append({'invoice_id':obj.id,'number':obj.number,'journal_id':obj.journal_entry_id,'path':name,'status':'GENERATED_FROM_CURRENT_RECORD'})
        except Exception as exc:missing.append({'invoice_id':obj.id,'status':'GENERATION_FAILED','error_type':type(exc).__name__})
    # Bank CSV originals may span periods: include all, with dates disclosed in manifest.
    for imp in BankImport.query.order_by(BankImport.id).all():
        path=Path(imp.filepath).resolve();name=f'statements/import-{imp.id}.csv'
        if path.is_relative_to(root) and path.is_file():
            files[name]=path.read_bytes();index.append({'bank_import_id':imp.id,'bank_id':imp.bank_account_id,'path':name,'scope':'full original statement; may span periods'})
        else:missing.append({'bank_import_id':imp.id,'status':'MISSING'})
    for model,kind in [(Bill,'bill'),(ExpenseClaim,'claim')]:
        field=model.bill_date if kind=='bill' else model.claim_date
        for obj in model.query.filter(field>=start,field<=end,model.journal_entry_id.isnot(None)).all():
            docs=Document.query.filter_by(**{kind+'_id':obj.id}).all()
            if not docs:missing.append({'type':kind,'id':obj.id,'number':obj.number,'status':'NO_ATTACHMENT'})
    manifest={'format':'accounting-year-pack-v1','start':str(start),'end':str(end),'generated_at':datetime.now(timezone.utc).isoformat(),'status':'PARTIAL' if missing else 'COMPLETE_IN_SCOPE','journal_count':len(entries),'documents':index,'missing':missing,'notes':['No tax return or tax adjustment conclusion is implied.','Supporting workpapers, receipts and accountant adjustments must be reviewed.','Invoice PDFs are generated now, not historical immutable renderings.']}
    manifest['files']={name:{'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)} for name,data in files.items()}
    files['manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    links=''.join('<li><a href="'+html.escape(name)+'">'+html.escape(name)+'</a></li>' for name in files)
    files['index.html']=('<!doctype html><meta charset="utf-8"><title>Accounting evidence index</title><h1>Accounting evidence '+str(start)+' to '+str(end)+'</h1><p>Status: '+manifest['status']+'</p><p>Read manifest.json for missing evidence, source IDs and SHA-256 checksums.</p><ul>'+links+'</ul><h2>Missing / unverified</h2><pre>'+html.escape(json.dumps(missing,ensure_ascii=False,indent=2))+'</pre>').encode()
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():z.writestr(name,data)
    return buf.getvalue()
