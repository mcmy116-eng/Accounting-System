"""The authorised sample must be complete, labelled and safe to retry."""
import io
import json
import unittest
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

import test_controls as fixtures
from app.extensions import db
from app.models import (Invoice, Bill, JournalEntry, Document, Payment, SampleDataBundle,
                        Contact, Account, CompanySettings, BankTransaction, BankImport,
                        LedgerControl)
from app.sample_data import install_dummy_data, TARGET_SERVICE, BUNDLE_KEY, MARK
from app.report_builder import build_pnl, build_ar_aging, build_ap_aging
from app.archive import annual_package


class SampleDataTest(unittest.TestCase):
    setUp = fixtures.ControlsTest.setUp
    tearDown = fixtures.ControlsTest.tearDown
    login = fixtures.ControlsTest.login
    post = fixtures.ControlsTest.post
    scalar = fixtures.ControlsTest.scalar
    count = fixtures.ControlsTest.count
    balance = fixtures.ControlsTest.balance
    invoice = fixtures.ControlsTest.invoice
    line_data = fixtures.ControlsTest.line_data

    def install(self):
        with self.app.app_context():
            return install_dummy_data(TARGET_SERVICE, date(2026, 10, 22))

    def test_dataset_reports_sources_and_historical_cutoff(self):
        result = self.install()
        self.assertTrue(result['readback_verified'])
        self.assertEqual((self.count(Invoice), self.count(Bill), self.count(Payment),
                          self.count(JournalEntry), self.count(Document), self.count(BankTransaction)),
                         (3, 3, 5, 13, 13, 7))
        with self.app.app_context():
            first = Invoice.query.filter_by(number='DUMMY-INV-001').one()
            second = Invoice.query.filter_by(number='DUMMY-INV-002').one()
            draft = Invoice.query.filter_by(number='DUMMY-INV-DRAFT').one()
            self.assertEqual((first.status, first.balance_due_cents()), ('partial', 700000))
            self.assertEqual((second.status, second.balance_due_cents()), ('paid', 0))
            self.assertIsNone(draft.journal_entry_id)
            pnl = build_pnl(date(2026, 10, 1), date(2026, 10, 31))
            self.assertEqual(pnl.rows[-1][1], ['Net income', 6750.0])
            self.assertEqual(build_ar_aging(date(2026, 10, 8)).rows[-1][1][-1], 12000)
            self.assertEqual(build_ap_aging(date(2026, 10, 8)).rows[-1][1][-1], 8200)
            self.assertEqual(build_ap_aging(date(2026, 10, 31)).rows[-1][1][-1], 6000)
            self.assertTrue(all(MARK in e.memo and e.reference.startswith('DUMMY-')
                                and e.created_by_id is None for e in JournalEntry.query.all()))
            self.assertTrue(all(Path(d.filepath).read_bytes().startswith(b'%PDF') for d in Document.query.all()))
            self.assertEqual(CompanySettings.query.first().next_invoice_seq, 1)
            self.assertEqual(CompanySettings.query.first().next_bill_seq, 1)

    def test_repeat_and_concurrent_install_do_not_duplicate(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.install(), range(2)))
        self.assertEqual(sum(bool(r.get('repeated')) for r in results), 1)
        self.assertEqual(self.count(JournalEntry), 13)
        self.assertEqual(self.count(Document), 13)
        self.assertEqual(self.count(SampleDataBundle), 1)

    def test_existing_posted_or_draft_records_block_import(self):
        self.invoice('100', posted=False)
        result = self.install()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.count(Invoice), 1)
        self.assertEqual(self.count(JournalEntry), 0)
        self.assertEqual(self.count(Document), 0)
        self.assertTrue(self.install()['repeated'])

    def test_target_service_and_expiry_are_enforced(self):
        with self.app.app_context():
            result = install_dummy_data('another-service', date(2026, 10, 22))
            self.assertEqual(result['status'], 'not_applicable')
            self.assertEqual(SampleDataBundle.query.count(), 0)
            result = install_dummy_data(TARGET_SERVICE, date(2026, 10, 30))
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(JournalEntry.query.count(), 0)

    def test_closed_period_is_not_unlocked(self):
        with self.app.app_context():
            db.session.get(LedgerControl, 1).locked_through = date(2026, 9, 30)
            db.session.commit()
        self.assertEqual(self.install()['status'], 'blocked')
        self.assertEqual(self.count(JournalEntry), 0)
        self.assertEqual(self.scalar(lambda: db.session.get(LedgerControl, 1).locked_through), date(2026, 9, 30))

    def test_missing_chart_account_blocks_without_partial_data(self):
        with self.app.app_context():
            Account.query.filter_by(code='4000').one().active = False
            db.session.commit()
        self.assertEqual(self.install()['status'], 'blocked')
        self.assertEqual(self.count(JournalEntry), 0)
        self.assertEqual(self.count(Invoice), 0)

    def test_attachment_failure_rolls_back_everything_and_allows_retry(self):
        before = self.count(Contact)
        with patch('app.sample_data._pdf', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.install()
        self.assertEqual(self.count(Contact), before)
        self.assertEqual(self.count(JournalEntry), 0)
        self.assertEqual(self.count(SampleDataBundle), 0)
        self.assertFalse((Path(self.app.config['UPLOAD_FOLDER']) / BUNDLE_KEY).exists())
        self.assertEqual(self.install()['status'], 'installed')

    def test_page_role_labels_search_and_downloaded_package(self):
        result = self.install()
        html = self.client.get('/settings/dummy-data')
        self.assertEqual(html.status_code, 200)
        self.assertIn('Dummy/testing 數'.encode(), html.data)
        self.assertIn(b'7,750.00', html.data)
        self.assertIn(b'DUMMY-INV-001', html.data)
        self.assertEqual(self.login('staff').get('/settings/dummy-data').status_code, 403)
        self.assertEqual(self.login('viewer').get('/settings/dummy-data').status_code, 200)
        search = self.client.get('/books/?q=Dummy%2Ftesting').data
        self.assertIn(b'DUMMY-COGS-001', search)
        self.assertIn(b'DUMMY-INV-001', search)
        csv = self.client.get('/reports/pnl/export.csv?start=2026-10-01&end=2026-10-31').data
        self.assertIn(b'Dummy/testing data included', csv)
        for doc_id in result['document_ids']:
            response = self.client.get(f'/books/documents/{doc_id}')
            try:
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.data.startswith(b'%PDF'))
            finally:
                response.close()
        response = self.post('/books/annual-package', {'start':'2026-10-01', 'end':'2026-10-31'})
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as z:
            manifest = json.loads(z.read('manifest.json'))
            self.assertEqual(manifest['status'], 'COMPLETE_IN_SCOPE')
            self.assertEqual(manifest['journal_count'], 13)
            self.assertTrue(z.read('DUMMY-TESTING-NOTICE.txt').startswith(b'Dummy/testing'))
            self.assertEqual(len([n for n in z.namelist() if n.startswith('originals/')]), 13)
        with self.app.app_context():
            statement = BankImport.query.one()
            raw = Path(statement.filepath).read_bytes()
        r = self.post(f'/bank/{result["bank_id"]}/import', {'file': (io.BytesIO(raw), 'DUMMY-bank-statement.csv')})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.count(BankTransaction), 7)
