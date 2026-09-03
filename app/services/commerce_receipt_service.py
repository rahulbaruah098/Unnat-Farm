from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


QTY_EPSILON = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")


def _decimal(value, default="0") -> Decimal:
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _qty_text(value) -> str:
    value = _decimal(value).quantize(Decimal("0.0001"))
    return f"{value:f}".rstrip("0").rstrip(".") or "0"


def _money(value) -> Decimal:
    return _decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _line_id(line, index: int) -> str:
    return str((line or {}).get("line_id") or ("legacy" if index == 0 else f"line-{index + 1}"))


def normalize_receipt_lines(
    order_items,
    submitted_lines=None,
    *,
    dispatched_fields=("dispatched_quantity", "delivered_quantity", "base_quantity", "approved_quantity"),
    price_field="unit_price",
    line_total_field="line_total",
    allow_legacy_full_receipt=True,
):
    """Validate one buyer-side goods receipt without mutating persistence.

    The seller owns dispatch. The buyer owns receipt/acceptance. Each returned row
    contains dispatched, physically received, accepted, damaged, rejected and
    automatically calculated missing quantities.

    `submitted_lines` is intentionally a list of plain dictionaries so all
    commerce services can share the same validation rules without sharing stock
    or accounting persistence.
    """
    source_items = [dict(x or {}) for x in (order_items or []) if isinstance(x, dict)]
    if not source_items:
        raise ValueError("This order has no product lines to receive.")

    submitted_map = {}
    if isinstance(submitted_lines, list):
        for row in submitted_lines:
            if isinstance(row, dict):
                key = str(row.get("line_id") or "").strip()
                if key:
                    submitted_map[key] = row

    results = []
    for index, source in enumerate(source_items):
        line = dict(source)
        line_id = _line_id(line, index)
        dispatched = Decimal("0")
        for field in dispatched_fields:
            candidate = _decimal(line.get(field))
            if candidate > 0:
                dispatched = candidate
                break
        if dispatched <= 0:
            # Non-approved/rejected lines remain visible in the order but do not
            # participate in the receipt transaction.
            line.update({
                "line_id": line_id,
                "receipt_applicable": False,
                "physically_received_quantity": 0.0,
                "accepted_quantity": 0.0,
                "damaged_quantity": 0.0,
                "rejected_quantity": 0.0,
                "missing_quantity": 0.0,
                "discrepancy_quantity": 0.0,
                "receipt_line_status": "not_dispatched",
            })
            results.append(line)
            continue

        submitted = submitted_map.get(line_id)
        if submitted is None and allow_legacy_full_receipt:
            submitted = {
                "physically_received_quantity": dispatched,
                "accepted_quantity": dispatched,
                "damaged_quantity": 0,
                "rejected_quantity": 0,
            }
        elif submitted is None:
            raise ValueError(f"Enter received quantities for {line.get('product_name') or 'each dispatched product'}.")

        physically_received = _decimal(
            submitted.get("physically_received_quantity")
            if submitted.get("physically_received_quantity") not in (None, "")
            else submitted.get("received_quantity")
        )
        accepted = _decimal(submitted.get("accepted_quantity"))
        damaged = _decimal(submitted.get("damaged_quantity"))
        rejected = _decimal(submitted.get("rejected_quantity"))

        product_name = line.get("product_name") or line.get("produce_name") or "Product"
        unit_code = line.get("unit_code") or "Unit"

        for label, value in (
            ("Received", physically_received),
            ("Accepted", accepted),
            ("Damaged", damaged),
            ("Rejected", rejected),
        ):
            if value < 0:
                raise ValueError(f"{label} quantity for {product_name} cannot be negative.")

        if physically_received - dispatched > QTY_EPSILON:
            raise ValueError(
                f"Received quantity for {product_name} cannot exceed the dispatched {_qty_text(dispatched)} {unit_code}."
            )

        classified = accepted + damaged + rejected
        if classified - physically_received > QTY_EPSILON:
            raise ValueError(
                f"Accepted + damaged + rejected for {product_name} cannot exceed the physically received quantity."
            )
        if abs(classified - physically_received) > QTY_EPSILON:
            raise ValueError(
                f"For {product_name}, Accepted + Damaged + Rejected must equal the physically received quantity."
            )

        missing = max(dispatched - physically_received, Decimal("0"))
        discrepancy = damaged + rejected + missing
        receipt_line_status = "accepted" if discrepancy <= QTY_EPSILON else "discrepancy"
        if accepted <= QTY_EPSILON and discrepancy > QTY_EPSILON:
            receipt_line_status = "not_accepted"

        price = _decimal(line.get(price_field))
        dispatched_subtotal = _money(dispatched * price)
        accepted_subtotal = _money(accepted * price)

        # Preserve a pre-existing line total when it carries pack pricing or a
        # different commercial basis. For proportional receipt settlement, use
        # the dispatched quantity ratio rather than assuming unit_price.
        source_line_total = _decimal(line.get(line_total_field))
        if source_line_total > 0 and dispatched > QTY_EPSILON:
            accepted_ratio = min(max(accepted / dispatched, Decimal("0")), Decimal("1"))
            accepted_commercial_total = _money(source_line_total * accepted_ratio)
            dispatched_commercial_total = _money(source_line_total)
        else:
            accepted_commercial_total = accepted_subtotal
            dispatched_commercial_total = dispatched_subtotal

        line.update({
            "line_id": line_id,
            "receipt_applicable": True,
            "dispatched_quantity_for_receipt": float(dispatched),
            "physically_received_quantity": float(physically_received),
            "received_quantity": float(physically_received),
            "accepted_quantity": float(accepted),
            "damaged_quantity": float(damaged),
            "rejected_quantity": float(rejected),
            "missing_quantity": float(missing),
            "discrepancy_quantity": float(discrepancy),
            "receipt_line_status": receipt_line_status,
            "accepted_subtotal": float(accepted_subtotal),
            "accepted_commercial_total": float(accepted_commercial_total),
            "dispatched_commercial_total": float(dispatched_commercial_total),
        })
        results.append(line)

    applicable = [row for row in results if row.get("receipt_applicable")]
    if not applicable:
        raise ValueError("There are no dispatched product lines to receive.")

    return results


