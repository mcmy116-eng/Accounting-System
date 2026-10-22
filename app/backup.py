"""Encrypted DB+attachment snapshots and empty-target restores.

The caller supplies a separate Fernet recovery key through BACKUP_KEY, never a
command argument. Restore refuses populated databases and writes no live data.
"""
import io, json, hashlib, zipfile
from pathlib import Path
from datetime import datetime, date, timezone
from cryptography.fernet import Fernet
from flask import current_app
from sqlalchemy import select
from app.extensions import db
from app.models import Document, BankImport
from app.controls import lock_books


def backup_bytes(key):
    cipher=Fernet(key)
    lock_books()
    tables={};attachments={};files={}
    for table in db.metadata.sorted_tables:
        tables[table.name]=[dict(r) for r in db.session.execute(select(table)).mappings().all()]
    root=Path(current_app.config['UPLOAD_FOLDER']).resolve()
    for table in ('documents','bank_imports'):
        for row in tables.get(table,[]):
            path=Path(row['filepath']).resolve()
            if not path.is_relative_to(root) or not path.is_file():raise ValueError(f'Missing/unsafe attachment: {table}#{row["id"]}. Backup aborted.')
            name=f'attachments/{table}-{row["id"]}{path.suffix.lower()}'
            data=path.read_bytes();files[name]=data;attachments[f'{table}:{row["id"]}']=name
    payload={'format':'accounting-backup-v1','created_at':datetime.now(timezone.utc).isoformat(),'tables':tables,'attachments':attachments,
             'files':{n:hashlib.sha256(b).hexdigest() for n,b in files.items()}}
    files['snapshot.json']=json.dumps(payload,default=str,ensure_ascii=False).encode()
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():z.writestr(name,data)
    db.session.rollback()  # release snapshot lock, no accounting mutation
    return cipher.encrypt(buf.getvalue())


def restore_bytes(data,key):
    raw=Fernet(key).decrypt(data)  # authentication before parsing any payload
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if sum(i.file_size for i in z.infolist())>512*1024*1024:raise ValueError('Snapshot exceeds the supported restore size.')
        snapshot=json.loads(z.read('snapshot.json'))
        if snapshot.get('format')!='accounting-backup-v1':raise ValueError('Unsupported backup version.')
        if set(snapshot['tables'])!=set(db.metadata.tables):raise ValueError('Restore with the matching application schema/version.')
        files={name:z.read(name) for name in snapshot['files']}
        for name,content in files.items():
            if hashlib.sha256(content).hexdigest()!=snapshot['files'][name]:raise ValueError('Attachment checksum mismatch.')
    lock_books()
    root=Path(current_app.config['UPLOAD_FOLDER']).resolve()
    if any(root.iterdir()):raise ValueError('Restore requires an empty upload folder.')
    for table in db.metadata.sorted_tables:
        if table.name=='ledger_control':continue
        if db.session.execute(select(table).limit(1)).first():raise ValueError('Restore requires an empty database; no existing accounting data was replaced.')
    db.session.execute(db.metadata.tables['ledger_control'].delete())
    created=[]
    try:
        for table in db.metadata.sorted_tables:
            rows=[]
            for original in snapshot['tables'][table.name]:
                row=dict(original)
                for col in table.columns:
                    value=row.get(col.name)
                    if value is None:continue
                    if isinstance(col.type,db.DateTime):row[col.name]=datetime.fromisoformat(value)
                    elif isinstance(col.type,db.Date):row[col.name]=date.fromisoformat(value)
                if table.name in ('documents','bank_imports'):
                    name=snapshot['attachments'][f'{table.name}:{row["id"]}']
                    path=root/Path(name).name
                    if path.exists():raise ValueError('Duplicate restore attachment name.')
                    path.write_bytes(files[name]);created.append(path);row['filepath']=str(path)
                rows.append(row)
            if rows:db.session.execute(table.insert(),rows)
        # PostgreSQL explicit ID restores must advance each serial sequence.
        if db.engine.dialect.name=='postgresql':
            from sqlalchemy import text
            for table in db.metadata.sorted_tables:
                if 'id' in table.c and isinstance(table.c.id.type,db.Integer):
                    db.session.execute(text("SELECT setval(pg_get_serial_sequence(:table, 'id'), COALESCE((SELECT MAX(id) FROM \""+table.name+"\"), 1), EXISTS(SELECT 1 FROM \""+table.name+"\")) WHERE pg_get_serial_sequence(:table, 'id') IS NOT NULL"),{'table':table.name})
        db.session.commit()
    except Exception:
        db.session.rollback()
        for p in created:p.unlink(missing_ok=True)
        raise
    return {'rows':{name:len(rows) for name,rows in snapshot['tables'].items()},'attachments':len(created)}
