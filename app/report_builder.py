"""Build structured, format-agnostic report data.

Each ``build_*`` function returns a :class:`Report` — a title, some metadata
lines, a column header, and a list of ``(style, cells)`` rows. The same objects
feed the on-screen tables' exporters (CSV / Excel / PDF) in ``report_export.py``,
so a downloaded statement always matches what's shown on screen.

Amount cells are stored as **float dollars** (cents / 100) so spreadsheets treat
them as numbers; label cells are strings. Row ``style`` is one of
``section`` / ``data`` / ``subtotal`` / ``total`` / ``blank`` and drives bolding.
"""
from datetime import date, timedelta

from app.models import (
    Account, AccountType, Invoice, Bill, InvoiceStatus, BillStatus, CompanySettings,
)
from app.ledger import trial_balance as tb_calc


def account_period_movement(account, start, end, cost_center_id=None):
    end_bal = account.balance_cents(as_of=end, cost_center_id=cost_center_id)
    start_bal = account.balance_cents(as_of=start - timedelta(days=1), cost_center_id=cost_center_id)
    return end_bal - start_bal


def aging_buckets(items, as_of):
    buckets = {"current": 0, "1-30": 0, "31-60": 0, "61-90": 0, "90+": 0}
    detail = []
    for item, due_date, balance in items:
        if balance == 0:
            continue
        days = (as_of - due_date).days
        if days <= 0:
            bucket = "current"
        elif days <= 30:
            bucket = "1-30"
        elif days <= 60:
            bucket = "31-60"
        elif days <= 90:
            bucket = "61-90"
        else:
            bucket = "90+"
        buckets[bucket] += balance
        detail.append({"item": item, "due_date": due_date, "balance": balance, "days": days, "bucket": bucket})
    return buckets, detail


def _d(cents):
    """cents -> float dollars."""
    return round((cents or 0) / 100, 2)


class Report:
    def __init__(self, title, meta=None, header=None, filename="report"):
        self.title = title
        self.meta = meta or []       # list[str]
        self.header = header or []   # list[str]
        self.rows = []               # list[(style, cells)]
        self.filename = filename

    def add(self, cells, style="data"):
        self.rows.append((style, list(cells)))

    def blank(self):
        self.rows.append(("blank", []))


def _base_meta(*extra):
    s = CompanySettings.query.first()
    name = s.company_name if s else "Company"
    ccy = s.base_currency if s else ""
    lines = [name, f"Currency: {ccy}"]
    lines.extend(extra)
    return lines, ccy


# --------------------------------------------------------------------------- #

def build_pnl(start, end, cost_center_id=None):
    meta, ccy = _base_meta(f"Period: {start} to {end}")
    rep = Report("Profit & Loss", meta=meta, header=["Account", f"Amount ({ccy})"],
                 filename=f"profit_and_loss_{start}_{end}")

    revenue = Account.query.filter_by(type=AccountType.REVENUE.value).order_by(Account.code).all()
    expenses = Account.query.filter_by(type=AccountType.EXPENSE.value).order_by(Account.code).all()
    cogs = [a for a in expenses if a.subtype == "cogs"]
    opex = [a for a in expenses if a.subtype != "cogs"]

    def section(title, accounts):
        rep.add([title, None], "section")
        total = 0
        for a in accounts:
            amt = account_period_movement(a, start, end, cost_center_id)
            if amt:
                rep.add([f"{a.code} {a.name}", _d(amt)])
                total += amt
        return total

    total_rev = section("Revenue", revenue)
    rep.add(["Total revenue", _d(total_rev)], "subtotal")
    rep.blank()
    total_cogs = section("Cost of Goods Sold", cogs)
    rep.add(["Total COGS", _d(total_cogs)], "subtotal")
    gross = total_rev - total_cogs
    rep.add(["Gross profit", _d(gross)], "total")
    rep.blank()
    total_opex = section("Operating Expenses", opex)
    rep.add(["Total operating expenses", _d(total_opex)], "subtotal")
    rep.add(["Net income", _d(gross - total_opex)], "total")
    return rep


