# Authorised Dummy/testing data — 2026-10-22

Mic requested dummy transactions in the currently empty accounting website, with
the remark **Dummy/testing 數**. This is an actual sample-data import into that
ledger, not a mock screen. Reports include these values and display a notice.

Only Render service `srv-d9ne0vm417fc73d5vms0` may run the startup import. Its
`RENDER_SERVICE_ID` must match. The first attempt is allowed only from
2026-10-22 through 2026-10-29 Hong Kong time. Other services do nothing. Existing
accounting records, a closed period, incompatible chart, or reserved identifiers
block the import. Existing contacts/accounts and login credentials are preserved.

The Docker startup runs `python load_dummy_data.py` before Gunicorn, after the
persistent uploads disk is mounted. The Render pre-deploy seed is unchanged:
pre-deploy does not have the persistent disk. The import creates all DB rows in
one accounting-lock transaction, writes and verifies the PDFs/CSV, and records
one unique `sample_data_bundles` row. Successful restarts do not recreate data,
even if the user subsequently changes the examples. Failures before commit roll
back rows and remove only that attempt's new evidence folder. Sample failure
does not intentionally stop the accounting website.

All samples are October 2026, use the configured base currency, have no sales
tax, and have DUMMY references plus the exact remark. There are no external API
calls, actual bank transfers, emails, new login users or changes to credentials.
Journal authors are recorded as system; no human approval is impersonated.

| Item | Initial state |
| --- | --- |
| DUMMY-CAPITAL-01 | 5,000 synthetic capital deposit |
| DUMMY-INV-001 | 10,000 invoice; 3,000 received; 7,000 outstanding |
| DUMMY-INV-002 | 2,000 invoice; two 1,000 receipts; paid |
| DUMMY-INV-DRAFT | 500 draft; no journal |
| DUMMY-BILL-001 | 1,200 software expense; paid |
| DUMMY-BILL-002 | 3,000 office supplies; 1,000 paid; 2,000 outstanding |
| DUMMY-BILL-003 | 4,000 inventory purchase; unpaid |
| DUMMY-COGS-001 | 1,000 supported manual inventory-cost adjustment |
| DUMMY-FEE-001 | 50 synthetic bank charge |

Expected October revenue 12,000; expenses 5,250; profit 6,750; training bank
7,750; AR 7,000; AP 6,000; inventory 3,000. Trial balance debit and credit each
23,000. Historical cutoff 2026-10-08: AR 12,000 and AP 8,200. There are 13 posted
journals, 13 labelled evidence PDFs, five receipt/payment records and seven
matched statement rows with a retained synthetic CSV original.

After deployment, log in and open `/settings/dummy-data` for links, expected vs
current figures, journal/source/evidence drill-down and the normal indexed ZIP
export. The first import checks balances and evidence, commits, and reads back
its saved bundle/journal/document IDs. Runtime stdout reports the import result;
a successful CI or deployment alone does not prove that the live import ran.

The read-only sample overview is available to existing finance roles; staff
access remains denied. Do not remove synthetic rows automatically: preserve the
sample evidence and arrange a deliberate cleanup before using this ledger for
real books. A reversible, dependency-aware cleanup is outside this import.
