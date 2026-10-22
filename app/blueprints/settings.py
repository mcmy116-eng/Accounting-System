from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models import CompanySettings, Currency, TaxRate, CostCenter, Budget, Account, cents

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.route('/dummy-data')
@login_required
def dummy_data():
    import json
    from app.controls import lock_books
    from app.models import SampleDataBundle, Invoice, Bill
    from app.sample_data import BUNDLE_KEY, current_snapshot
    lock_books()
    bundle = db.session.get(SampleDataBundle, BUNDLE_KEY)
    manifest = json.loads(bundle.manifest_json) if bundle else {}
    installed = bundle and bundle.status == 'installed'
    return render_template('settings/dummy_data.html', bundle=bundle, manifest=manifest,
        actual=current_snapshot() if installed else {},
        invoices=Invoice.query.filter(Invoice.id.in_(manifest.get('invoice_ids', []))).all(),
        bills=Bill.query.filter(Bill.id.in_(manifest.get('bill_ids', []))).all())


@bp.route("/", methods=["GET", "POST"])
@login_required
def index():
    settings = CompanySettings.query.first()
    if request.method == "POST":
        if not current_user.is_admin():
            flash("Admins only.", "error")
            return redirect(url_for("settings.index"))
        settings.company_name = request.form.get("company_name", settings.company_name)
        settings.legal_address = request.form.get("legal_address", "")
        settings.tax_id = request.form.get("tax_id", "")
        from app.models import JournalEntry
        from flask import abort
        target = request.form.get('base_currency', settings.base_currency)
        if not db.session.get(Currency, target):
            abort(400, 'Select a configured currency.')
        if target != settings.base_currency and JournalEntry.query.first():
            abort(409, 'Base currency cannot change after posting. A reviewed conversion/migration is required.')
        if int(request.form.get('fiscal_year_start_month', 1)) not in range(1, 13):
            abort(400, 'Fiscal year start month must be 1–12.')
        settings.base_currency = request.form.get("base_currency", settings.base_currency)
        settings.fiscal_year_start_month = int(request.form.get("fiscal_year_start_month", 1))
        settings.invoice_prefix = request.form.get("invoice_prefix", settings.invoice_prefix)
        settings.bill_prefix = request.form.get("bill_prefix", settings.bill_prefix)
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings.index"))
    currencies = Currency.query.all()
    return render_template("settings/index.html", settings=settings, currencies=currencies)


@bp.route("/currencies/new", methods=["POST"])
@login_required
def new_currency():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("settings.index"))
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    symbol = request.form.get("symbol", "").strip()
    if len(code) != 3:
        flash("Currency code must be 3 letters (ISO 4217).", "error")
        return redirect(url_for("settings.index"))
    if not Currency.query.get(code):
        db.session.add(Currency(code=code, name=name, symbol=symbol))
        db.session.commit()
        flash(f"Currency {code} added.", "success")
    return redirect(url_for("settings.index"))


@bp.route("/tax-rates")
@login_required
def tax_rates():
    rates = TaxRate.query.all()
    return render_template("settings/tax_rates.html", rates=rates)


@bp.route("/tax-rates/new", methods=["POST"])
@login_required
def new_tax_rate():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("settings.tax_rates"))
    name = request.form.get("name", "").strip()
    rate = float(request.form.get("rate", 0))
    import math
    if not math.isfinite(rate) or not 0 <= rate <= 100:
        from flask import abort
        abort(400, 'Tax rate must be between 0 and 100 percent.')
    if not name:
        flash("Name is required.", "error")
        return redirect(url_for("settings.tax_rates"))
    db.session.add(TaxRate(name=name, rate=rate))
    db.session.commit()
    flash(f"Tax rate '{name}' added.", "success")
    return redirect(url_for("settings.tax_rates"))


@bp.route("/tax-rates/<int:rate_id>/toggle", methods=["POST"])
@login_required
def toggle_tax_rate(rate_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("settings.tax_rates"))
    r = TaxRate.query.get_or_404(rate_id)
    r.active = not r.active
    db.session.commit()
    return redirect(url_for("settings.tax_rates"))


@bp.route("/cost-centers")
@login_required
def cost_centers():
    centers = CostCenter.query.all()
    return render_template("settings/cost_centers.html", centers=centers)


@bp.route("/cost-centers/new", methods=["POST"])
@login_required
def new_cost_center():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("settings.cost_centers"))
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    if not code or not name:
        flash("Code and name are required.", "error")
        return redirect(url_for("settings.cost_centers"))
    if CostCenter.query.filter_by(code=code).first():
        flash("A cost center with that code already exists.", "error")
        return redirect(url_for("settings.cost_centers"))
    db.session.add(CostCenter(code=code, name=name))
    db.session.commit()
    flash(f"Cost center {code} - {name} created.", "success")
    return redirect(url_for("settings.cost_centers"))


@bp.route("/cost-centers/<int:cc_id>/toggle", methods=["POST"])
@login_required
def toggle_cost_center(cc_id):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("settings.cost_centers"))
    c = CostCenter.query.get_or_404(cc_id)
    c.active = not c.active
    db.session.commit()
    return redirect(url_for("settings.cost_centers"))


@bp.route("/budgets", methods=["GET", "POST"])
@login_required
def budgets():
    if request.method == "POST":
        if not current_user.can_edit():
            flash("You don't have permission to do that.", "error")
            return redirect(url_for("settings.budgets"))
        b = Budget(
            account_id=int(request.form["account_id"]),
            cost_center_id=int(request.form["cost_center_id"]) if request.form.get("cost_center_id") else None,
            period=request.form["period"],
            amount_cents=cents(request.form.get("amount") or 0),
        )
        db.session.add(b)
        db.session.commit()
        flash("Budget line added.", "success")
        return redirect(url_for("settings.budgets"))
    budget_lines = Budget.query.order_by(Budget.period.desc()).all()
    accounts = Account.query.filter(Account.type.in_(["revenue", "expense"]), Account.active == True).order_by(Account.code).all()  # noqa: E712
    cost_centers_list = CostCenter.query.filter_by(active=True).all()
    return render_template("settings/budgets.html", budget_lines=budget_lines, accounts=accounts, cost_centers=cost_centers_list)
