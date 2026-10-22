"""Sales-channel integrations (Shopify today).

Admins configure a store connection, test it, and run a sync for a date range;
each imported order becomes a balanced journal entry. Read access is open to
any signed-in bookkeeper/admin; changes and syncs require edit rights.
"""
from datetime import date, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from app.extensions import db
from app.models import SalesIntegration, SyncedOrder, CostCenter, Account, IntegrationReview, JournalEntry
from app import shopify_sync
from app.audit import record

bp = Blueprint("integrations", __name__, url_prefix="/integrations")


def _period():
    today = date.today()
    start = request.values.get("start")
    end = request.values.get("end")
    start = date.fromisoformat(start) if start else today.replace(day=1)
    end = date.fromisoformat(end) if end else today
    if start>end:abort(400,'Start date must not follow end date.')
    return start, end


@bp.route("/")
@login_required
def index():
    if current_user.is_staff_only():
        flash("You don't have access to integrations.", "error")
        return redirect(url_for("claims.index"))
    integrations = SalesIntegration.query.order_by(SalesIntegration.id).all()
    recent = (SyncedOrder.query.order_by(SyncedOrder.synced_at.desc()).limit(20).all())
    cost_centers = CostCenter.query.filter_by(active=True).all()
    start, end = _period()
    return render_template(
        "integrations/index.html", integrations=integrations, recent=recent,
        cost_centers=cost_centers, start=start, end=end, reviews=IntegrationReview.query.filter_by(status='pending').all(),
    )


@bp.route("/shopify/save", methods=["POST"])
@login_required
def save_shopify():
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("integrations.index"))
    iid = request.form.get("id", type=int)
    integ = SalesIntegration.query.get(iid) if iid else None
    if not integ:
        integ = SalesIntegration(provider="shopify")
        db.session.add(integ)

    integ.name = request.form.get("name", "").strip()
    integ.shop_domain = request.form.get("shop_domain", "").strip()
    try:
        shopify_sync._endpoint(integ.shop_domain, integ.api_version or '2024-10')
    except shopify_sync.ShopifyError as e:
        abort(400, str(e))
    token = request.form.get("access_token", "").strip()
    if token and token != "__unchanged__":
        integ.access_token = token
    integ.store_currency = (request.form.get("store_currency", "USD").strip().upper() or "USD")[:3]
    try:
        integ.fx_rate = float(request.form.get("fx_rate") or 1.0)
    except ValueError:
        abort(400, 'Invalid FX rate; it was not replaced with 1:1.')
    try:
        integ.fee_percent = float(request.form.get("fee_percent") or 0.0)
    except ValueError:
        abort(400, 'Invalid fee percentage.')
    import math
    if not math.isfinite(integ.fx_rate) or integ.fx_rate <= 0 or not math.isfinite(integ.fee_percent) or not 0 <= integ.fee_percent < 100:
        abort(400, 'Enter a positive verified FX rate and a fee below 100%.')
    integ.cost_center_id = request.form.get("cost_center_id", type=int) or None
    integ.active = bool(request.form.get("active"))
    db.session.commit()
    record("update", "integration", integ.id, f"Saved Shopify integration '{integ.name or integ.shop_domain}'")
    db.session.commit()
    flash("Shopify integration saved.", "success")
    return redirect(url_for("integrations.index"))


@bp.route("/<int:iid>/test", methods=["POST"])
@login_required
def test(iid):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("integrations.index"))
    integ = SalesIntegration.query.get_or_404(iid)
    try:
        info = shopify_sync.test_connection(integ)
        flash(f"Connected to '{info['name']}' (store currency {info['currency']}).", "success")
    except shopify_sync.ShopifyError as e:
        flash(f"Connection failed: {e}", "error")
    return redirect(url_for("integrations.index"))


@bp.route("/<int:iid>/sync", methods=["POST"])
@login_required
def run_sync(iid):
    if not current_user.can_edit():
        flash("You don't have permission to do that.", "error")
        return redirect(url_for("integrations.index"))
    integ = SalesIntegration.query.get_or_404(iid)
    if not integ.active:abort(409,'Enable and review the connection before syncing.')
    start, end = _period()
    try:
        res = shopify_sync.sync(integ, start, end, created_by_id=current_user.id)
    except shopify_sync.ShopifyError as e:
        flash(f"Sync failed: {e}", "error")
        return redirect(url_for("integrations.index", start=start, end=end))
    record("sync", "integration", integ.id,
           f"Shopify sync {start}..{end}: {res['posted']} posted, {res['skipped']} skipped")
    db.session.commit()
    msg = f"Sync complete: {res['posted']} order(s) booked, {res['skipped']} already booked, {res['fetched']} fetched."
    flash(msg, "success" if not res["errors"] else "error")
    for err in res["errors"][:5]:
        flash(f"Order error — {err}", "error")
    return redirect(url_for("integrations.index", start=start, end=end))


@bp.route("/<int:iid>/delete", methods=["POST"])
@login_required
def delete(iid):
    if not current_user.is_admin():
        flash("Admins only.", "error")
        return redirect(url_for("integrations.index"))
    integ = SalesIntegration.query.get_or_404(iid)
    if SyncedOrder.query.filter_by(integration_id=iid).first():
        flash("This integration has booked orders and can't be deleted (disable it instead).", "error")
        return redirect(url_for("integrations.index"))
    db.session.delete(integ)
    db.session.commit()
    flash("Integration removed.", "success")
    return redirect(url_for("integrations.index"))


@bp.route('/reviews/<int:review_id>/resolve',methods=['POST'])
@login_required
def resolve(review_id):
    if not current_user.can_edit():abort(403)
    review=IntegrationReview.query.get_or_404(review_id)
    if review.status!='pending':abort(409,'Review already resolved.')
    reason=request.form.get('reason','').strip()
    entry=JournalEntry.query.get_or_404(request.form.get('journal_entry_id',type=int))
    from app.models import Document
    if not reason or not entry.is_posted or entry.reference != f'SALES-REVIEW-{review.id}' or not Document.query.filter_by(journal_entry_id=entry.id).first():
        abort(409,'Attach the source refund/change evidence to a posted adjustment with reference SALES-REVIEW-'+str(review.id))
    review.status='resolved';review.journal_entry_id=entry.id
    import json
    original=SyncedOrder.query.filter_by(integration_id=review.integration_id,external_id=review.external_id).first()
    if original:
        original.source_snapshot=json.dumps(json.loads(review.detail)['updated_order'],sort_keys=True,default=str)
    record('resolve','integration_review',review.id,reason,{'journal_id':entry.id,'external_id':review.external_id})
    db.session.commit();return redirect(url_for('integrations.index'))
