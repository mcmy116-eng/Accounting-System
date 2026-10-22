from decimal import Decimal, ROUND_HALF_UP
from flask import abort


def line_amount(quantity, unit_cents):
    qty = Decimal(str(quantity or 1))
    if not qty.is_finite() or qty <= 0:
        raise ValueError('Quantity must be positive and finite.')
    if unit_cents < 0:
        raise ValueError('Use a credit/refund adjustment for negative amounts.')
    amount = int((qty * unit_cents).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if amount > 2147483647:
        raise ValueError('Amount exceeds the supported limit.')
    return float(qty), amount


def validate_lines(form):
    fields = ['line_description[]','line_quantity[]','line_price[]','line_account[]','line_tax[]','line_cost_center[]']
    lengths = [len(form.getlist(f)) for f in fields]
    if not lengths[0] or len(set(lengths)) != 1 or lengths[0] > 500:
        abort(400, 'Line fields must have matching lengths (1–500 rows).')
    if any(len(x) > 255 for x in form.getlist('line_description[]')):
        abort(400, 'Line descriptions must be at most 255 characters.')
