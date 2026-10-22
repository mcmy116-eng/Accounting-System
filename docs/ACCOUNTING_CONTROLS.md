# Accounting controls and release verification

This change addresses the reproducible 2026-10-22 audit defects. Existing ledger
rows are preserved. Run the month-close exception view after upgrading; this
does not silently repair historical duplicate postings or bank matches.

## Controls

- Admin/bookkeeper can write finance records; viewers cannot write; staff can
  access their own claims only. Every document URL uses the same owner policy.
- All POST forms require CSRF and a per-form request ID. The request receipt
  commits with the business transaction. Replayed requests return 409. New forms
  get fresh IDs; payment reference checks additionally flag duplicate allocation.
- Database locks serialize accounting mutations across workers (PostgreSQL row
  lock; SQLite writer lock). Reports also take the lock while building a snapshot.
  This deliberately favours consistency for a small company over write throughput.
- A reversal can be posted once. Payments require a posted, outstanding source,
  an active matching-currency bank and a date no earlier than the source.
- Historical aging uses dated control-account postings, reversals and payment
  allocations. Inactive accounts remain included in financial reports.
- CSV statements are validated completely before import. Identical files and
  source IDs are deduplicated. Distinct bank IDs preserve equal-value transactions.
  Ambiguous overlaps without IDs are rejected for review, not silently dropped.
- Bank matching checks bank ownership, signed amount, posted state and exclusive
  matching. Quick-entry cannot repost a previously booked transaction.
- Amounts are integer cents. Nonfinite/negative posting lines and unsupported FX
  are rejected. Foreign-currency invoices/bills require a reviewed base-currency
  adjustment until a full FX subledger is implemented; no 1:1 conversion is assumed.
- Shopify unchanged orders deduplicate. Changed/legacy orders enter a persistent
  review queue with original/new evidence; adjustments require supporting files
  and a SALES-REVIEW-{id} reference. Refund dates/taxes are not inferred from a
  changed net order total. Unpaid/pre-refunded first imports are rejected.
- Shopify Admin tokens are sent only to canonical `*.myshopify.com` hosts.
- Posted claims cannot return to an editable state for a second approval.

## Closing, workpapers and export

Journal search links reports → account ledger → journal → source, receipts,
payment postings, bank matches and audit records. Supported manual adjustments
require an attached calculation. AR/AP/employee-control accounts are restricted
to their source workflows so manual lines cannot silently break subledger aging.

Use reviewed external schedules for depreciation, prepayments, director loans
and inventory/COGS adjustments. This is a controlled posting/evidence interface,
not an automatic SKU, tax-allowance or depreciation calculation engine. No
accounting policy, budget or company-year convention is prescribed by this change.

Only admins can close or unlock periods, with a reason and audit record. Automated
exceptions block close. A reviewer must also confirm bank statements, originals
and external workpapers. Closed dates reject every ledger posting path; corrections
use an open-date reversal rather than modifying prior rows.

The year/period package includes dated reports, ledger, existing originals, current
generated invoice PDFs, an offline index and SHA-256 manifest. Missing attachments
are explicit and make the package PARTIAL. It is not a tax return. Bank statement
originals can span periods and are disclosed as such in the manifest.

## Encrypted backup and empty-target restore

Install `requirements.txt`. Generate a Fernet recovery key using a trusted secrets
tool and store it as `BACKUP_KEY`, separately from the snapshot and application
SECRET_KEY. Never put the recovery key in a repository, command argument or log.

With `DATABASE_URL`, `UPLOAD_FOLDER` and `BACKUP_KEY` explicitly set in Secrets:

```
python manage_backup.py backup /restricted-finance/accounting-YYYY-MM-DD.backup
```

This snapshots all database tables and referenced original uploads under the
accounting lock, checks file presence, encrypts the archive, and refuses to
overwrite an existing backup. Store the encrypted output in the restricted
Finance Drive area. Backup contains account/authentication data; restrict access.

Restore into a **new empty database and empty upload folder**, using the matching
application revision and recovery key:

```
python manage_backup.py restore /restricted-finance/accounting-YYYY-MM-DD.backup
```

Restore authenticates the archive, checks attachment hashes and schema, restores
row IDs/relationships and rewrites attachment paths into the new upload folder.
It refuses populated targets. Review counts, balances and original files before
switching services. The utility does not schedule backups or certify a provider's
retention policy. Keep the recovery key backed up separately or restore is impossible.

## Release checklist

1. Run `python -m unittest discover -s tests -v` on SQLite and a dedicated
   PostgreSQL 16 database named `accounting_test` (`TEST_DATABASE_URL`). Tests
   explicitly reset that test database. Never point them at company data.
2. Take and verify a current backup of the live database and referenced uploads.
3. Review the additive schema migration and deploy this commit first to staging.
   Startup adds missing columns/tables, preserving existing data; PostgreSQL
   startup DDL is serialized across workers. It does not clean historical rows.
4. Verify login, each role, a draft→posted→partial→paid cycle, same-date reports,
   historical cutoff, documents, close/reopen, year export and an isolated restore.
5. Check `/books/close` exceptions against original records before any data repair.
6. Confirm deployed commit, health and persistent uploads; record the release.

`render.yaml` deploys main automatically. A branch or PR is not a live deployment.
No live database, Render credential, backup schedule or actual Shopify connection
is validated by the isolated test suite.