def build_balance_sheet(as_of):
    meta, ccy = _base_meta(f"As of: {as_of}")
    rep = Report("Balance Sheet", meta=meta, header=["Account", f"Amount ({ccy})"],
                 filename=f"balance_sheet_{as_of}")

    def section(title, accounts):
        rep.add([title, None], "section")
        total = 0
        for a in accounts:
            bal = a.balance_cents(as_of=as_of)
            if bal:
                rep.add([f"{a.code} {a.name}", _d(bal)])
                total += bal
        return total

    assets = Account.query.filter_by(type=AccountType.ASSET.value).order_by(Account.code).all()
    liabs = Account.query.filter_by(type=AccountType.LIABILITY.value).order_by(Account.code).all()
    equity = Account.query.filter_by(type=AccountType.EQUITY.value).order_by(Account.code).all()
    rev = Account.query.filter_by(type=AccountType.REVENUE.value).all()
    exp = Account.query.filter_by(type=AccountType.EXPENSE.value).all()

    total_assets = section("Assets", assets)
    rep.add(["Total assets", _d(total_assets)], "total")
    rep.blank()
    total_liab = section("Liabilities", liabs)
    rep.add(["Total liabilities", _d(total_liab)], "subtotal")
    rep.blank()
    total_eq = section("Equity", equity)
    retained = sum(a.balance_cents(as_of=as_of) for a in rev) - sum(a.balance_cents(as_of=as_of) for a in exp)
    rep.add(["Retained earnings (current)", _d(retained)])
    total_eq += retained
    rep.add(["Total equity", _d(total_eq)], "subtotal")
    rep.add(["Total liabilities & equity", _d(total_liab + total_eq)], "total")
    return rep


def build_cash_flow(start, end):
    meta, ccy = _base_meta(f"Period: {start} to {end}")
    rep = Report("Cash Flow", meta=meta, header=["", f"Amount ({ccy})"],
                 filename=f"cash_flow_{start}_{end}")

    rev = Account.query.filter_by(type=AccountType.REVENUE.value).all()
    exp = Account.query.filter_by(type=AccountType.EXPENSE.value).all()
    net_income = sum(account_period_movement(a, start, end) for a in rev) - \
        sum(account_period_movement(a, start, end) for a in exp)

    all_assets = Account.query.filter_by(type=AccountType.ASSET.value).all()
    wc_assets = [a for a in all_assets if not a.is_bank and a.code not in ("1400", "1450")]
    fixed = [a for a in all_assets if a.code in ("1400", "1450")]
    liabs = Account.query.filter_by(type=AccountType.LIABILITY.value).all()
    op_liab = [a for a in liabs if a.code != "2600"]
    fin_liab = [a for a in liabs if a.code == "2600"]
    equity = Account.query.filter_by(type=AccountType.EQUITY.value).all()

    wc_change = sum(account_period_movement(a, start, end) for a in wc_assets)
    liab_change = sum(account_period_movement(a, start, end) for a in op_liab)
    operating = net_income - wc_change + liab_change
    investing = -sum(account_period_movement(a, start, end) for a in fixed)
    financing = sum(account_period_movement(a, start, end) for a in equity) + \
        sum(account_period_movement(a, start, end) for a in fin_liab)
    net_change = operating + investing + financing

    banks = [a for a in all_assets if a.is_bank]
    cash_start = sum(a.balance_cents(as_of=start - timedelta(days=1)) for a in banks)
    cash_end = sum(a.balance_cents(as_of=end) for a in banks)

    rep.add(["Operating activities", None], "section")
    rep.add(["Net income", _d(net_income)])
    rep.add(["Change in working-capital assets", _d(-wc_change)])
    rep.add(["Change in operating liabilities", _d(liab_change)])
    rep.add(["Net cash from operating activities", _d(operating)], "subtotal")
    rep.blank()
    rep.add(["Investing activities", None], "section")
    rep.add(["Net cash from investing activities", _d(investing)], "subtotal")
    rep.blank()
    rep.add(["Financing activities", None], "section")
    rep.add(["Net cash from financing activities", _d(financing)], "subtotal")
    rep.blank()
    rep.add(["Net change in cash", _d(net_change)], "total")
    rep.add(["Cash at start of period", _d(cash_start)])
    rep.add(["Cash at end of period", _d(cash_end)], "total")
    return rep


