"""Shopify sales sync.

Pulls orders from the Shopify Admin GraphQL API and books each one as a single
balanced journal entry, converting the store currency to the company's base
currency at a configured FX rate. Idempotent per order via ``SyncedOrder``.

The accounting for one order (all amounts converted to base currency):

    Dr  Payment Processor Clearing   total collected − processor fee
    Dr  Merchant / Processing Fees    processor fee (if a fee % is configured)
    Dr  Sales Discounts               order discounts
        Cr  Sales Tax Payable             tax collected
        Cr  Shipping Income               shipping charged
        Cr  E-commerce Sales Revenue      (balancing figure = gross product sales)

By making revenue the balancing figure the entry always ties out exactly, even
when Shopify's own totals include tips, small rounding, or order-level
adjustments — those simply fall into revenue rather than breaking the books.

Network access is isolated behind ``transport`` so the posting logic can be
unit-tested with canned Shopify responses and no live call.
"""
import json
import hashlib
import re
import urllib.error
import urllib.request
from datetime import date, datetime

from app.extensions import db
from app.models import Account, CostCenter, JournalEntry, SalesIntegration, SyncedOrder, IntegrationReview, cents, now
from app.ledger import create_journal_entry, LedgerError


class ShopifyError(Exception):
    pass


_ORDERS_QUERY = """
query($cursor: String, $q: String) {
  orders(first: 50, after: $cursor, query: $q, sortKey: CREATED_AT) {
    edges {
      cursor
      node {
        id
        name
        createdAt
        processedAt
        displayFinancialStatus
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        totalShippingPriceSet { shopMoney { amount } }
        currentTotalTaxSet { shopMoney { amount } }
        currentTotalDiscountsSet { shopMoney { amount } }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_SHOP_QUERY = "{ shop { name currencyCode } }"


def _endpoint(shop_domain, api_version):
    domain = (shop_domain or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', domain, re.I):
        raise ShopifyError('Use the canonical store-name.myshopify.com host; custom hosts are not accepted for Admin API credentials.')
    if not re.fullmatch(r'\d{4}-(01|04|07|10)', api_version or ''):
        raise ShopifyError('Invalid Shopify API version.')
    return f"https://{domain}/admin/api/{api_version}/graphql.json"


def _default_transport(url, token, query, variables, timeout=30):
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Shopify-Access-Token": token or ""},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300] if hasattr(e, "read") else ""
        raise ShopifyError(f"Shopify API HTTP {e.code}: {detail or e.reason}")
    except urllib.error.URLError as e:
        raise ShopifyError(f"Could not reach Shopify: {e.reason}")
    except json.JSONDecodeError:
        raise ShopifyError("Shopify returned a non-JSON response (check the store domain).")
    if payload.get("errors"):
        raise ShopifyError(f"Shopify GraphQL error: {payload['errors']}")
    return payload.get("data", {})


def test_connection(integration, transport=_default_transport):
    """Return {'name', 'currency'} for the store, or raise ShopifyError."""
    url = _endpoint(integration.shop_domain, integration.api_version)
    data = transport(url, integration.access_token, _SHOP_QUERY, {})
    shop = (data or {}).get("shop")
    if not shop:
        raise ShopifyError("Connected, but no shop data returned — check the access token's scopes.")
    return {"name": shop.get("name", ""), "currency": shop.get("currencyCode", "")}


def _money(node, key):
    try:
        return cents(node[key]["shopMoney"]["amount"])
    except (KeyError, TypeError):
        return 0


def _parse_date(node):
    raw = node.get("processedAt") or node.get("createdAt")
    if not raw:
        return date.today()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return date.today()


def fetch_orders(integration, start, end, transport=_default_transport, max_pages=100):
    """Fetch normalized orders in [start, end] (store-currency cents)."""
    url = _endpoint(integration.shop_domain, integration.api_version)
    q = f"updated_at:>={start.isoformat()} updated_at:<={end.isoformat()}T23:59:59Z"
    orders, cursor, pages = [], None, 0
    while pages < max_pages:
        data = transport(url, integration.access_token, _ORDERS_QUERY, {"cursor": cursor, "q": q})
        block = (data or {}).get("orders") or {}
        for edge in block.get("edges", []):
            node = edge["node"]
            total_node = node.get("currentTotalPriceSet") or {}
            currency = ""
            try:
                currency = total_node["shopMoney"]["currencyCode"]
            except (KeyError, TypeError):
                pass
            orders.append({
                "external_id": node["id"],
                "name": node.get("name", ""),
                "date": _parse_date(node),
                "currency": currency,
                "total_cents": _money(node, "currentTotalPriceSet"),
                "shipping_cents": _money(node, "totalShippingPriceSet"),
                "tax_cents": _money(node, "currentTotalTaxSet"),
                "discounts_cents": _money(node, "currentTotalDiscountsSet"),
                "financial_status": node.get("displayFinancialStatus", ""),
            })
        page_info = block.get("pageInfo") or {}
        if page_info.get("hasNextPage") and block.get("edges"):
            cursor = page_info.get("endCursor")
            pages += 1
        else:
            break
    if pages >= max_pages:
        raise ShopifyError('Pagination limit reached. Narrow the date range; no incomplete import was posted.')
    return orders


def _acct(code):
    a = Account.query.filter_by(code=code).first()
    if not a:
        raise ShopifyError(f"GL account {code} is missing — run the seed or pick another in the integration settings.")
    return a


def _post_order(integration, order, created_by_id=None):
    """Book one normalized order as a balanced journal entry. Amounts -> base currency."""
    rate = integration.fx_rate
    import math
    if rate is None or not math.isfinite(rate) or rate <= 0 or not 0 <= (integration.fee_percent or 0) < 100:
        raise ShopifyError('A positive verified FX rate and fee percentage below 100 are required.')
    if order['currency'] != integration.store_currency:
        raise ShopifyError('Order currency differs from the configured store currency.')
    if order.get('financial_status') not in ('PAID', 'PARTIALLY_REFUNDED'):
        raise ShopifyError('Unpaid, voided or fully refunded orders need review before posting.')
    if order.get('financial_status') == 'PARTIALLY_REFUNDED':
        raise ShopifyError('Previously refunded order requires original sale and refund evidence; the net total is not a new sale.')

    def base(c):
        return int(round(c * rate))

    total = base(order["total_cents"])
    shipping = base(order["shipping_cents"])
    tax = base(order["tax_cents"])
    discounts = base(order["discounts_cents"])
    fee = int(round(total * (integration.fee_percent or 0) / 100.0))
    revenue = (total + discounts) - tax - shipping  # balancing figure = gross product sales

    cc = integration.cost_center_id
    lines = [
        {"account_id": _acct(integration.clearing_code).id, "debit_cents": total - fee,
         "description": f"Shopify {order['name']} net settlement"},
    ]
    if fee:
        lines.append({"account_id": _acct(integration.fee_code).id, "debit_cents": fee,
                      "cost_center_id": cc, "description": f"Processing fee {order['name']}"})
    if discounts:
        lines.append({"account_id": _acct(integration.discount_code).id, "debit_cents": discounts,
                      "cost_center_id": cc, "description": f"Discounts {order['name']}"})
    if tax:
        lines.append({"account_id": _acct(integration.tax_code).id, "credit_cents": tax,
                      "description": f"Sales tax {order['name']}"})
    if shipping:
        lines.append({"account_id": _acct(integration.shipping_code).id, "credit_cents": shipping,
                      "cost_center_id": cc, "description": f"Shipping {order['name']}"})
    # Revenue plug — normally >=0; if a heavy discount pushes it negative, flip sides.
    if revenue >= 0:
        lines.append({"account_id": _acct(integration.revenue_code).id, "credit_cents": revenue,
                      "cost_center_id": cc, "description": f"Sales {order['name']}"})
    else:
        lines.append({"account_id": _acct(integration.revenue_code).id, "debit_cents": -revenue,
                      "cost_center_id": cc, "description": f"Sales adj {order['name']}"})

    entry = create_journal_entry(
        order["date"],
        f"Shopify order {order['name']} ({integration.name or integration.shop_domain})",
        lines,
        source_type="shopify",
        reference=order["name"],
        created_by_id=created_by_id,
    )
    return entry, total


def sync(integration, start, end, created_by_id=None, transport=_default_transport):
    """Fetch orders in the range and post the ones not already booked.

    Fetches the complete range before posting. Each failed order rolls back its
    own savepoint; valid orders and review records commit together at the end.
    The summary explicitly reports rejected orders for follow-up.
    """
    orders = fetch_orders(integration, start, end, transport=transport)
    result = {"fetched": len(orders), "posted": 0, "skipped": 0, "errors": []}
    for order in orders:
        snapshot = json.dumps(order, sort_keys=True, default=str)
        exists = SyncedOrder.query.filter_by(
            integration_id=integration.id, external_id=order["external_id"]).first()
        if exists:
            if exists.source_snapshot == snapshot:
                result["skipped"] += 1
                continue
            digest = hashlib.sha256(snapshot.encode()).hexdigest()
            if not IntegrationReview.query.filter_by(integration_id=integration.id, external_id=order['external_id'], payload_hash=digest).first():
                db.session.add(IntegrationReview(integration_id=integration.id,external_id=order['external_id'],payload_hash=digest,
                                                detail=json.dumps({'original_posting':exists.journal_entry_id,'original_snapshot':exists.source_snapshot,'updated_order':order},default=str)))
            result['errors'].append(f"{order['name']}: changed or legacy order requires review; no refund or amended sale was silently skipped.")
            continue
        try:
            with db.session.begin_nested():
                entry, total = _post_order(integration, order, created_by_id)
                db.session.flush()
                db.session.add(SyncedOrder(
                    integration_id=integration.id, external_id=order["external_id"],
                    order_number=order["name"], order_date=order["date"],
                    currency=order["currency"], gross_cents=total, journal_entry_id=entry.id,
                    source_snapshot=snapshot,
                ))
                db.session.flush()
            result["posted"] += 1
        except (LedgerError, ShopifyError) as e:
            result["errors"].append(f"{order.get('name', '?')}: {e}")
    integration.last_sync_at = now()
    db.session.commit()
    return result
