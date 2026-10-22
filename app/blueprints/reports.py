from datetime import date, timedelta
from flask import Blueprint, render_template, request, Response, abort
from flask_login import login_required
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Account, AccountType, JournalLine, JournalEntry, Invoice, Bill, InvoiceStatus, BillStatus,
    CostCenter, Budget, money,
)
from app.ledger import trial_balance as tb_calc
from app.report_builder import BUILDERS
from app.report_export import FORMATS

bp = Blueprint("reports", __name__, url_prefix="/reports")


@bp.route("/<report_slug>/export.<fmt>")
@login_required
def export(report_slug, fmt):
    """Download any core statement as CSV, Excel (.xlsx) or PDF.

    Uses the same period/as-of/cost-centre query params as the on-screen report
    so the file matches exactly what's displayed.
    """
    if report_slug not in BUILDERS or fmt not in FORMATS:
        abort(404)
    builder, kind = BUILDERS[report_slug]
    if kind == "period_cc":
        start, end = _period_bounds()
        rep = builder(start, end, request.args.get("cost_center_id", type=int))
    elif kind == "period":
        start, end = _period_bounds()
        rep = builder(start, end)
    elif kind in ("as_of", "as_of_opt"):
        as_of = request.args.get("as_of")
        rep = builder(date.fromisoformat(as_of) if as_of else date.today())
    else:  # as_of_opt — report defaults to today
        rep = builder()
    serializer, mimetype = FORMATS[fmt]
    resp = Response(serializer(rep), mimetype=mimetype)
    resp.headers["Content-Disposition"] = f"attachment; filename={rep.filename}.{fmt}"
    return resp


def _period_bounds():
    today = date.today()
    start = request.args.get("start")
    end = request.args.get("end")
    start = date.fromisoformat(start) if start else today.replace(day=1)
    end = date.fromisoformat(end) if end else today
    if start > end:
        abort(400, 'Start date must not follow end date.')
    return start, end


def _account_period_movement(account: Account, start, end, cost_center_id=None):
    end_bal = account.balance_cents(as_of=end, cost_center_id=cost_center_id)
    start_bal = account.balance_cents(as_of=start - timedelta(days=1), cost_center_id=cost_center_id)
    return end_bal - start_bal


@bp.route("/")
@login_required
def index():
    return render_template("reports/index.html")


@bp.route("/pnl")
@login_required
def pnl():
    start, end = _period_bounds()
    cost_center_id = request.args.get("cost_center_id", type=int)

    revenue_accounts = Account.query.filter_by(type=AccountType.REVENUE.value).order_by(Account.code).all()
    expense_accounts = Account.query.filter_by(type=AccountType.EXPENSE.value).order_by(Account.code).all()
    cogs = [a for a in expense_accounts if a.subtype == "cogs"]
    opex = [a for a in expense_accounts if a.subtype != "cogs"]

    def rows_for(accounts):
        out = []
        for a in accounts:
            amt = _account_period_movement(a, start, end, cost_center_id)
            if amt:
                out.append((a, amt))
        return out

    revenue_rows = rows_for(revenue_accounts)
    cogs_rows = rows_for(cogs)
    opex_rows = rows_for(opex)

    total_revenue = sum(r[1] for r in revenue_rows)
    total_cogs = sum(r[1] for r in cogs_rows)
    gross_profit = total_revenue - total_cogs
    total_opex = sum(r[1] for r in opex_rows)
    net_income = gross_profit - total_opex

    cost_centers = CostCenter.query.filter_by(active=True).all()
    return render_template(
        "reports/pnl.html", start=start, end=end, revenue_rows=revenue_rows, cogs_rows=cogs_rows,
        opex_rows=opex_rows, total_revenue=total_revenue, total_cogs=total_cogs,
        gross_profit=gross_profit, total_opex=total_opex, net_income=net_income,
        cost_centers=cost_centers, cost_center_id=cost_center_id,
    )


@bp.route("/pnl-by-segment")
@login_required
def pnl_by_segment():
    start, end = _period_bounds()
    cost_centers = CostCenter.query.filter_by(active=True).all()
    revenue_accounts = Account.query.filter_by(type=AccountType.REVENUE.value).all()
    expense_accounts = Account.query.filter_by(type=AccountType.EXPENSE.value).all()

    segments = []
    for cc in cost_centers:
        rev = sum(_account_period_movement(a, start, end, cc.id) for a in revenue_accounts)
        exp = sum(_account_period_movement(a, start, end, cc.id) for a in expense_accounts)
        segments.append({"cost_center": cc, "revenue": rev, "expense": exp, "net": rev - exp})

    rev_unassigned = sum(_account_period_movement(a, start, end, None) for a in revenue_accounts) - sum(s["revenue"] for s in segments)
    exp_unassigned = sum(_account_period_movement(a, start, end, None) for a in expense_accounts) - sum(s["expense"] for s in segments)

    return render_template("reports/pnl_by_segment.html", start=start, end=end, segments=segments,
                            rev_unassigned=rev_unassigned, exp_unassigned=exp_unassigned)


