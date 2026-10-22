"""Run only against an explicitly selected database and upload folder."""
import argparse, os, json
from pathlib import Path
from app import create_app
from app.backup import backup_bytes, restore_bytes

def main():
    parser=argparse.ArgumentParser(description='Encrypted accounting backup / empty-target restore')
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('path')
    args=parser.parse_args()
    if not all(os.environ.get(k) for k in ['DATABASE_URL','UPLOAD_FOLDER','BACKUP_KEY']):
        parser.error('Set DATABASE_URL, UPLOAD_FOLDER and BACKUP_KEY in the environment. Keep the recovery key separately in Secrets.')
    app=create_app()
    with app.app_context():
        if args.action=='backup':
            data=backup_bytes(os.environ['BACKUP_KEY'])
            # Exclusive create avoids overwriting an earlier backup.
            with open(args.path,'xb') as f:f.write(data)
            print(json.dumps({'status':'saved','bytes':len(data)}))
        else:print(json.dumps(restore_bytes(Path(args.path).read_bytes(),os.environ['BACKUP_KEY'])))

if __name__=='__main__':main()
