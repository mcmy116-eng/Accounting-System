"""Regression coverage for the 2026-10-22 audit. Synthetic data only."""
import io, os, tempfile, unittest, uuid, json
from pathlib import Path
from datetime import date
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from app import create_app
from app.extensions import db
from app.models import *
from app.ledger import create_journal_entry, trial_balance, LedgerError
from app.report_builder import build_ar_aging, build_ap_aging
from seed import CHART_OF_ACCOUNTS, CURRENCIES


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        target=os.environ.get('TEST_DATABASE_URL') or 'sqlite:///'+self.tmp.name+'/test.sqlite'
        self.env=patch.dict(os.environ,{'DATABASE_URL':target,'DATABASE_PATH':self.tmp.name+'/test.sqlite','UPLOAD_FOLDER':self.tmp.name+'/uploads','SECRET_KEY':'synthetic-test-only','ANTHROPIC_API_KEY':''})
        self.env.start();self.app=create_app();self.app.config['TESTING']=True
        with self.app.app_context():
            if os.environ.get('TEST_DATABASE_URL'):
                if db.engine.url.database!='accounting_test':raise RuntimeError('PostgreSQL tests require the isolated accounting_test DB.')
                db.drop_all();db.create_all();db.session.add(LedgerControl(id=1));db.session.commit()
            for code,name,symbol in CURRENCIES:db.session.add(Currency(code=code,name=name,symbol=symbol))
            db.session.add(CompanySettings(company_name='SYNTHETIC ONLY'))
            for code,name,typ,sub,bank,locked in CHART_OF_ACCOUNTS:db.session.add(Account(code=code,name=name,type=typ,subtype=sub,is_bank=bank,system_locked=locked))
            # Password hashes are fixture data, not company identities.
            u=User(name='admin',email='admin@example.invalid',role='admin');u.set_password('synthetic-password-only');pw=u.password_hash;db.session.add(u)
            for name,role in [('staff','staff'),('other','staff'),('viewer','viewer'),('bookkeeper','bookkeeper')]:db.session.add(User(name=name,email=name+'@example.invalid',role=role,password_hash=pw))
            db.session.add(Contact(name='SYNTHETIC Vendor/Customer',type='both'));db.session.commit()
            self.ids={a.code:a.id for a in Account.query.all()};self.users={u.name:u.id for u in User.query.all()};self.contact=Contact.query.first().id
        self.client=self.login('admin')
        self.post('/bank/new',{'name':'Bank A','currency_code':'HKD','opening_balance':'0'})
        self.post('/bank/new',{'name':'Bank B','currency_code':'HKD','opening_balance':'0'})
        with self.app.app_context():
            banks=BankAccount.query.order_by(BankAccount.id).all();self.bank=banks[0].id;self.bank2=banks[1].id;self.gl=banks[0].gl_account_id;self.gl2=banks[1].gl_account_id

    def tearDown(self):
        with self.app.app_context():db.session.remove();db.engine.dispose()
        self.env.stop();self.tmp.cleanup()

    def login(self,user):
        c=self.app.test_client();c.get('/auth/login')
        with c.session_transaction() as sess:token=sess['_csrf']
        self.assertEqual(c.post('/auth/login',data={'email':user+'@example.invalid','password':'synthetic-password-only','_csrf':token}).status_code,302)
        c.get('/claims/' if user in ('staff','other') else '/')
        return c

    def post(self,path,data=None,client=None,key=None):
        c=client or self.client
        with c.session_transaction() as sess:
            if '_csrf' not in sess:sess['_csrf']=uuid.uuid4().hex
            token=sess['_csrf']
        data=dict(data or {});data.update(_csrf=token,_request_id=key or uuid.uuid4().hex)
        return c.post(path,data=data)

    def scalar(self,fn):
        with self.app.app_context():return fn()

    def balance(self,code,dt=None):return self.scalar(lambda:Account.query.filter_by(code=code).one().balance_cents(dt))
    def count(self,model):return self.scalar(lambda:model.query.count())

    def invoice(self,amount='10000',posted=True,day='2026-09-30'):
        data={'contact_id':self.contact,'issue_date':day,'due_date':'2026-10-15','currency_code':'HKD',**self.line_data(amount,'4000')}
        r=self.post('/invoices/new',data);self.assertEqual(r.status_code,302,r.data);id=int(r.location.split('/')[-1])
        if posted:self.assertEqual(self.post(f'/invoices/{id}/send').status_code,302)
        return id

    def line_data(self,amount,code):return {'line_description[]':['SYNTHETIC item'],'line_quantity[]':['1'],'line_price[]':[amount],'line_account[]':[str(self.ids[code])],'line_tax[]':[''],'line_cost_center[]':[''],'line_id[]':['']}

    def bill(self,amount='10000',reference=None,code='6050'):
        r=self.post('/bills/new');id=int(r.location.split('/')[-2])
        data={**self.line_data(amount,code),'contact_id':self.contact,'bill_date':'2026-09-30','due_date':'2026-10-15','vendor_ref':reference or 'TEST-'+str(id),'action':'approve'}
        r=self.post(f'/bills/{id}/review',data);self.assertEqual(r.status_code,302,r.data);return id

    def pay(self,id,amount,kind='receive',client=None,key=None,reference=None):
        return self.post(f'/payments/{kind}/{id}',{'amount':amount,'bank_account_id':self.bank,'date':'2026-10-05','reference':reference or uuid.uuid4().hex},client,key)

    def test_unbalanced_and_invalid_lines(self):
        with self.app.app_context():
            for lines in [[{'account_id':self.gl,'debit_cents':100},{'account_id':self.ids['4000'],'credit_cents':99}], [{'account_id':self.gl,'debit_cents':-100},{'account_id':self.ids['4000'],'credit_cents':-100}]]:
                with self.assertRaises(LedgerError):create_journal_entry(date.today(),'invalid',lines)
            self.assertEqual(JournalEntry.query.count(),0)

    def test_draft_post_and_duplicate_void(self):
        i=self.invoice('100',False);self.assertEqual(self.balance('1100'),0)
        self.post(f'/invoices/{i}/send');self.post(f'/invoices/{i}/send');self.assertEqual(self.count(JournalEntry),1)
        self.post(f'/invoices/{i}/void');self.post(f'/invoices/{i}/void');self.assertEqual(self.balance('1100'),0);self.assertEqual(self.count(JournalEntry),2)
        b=self.bill('100');self.post(f'/bills/{b}/void');self.post(f'/bills/{b}/void');self.assertEqual(self.balance('2000'),0)

    def test_split_receipts_and_historical_aging(self):
        i=self.invoice();self.assertEqual(self.pay(i,'3000').status_code,302);self.assertEqual(self.balance('1100'),700000);self.assertEqual(self.pay(i,'7000').status_code,302);self.assertEqual(self.balance('1100'),0)
        with self.app.app_context():rep=build_ar_aging(date(2026,9,30));self.assertEqual(rep.rows[-1][1][-1],10000)
        csv=self.client.get('/reports/ar-aging/export.csv?as_of=2026-09-30').data.decode();self.assertIn('As of: 2026-09-30',csv);self.assertIn('10000.0',csv)
        self.assertIn(b'2026-09-30',self.client.get('/reports/ar-aging?as_of=2026-09-30').data)

    def test_bill_cutoff_and_asset_categories(self):
        b=self.bill();self.pay(b,'3000','pay');self.pay(b,'7000','pay');self.assertEqual(self.balance('6050'),1000000);self.assertEqual(self.balance('2000',date(2026,9,30)),1000000)
        with self.app.app_context():self.assertEqual(build_ap_aging(date(2026,9,30)).rows[-1][1][-1],10000)
        b=self.bill('12000',code='1300');self.assertEqual(self.balance('1300'),1200000)
        r=self.post('/bills/new');html=self.client.get(r.location).data.decode();self.assertIn('Prepaid Expenses',html);self.assertIn('Inventory',html)

    def test_duplicate_vendor_reference(self):
        self.bill(reference='SUPPLIER-001');r=self.post('/bills/new');id=int(r.location.split('/')[-2]);data={**self.line_data('10000','6050'),'contact_id':self.contact,'bill_date':'2026-09-30','due_date':'2026-10-15','vendor_ref':' supplier-001 ','action':'approve'}
        self.assertEqual(self.post(f'/bills/{id}/review',data).status_code,409);self.assertEqual(self.balance('2000'),1000000)

    def test_payment_replay_and_draft_guards(self):
        i=self.invoice();key=uuid.uuid4().hex;reference=uuid.uuid4().hex
        self.assertEqual(self.pay(i,'3000',key=key,reference=reference).status_code,302)
        self.assertEqual(self.pay(i,'3000',key=key,reference=reference).status_code,409)
        self.assertEqual(self.pay(i,'3000',reference=reference).status_code,409)
        self.assertEqual(self.balance('1100'),700000)
        draft=self.invoice('100',False);self.assertEqual(self.pay(draft,'100').status_code,409)

    def test_injected_failure_atomic(self):
        i=self.invoice();before=[self.count(m) for m in [Payment,PaymentAllocation,JournalEntry]]
        with patch('app.blueprints.payments.create_journal_entry',side_effect=LedgerError('injected failure')):self.pay(i,'100')
        self.assertEqual(before,[self.count(m) for m in [Payment,PaymentAllocation,JournalEntry]])

    def test_concurrent_overpayment_and_replay(self):
        i=self.invoice('100');c1=self.login('admin');c2=self.login('admin')
        def receive(c):return self.pay(i,'70',client=c).status_code
        with ThreadPoolExecutor(2) as pool:responses=list(pool.map(receive,[c1,c2]))
        self.assertEqual(self.count(Payment),1);self.assertEqual(self.balance('1100'),3000)
        j=self.invoice('100');key=uuid.uuid4().hex
        with ThreadPoolExecutor(2) as pool:responses=list(pool.map(lambda c:self.pay(j,'30',client=c,key=key).status_code,[c1,c2]))
        self.assertEqual(sorted(responses),[302,409]);self.assertEqual(self.scalar(lambda:db.session.get(Invoice,j).amount_paid_cents),3000)

    def test_bank_csv_dedup_and_validation(self):
        raw=b'Date,Description,Amount,Transaction ID\n2026-09-30,Sale,100,A\n2026-09-30,Sale,100,B\n'
        def upload(data):return self.post(f'/bank/{self.bank}/import',{'file':(io.BytesIO(data),'statement.csv')})
        self.assertEqual(upload(raw).status_code,302);self.assertEqual(upload(raw).status_code,302);self.assertEqual(self.count(BankTransaction),2)
        invalid=b'Date,Description,Amount\n2026-09-30,Fee,-30\nnot-a-date,Bad,20\n';r=upload(invalid);self.assertEqual(r.status_code,409);self.assertIn(b'Row 3',r.data);self.assertEqual(self.count(BankTransaction),2)
        changed=raw.replace(b'100,A',b'101,A');self.assertEqual(upload(changed).status_code,409);self.assertEqual(self.count(BankTransaction),2)

    def add_txn(self,amount=-3000):
        with self.app.app_context():
            t=BankTransaction(bank_account_id=self.bank,date=date(2026,9,30),description='SYNTHETIC bank',amount_cents=amount);db.session.add(t);db.session.commit();return t.id

    def test_bank_quick_entry_and_match_guards(self):
        tid=self.add_txn();data={'txn_id':tid,'contra_account_id':self.ids['6080']}
        self.assertEqual(self.post(f'/bank/{self.bank2}/quick-entry',data).status_code,409)
        self.assertEqual(self.post(f'/bank/{self.bank}/quick-entry',data).status_code,302)
        self.assertEqual(self.post(f'/bank/{self.bank}/quick-entry',data).status_code,409);self.assertEqual(self.balance('6080'),3000)
        line=self.scalar(lambda:db.session.get(BankTransaction,tid).matched_journal_line_id)
        viewer=self.login('viewer');self.assertEqual(self.post(f'/bank/{self.bank}/reconcile-line/{line}',client=viewer).status_code,403)
        self.assertEqual(self.post(f'/bank/{self.bank2}/reconcile-line/{line}').status_code,409)
        self.assertEqual(self.post(f'/bank/{self.bank}/reconcile-line/{line}').status_code,302)
        other=self.add_txn(10000);self.assertEqual(self.post(f'/bank/{self.bank}/match',{'txn_id':other,'line_id':line}).status_code,409)

    def test_inactive_account_reporting(self):
        self.invoice('1000');self.bill('250');r=self.post(f'/accounts/{self.ids["6050"]}/toggle');self.assertEqual(r.status_code,409)
        # Simulate legacy inactive balance: reports must still include it.
        with self.app.app_context():a=db.session.get(Account,self.ids['6050']);a.active=False;db.session.commit();_,d,c=trial_balance(date(2026,9,30));self.assertEqual(d,c)
        self.assertIn(b'250.0',self.client.get('/reports/pnl/export.csv?start=2026-09-01&end=2026-09-30').data)

    def test_search_drilldown_and_exports(self):
        i=self.invoice('1000');self.bill('250');html=self.client.get('/invoices/?q=not-found-xxx').data.split(b'<tbody>')[-1];self.assertNotIn(b'INV-0001',html)
        self.assertIn(b'INV-0001',self.client.get('/invoices/?q=INV-0001').data)
        html=self.client.get('/reports/pnl?start=2026-09-01&end=2026-09-30').data;self.assertIn(f'/accounts/{self.ids["6050"]}'.encode(),html)
        for ext in ['csv','xlsx','pdf']:self.assertEqual(self.client.get(f'/reports/pnl/export.{ext}?start=2026-09-01&end=2026-09-30').status_code,200)
        je=self.scalar(lambda:db.session.get(Invoice,i).journal_entry_id);self.assertIn(f'/invoices/{i}'.encode(),self.client.get(f'/books/{je}').data)

    def test_document_authorization_and_inactive_session(self):
        with self.app.app_context():
            c=ExpenseClaim(number='TEST-1',staff_id=self.users['staff'],title='Synthetic');db.session.add(c);db.session.flush();p=Path(self.tmp.name)/'uploads'/'test.pdf';p.write_bytes(b'%PDF-synthetic')
            d=Document(filename='test.pdf',filepath=str(p),claim_id=c.id);db.session.add(d);db.session.commit();did=d.id;cid=c.id
        owner=self.login('staff');other=self.login('other')
        self.assertEqual(owner.get(f'/claims/documents/{did}/file').data,b'%PDF-synthetic')
        for path in [f'/claims/{cid}',f'/claims/documents/{did}/file',f'/bills/documents/{did}/file',f'/books/documents/{did}','/reports/pnl/export.csv']:
            self.assertEqual(other.get(path).status_code,403,path)
        self.post(f'/auth/users/{self.users["staff"]}/toggle');self.assertEqual(owner.get('/claims/').status_code,302)

    def test_csrf_and_viewer_write(self):
        self.assertEqual(self.client.post('/bank/new',data={'name':'forged'}).status_code,400)
        viewer=self.login('viewer');self.assertEqual(self.post('/claims/new',{'title':'forged'},viewer).status_code,403)
        self.assertEqual(self.client.get('/auth/logout').status_code,405)

    def test_period_lock_and_unlock(self):
        self.invoice('100');r=self.post('/books/close',{'as_of':'2026-09-30','reason':'Synthetic close','reviewed':'1','action':'close'});self.assertEqual(r.status_code,302,r.data)
        draft=self.invoice('100',False);self.assertIn(self.post(f'/invoices/{draft}/send').status_code,[302,409]);self.assertEqual(self.count(JournalEntry),1)
        # A current-date reversal is permitted without rewriting the closed period.
        self.post('/invoices/1/void');self.assertEqual(self.balance('1100',date(2026,9,30)),10000)
        self.assertEqual(self.post('/books/close',{'as_of':'2026-09-30','reason':'Reviewed unlock','action':'unlock'}).status_code,302)
        self.assertEqual(self.post(f'/invoices/{draft}/send').status_code,302)

    def test_shopify_changed_order_review_not_silent(self):
        from app.shopify_sync import sync
        amount=['1000']
        def transport(*args,**kwargs):return {'orders':{'edges':[{'node':{'id':'gid://shopify/Order/SYNTHETIC','name':'TEST','processedAt':'2026-09-30T00:00:00Z','displayFinancialStatus':'PAID','currentTotalPriceSet':{'shopMoney':{'amount':amount[0],'currencyCode':'HKD'}}}}],'pageInfo':{'hasNextPage':False}}}
        with self.app.app_context():
            it=SalesIntegration(name='SYNTHETIC',shop_domain='synthetic.myshopify.com',store_currency='HKD',fx_rate=1,fee_percent=3);db.session.add(it);db.session.commit()
            self.assertEqual(sync(it,date(2026,9,1),date(2026,9,30),transport=transport)['posted'],1)
            self.assertEqual(sync(it,date(2026,9,1),date(2026,9,30),transport=transport)['skipped'],1)
            amount[0]='900';res=sync(it,date(2026,9,1),date(2026,9,30),transport=transport);self.assertTrue(res['errors']);self.assertEqual(res['skipped'],0);self.assertEqual(IntegrationReview.query.count(),1)
            sync(it,date(2026,9,1),date(2026,9,30),transport=transport);self.assertEqual(IntegrationReview.query.count(),1)

    def test_shopify_invalid_host_and_zero_fx(self):
        before=self.count(SalesIntegration)
        r=self.post('/integrations/shopify/save',{'name':'SYNTHETIC','shop_domain':'example.invalid','access_token':'synthetic-only'})
        self.assertEqual(r.status_code,400)
        self.assertEqual(self.count(SalesIntegration),before)
        from app.shopify_sync import _post_order, ShopifyError
        with self.app.app_context():
            it=SalesIntegration(shop_domain='synthetic.myshopify.com',store_currency='HKD',fx_rate=0,fee_percent=0)
            with self.assertRaises(ShopifyError):_post_order(it,{})
            self.assertEqual(JournalEntry.query.count(),0)

    def adjustment(self,reference='SYNTHETIC-WP'):
        r=self.post('/books/new',{'date':'2026-09-30','memo':'Supported synthetic adjustment','reference':reference,'account_id[]':[self.ids['6050'],self.ids['2600']],'debit[]':['100',''],'credit[]':['','100'],'description[]':['Supplies','Director advance'],'file':(io.BytesIO(b'SYNTHETIC CALCULATION'),'workpaper.txt')})
        self.assertEqual(r.status_code,302,r.data);return int(r.location.split('/')[-1])

    def test_supported_adjustment_and_annual_originals(self):
        import zipfile,hashlib
        je=self.adjustment();self.assertEqual(self.balance('6050'),10000);self.assertEqual(self.balance('2600'),10000)
        self.assertIn(b'workpaper.txt',self.client.get(f'/books/{je}').data)
        r=self.post('/books/annual-package',{'start':'2026-01-01','end':'2026-12-31'});self.assertEqual(r.status_code,200)
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            manifest=json.loads(z.read('manifest.json'));self.assertEqual(manifest['status'],'COMPLETE_IN_SCOPE')
            self.assertIn('index.html',z.namelist())
            for name,info in manifest['files'].items():self.assertEqual(hashlib.sha256(z.read(name)).hexdigest(),info['sha256'])
            self.assertTrue(any(z.read(n)==b'SYNTHETIC CALCULATION' for n in z.namelist() if n.startswith('originals/')))
        r=self.post(f'/books/{je}/reverse',{'date':'2026-10-22','reason':'Synthetic reversal'});self.assertEqual(r.status_code,302)
        self.post(f'/books/{je}/reverse',{'date':'2026-10-22','reason':'Replay'});self.assertEqual(self.balance('6050'),0)

    def test_missing_evidence_manifest(self):
        import zipfile
        self.bill('100');r=self.post('/books/annual-package',{'start':'2026-01-01','end':'2026-12-31'})
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:m=json.loads(z.read('manifest.json'));self.assertEqual(m['status'],'PARTIAL');self.assertEqual(m['missing'][0]['status'],'NO_ATTACHMENT')

    def test_encrypted_backup_empty_target_restore(self):
        from app.backup import backup_bytes,restore_bytes
        from cryptography.fernet import Fernet, InvalidToken
        je=self.adjustment();key=Fernet.generate_key()
        with self.app.app_context():
            data=backup_bytes(key);self.assertNotIn(b'SYNTHETIC',data)
            with self.assertRaises(InvalidToken):restore_bytes(data,Fernet.generate_key())
            with self.assertRaises(ValueError):restore_bytes(data,key)
            db.session.rollback()
        # Restore only into a new separate SQLite database, never the source.
        target=Path(self.tmp.name)/'restore';target.mkdir()
        with patch.dict(os.environ,{'DATABASE_URL':'sqlite:///'+str(target/'restore.sqlite'),'UPLOAD_FOLDER':str(target/'uploads')}):
            app2=create_app()
            with app2.app_context():
                result=restore_bytes(data,key);self.assertEqual(result['attachments'],1)
                self.assertEqual(Account.query.filter_by(code='6050').one().balance_cents(),10000)
                doc=Document.query.one();self.assertEqual(Path(doc.filepath).read_bytes(),b'SYNTHETIC CALCULATION');self.assertEqual(doc.journal_entry_id,je)
                with self.assertRaises(ValueError):restore_bytes(data,key)
                db.session.rollback();db.engine.dispose()

    def test_claim_approval_payment_and_reopen_guard(self):
        staff=self.login('staff');r=self.post('/claims/new',{'title':'Synthetic claim'},staff);id=int(r.location.split('/')[-1])
        self.post(f'/claims/{id}/save',{'title':'Synthetic claim','claim_date':'2026-09-30','line_description[]':['Supplies'],'line_merchant[]':['Synthetic vendor'],'line_amount[]':['100'],'line_account[]':[self.ids['6050']]},staff)
        self.post(f'/claims/{id}/submit',client=staff);self.assertEqual(self.post(f'/claims/{id}/approve').status_code,302)
        self.assertEqual(self.post(f'/claims/{id}/clarify',{'notes':'reopen'}).status_code,409)
        self.post(f'/claims/{id}/pay',{'bank_account_id':self.bank});self.post(f'/claims/{id}/pay',{'bank_account_id':self.bank});self.assertEqual(self.balance('6050'),10000);self.assertEqual(self.balance('2550'),0)

    def test_legacy_schema_additive_migration(self):
        from app.schema import ensure_schema
        from sqlalchemy import inspect
        with self.app.app_context():
            from sqlalchemy import text
            before=User.query.count()
            for table,column in [('documents','sha256'),('documents','drive_file_id'),('documents','journal_entry_id'),('bank_transactions','source_key'),('synced_orders','source_snapshot')]:
                # SQLite cannot DROP a column referenced by its inline FK. The
                # PostgreSQL run covers this additional-column migration too.
                if db.engine.dialect.name=='sqlite' and column=='journal_entry_id':continue
                db.session.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
            db.session.commit()
            ensure_schema(self.app);ensure_schema(self.app)
            self.assertEqual(User.query.count(),before);self.assertIn('source_snapshot',{c['name'] for c in inspect(db.engine).get_columns('synced_orders')})

    def test_export_formula_and_pdf_markup_safety(self):
        from app.report_builder import Report
        from app.report_export import to_csv,to_xlsx,to_pdf
        import openpyxl
        rep=Report('=UNTRUSTED',meta=['<img src="file:///not-readable"/>'],header=['Name','Amount']);rep.add(['=1+1',100])
        self.assertIn(b"'=1+1",to_csv(rep));ws=openpyxl.load_workbook(io.BytesIO(to_xlsx(rep))).active
        self.assertFalse(any(c.data_type=='f' for row in ws for c in row));self.assertTrue(to_pdf(rep).startswith(b'%PDF'))

    def test_fx_and_invalid_date_guard(self):
        i=self.invoice('100',False)
        with self.app.app_context():obj=db.session.get(Invoice,i);obj.currency_code='USD';db.session.commit()
        self.assertEqual(self.post(f'/invoices/{i}/send').status_code,409);self.assertEqual(self.count(JournalEntry),0)
        self.assertEqual(self.client.get('/reports/ar-aging?as_of=invalid').status_code,400)

    def test_cross_bank_same_amount_and_duplicate_line(self):
        with self.app.app_context():
            e=create_journal_entry(date(2026,9,30),'Synthetic',[{'account_id':self.gl2,'debit_cents':10000},{'account_id':self.ids['4000'],'credit_cents':10000}]);db.session.commit();line=next(l.id for l in e.lines if l.account_id==self.gl2)
        t=self.add_txn(10000);self.assertEqual(self.post(f'/bank/{self.bank}/match',{'txn_id':t,'line_id':line}).status_code,409)


if __name__=='__main__':unittest.main()