def summarize_receipt(receipt_lines):
    rows = [dict(x or {}) for x in (receipt_lines or []) if isinstance(x, dict) and x.get("receipt_applicable")]
    if not rows:
        return {
            "receipt_status": "none",
            "dispatched_item_count": 0,
            "received_item_count": 0,
            "accepted_item_count": 0,
            "discrepancy_item_count": 0,
            "accepted_value": 0.0,
            "dispatched_value": 0.0,
            "adjustment_value": 0.0,
        }

    accepted_value = sum((_decimal(x.get("accepted_commercial_total")) for x in rows), Decimal("0"))
    dispatched_value = sum((_decimal(x.get("dispatched_commercial_total")) for x in rows), Decimal("0"))
    discrepancy_count = sum(1 for x in rows if _decimal(x.get("discrepancy_quantity")) > QTY_EPSILON)
    received_count = sum(1 for x in rows if _decimal(x.get("physically_received_quantity")) > QTY_EPSILON)
    accepted_count = sum(1 for x in rows if _decimal(x.get("accepted_quantity")) > QTY_EPSILON)

    return {
        "receipt_status": "full" if discrepancy_count == 0 else "discrepancy",
        "dispatched_item_count": len(rows),
        "received_item_count": received_count,
        "accepted_item_count": accepted_count,
        "discrepancy_item_count": discrepancy_count,
        "accepted_value": float(_money(accepted_value)),
        "dispatched_value": float(_money(dispatched_value)),
        "adjustment_value": float(_money(max(dispatched_value - accepted_value, Decimal("0")))),
    }


def proportional_amount(amount, accepted_quantity, dispatched_quantity):
    """Return the buyer-accepted share of a line amount, rounded to paise."""
    amount = _decimal(amount)
    accepted = max(_decimal(accepted_quantity), Decimal("0"))
    dispatched = max(_decimal(dispatched_quantity), Decimal("0"))
    if amount <= 0 or dispatched <= QTY_EPSILON or accepted <= 0:
        return Decimal("0.00")
    ratio = min(accepted / dispatched, Decimal("1"))
    return _money(amount * ratio)


def receipt_label(status):
    return {
        "full": "Fully Received",
        "discrepancy": "Received with Discrepancy",
        "none": "Not Received",
    }.get(str(status or ""), str(status or "").replace("_", " ").title())


def receipt_issue_details(receipt_lines, *, max_lines=6):
    """Return compact, unit-safe buyer receipt discrepancy details.

    This is presentation/audit text only. It never changes stock, invoice or
    settlement calculations. Quantities remain line-wise so unlike units are
    never added together.
    """
    details = []
    for raw in receipt_lines or []:
        if not isinstance(raw, dict) or not raw.get("receipt_applicable"):
            continue
        damaged = _decimal(raw.get("damaged_quantity"))
        rejected = _decimal(raw.get("rejected_quantity"))
        missing = _decimal(raw.get("missing_quantity"))
        if damaged + rejected + missing <= QTY_EPSILON:
            continue

        product = str(raw.get("product_name") or raw.get("produce_name") or "Product").strip()
        unit = str(raw.get("unit_code") or "Unit").strip()
        dispatched = _decimal(
            raw.get("dispatched_quantity_for_receipt")
            if raw.get("dispatched_quantity_for_receipt") is not None
            else raw.get("dispatched_quantity")
            if raw.get("dispatched_quantity") is not None
            else raw.get("delivered_quantity")
            if raw.get("delivered_quantity") is not None
            else raw.get("base_quantity")
        )
        received = _decimal(
            raw.get("physically_received_quantity")
            if raw.get("physically_received_quantity") is not None
            else raw.get("received_quantity")
        )
        accepted = _decimal(raw.get("accepted_quantity"))

        issues = []
        if damaged > QTY_EPSILON:
            issues.append(f"{_qty_text(damaged)} damaged")
        if rejected > QTY_EPSILON:
            issues.append(f"{_qty_text(rejected)} rejected")
        if missing > QTY_EPSILON:
            issues.append(f"{_qty_text(missing)} missing")

        details.append(
            f"{product}: {_qty_text(dispatched)} {unit} dispatched, "
            f"{_qty_text(received)} received, {_qty_text(accepted)} accepted; "
            + ", ".join(issues)
        )
        if len(details) >= max(int(max_lines or 6), 1):
            break
    return details


def receipt_issue_summary(receipt_lines, *, max_lines=6):
    """Return one concise seller-facing sentence for receipt issues."""
    details = receipt_issue_details(receipt_lines, max_lines=max_lines)
    if not details:
        return "No receipt discrepancy."
    extra_count = 0
    all_discrepant = 0
    for raw in receipt_lines or []:
        if not isinstance(raw, dict) or not raw.get("receipt_applicable"):
            continue
        discrepancy = (
            _decimal(raw.get("damaged_quantity"))
            + _decimal(raw.get("rejected_quantity"))
            + _decimal(raw.get("missing_quantity"))
        )
        if discrepancy > QTY_EPSILON:
            all_discrepant += 1
    extra_count = max(all_discrepant - len(details), 0)
    suffix = f"; +{extra_count} more discrepant line(s)" if extra_count else ""
    return "Receipt issue: " + "; ".join(details) + suffix + "."