@bp.route("/balance-sheet")
@login_required
def balance_sheet():
    as_of = request.args.get("as_of")
    as_of = date.fromisoformat(as_of) if as_of else date.today()

    assets = Account.query.filter_by(type=AccountType.ASSET.value).order_by(Account.code).all()
    liabilities = Account.query.filter_by(type=AccountType.LIABILITY.value).order_by(Account.code).all()
    equity = Account.query.filter_by(type=AccountType.EQUITY.value).order_by(Account.code).all()
    revenue_accounts = Account.query.filter_by(type=AccountType.REVENUE.value).all()
    expense_accounts = Account.query.filter_by(type=AccountType.EXPENSE.value).all()

    asset_rows = [(a, a.balance_cents(as_of=as_of)) for a in assets]
    asset_rows = [r for r in asset_rows if r[1]]
    liab_rows = [(a, a.balance_cents(as_of=as_of)) for a in liabilities]
    liab_rows = [r for r in liab_rows if r[1]]
    equity_rows = [(a, a.balance_cents(as_of=as_of)) for a in equity]
    equity_rows = [r for r in equity_rows if r[1]]

    retained_earnings = sum(a.balance_cents(as_of=as_of) for a in revenue_accounts) - \
        sum(a.balance_cents(as_of=as_of) for a in expense_accounts)

    total_assets = sum(r[1] for r in asset_rows)
    total_liabilities = sum(r[1] for r in liab_rows)
    total_equity = sum(r[1] for r in equity_rows) + retained_earnings

    return render_template(
        "reports/balance_sheet.html", as_of=as_of, asset_rows=asset_rows, liab_rows=liab_rows,
        equity_rows=equity_rows, retained_earnings=retained_earnings,
        total_assets=total_assets, total_liabilities=total_liabilities, total_equity=total_equity,
    )


@bp.route("/cash-flow")
@login_required
def cash_flow():
    start, end = _period_bounds()

    revenue_accounts = Account.query.filter_by(type=AccountType.REVENUE.value).all()
    expense_accounts = Account.query.filter_by(type=AccountType.EXPENSE.value).all()
    net_income = sum(_account_period_movement(a, start, end) for a in revenue_accounts) - \
        sum(_account_period_movement(a, start, end) for a in expense_accounts)

    all_assets = Account.query.filter_by(type=AccountType.ASSET.value).all()
    working_capital_assets = [a for a in all_assets if not a.is_bank and a.code not in ("1400", "1450")]
    fixed_assets = [a for a in all_assets if a.code in ("1400", "1450")]
    liabilities = Account.query.filter_by(type=AccountType.LIABILITY.value).all()
    op_liabilities = [a for a in liabilities if a.code != "2600"]
    financing_liabilities = [a for a in liabilities if a.code == "2600"]
    equity_accounts = Account.query.filter_by(type=AccountType.EQUITY.value).all()

    wc_change = sum(_account_period_movement(a, start, end) for a in working_capital_assets)
    liab_change = sum(_account_period_movement(a, start, end) for a in op_liabilities)
    operating = net_income - wc_change + liab_change

    investing = -sum(_account_period_movement(a, start, end) for a in fixed_assets)
    financing = sum(_account_period_movement(a, start, end) for a in equity_accounts) + \
        sum(_account_period_movement(a, start, end) for a in financing_liabilities)

    net_change = operating + investing + financing

    bank_accounts = [a for a in all_assets if a.is_bank]
    cash_start = sum(a.balance_cents(as_of=start - timedelta(days=1)) for a in bank_accounts)
    cash_end = sum(a.balance_cents(as_of=end) for a in bank_accounts)

    return render_template(
        "reports/cash_flow.html", start=start, end=end, net_income=net_income, wc_change=wc_change,
        liab_change=liab_change, operating=operating, investing=investing, financing=financing,
        net_change=net_change, cash_start=cash_start, cash_end=cash_end,
    )


@bp.route("/trial-balance")
@login_required
def trial_balance():
    as_of = request.args.get("as_of")
    as_of = date.fromisoformat(as_of) if as_of else date.today()
    rows, total_debit, total_credit = tb_calc(as_of=as_of)
    return render_template("reports/trial_balance.html", as_of=as_of, rows=rows,
                            total_debit=total_debit, total_credit=total_credit)


def _aging_buckets(items, as_of):
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


@bp.route("/ar-aging")
@login_required
def ar_aging():
    as_of = date.fromisoformat(request.args['as_of']) if request.args.get('as_of') else date.today()
    from app.aging import aging_items
    items = aging_items('invoice', as_of)
    buckets, detail = _aging_buckets(items, as_of)
    return render_template("reports/ar_aging.html", as_of=as_of, buckets=buckets, detail=detail, kind="invoice")


@bp.route("/ap-aging")
@login_required
def ap_aging():
    as_of = date.fromisoformat(request.args['as_of']) if request.args.get('as_of') else date.today()
    from app.aging import aging_items
    items = aging_items('bill', as_of)
    buckets, detail = _aging_buckets(items, as_of)
    return render_template("reports/ar_aging.html", as_of=as_of, buckets=buckets, detail=detail, kind="bill")


@bp.route("/tax")
@login_required
def tax():
    start, end = _period_bounds()
    payable = Account.query.filter_by(code="2200").first()
    receivable = Account.query.filter_by(code="1500").first()
    collected = _account_period_movement(payable, start, end) if payable else 0
    paid = _account_period_movement(receivable, start, end) if receivable else 0
    net_due = collected - paid
    return render_template("reports/tax.html", start=start, end=end, collected=collected, paid=paid, net_due=net_due)


@bp.route("/budget")
@login_required
def budget():
    period = request.args.get("period") or date.today().strftime("%Y-%m")
    year, month = int(period[:4]), int(period[5:7])
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)

    budgets = Budget.query.filter_by(period=period).all()
    rows = []
    for b in budgets:
        actual = _account_period_movement(b.account, start, end, b.cost_center_id)
        rows.append({
            "account": b.account, "cost_center": b.cost_center,
            "budget": b.amount_cents, "actual": actual, "variance": actual - b.amount_cents,
        })
    return render_template("reports/budget.html", period=period, rows=rows)