def build_trial_balance(as_of):
    meta, ccy = _base_meta(f"As of: {as_of}")
    rep = Report("Trial Balance", meta=meta,
                 header=["Account", f"Debit ({ccy})", f"Credit ({ccy})"],
                 filename=f"trial_balance_{as_of}")
    rows, total_debit, total_credit = tb_calc(as_of=as_of)
    for r in rows:
        a = r["account"]
        rep.add([f"{a.code} {a.name}", _d(r["debit"]) or None, _d(r["credit"]) or None])
    rep.add(["Total", _d(total_debit), _d(total_credit)], "total")
    return rep


def _aging_report(title, filename, items, as_of):
    meta, ccy = _base_meta(f"As of: {as_of}")
    rep = Report(title, meta=meta,
                 header=["Name", "Reference", "Due date", "Days overdue", "Bucket", f"Balance ({ccy})"],
                 filename=filename)
    buckets, detail = aging_buckets(items, as_of)
    for d in sorted(detail, key=lambda x: -x["days"]):
        item = d["item"]
        name = item.contact.name if getattr(item, "contact", None) else ""
        rep.add([name, item.number, str(d["due_date"]), d["days"], d["bucket"], _d(d["balance"])])
    rep.blank()
    for b in ["current", "1-30", "31-60", "61-90", "90+"]:
        rep.add([f"Total {b}", None, None, None, None, _d(buckets[b])], "subtotal")
    rep.add(["Total outstanding", None, None, None, None, _d(sum(buckets.values()))], "total")
    return rep


def build_ar_aging(as_of=None):
    as_of = as_of or date.today()
    from app.aging import aging_items
    items = aging_items('invoice', as_of)
    return _aging_report("Accounts Receivable Aging", f"ar_aging_{as_of}", items, as_of)


def build_ap_aging(as_of=None):
    as_of = as_of or date.today()
    from app.aging import aging_items
    items = aging_items('bill', as_of)
    return _aging_report("Accounts Payable Aging", f"ap_aging_{as_of}", items, as_of)


def build_tax(start, end):
    meta, ccy = _base_meta(f"Period: {start} to {end}")
    rep = Report("Tax Summary", meta=meta, header=["", f"Amount ({ccy})"],
                 filename=f"tax_summary_{start}_{end}")
    payable = Account.query.filter_by(code="2200").first()
    receivable = Account.query.filter_by(code="1500").first()
    collected = account_period_movement(payable, start, end) if payable else 0
    paid = account_period_movement(receivable, start, end) if receivable else 0
    rep.add(["Tax collected on sales (output)", _d(collected)])
    rep.add(["Tax paid on purchases (input)", _d(paid)])
    rep.add(["Net tax due", _d(collected - paid)], "total")
    return rep


# Registry used by the export routes: slug -> (builder, param-kind)
BUILDERS = {
    "pnl": (build_pnl, "period_cc"),
    "balance-sheet": (build_balance_sheet, "as_of"),
    "cash-flow": (build_cash_flow, "period"),
    "trial-balance": (build_trial_balance, "as_of"),
    "ar-aging": (build_ar_aging, "as_of_opt"),
    "ap-aging": (build_ap_aging, "as_of_opt"),
    "tax": (build_tax, "period"),
}
