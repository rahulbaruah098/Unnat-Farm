from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from bson import ObjectId

from app.extensions import mongo
from app.utils.timezone import business_today, to_india_datetime


OPERATIONAL_REPORT_ROLES = {"ufc_admin", "ufc_mitra", "farmer"}
CENTRE_SECTIONS = [
    ("overview", "Overview"),
    ("farmers", "Farmers"),
    ("mitras", "Mitras"),
    ("revenue", "Revenue"),
    ("produce", "Produce"),
    ("purchases", "Purchases"),
    ("sales", "Sales"),
    ("stock", "Stock"),
    ("payments", "Payments"),
    ("orders", "Orders"),
]
FARMER_SECTIONS = [
    ("overview", "Overview"),
    ("production", "Production"),
    ("sales", "Sales"),
    ("purchases", "Purchases"),
    ("payments", "Payments"),
]
MITRA_SECTIONS = [
    ("overview", "Overview"),
    ("farmers", "Farmers"),
    ("business", "Business"),
    ("earnings", "Earnings"),
]

STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Approved",
    "dispatched": "Dispatched",
    "delivered": "Delivered",
    "received": "Received",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "rejected": "Rejected",
    "pending_confirmation": "Awaiting Confirmation",
    "processing": "Processing",
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
    "open": "Open",
    "closed": "Closed",
    "published": "Live",
    "paused": "Paused",
    "draft": "Draft",
}


def _dec(value: Any, default: str = "0") -> Decimal:
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _num(value: Any) -> float:
    return float(_dec(value))


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _clean(value) -> str:
    return str(value or "").strip()


def _date_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        converted = to_india_datetime(value)
        return converted.date() if converted else value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    # Business date strings are already local calendar dates.
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        converted = to_india_datetime(parsed)
        return converted.date() if converted else parsed.date()
    except (TypeError, ValueError):
        return None


def _row_date(row, *fields):
    for field in fields:
        value = (row or {}).get(field)
        parsed = _date_value(value)
        if parsed:
            return parsed
    return None


def _in_range(row, filters, *fields):
    value = _row_date(row, *fields)
    if not value:
        return True
    start = filters.get("from")
    end = filters.get("to")
    if start and value < start:
        return False
    if end and value > end:
        return False
    return True


def _first(row, *fields, default=None):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            return value
    return default


def _amount(row, *fields):
    return _num(_first(row, *fields, default=0))


def _status(value):
    raw = _clean(value).lower()
    return STATUS_LABELS.get(raw, raw.replace("_", " ").title() if raw else "-")


def _money(value):
    return round(_num(value), 2)


def _format_qty_number(value):
    number = _num(value)
    if abs(number - round(number)) < 0.000001:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _unit(value):
    return _clean(value).upper() or "UNIT"


def _quantity_summary(rows: Iterable[dict], quantity_field="quantity", unit_field="unit_code"):
    totals = defaultdict(float)
    for row in rows or []:
        qty = _amount(row, quantity_field)
        if qty <= 0:
            continue
        totals[_unit(row.get(unit_field))] += qty
    if not totals:
        return "0"
    return " · ".join(f"{_format_qty_number(qty)} {unit}" for unit, qty in sorted(totals.items(), key=lambda item: item[0]))


def _resolve_actor(user_id, role=None, centre_uid_hint="", mitra_uid_hint=""):
    actor_id = _oid(user_id)
    actor = mongo.db.users.find_one({"_id": actor_id}) if actor_id else None
    actor = actor or {}
    role = _clean(role or actor.get("role"))
    actor_str = str(actor_id or user_id or "")
    centre_uid = _clean(
        centre_uid_hint
        or actor.get("centre_uid")
        or actor.get("mapped_centre_uid")
        or actor.get("center_uid")
        or actor.get("mapped_center_uid")
    )
    mitra_uid = _clean(mitra_uid_hint or actor.get("mitra_uid") or actor.get("mapped_mitra_uid"))
    farmer = {}
    mitra = {}
    centre = {}

    if role == "ufc_admin":
        centre = mongo.db.ufc_admin_master.find_one({"$or": [
            {"linked_user_id": actor_id}, {"linked_user_id": actor_str},
        ]}) or {}
        centre_uid = _clean(centre_uid or centre.get("centre_uid") or centre.get("mapped_centre_uid"))
    elif role == "ufc_mitra":
        clauses = [{"mitra_uid": mitra_uid}] if mitra_uid else []
        if actor_id:
            clauses.extend([{"linked_user_id": actor_id}, {"linked_user_id": actor_str}])
        mitra = mongo.db.ufc_mitra_master.find_one({"$or": clauses}) if clauses else None
        mitra = mitra or {}
        mitra_uid = _clean(mitra_uid or mitra.get("mitra_uid"))
        centre_uid = _clean(centre_uid or mitra.get("mapped_centre_uid") or mitra.get("centre_uid"))
    elif role == "farmer":
        clauses = []
        if actor_id:
            clauses.extend([{"linked_user_id": actor_id}, {"linked_user_id": actor_str}])
        phone = _clean(actor.get("phone") or actor.get("contact_no"))
        if phone:
            clauses.append({"contact_no": phone})
        farmer = mongo.db.farmer_master.find_one({"$or": clauses}) if clauses else None
        farmer = farmer or {}
        centre_uid = _clean(centre_uid or farmer.get("centre_uid") or farmer.get("mapped_centre_uid"))
        mitra_uid = _clean(mitra_uid or farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid"))

    if centre_uid and not centre:
        centre = (
            mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid})
            or mongo.db.ufc_centre_master.find_one({"centre_uid": centre_uid})
            or {}
        )

    return {
        "role": role,
        "actor": actor,
        "user_id": actor_id,
        "user_id_str": actor_str,
        "centre_uid": centre_uid,
        "centre": centre,
        "centre_name": _clean(centre.get("name_of_enterprise") or centre.get("centre_name") or centre.get("name") or centre_uid or "UFC Centre"),
        "mitra_uid": mitra_uid,
        "mitra": mitra,
        "farmer": farmer,
    }


def _period_bounds(period: str, raw_from="", raw_to=""):
    today = business_today()
    period = _clean(period).lower() or "this_month"
    if period == "all":
        return None, None, "All Time"
    if period == "today":
        return today, today, "Today"
    if period == "yesterday":
        day = today - timedelta(days=1)
        return day, day, "Yesterday"
    if period == "7d":
        return today - timedelta(days=6), today, "Last 7 Days"
    if period == "30d":
        return today - timedelta(days=29), today, "Last 30 Days"
    if period == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        start = end.replace(day=1)
        return start, end, end.strftime("%B %Y")
    if period == "3m":
        first = today.replace(day=1)
        month_index = first.year * 12 + first.month - 1 - 2
        start = date(month_index // 12, month_index % 12 + 1, 1)
        return start, today, "Last 3 Months"
    if period == "financial_year":
        year = today.year if today.month >= 4 else today.year - 1
        return date(year, 4, 1), date(year + 1, 3, 31), f"FY {year}-{str(year + 1)[-2:]}"
    if period == "custom":
        start = _date_value(raw_from)
        end = _date_value(raw_to)
        if start and end and end < start:
            start, end = end, start
        if not start and not end:
            start = today.replace(day=1)
            end = today
        label = "Custom Period"
        if start and end:
            label = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
        elif start:
            label = f"From {start.strftime('%d %b %Y')}"
        elif end:
            label = f"Up to {end.strftime('%d %b %Y')}"
        return start, end, label
    start = today.replace(day=1)
    return start, today, today.strftime("%B %Y")


def parse_operational_filters(args):
    period = _clean(args.get("period") or "this_month").lower()
    start, end, label = _period_bounds(period, args.get("from"), args.get("to"))
    return {
        "period": period,
        "period_label": label,
        "from": start,
        "to": end,
        "from_text": start.isoformat() if start else "",
        "to_text": end.isoformat() if end else "",
        "farmer": _clean(args.get("farmer")),
        "mitra": _clean(args.get("mitra")),
        "product": _clean(args.get("product")),
        "status": _clean(args.get("status") or "all").lower(),
        "q": _clean(args.get("q")),
    }


def _mapped_farmers(scope):
    query = {}
    if scope["role"] == "ufc_admin":
        query = {"centre_uid": scope.get("centre_uid") or "__NO_CENTRE__"}
    elif scope["role"] == "ufc_mitra":
        query = {"mitra_uid": scope.get("mitra_uid") or "__NO_MITRA__"}
    elif scope["role"] == "farmer":
        farmer = scope.get("farmer") or {}
        return [farmer] if farmer else []
    return list(mongo.db.farmer_master.find(query).sort("name", 1))


def _mapped_mitras(scope):
    if scope["role"] == "ufc_mitra":
        return [scope.get("mitra") or {}] if scope.get("mitra") else []
    if scope["role"] != "ufc_admin":
        return []
    centre_uid = scope.get("centre_uid") or "__NO_CENTRE__"
    return list(mongo.db.ufc_mitra_master.find({"$or": [
        {"mapped_centre_uid": centre_uid}, {"centre_uid": centre_uid},
    ]}).sort("name", 1))


def _farmer_user_id(farmer):
    return farmer.get("linked_user_id") or farmer.get("user_id")


def _farmer_key(farmer):
    value = _farmer_user_id(farmer)
    return str(value or farmer.get("_id") or "")


def _farmer_name(farmer):
    return _clean(farmer.get("name") or farmer.get("farmer_name") or "Farmer")


def _selected_farmers(scope, filters):
    farmers = _mapped_farmers(scope)
    selected = filters.get("farmer")
    if selected:
        result = []
        for farmer in farmers:
            candidates = {
                str(farmer.get("_id") or ""),
                str(farmer.get("linked_user_id") or ""),
                _clean(farmer.get("contact_no")),
            }
            if selected in candidates:
                result.append(farmer)
        farmers = result
    term = _clean(filters.get("q")).lower()
    if term:
        matched = []
        for farmer in farmers:
            searchable = " ".join([
                _farmer_name(farmer), _clean(farmer.get("farmer_uid")),
                _clean(farmer.get("contact_no")), _clean(farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid")),
                " ".join(farmer.get("activities") or []),
            ]).lower()
            if term in searchable:
                matched.append(farmer)
        # A search may be an order/payment reference instead of a Farmer name.
        # Keep the full mapped Farmer scope when no Farmer itself matches; the
        # transaction-level search below will then resolve the reference safely.
        if matched or filters.get("_farmer_search_only"):
            farmers = matched
    return farmers


def _farmer_id_sets(farmers):
    object_ids = set()
    strings = set()
    master_ids = set()
    for farmer in farmers or []:
        uid = _farmer_user_id(farmer)
        oid = _oid(uid)
        if oid:
            object_ids.add(oid)
        if uid not in (None, ""):
            strings.add(str(uid))
        fid = farmer.get("_id")
        if fid:
            master_ids.add(str(fid))
    return object_ids, strings, master_ids


def _row_matches_farmer(row, object_ids, strings, fields):
    if not object_ids and not strings:
        return False
    for field in fields:
        value = row.get(field)
        if value in object_ids or str(value or "") in strings:
            return True
    seller = row.get("seller") or {}
    buyer = row.get("buyer") or {}
    for nested in (seller, buyer):
        for key in ("farmer_user_id", "farmer_user_id_str", "user_id"):
            value = nested.get(key)
            if value in object_ids or str(value or "") in strings:
                return True
    return False


def _product_match(name, selected):
    if not selected:
        return True
    return _clean(name).lower() == selected.lower()


def _row_items(row):
    """Return commerce lines enriched with buyer-receipt facts when present."""
    row = row or {}
    items = [dict(item or {}) for item in (row.get("items") or []) if isinstance(item, dict)]
    receipt_map = {
        str(line.get("line_id") or "legacy"): dict(line)
        for line in (row.get("receipt_lines") or [])
        if isinstance(line, dict)
    }
    if receipt_map:
        for item in items:
            receipt = receipt_map.get(str(item.get("line_id") or "legacy"))
            if not receipt:
                continue
            for key in (
                "received_quantity", "accepted_quantity", "damaged_quantity",
                "rejected_quantity", "missing_quantity", "accepted_commercial_total",
            ):
                if receipt.get(key) is not None:
                    item[key] = receipt.get(key)
    return items


def _matching_items(row, selected_product):
    items = _row_items(row)
    if not selected_product:
        return items
    return [item for item in items if _product_match(item.get("product_name"), selected_product)]


def _row_product_matches(row, selected_product):
    if not selected_product:
        return True
    if _product_match((row or {}).get("product_name"), selected_product):
        return True
    return bool(_matching_items(row, selected_product))


def _row_product_summary(row, selected_product=None):
    items = _matching_items(row, selected_product) if selected_product else _row_items(row)
    if items:
        names=[]
        for item in items:
            name=_clean(item.get("product_name")) or "Product"
            if name not in names:
                names.append(name)
        if len(names)==1:
            return names[0]
        return f"{len(names)} products"
    return _clean((row or {}).get("product_name")) or "Product"


def _row_search_match(row, q, fields):
    if _search_match(row, q, fields):
        return True
    term=_clean(q).lower()
    if not term:
        return True
    for item in _row_items(row):
        if term in " ".join(str(item.get(k) or "") for k in ("product_name","product_code","listing_number","line_id")).lower():
            return True
    return False


def _commercial_total(row, *total_fields):
    if (row or {}).get("settlement_total") not in (None, ""):
        return _amount(row, "settlement_total")
    if (row or {}).get("accepted_goods_total") not in (None, ""):
        return _amount(row, "accepted_goods_total")
    return _amount(row, *total_fields)


def _scoped_transaction_value(row, selected_product, *total_fields):
    items=_matching_items(row, selected_product) if selected_product else []
    if selected_product and items:
        return round(sum(_amount(item, "accepted_commercial_total", "line_total", "grand_total", "total_amount", "taxable_value") for item in items), 2)
    return _commercial_total(row, *total_fields)


def _scoped_transaction_qty(row, selected_product=None):
    items=_matching_items(row, selected_product) if selected_product else _row_items(row)
    if items:
        units={_unit(item.get("unit_code")) for item in items}
        if len(items)==1:
            item=items[0]
            return _amount(item, "accepted_quantity", "quantity", "received_quantity", "base_quantity", "dispatched_quantity", "approved_quantity", "requested_quantity"), _unit(item.get("unit_code"))
        if len(units)==1:
            return sum(_amount(item, "accepted_quantity", "quantity", "received_quantity", "base_quantity", "dispatched_quantity", "approved_quantity", "requested_quantity") for item in items), next(iter(units))
        return 0.0, "MULTI"
    return _amount(row, "accepted_quantity", "quantity", "received_quantity", "base_quantity", "dispatched_quantity", "approved_quantity", "requested_quantity"), _unit(row.get("unit_code"))


def _scoped_settlement_values(row, selected_product, *total_fields):
    full_total = _commercial_total(row, *total_fields)
    scoped_total = _scoped_transaction_value(row, selected_product, *total_fields)
    paid, outstanding = _settlement_values(row, full_total)
    if selected_product and full_total > 0 and scoped_total < full_total:
        ratio = max(min(scoped_total / full_total, 1.0), 0.0)
        return round(scoped_total, 2), round(paid * ratio, 2), round(outstanding * ratio, 2)
    return round(scoped_total, 2), paid, outstanding


def _search_match(row, q, fields):
    term = _clean(q).lower()
    if not term:
        return True
    values = []
    for field in fields:
        value = row.get(field)
        if isinstance(value, dict):
            values.extend(str(x) for x in value.values())
        else:
            values.append(str(value or ""))
    return term in " ".join(values).lower()


def _filter_status(row, filters):
    selected = filters.get("status") or "all"
    return selected == "all" or _clean(row.get("status")).lower() == selected or _clean(row.get("payment_status")).lower() == selected


def _payment_product_matches(payment, selected_product):
    if not selected_product:
        return True
    collection = _clean(payment.get("invoice_collection"))
    invoice_id = payment.get("invoice_id") or _oid(payment.get("invoice_id_str"))
    if collection and invoice_id:
        invoice = mongo.db[collection].find_one({"_id": invoice_id}, {"product_name": 1, "items": 1}) or {}
        if _product_match(invoice.get("product_name"), selected_product):
            return True
        if any(_product_match(item.get("product_name"), selected_product) for item in (invoice.get("items") or [])):
            return True
    # Never guess from free-text labels when the source document is unavailable.
    return False


def _payment_scope_rows(scope, farmers, filters):
    object_ids, strings, _ = _farmer_id_sets(farmers)
    centre_uid = scope.get("centre_uid") or ""
    mitra_uid = scope.get("mitra_uid") or ""
    rows = []
    for row in mongo.db.payments.find({"status": {"$ne": "reversed"}}).sort("created_at", -1):
        payer = str(row.get("payer_key") or "")
        payee = str(row.get("payee_key") or "")
        include = False
        if scope["role"] == "ufc_admin":
            centre_involved = centre_uid in {payer, payee, str(row.get("centre_uid") or "")}
            # When a Farmer/Mitra filter is active, keep only payments that involve
            # both this UFC and one of the selected Farmers. This prevents AVPL or
            # unrelated centre payments from leaking into Farmer-wise KPIs/exports.
            if filters.get("farmer") or filters.get("mitra"):
                farmer_involved = payer in strings or payee in strings
                include = centre_involved and farmer_involved
            else:
                include = centre_involved
        elif scope["role"] == "farmer":
            actor_key = scope.get("user_id_str") or ""
            include = actor_key in {payer, payee}
        elif scope["role"] == "ufc_mitra":
            # Mitra earnings are not settled through the unified payment engine today.
            # Payments are only included when they directly involve a mapped Farmer.
            include = payer in strings or payee in strings
        if not include:
            continue
        if not _in_range(row, filters, "payment_date", "confirmed_at", "completed_at", "created_at"):
            continue
        if not _filter_status(row, filters):
            continue
        if not _payment_product_matches(row, filters.get("product")):
            continue
        if not _search_match(row, filters.get("q"), ("payment_number", "invoice_number", "payer_name", "payee_name", "source_label", "reference")):
            continue
        rows.append(row)
    return rows


def _load_centre_data(scope, filters):
    centre_uid = scope.get("centre_uid") or "__NO_CENTRE__"
    farmers = _selected_farmers(scope, filters)
    all_farmers = _mapped_farmers(scope)
    mitras = _mapped_mitras(scope)
    object_ids, strings, _ = _farmer_id_sets(farmers)
    all_object_ids, all_strings, _ = _farmer_id_sets(all_farmers)
    selected_mitra = filters.get("mitra")
    if selected_mitra:
        farmers = [f for f in farmers if _clean(f.get("mitra_uid") or f.get("mapped_mitra_uid")) == selected_mitra]
        object_ids, strings, _ = _farmer_id_sets(farmers)

    data = {
        "farmers": farmers,
        "all_farmers": all_farmers,
        "mitras": [m for m in mitras if not selected_mitra or _clean(m.get("mitra_uid")) == selected_mitra],
        "all_mitras": mitras,
        "farmer_object_ids": object_ids,
        "farmer_strings": strings,
        "all_farmer_object_ids": all_object_ids,
        "all_farmer_strings": all_strings,
    }

    # Centre operational streams.
    data["ufc_orders"] = [row for row in mongo.db.ufc_farmer_orders.find({"centre_uid": centre_uid})
        if _in_range(row, filters, "delivered_at", "approved_at", "created_at")
        and _filter_status(row, filters)
        and (not strings or _row_matches_farmer(row, object_ids, strings, ("farmer_user_id", "farmer_user_id_str")))
        and _row_product_matches(row, filters.get("product"))
        and _row_search_match(row, filters.get("q"), ("order_number", "farmer_name", "product_name", "status"))]

    ufc_sales = []
    for row in mongo.db.ufc_farmer_sales.find({"centre_uid": centre_uid, "status": {"$ne": "voided"}}):
        if not _in_range(row, filters, "sale_date", "created_at"):
            continue
        if strings and not _row_matches_farmer(row, object_ids, strings, ("farmer_user_id", "farmer_user_id_str")):
            continue
        if not _row_product_matches(row, filters.get("product")):
            continue
        matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
        if matches:
            row = {**row, "_report_items": matches}
        ufc_sales.append(row)
    data["ufc_sales"] = ufc_sales

    pos_rows = []
    for sale in mongo.db.pos_sales.find({"centre_uid": centre_uid, "status": "completed", "$or": [{"seller_type": "ufc"}, {"seller_type": {"$exists": False}}]}):
        if not _in_range(sale, filters, "sale_date", "created_at"):
            continue
        if selected_mitra and _clean(sale.get("mitra_uid")) != selected_mitra:
            continue
        # Farmer filter only applies to POS rows when the buyer is the selected registered Farmer.
        if filters.get("farmer"):
            buyer = sale.get("buyer") or {}
            buyer_uid = str(buyer.get("farmer_user_id") or buyer.get("farmer_user_id_str") or sale.get("farmer_user_id") or "")
            if buyer_uid not in strings:
                continue
        if filters.get("product"):
            matching_items = [item for item in (sale.get("items") or []) if _product_match(item.get("product_name"), filters.get("product"))]
            if not matching_items and not _product_match(sale.get("product_name"), filters.get("product")):
                continue
            if matching_items:
                sale = {**sale, "_report_items": matching_items}
        pos_rows.append(sale)
    data["pos_sales"] = pos_rows

    data["avpl_purchases"] = []
    if not (filters.get("farmer") or selected_mitra):
        for row in mongo.db.ufc_purchase_entries.find({"centre_uid": centre_uid, "status": {"$ne": "voided"}}):
            if not _in_range(row, filters, "purchase_date", "created_at") or not _row_product_matches(row, filters.get("product")):
                continue
            matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
            if matches:
                row = {**row, "_report_items": matches}
            data["avpl_purchases"].append(row)

    produce_purchases = []
    for row in mongo.db.farmer_marketplace_purchase_entries.find({"buyer_type": "ufc", "buyer_key": centre_uid, "status": {"$ne": "voided"}}):
        if not _in_range(row, filters, "received_at", "created_at"):
            continue
        if filters.get("farmer") and not _row_matches_farmer(row, object_ids, strings, ("seller_farmer_user_id", "seller_farmer_user_id_str")):
            continue
        if selected_mitra:
            seller_id = str(row.get("seller_farmer_user_id") or "")
            farmer = next((f for f in all_farmers if str(_farmer_user_id(f) or "") == seller_id), None)
            if not farmer or _clean(farmer.get("mitra_uid")) != selected_mitra:
                continue
        if not _row_product_matches(row, filters.get("product")):
            continue
        matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
        if matches:
            row = {**row, "_report_items": matches}
        produce_purchases.append(row)
    data["produce_purchases"] = produce_purchases

    # Farmer-side activity for mapped Farmers.
    productions = []
    listings = []
    market_sales = []
    external_sales = []
    farmer_pos_sales = []
    if strings or object_ids:
        owner_values = list(object_ids) + list(strings)
        for row in mongo.db.farmer_production_entries.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": {"$in": list(strings)}}], "status": {"$ne": "voided"}}):
            if _in_range(row, filters, "harvest_date", "created_at") and _product_match(row.get("product_name"), filters.get("product")):
                productions.append(row)
        for row in mongo.db.farmer_produce_marketplace_listings.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": {"$in": list(strings)}}]}):
            if _in_range(row, filters, "published_at", "created_at") and _product_match(row.get("product_name"), filters.get("product")):
                listings.append(row)
        for row in mongo.db.farmer_marketplace_sales.find({"status": "completed"}):
            if not _row_matches_farmer(row, object_ids, strings, ("seller_farmer_user_id", "seller_farmer_user_id_str", "farmer_user_id", "farmer_user_id_str")):
                continue
            if _in_range(row, filters, "sale_date", "created_at") and _row_product_matches(row, filters.get("product")):
                matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
                if matches:
                    row = {**row, "_report_items": matches}
                market_sales.append(row)
        for row in mongo.db.farmer_external_sales.find({"status": "completed"}):
            if not _row_matches_farmer(row, object_ids, strings, ("farmer_user_id", "farmer_user_id_str")):
                continue
            if _in_range(row, filters, "sale_date", "created_at") and _product_match(row.get("product_name"), filters.get("product")):
                external_sales.append(row)
        for sale in mongo.db.pos_sales.find({"seller_type": "farmer", "status": "completed"}):
            if not _row_matches_farmer(sale, object_ids, strings, ("seller_user_id", "seller_user_id_str", "farmer_user_id", "farmer_user_id_str")):
                continue
            if not _in_range(sale, filters, "sale_date", "created_at"):
                continue
            if filters.get("product"):
                matching_items = [item for item in (sale.get("items") or []) if _product_match(item.get("product_name"), filters.get("product"))]
                if not matching_items and not _product_match(sale.get("product_name"), filters.get("product")):
                    continue
                if matching_items:
                    sale = {**sale, "_report_items": matching_items}
            farmer_pos_sales.append(sale)
    data["productions"] = productions
    data["listings"] = listings
    data["market_sales"] = market_sales
    data["external_sales"] = external_sales
    data["farmer_pos_sales"] = farmer_pos_sales

    data["payments"] = _payment_scope_rows(scope, farmers, filters)
    return data


def _load_farmer_data(scope, filters):
    farmer = scope.get("farmer") or {}
    uid = _farmer_user_id(farmer) or scope.get("user_id")
    oid = _oid(uid)
    uid_str = str(uid or scope.get("user_id_str") or "")
    object_ids = {oid} if oid else set()
    strings = {uid_str} if uid_str else set()
    owner_values = list(object_ids) + list(strings)
    data = {"farmer": farmer, "farmer_object_ids": object_ids, "farmer_strings": strings}

    data["productions"] = [row for row in mongo.db.farmer_production_entries.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": uid_str}], "status": {"$ne": "voided"}})
        if _in_range(row, filters, "harvest_date", "created_at") and _row_product_matches(row, filters.get("product"))]
    data["listings"] = [row for row in mongo.db.farmer_produce_marketplace_listings.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": uid_str}]})
        if _in_range(row, filters, "published_at", "created_at") and _row_product_matches(row, filters.get("product"))]
    data["market_sales"] = []
    for row in mongo.db.farmer_marketplace_sales.find({"status": "completed"}):
        if not _row_matches_farmer(row, object_ids, strings, ("seller_farmer_user_id", "seller_farmer_user_id_str", "farmer_user_id", "farmer_user_id_str")):
            continue
        if not _in_range(row, filters, "sale_date", "created_at") or not _row_product_matches(row, filters.get("product")):
            continue
        matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
        if matches:
            row = {**row, "_report_items": matches}
        data["market_sales"].append(row)
    data["external_sales"] = [row for row in mongo.db.farmer_external_sales.find({"status": "completed"})
        if _row_matches_farmer(row, object_ids, strings, ("farmer_user_id", "farmer_user_id_str"))
        and _in_range(row, filters, "sale_date", "created_at") and _row_product_matches(row, filters.get("product"))]
    pos_sales = []
    for sale in mongo.db.pos_sales.find({"seller_type": "farmer", "status": "completed"}):
        if not _row_matches_farmer(sale, object_ids, strings, ("seller_user_id", "seller_user_id_str", "farmer_user_id", "farmer_user_id_str")):
            continue
        if not _in_range(sale, filters, "sale_date", "created_at"):
            continue
        if filters.get("product"):
            items = [item for item in (sale.get("items") or []) if _product_match(item.get("product_name"), filters.get("product"))]
            if not items and not _product_match(sale.get("product_name"), filters.get("product")):
                continue
            if items:
                sale = {**sale, "_report_items": items}
        pos_sales.append(sale)
    data["pos_sales"] = pos_sales
    data["purchases"] = []
    for row in mongo.db.farmer_purchase_entries.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": uid_str}], "status": {"$ne": "voided"}}):
        if not _in_range(row, filters, "purchase_date", "created_at") or not _row_product_matches(row, filters.get("product")):
            continue
        matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
        if matches:
            row = {**row, "_report_items": matches}
        data["purchases"].append(row)
    data["market_purchases"] = []
    for row in mongo.db.farmer_marketplace_purchase_entries.find({"buyer_type": "farmer", "buyer_key": uid_str, "status": {"$ne": "voided"}}):
        if not _in_range(row, filters, "received_at", "created_at") or not _row_product_matches(row, filters.get("product")):
            continue
        matches = _matching_items(row, filters.get("product")) if filters.get("product") else []
        if matches:
            row = {**row, "_report_items": matches}
        data["market_purchases"].append(row)
    data["payments"] = _payment_scope_rows(scope, [farmer], filters)
    data["stock"] = list(mongo.db.farmer_produce_lots.find({"$or": [{"farmer_user_id": {"$in": owner_values}}, {"farmer_user_id_str": uid_str}], "status": "active"}))
    return data


def _settlement_values(row, total):
    """Return a self-consistent confirmed paid/outstanding pair.

    Operational purchase/sale rows are linked mirrors of the payment invoice and
    older records can contain a stale ``outstanding_amount`` even after the
    confirmed ``amount_paid`` was synchronized.  Reports must never present an
    impossible state such as "Paid = total" together with a positive due.

    This intentionally follows the payment engine's reconciliation rule:
    confirmed paid is capped at the document total and outstanding can never be
    greater than ``total - paid``.
    """
    total = max(_num(total), 0.0)
    paid_raw = _first(row, "amount_paid", "paid_amount", default=None)
    outstanding_raw = row.get("outstanding_amount")
    if paid_raw is None and outstanding_raw is None:
        return 0.0, round(total, 2)

    paid = max(_num(paid_raw), 0.0) if paid_raw is not None else max(total - _num(outstanding_raw), 0.0)
    paid = min(paid, total) if total else paid

    outstanding = max(_num(outstanding_raw), 0.0) if outstanding_raw is not None else max(total - paid, 0.0)
    if total:
        outstanding = min(outstanding, max(total - paid, 0.0))

    return round(paid, 2), round(outstanding, 2)


def _pos_value(sale, key="line_total"):
    items = sale.get("_report_items") or sale.get("items") or []
    if sale.get("_report_items"):
        return sum(_amount(item, key, "line_total", "total_amount") for item in items)
    return _amount(sale, "grand_total", "total_amount", "amount")


def _pos_cogs(sale):
    if sale.get("_report_items"):
        return sum(_amount(item, "cogs") for item in sale.get("_report_items") or [])
    return _amount(sale, "cogs")


def _sale_rows_for_farmers(data):
    rows = []
    for sale in data.get("market_sales", []):
        items = sale.get("_report_items") or sale.get("items") or []
        sale_total = _amount(sale, "total_amount", "grand_total") or sum(_amount(item, "line_total", "total_amount") for item in items)
        received, outstanding = _settlement_values(sale, sale_total)
        if items:
            for item in items:
                line_value = _amount(item, "line_total", "total_amount")
                ratio = (line_value / sale_total) if sale_total > 0 else 0
                rows.append({
                    "source": "Marketplace",
                    "farmer_user_id": sale.get("seller_farmer_user_id") or sale.get("farmer_user_id"),
                    "farmer_name": sale.get("seller_farmer_name") or sale.get("farmer_name") or "Farmer",
                    "product_name": item.get("product_name") or "Produce",
                    "quantity": _amount(item, "quantity", "base_quantity"),
                    "unit_code": _unit(item.get("unit_code")),
                    "sales_value": line_value,
                    "received": round(received * ratio, 2),
                    "outstanding": round(outstanding * ratio, 2),
                    "date": _row_date(sale, "sale_date", "created_at"),
                    "reference": sale.get("sale_number") or sale.get("order_number") or "",
                })
        else:
            rows.append({
                "source": "Marketplace",
                "farmer_user_id": sale.get("seller_farmer_user_id") or sale.get("farmer_user_id"),
                "farmer_name": sale.get("seller_farmer_name") or sale.get("farmer_name") or "Farmer",
                "product_name": sale.get("product_name") or "Produce",
                "quantity": _amount(sale, "quantity", "base_quantity"),
                "unit_code": _unit(sale.get("unit_code")),
                "sales_value": sale_total,
                "received": received,
                "outstanding": outstanding,
                "date": _row_date(sale, "sale_date", "created_at"),
                "reference": sale.get("sale_number") or sale.get("order_number") or "",
            })
    for sale in data.get("external_sales", []):
        sale_value = _amount(sale, "grand_total", "total_amount")
        received, outstanding = _settlement_values(sale, sale_value)
        rows.append({
            "source": "Direct Sale",
            "farmer_user_id": sale.get("farmer_user_id"),
            "farmer_name": sale.get("farmer_name") or "Farmer",
            "product_name": sale.get("product_name") or "Produce",
            "quantity": _amount(sale, "quantity"),
            "unit_code": _unit(sale.get("unit_code")),
            "sales_value": sale_value,
            "received": received,
            "outstanding": outstanding,
            "date": _row_date(sale, "sale_date", "created_at"),
            "reference": sale.get("sale_number") or "",
        })
    for sale in data.get("farmer_pos_sales", data.get("pos_sales", [])):
        seller_uid = sale.get("seller_user_id") or sale.get("farmer_user_id") or (sale.get("seller") or {}).get("farmer_user_id")
        seller_name = sale.get("seller_name") or sale.get("farmer_name") or (sale.get("seller") or {}).get("name") or "Farmer"
        items = sale.get("_report_items") or sale.get("items") or []
        if items:
            sale_total = _amount(sale, "grand_total", "total_amount") or sum(_amount(item, "line_total", "total_amount") for item in items)
            sale_received, sale_outstanding = _settlement_values(sale, sale_total)
            for item in items:
                line_value = _amount(item, "line_total", "total_amount")
                ratio = (line_value / sale_total) if sale_total > 0 else 0
                rows.append({
                    "source": "POS",
                    "farmer_user_id": seller_uid,
                    "farmer_name": seller_name,
                    "product_name": item.get("product_name") or "Produce",
                    "quantity": _amount(item, "quantity"),
                    "unit_code": _unit(item.get("unit_code")),
                    "sales_value": line_value,
                    "received": round(sale_received * ratio, 2),
                    "outstanding": round(sale_outstanding * ratio, 2),
                    "date": _row_date(sale, "sale_date", "created_at"),
                    "reference": sale.get("sale_number") or "",
                })
        else:
            sale_value = _amount(sale, "grand_total", "total_amount")
            received, outstanding = _settlement_values(sale, sale_value)
            rows.append({
                "source": "POS",
                "farmer_user_id": seller_uid,
                "farmer_name": seller_name,
                "product_name": sale.get("product_name") or "Produce",
                "quantity": _amount(sale, "quantity"),
                "unit_code": _unit(sale.get("unit_code")),
                "sales_value": sale_value,
                "received": received,
                "outstanding": outstanding,
                "date": _row_date(sale, "sale_date", "created_at"),
                "reference": sale.get("sale_number") or "",
            })
    return rows


def _period_bucket(value, start, end):
    if not value:
        return None
    span = (end - start).days if start and end else 365
    if span <= 31:
        return value.isoformat(), value.strftime("%d %b")
    return value.strftime("%Y-%m"), value.strftime("%b %Y")


def _trend(rows, value_field, date_field="date", start=None, end=None, label="Value"):
    grouped = defaultdict(float)
    labels = {}
    for row in rows:
        d = row.get(date_field)
        if not isinstance(d, date):
            d = _date_value(d)
        if not d:
            continue
        bucket = _period_bucket(d, start, end)
        if not bucket:
            continue
        key, display = bucket
        grouped[key] += _num(row.get(value_field))
        labels[key] = display
    points = [{"label": labels[key], "value": round(grouped[key], 2)} for key in sorted(grouped)]
    maximum = max([p["value"] for p in points] or [0])
    for point in points:
        point["height"] = max(8, round((point["value"] / maximum * 100), 1)) if maximum > 0 else 8
    return {"title": label, "points": points}


def _payment_received_for_farmers(payments, farmer_strings):
    totals = defaultdict(float)
    for row in payments:
        if row.get("status") != "completed":
            continue
        payee = str(row.get("payee_key") or "")
        if payee in farmer_strings:
            totals[payee] += _amount(row, "amount")
    return totals


def _active_farmer_ids(data):
    ids = set()
    for key, fields in [
        ("productions", ("farmer_user_id", "farmer_user_id_str")),
        ("market_sales", ("seller_farmer_user_id", "seller_farmer_user_id_str", "farmer_user_id")),
        ("external_sales", ("farmer_user_id", "farmer_user_id_str")),
        ("ufc_orders", ("farmer_user_id", "farmer_user_id_str")),
        ("produce_purchases", ("seller_farmer_user_id", "seller_farmer_user_id_str")),
    ]:
        for row in data.get(key, []):
            for field in fields:
                if row.get(field):
                    ids.add(str(row.get(field)))
                    break
    return ids


def _ufc_order_cogs_by_line(sale):
    order_id = sale.get("ufc_farmer_order_id")
    if not order_id:
        return {}, 0.0
    order = mongo.db.ufc_farmer_orders.find_one({"_id": order_id}, {"reservation_allocations": 1}) or {}
    by_line = defaultdict(float)
    total = 0.0
    for allocation in order.get("reservation_allocations") or []:
        lot_id = allocation.get("inventory_lot_id")
        lot = mongo.db.ufc_inventory_lots.find_one({"_id": lot_id}, {"unit_cost": 1, "purchase_unit_cost": 1, "purchase_cost_total": 1, "received_quantity": 1}) if lot_id else None
        lot = lot or {}
        unit_cost = _amount(lot, "unit_cost", "purchase_unit_cost")
        if unit_cost <= 0 and _amount(lot, "received_quantity") > 0:
            unit_cost = _amount(lot, "purchase_cost_total") / max(_amount(lot, "received_quantity"), 0.000001)
        value = _amount(allocation, "quantity") * unit_cost
        line_id = str(allocation.get("line_id") or "legacy")
        by_line[line_id] += value
        total += value
    return {k: round(v, 2) for k, v in by_line.items()}, round(total, 2)


def _ufc_order_cogs(sale):
    return _ufc_order_cogs_by_line(sale)[1]


def _centre_ufc_sales_rows(data):
    rows = []
    for sale in data.get("ufc_sales", []):
        items = sale.get("_report_items") or sale.get("items") or []
        sale_total = _amount(sale, "grand_total", "total_amount") or sum(_amount(item, "line_total", "grand_total", "total_amount") for item in items)
        received, outstanding = _settlement_values(sale, sale_total)
        cogs_by_line, total_cogs = _ufc_order_cogs_by_line(sale)
        if items:
            for item in items:
                value = _amount(item, "line_total", "grand_total", "total_amount")
                ratio = (value / sale_total) if sale_total > 0 else 0
                line_id = str(item.get("line_id") or "legacy")
                cogs = cogs_by_line.get(line_id, 0.0)
                rows.append({
                    "channel": "Farmer Order",
                    "reference": sale.get("sale_number") or sale.get("order_number") or "",
                    "customer": sale.get("farmer_name") or "Farmer",
                    "product": item.get("product_name") or "Product",
                    "quantity": _amount(item, "quantity", "delivered_quantity", "approved_quantity"),
                    "unit": _unit(item.get("unit_code")),
                    "sales_value": value,
                    "received": round(received * ratio, 2),
                    "outstanding": round(outstanding * ratio, 2),
                    "cogs": cogs,
                    "margin": round(value - cogs, 2),
                    "date": _row_date(sale, "sale_date", "created_at"),
                })
        else:
            rows.append({
                "channel": "Farmer Order",
                "reference": sale.get("sale_number") or sale.get("order_number") or "",
                "customer": sale.get("farmer_name") or "Farmer",
                "product": sale.get("product_name") or "Product",
                "quantity": _amount(sale, "quantity"),
                "unit": _unit(sale.get("unit_code")),
                "sales_value": sale_total,
                "received": received,
                "outstanding": outstanding,
                "cogs": total_cogs,
                "margin": round(sale_total - total_cogs, 2),
                "date": _row_date(sale, "sale_date", "created_at"),
            })
    for sale in data.get("pos_sales", []):
        items = sale.get("_report_items") or sale.get("items") or []
        buyer = (sale.get("buyer") or {}).get("name") or sale.get("farmer_name") or "Customer"
        if items:
            sale_total = _amount(sale, "grand_total", "total_amount") or sum(_amount(item, "line_total", "total_amount") for item in items)
            sale_received, sale_outstanding = _settlement_values(sale, sale_total)
            for item in items:
                value = _amount(item, "line_total", "total_amount")
                ratio = (value / sale_total) if sale_total > 0 else 0
                cogs = _amount(item, "cogs")
                rows.append({
                    "channel": "POS",
                    "reference": sale.get("sale_number") or "",
                    "customer": buyer,
                    "product": item.get("product_name") or "Product",
                    "quantity": _amount(item, "quantity"),
                    "unit": _unit(item.get("unit_code")),
                    "sales_value": value,
                    "received": round(sale_received * ratio, 2),
                    "outstanding": round(sale_outstanding * ratio, 2),
                    "cogs": cogs,
                    "margin": round(value - cogs, 2),
                    "date": _row_date(sale, "sale_date", "created_at"),
                })
        else:
            value = _amount(sale, "grand_total", "total_amount")
            received, outstanding = _settlement_values(sale, value)
            cogs = _amount(sale, "cogs")
            rows.append({
                "channel": "POS",
                "reference": sale.get("sale_number") or "",
                "customer": buyer,
                "product": sale.get("product_name") or "Product",
                "quantity": _amount(sale, "quantity"),
                "unit": _unit(sale.get("unit_code")),
                "sales_value": value,
                "received": received,
                "outstanding": outstanding,
                "cogs": cogs,
                "margin": round(value - cogs, 2),
                "date": _row_date(sale, "sale_date", "created_at"),
            })
    return rows


def _mitra_earning_rows(scope, filters, mitras=None, farmers=None):
    if mitras is None:
        mitras = _mapped_mitras(scope)
    if farmers is None:
        farmers = _selected_farmers(scope, filters)
    farmer_object_ids, farmer_strings, _ = _farmer_id_sets(farmers)
    selected = filters.get("mitra")
    if scope["role"] == "ufc_mitra":
        selected = scope.get("mitra_uid")
    allowed = {_clean(m.get("mitra_uid")) for m in mitras if _clean(m.get("mitra_uid"))}
    if selected:
        allowed = {selected} if selected in allowed or scope["role"] == "ufc_mitra" else set()
    name_map = {_clean(m.get("mitra_uid")): _clean(m.get("name") or m.get("mitra_name") or m.get("mitra_uid")) for m in mitras}
    grouped = {}

    for sale in mongo.db.pos_sales.find({"status": "completed", "mitra_uid": {"$in": list(allowed)}} if allowed else {"_id": None}):
        buyer = sale.get("buyer") or {}
        if _clean(sale.get("buyer_type") or buyer.get("type")).lower() != "registered_farmer" and _clean(sale.get("sale_type")).lower() != "registered":
            continue
        if not _in_range(sale, filters, "sale_date", "created_at"):
            continue
        items = [item for item in (sale.get("items") or []) if isinstance(item, dict)]
        eligible_items = [item for item in items if _clean(item.get("bonus_type")) == "avpl_product_sale" or item.get("bonus_snapshot_version")]
        business_value = _amount(sale, "bonus_base_total") or (sum(_amount(item, "bonus_basis_amount", "line_total") for item in eligible_items) if eligible_items else _amount(sale, "grand_total", "total_amount"))
        bonus = _amount(sale, "bonus_amount")
        if filters.get("product"):
            items = sale.get("items") or []
            matching = [item for item in items if _product_match(item.get("product_name"), filters.get("product"))]
            if matching:
                full_value = business_value or sum(_amount(item, "line_total", "total_amount") for item in items)
                matched_value = sum(_amount(item, "line_total", "total_amount") for item in matching)
                ratio = (matched_value / full_value) if full_value > 0 else 0
                business_value = matched_value
                bonus = round(bonus * ratio, 2)
            elif not _product_match(sale.get("product_name"), filters.get("product")):
                continue
        if filters.get("farmer"):
            buyer = sale.get("buyer") or {}
            buyer_uid = str(buyer.get("farmer_user_id") or buyer.get("farmer_user_id_str") or sale.get("farmer_user_id") or sale.get("buyer_key") or "")
            if buyer_uid not in farmer_strings:
                continue
        uid = _clean(sale.get("mitra_uid"))
        row = grouped.setdefault(uid, {"mitra_uid": uid, "mitra_name": name_map.get(uid, uid), "transactions": 0, "business_value": 0.0, "earnings": 0.0, "avpl_earnings": 0.0, "farmer_earnings": 0.0})
        row["transactions"] += 1
        row["business_value"] += business_value
        row["earnings"] += bonus
        row["avpl_earnings"] += bonus

    for sale in mongo.db.ufc_farmer_sales.find({"mitra_uid": {"$in": list(allowed)}, "bonus_snapshot_version": {"$exists": True}, "bonus_financial_sync_status": "complete"} if allowed else {"_id": None}):
        if not _in_range(sale, filters, "sale_date", "created_at"):
            continue
        uid = _clean(sale.get("mitra_uid"))
        items = [item for item in (sale.get("items") or []) if isinstance(item, dict) and item.get("bonus_snapshot_version")]
        business_value = sum(_amount(item, "bonus_basis_amount", "line_total") for item in items) if items else _amount(sale, "bonus_base_total", "grand_total")
        bonus = sum(_amount(item, "bonus_amount") for item in items) if items else _amount(sale, "bonus_amount")
        if filters.get("product"):
            matching = [item for item in items if _product_match(item.get("product_name"), filters.get("product"))]
            if items and matching:
                business_value = sum(_amount(item, "bonus_basis_amount", "line_total") for item in matching)
                bonus = sum(_amount(item, "bonus_amount") for item in matching)
            elif items or not _product_match(sale.get("product_name"), filters.get("product")):
                continue
        if filters.get("farmer") and not _row_matches_farmer(
            sale, farmer_object_ids, farmer_strings, ("farmer_user_id", "farmer_user_id_str")
        ):
            continue
        row = grouped.setdefault(uid, {"mitra_uid": uid, "mitra_name": name_map.get(uid, uid), "transactions": 0, "business_value": 0.0, "earnings": 0.0, "avpl_earnings": 0.0, "farmer_earnings": 0.0})
        row["transactions"] += 1
        row["business_value"] += business_value
        row["earnings"] += bonus
        row["avpl_earnings"] += bonus

    for sale in mongo.db.farmer_product_sales.find({"mitra_uid": {"$in": list(allowed)}} if allowed else {"_id": None}):
        if not _in_range(sale, filters, "created_at"):
            continue
        if not _product_match(sale.get("product_name"), filters.get("product")):
            continue
        if filters.get("farmer"):
            # Legacy rows are only included in Farmer-wise reports when they carry
            # an explicit Farmer reference. Unknown ownership is never guessed.
            if not _row_matches_farmer(sale, farmer_object_ids, farmer_strings, ("farmer_user_id", "farmer_user_id_str", "buyer_user_id", "buyer_key")):
                continue
        uid = _clean(sale.get("mitra_uid"))
        row = grouped.setdefault(uid, {"mitra_uid": uid, "mitra_name": name_map.get(uid, uid), "transactions": 0, "business_value": 0.0, "earnings": 0.0, "avpl_earnings": 0.0, "farmer_earnings": 0.0})
        row["transactions"] += 1
        row["business_value"] += _amount(sale, "total_amount")
        bonus = _amount(sale, "bonus_amount")
        row["earnings"] += bonus
        row["farmer_earnings"] += bonus

    for uid in allowed:
        grouped.setdefault(uid, {"mitra_uid": uid, "mitra_name": name_map.get(uid, uid), "transactions": 0, "business_value": 0.0, "earnings": 0.0, "avpl_earnings": 0.0, "farmer_earnings": 0.0})

    rows = list(grouped.values())
    for row in rows:
        row["business_value"] = round(row["business_value"], 2)
        row["earnings"] = round(row["earnings"], 2)
        row["avpl_earnings"] = round(row["avpl_earnings"], 2)
        row["farmer_earnings"] = round(row["farmer_earnings"], 2)
    return sorted(rows, key=lambda row: (-row["earnings"], -row["business_value"], row["mitra_name"]))


def _filter_options(scope, filters, data=None):
    farmers = _mapped_farmers(scope)
    mitras = _mapped_mitras(scope)
    products = set()
    centre_uid = scope.get("centre_uid") or ""
    if scope["role"] in {"ufc_admin", "ufc_mitra"}:
        if centre_uid:
            for collection, query in [
                ("ufc_farmer_sales", {"centre_uid": centre_uid}),
                ("ufc_purchase_entries", {"centre_uid": centre_uid}),
                ("farmer_production_entries", {"centre_uid": centre_uid}),
                ("farmer_marketplace_purchase_entries", {"buyer_type": "ufc", "buyer_key": centre_uid}),
            ]:
                for row in mongo.db[collection].find(query, {"product_name": 1, "items.product_name": 1}).limit(1000):
                    if _clean(row.get("product_name")):
                        products.add(_clean(row.get("product_name")))
                    for item in row.get("items") or []:
                        if _clean((item or {}).get("product_name")):
                            products.add(_clean(item.get("product_name")))
    else:
        uid = _farmer_user_id(scope.get("farmer") or {}) or scope.get("user_id")
        uid_values = [x for x in [uid, str(uid or "")] if x]
        for collection in ("farmer_production_entries", "farmer_external_sales", "farmer_marketplace_sales"):
            for row in mongo.db[collection].find({"$or": [{"farmer_user_id": {"$in": uid_values}}, {"farmer_user_id_str": str(uid or "")}, {"seller_farmer_user_id": {"$in": uid_values}}]}, {"product_name": 1, "items.product_name": 1}).limit(1000):
                if _clean(row.get("product_name")):
                    products.add(_clean(row.get("product_name")))
                for item in row.get("items") or []:
                    if _clean((item or {}).get("product_name")):
                        products.add(_clean(item.get("product_name")))
    return {
        "periods": [
            ("today", "Today"), ("yesterday", "Yesterday"), ("7d", "Last 7 Days"),
            ("this_month", "This Month"), ("last_month", "Last Month"),
            ("3m", "Last 3 Months"), ("financial_year", "This Financial Year"),
            ("all", "All Time"), ("custom", "Custom"),
        ],
        "farmers": [{"value": _farmer_key(f), "label": _farmer_name(f)} for f in farmers if _farmer_key(f)],
        "mitras": [{"value": _clean(m.get("mitra_uid")), "label": _clean(m.get("name") or m.get("mitra_name") or m.get("mitra_uid"))} for m in mitras if _clean(m.get("mitra_uid"))],
        "products": sorted(products, key=str.lower),
        "statuses": [("all", "All Status"), ("requested", "Requested"), ("approved", "Approved"), ("dispatched", "Dispatched"), ("delivered", "Delivered"), ("received", "Received"), ("completed", "Completed"), ("cancelled", "Cancelled"), ("rejected", "Rejected"), ("pending_confirmation", "Awaiting Confirmation"), ("partially_paid", "Partially Paid"), ("paid", "Paid"), ("unpaid", "Unpaid")],
    }


def _kpi(label, value, *, kind="number", note="", tone="neutral"):
    return {"label": label, "value": value, "kind": kind, "note": note, "tone": tone}


def _table(title, columns, rows, *, empty="No records for the selected filters."):
    return {"title": title, "columns": columns, "rows": rows, "empty": empty}


def _centre_farmer_summary(data):
    sale_rows = _sale_rows_for_farmers(data)
    by_farmer = {}
    for farmer in data.get("farmers", []):
        uid = str(_farmer_user_id(farmer) or "")
        by_farmer[uid] = {
            "farmer_id": _farmer_key(farmer),
            "farmer_name": _farmer_name(farmer),
            "mitra_uid": _clean(farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid")),
            "activity": ", ".join(farmer.get("activities") or []) or "-",
            "purchases": 0.0,
            "produce_sold": defaultdict(float),
            "sales_value": 0.0,
            "received": 0.0,
            "pending": 0.0,
            "last_activity": None,
        }
    for purchase in data.get("ufc_sales", []):
        uid = str(purchase.get("farmer_user_id") or "")
        if uid in by_farmer:
            by_farmer[uid]["purchases"] += _amount(purchase, "grand_total", "total_amount")
            d = _row_date(purchase, "sale_date", "created_at")
            if d and (not by_farmer[uid]["last_activity"] or d > by_farmer[uid]["last_activity"]):
                by_farmer[uid]["last_activity"] = d
    for row in sale_rows:
        uid = str(row.get("farmer_user_id") or "")
        if uid not in by_farmer:
            continue
        summary = by_farmer[uid]
        summary["sales_value"] += _num(row.get("sales_value"))
        summary["received"] += _num(row.get("received"))
        summary["pending"] += _num(row.get("outstanding"))
        summary["produce_sold"][(row.get("unit_code") or "UNIT")] += _num(row.get("quantity"))
        d = row.get("date")
        if d and (not summary["last_activity"] or d > summary["last_activity"]):
            summary["last_activity"] = d
    result = []
    for row in by_farmer.values():
        row["sales_value"] = round(row["sales_value"], 2)
        row["purchases"] = round(row["purchases"], 2)
        row["received"] = round(row["received"], 2)
        row["pending"] = round(max(row["pending"], 0), 2)
        row["produce_sold_display"] = " · ".join(f"{_format_qty_number(v)} {u}" for u, v in sorted(row["produce_sold"].items())) or "-"
        row["last_activity_display"] = row["last_activity"].strftime("%d %b %Y") if row["last_activity"] else "-"
        result.append(row)
    return sorted(result, key=lambda row: (-row["sales_value"], row["farmer_name"]))


def _centre_produce_summary(data):
    grouped = {}
    for production in data.get("productions", []):
        key = (_clean(production.get("product_name")) or "Produce", _unit(production.get("unit_code")))
        row = grouped.setdefault(key, {"product": key[0], "unit": key[1], "farmers": set(), "produced": 0.0, "listed": 0.0, "sold": 0.0, "sales_value": 0.0})
        row["produced"] += _amount(production, "quantity_produced", "quantity")
        if production.get("farmer_user_id"):
            row["farmers"].add(str(production.get("farmer_user_id")))
    for listing in data.get("listings", []):
        key = (_clean(listing.get("product_name")) or "Produce", _unit(listing.get("unit_code")))
        row = grouped.setdefault(key, {"product": key[0], "unit": key[1], "farmers": set(), "produced": 0.0, "listed": 0.0, "sold": 0.0, "sales_value": 0.0})
        row["listed"] += max(_amount(listing, "listed_quantity") - _amount(listing, "fulfilled_quantity") - _amount(listing, "reserved_quantity"), 0)
        if listing.get("farmer_user_id"):
            row["farmers"].add(str(listing.get("farmer_user_id")))
    for sale in _sale_rows_for_farmers(data):
        key = (_clean(sale.get("product_name")) or "Produce", _unit(sale.get("unit_code")))
        row = grouped.setdefault(key, {"product": key[0], "unit": key[1], "farmers": set(), "produced": 0.0, "listed": 0.0, "sold": 0.0, "sales_value": 0.0})
        row["sold"] += _num(sale.get("quantity"))
        row["sales_value"] += _num(sale.get("sales_value"))
        if sale.get("farmer_user_id"):
            row["farmers"].add(str(sale.get("farmer_user_id")))
    result = []
    for row in grouped.values():
        result.append({
            "product": row["product"], "unit": row["unit"], "farmers": len(row["farmers"]),
            "produced": round(row["produced"], 4), "listed": round(row["listed"], 4),
            "sold": round(row["sold"], 4), "sales_value": round(row["sales_value"], 2),
        })
    return sorted(result, key=lambda row: (-row["sales_value"], row["product"]))


def _centre_stock_rows(scope, filters):
    centre_uid = scope.get("centre_uid") or "__NO_CENTRE__"
    grouped = {}
    today = business_today()
    for lot in mongo.db.ufc_inventory_lots.find({"centre_uid": centre_uid, "status": {"$ne": "cancelled"}}):
        if not _product_match(lot.get("product_name"), filters.get("product")):
            continue
        key = ("Input", _clean(lot.get("product_name")) or "Product", _unit(lot.get("unit_code")))
        row = grouped.setdefault(key, {"source": key[0], "product": key[1], "unit": key[2], "physical": 0.0, "reserved": 0.0, "available": 0.0, "damaged": 0.0, "expiring": 0.0, "value": 0.0})
        physical = max(_amount(lot, "available_quantity"), 0)
        reserved = min(max(_amount(lot, "reserved_quantity"), 0), physical)
        damaged = min(max(_amount(lot, "damaged_quantity"), 0), physical)
        blocked = min(max(_amount(lot, "blocked_quantity"), 0), physical)
        expiry = _date_value(lot.get("expiry_date"))
        expired = expiry and expiry < today
        saleable = 0 if expired or _clean(lot.get("status")).lower() == "expired" else max(physical - reserved - damaged - blocked, 0)
        unit_cost = _amount(lot, "unit_cost", "purchase_unit_cost")
        if unit_cost <= 0 and _amount(lot, "received_quantity") > 0:
            unit_cost = _amount(lot, "purchase_cost_total") / max(_amount(lot, "received_quantity"), 0.000001)
        row["physical"] += physical; row["reserved"] += reserved; row["available"] += saleable; row["damaged"] += damaged
        row["value"] += physical * unit_cost
        if expiry and today <= expiry <= today + timedelta(days=30):
            row["expiring"] += physical
    for lot in mongo.db.farmer_marketplace_buyer_stock_lots.find({"buyer_type": "ufc", "buyer_key": centre_uid, "status": "active"}):
        if not _product_match(lot.get("product_name"), filters.get("product")):
            continue
        key = ("Farmer Produce", _clean(lot.get("product_name")) or "Produce", _unit(lot.get("unit_code")))
        row = grouped.setdefault(key, {"source": key[0], "product": key[1], "unit": key[2], "physical": 0.0, "reserved": 0.0, "available": 0.0, "damaged": 0.0, "expiring": 0.0, "value": 0.0})
        qty = max(_amount(lot, "available_quantity"), 0)
        row["physical"] += qty; row["available"] += qty
        unit_cost = _amount(lot, "unit_cost")
        if unit_cost <= 0 and _amount(lot, "original_quantity") > 0:
            purchase = mongo.db.farmer_marketplace_purchase_entries.find_one({"_id": lot.get("purchase_id")}, {"total_amount": 1, "quantity": 1}) or {}
            if _amount(purchase, "quantity") > 0:
                unit_cost = _amount(purchase, "total_amount") / _amount(purchase, "quantity")
        row["value"] += qty * unit_cost
    result = []
    for row in grouped.values():
        for field in ("physical", "reserved", "available", "damaged", "expiring"):
            row[field] = round(row[field], 4)
        row["value"] = round(row["value"], 2)
        result.append(row)
    return sorted(result, key=lambda row: (row["source"], -row["value"], row["product"]))


def _build_centre_report(scope, section, filters):
    data_filters = {**filters, "_farmer_search_only": section == "farmers"}
    data = _load_centre_data(scope, data_filters)
    farmer_rows = _centre_farmer_summary(data)
    sale_rows = _centre_ufc_sales_rows(data)
    farmer_sale_rows = _sale_rows_for_farmers(data)
    mitra_rows = _mitra_earning_rows(scope, filters, data.get("all_mitras"), data.get("farmers"))
    active_ids = _active_farmer_ids(data)
    money_received_by_farmers = sum(row["received"] for row in farmer_rows)
    farmer_sales_value = sum(row["sales_value"] for row in farmer_rows)
    farmer_pending = sum(row["pending"] for row in farmer_rows)
    ufc_sales_value = sum(row["sales_value"] for row in sale_rows)
    ufc_cogs = sum(row["cogs"] for row in sale_rows)
    ufc_margin = ufc_sales_value - ufc_cogs
    ufc_sale_received = sum(_num(row.get("received")) for row in sale_rows)
    ufc_sale_outstanding = sum(_num(row.get("outstanding")) for row in sale_rows)
    ufc_money_collected = sum(_amount(p, "amount") for p in data["payments"] if p.get("status") == "completed" and str(p.get("payee_key") or "") == scope.get("centre_uid"))
    ufc_money_paid = sum(_amount(p, "amount") for p in data["payments"] if p.get("status") == "completed" and str(p.get("payer_key") or "") == scope.get("centre_uid"))
    purchase_paid = sum(_scoped_settlement_values(row, filters.get("product"), "total_amount", "grand_total")[1] for row in data["avpl_purchases"])
    purchase_paid += sum(_scoped_settlement_values(row, filters.get("product"), "total_amount", "grand_total")[1] for row in data["produce_purchases"])
    # Outstanding is derived from the same filtered operational transactions used
    # by the visible report. This keeps KPI, table, PDF and Excel perfectly aligned.
    ufc_receivable = round(ufc_sale_outstanding, 2)
    # Use reconciled settlement pairs, not the raw mirror outstanding field.
    # This keeps Reports aligned with Payments & Settlement for legacy/stale rows.
    avpl_payable = sum(_scoped_settlement_values(row, filters.get("product"), "total_amount", "grand_total")[2] for row in data["avpl_purchases"])
    produce_payable = sum(_scoped_settlement_values(row, filters.get("product"), "total_amount", "grand_total")[2] for row in data["produce_purchases"])
    payable = avpl_payable + produce_payable
    pending_confirmation = sum(_amount(row, "amount") for row in data["payments"] if row.get("status") == "pending_confirmation")

    common = {
        "scope_label": scope.get("centre_name") or scope.get("centre_uid"),
        "nav": CENTRE_SECTIONS,
        "filters": filters,
        "filter_options": _filter_options(scope, filters, data),
        "show_filters": {"farmer": True, "mitra": True, "product": True, "status": section in {"payments", "orders"}, "q": section in {"payments", "orders", "farmers"}},
        "exportable": True,
        "notice": "Sales value and confirmed money received are shown separately. Farmer, UFC and Mitra figures are not added together as one revenue number.",
    }

    if section == "overview":
        top_farmers = farmer_rows[:8]
        return {**common,
            "section": section, "title": "Centre Overview", "subtitle": filters["period_label"],
            "kpis": [
                _kpi("Mapped Farmers", len(data["farmers"]), note=f"{len(active_ids)} active in period"),
                _kpi("Active Farmers", len(active_ids)),
                _kpi("UFC Sales", round(ufc_sales_value, 2), kind="money"),
                _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money"),
                _kpi("Money to Receive", round(ufc_receivable, 2), kind="money", tone="warning" if ufc_receivable else "neutral"),
                _kpi("Money to Pay", round(payable, 2), kind="money", tone="warning" if payable else "neutral"),
            ],
            "trend": _trend(sale_rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="UFC Sales Trend"),
            "tables": [_table("Top Farmers", [
                ("farmer_name", "Farmer", "text"), ("sales_value", "Farmer Sales", "money"),
                ("received", "Money Received", "money"), ("pending", "Pending", "money"),
            ], top_farmers, empty="No Farmer sales in this period.")],
        }

    if section == "farmers":
        return {**common,
            "section": section, "title": "Farmer Performance", "subtitle": filters["period_label"],
            "kpis": [
                _kpi("Farmers", len(data["farmers"])), _kpi("Active", len(active_ids)),
                _kpi("Farmers Who Sold", sum(1 for row in farmer_rows if row["sales_value"] > 0)),
                _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money"),
                _kpi("Money Received", round(money_received_by_farmers, 2), kind="money"),
                _kpi("Pending", round(farmer_pending, 2), kind="money", tone="warning" if farmer_pending else "neutral"),
            ],
            "trend": _trend(farmer_sale_rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="Farmer Sales Trend"),
            "tables": [_table("Farmer-wise Performance", [
                ("farmer_name", "Farmer", "text"), ("activity", "Activity", "text"), ("purchases", "Inputs Purchased", "money"),
                ("produce_sold_display", "Produce Sold", "text"), ("sales_value", "Sales Value", "money"),
                ("received", "Received", "money"), ("pending", "Pending", "money"), ("last_activity_display", "Last Activity", "text"),
            ], farmer_rows)],
        }

    if section == "mitras":
        farmer_counts = defaultdict(lambda: {"total": 0, "active": 0})
        for farmer in data["farmers"]:
            uid = _clean(farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid"))
            if not uid:
                continue
            farmer_counts[uid]["total"] += 1
            if str(_farmer_user_id(farmer) or "") in active_ids:
                farmer_counts[uid]["active"] += 1
        for row in mitra_rows:
            row["farmers"] = farmer_counts[row["mitra_uid"]]["total"]
            row["active_farmers"] = farmer_counts[row["mitra_uid"]]["active"]
        total_earnings = sum(r["earnings"] for r in mitra_rows)
        return {**common,
            "section": section, "title": "Mitra Performance", "subtitle": filters["period_label"],
            "notice": "Mitra Earnings uses the commission/bonus amount already stored by the current sales rules. The project does not yet store a separate Mitra commission paid/pending settlement state, so this report does not invent one.",
            "kpis": [
                _kpi("Mitras", len(data["mitras"])),
                _kpi("Active Mitras", sum(1 for r in mitra_rows if r["transactions"] > 0)),
                _kpi("Farmers Served", len(data["farmers"])),
                _kpi("Business Generated", round(sum(r["business_value"] for r in mitra_rows), 2), kind="money"),
                _kpi("Mitra Earnings", round(total_earnings, 2), kind="money"),
            ],
            "trend": None,
            "tables": [_table("Mitra-wise Performance", [
                ("mitra_name", "Mitra", "text"), ("farmers", "Farmers", "number"), ("active_farmers", "Active", "number"),
                ("transactions", "Transactions", "number"), ("business_value", "Business Generated", "money"),
                ("earnings", "Earnings", "money"),
            ], mitra_rows)],
        }

    if section == "revenue":
        total_mitra = sum(r["earnings"] for r in mitra_rows)
        eco_rows = [
            {"layer": "UFC", "sales_value": round(ufc_sales_value, 2), "money_received": round(ufc_sale_received, 2), "pending": round(ufc_receivable, 2), "note": "Centre sales"},
            {"layer": "Farmers", "sales_value": round(farmer_sales_value, 2), "money_received": round(money_received_by_farmers, 2), "pending": round(farmer_pending, 2), "note": "Mapped Farmer produce sales"},
            {"layer": "Mitras", "sales_value": round(sum(r["business_value"] for r in mitra_rows), 2), "money_received": round(total_mitra, 2), "pending": None, "note": "Business generated / earnings"},
        ]
        return {**common,
            "section": section, "title": "Revenue", "subtitle": filters["period_label"],
            "notice": "UFC sales, Farmer sales and Mitra earnings are different economic layers. They are shown side by side and are never added into a misleading 'total revenue'.",
            "kpis": [
                _kpi("UFC Sales", round(ufc_sales_value, 2), kind="money"), _kpi("UFC Gross Margin", round(ufc_margin, 2), kind="money"),
                _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money"), _kpi("Farmers Received", round(money_received_by_farmers, 2), kind="money"),
                _kpi("Mitra Earnings", round(total_mitra, 2), kind="money"), _kpi("Pending Confirmation", round(pending_confirmation, 2), kind="money", tone="warning" if pending_confirmation else "neutral"),
            ],
            "trend": _trend(sale_rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="UFC Revenue Trend"),
            "tables": [
                _table("Ecosystem View", [("layer", "Layer", "text"), ("sales_value", "Sales / Business", "money"), ("money_received", "Confirmed Money / Earnings", "money"), ("pending", "Pending", "money_optional"), ("note", "Meaning", "text")], eco_rows),
                _table("UFC Sales & Margin", [("date", "Date", "date"), ("channel", "Channel", "text"), ("reference", "Reference", "text"), ("customer", "Customer", "text"), ("product", "Product", "text"), ("sales_value", "Sales", "money"), ("cogs", "COGS", "money"), ("margin", "Gross Margin", "money")], sale_rows),
            ],
        }

    if section == "produce":
        produce_rows = _centre_produce_summary(data)
        return {**common,
            "section": section, "title": "Produce", "subtitle": filters["period_label"],
            "kpis": [
                _kpi("Producing Farmers", len({str(r.get("farmer_user_id")) for r in data["productions"] if r.get("farmer_user_id")})),
                _kpi("Produce Types", len(produce_rows)),
                _kpi("Produced", _quantity_summary(data["productions"], "quantity_produced", "unit_code"), kind="text"),
                _kpi("Sold", _quantity_summary(farmer_sale_rows, "quantity", "unit_code"), kind="text"),
                _kpi("Sales Value", round(farmer_sales_value, 2), kind="money"),
            ],
            "trend": _trend(farmer_sale_rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="Produce Sales Trend"),
            "tables": [_table("Produce-wise Performance", [
                ("product", "Produce", "text"), ("farmers", "Farmers", "number"), ("produced", "Produced", "qty_with_unit"),
                ("listed", "Listed Open", "qty_with_unit"), ("sold", "Sold", "qty_with_unit"), ("sales_value", "Sales Value", "money"),
            ], produce_rows)],
        }

    if section == "purchases":
        rows = []
        for row in data["avpl_purchases"]:
            qty, unit = _scoped_transaction_qty(row, filters.get("product"))
            value = _scoped_transaction_value(row, filters.get("product"), "total_amount", "grand_total")
            rows.append({"date": _row_date(row, "purchase_date", "created_at"), "source": "AVPL", "party": "AVPL", "reference": row.get("purchase_number") or row.get("avpl_order_number") or "", "product": _row_product_summary(row, filters.get("product")), "quantity": qty, "unit": unit, "value": value, "payment_status": _status(row.get("payment_status"))})
        for row in data["produce_purchases"]:
            qty, unit = _scoped_transaction_qty(row, filters.get("product"))
            value = _scoped_transaction_value(row, filters.get("product"), "total_amount", "grand_total")
            rows.append({"date": _row_date(row, "received_at", "created_at"), "source": "Farmer", "party": row.get("seller_farmer_name") or "Farmer", "reference": row.get("purchase_number") or row.get("order_number") or "", "product": _row_product_summary(row, filters.get("product")), "quantity": qty, "unit": unit, "value": value, "payment_status": _status(row.get("payment_status"))})
        rows.sort(key=lambda r: r.get("date") or date.min, reverse=True)
        avpl_value = sum(_scoped_transaction_value(r, filters.get("product"), "total_amount", "grand_total") for r in data["avpl_purchases"])
        produce_value = sum(_scoped_transaction_value(r, filters.get("product"), "total_amount", "grand_total") for r in data["produce_purchases"])
        return {**common,
            "section": section, "title": "Purchases", "subtitle": filters["period_label"],
            "kpis": [_kpi("Purchase Value", round(avpl_value + produce_value, 2), kind="money"), _kpi("From AVPL", round(avpl_value, 2), kind="money"), _kpi("From Farmers", round(produce_value, 2), kind="money"), _kpi("Orders Received", len(rows)), _kpi("Money Paid", round(purchase_paid, 2), kind="money"), _kpi("Outstanding", round(payable, 2), kind="money", tone="warning" if payable else "neutral")],
            "trend": _trend([{"date": r["date"], "value": r["value"]} for r in rows], "value", start=filters.get("from"), end=filters.get("to"), label="Purchase Trend"),
            "tables": [_table("Purchase Details", [("date", "Date", "date"), ("source", "Source", "text"), ("party", "Party", "text"), ("reference", "Reference", "text"), ("product", "Product", "text"), ("quantity", "Qty", "qty_with_unit"), ("value", "Value", "money"), ("payment_status", "Payment", "status")], rows)],
        }

    if section == "sales":
        return {**common,
            "section": section, "title": "Sales", "subtitle": filters["period_label"],
            "kpis": [_kpi("Total Sales", round(ufc_sales_value, 2), kind="money"), _kpi("Transactions", len({r["reference"] for r in sale_rows})), _kpi("Money Collected", round(ufc_sale_received, 2), kind="money"), _kpi("Receivable", round(ufc_receivable, 2), kind="money", tone="warning" if ufc_receivable else "neutral"), _kpi("COGS", round(ufc_cogs, 2), kind="money"), _kpi("Gross Margin", round(ufc_margin, 2), kind="money")],
            "trend": _trend(sale_rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="Sales Trend"),
            "tables": [_table("Sales Details", [("date", "Date", "date"), ("channel", "Channel", "text"), ("reference", "Reference", "text"), ("customer", "Customer", "text"), ("product", "Product", "text"), ("quantity", "Qty", "qty_with_unit"), ("sales_value", "Sales", "money"), ("margin", "Margin", "money")], sale_rows)],
        }

    if section == "stock":
        stock_rows = _centre_stock_rows(scope, filters)
        return {**common,
            "section": section, "title": "Stock", "subtitle": "Current stock · filters apply to product only",
            "show_filters": {"farmer": False, "mitra": False, "product": True, "status": False, "q": False},
            "kpis": [_kpi("Products", len(stock_rows)), _kpi("Available", _quantity_summary(stock_rows, "available", "unit"), kind="text"), _kpi("Reserved", _quantity_summary(stock_rows, "reserved", "unit"), kind="text"), _kpi("Damaged", _quantity_summary(stock_rows, "damaged", "unit"), kind="text"), _kpi("Expiring ≤30 Days", _quantity_summary(stock_rows, "expiring", "unit"), kind="text"), _kpi("Stock Value", round(sum(r["value"] for r in stock_rows), 2), kind="money")],
            "trend": None,
            "tables": [_table("Current Stock", [("source", "Stock Type", "text"), ("product", "Product", "text"), ("physical", "Physical", "qty_with_unit"), ("reserved", "Reserved", "qty_with_unit"), ("available", "Available", "qty_with_unit"), ("damaged", "Damaged", "qty_with_unit"), ("expiring", "Expiring", "qty_with_unit"), ("value", "Value", "money")], stock_rows)],
        }

    if section == "payments":
        rows = []
        for p in data["payments"]:
            rows.append({
                "date": _row_date(p, "payment_date", "confirmed_at", "completed_at", "created_at"),
                "reference": p.get("payment_number") or p.get("reference") or "",
                "from": p.get("payer_name") or p.get("payer_key") or "-",
                "to": p.get("payee_name") or p.get("payee_key") or "-",
                "for": p.get("source_label") or p.get("invoice_number") or "Payment",
                "amount": _amount(p, "amount"), "mode": _clean(p.get("payment_mode")).replace("_", " ").title() or "-",
                "status": _status(p.get("status")),
            })
        completed = [p for p in data["payments"] if p.get("status") == "completed"]
        return {**common,
            "section": section, "title": "Payments", "subtitle": filters["period_label"],
            "kpis": [_kpi("Money Received", round(ufc_money_collected, 2), kind="money"), _kpi("Money Paid", round(ufc_money_paid, 2), kind="money"), _kpi("Receivable", round(ufc_receivable, 2), kind="money"), _kpi("Payable", round(payable, 2), kind="money"), _kpi("Awaiting Confirmation", round(pending_confirmation, 2), kind="money", tone="warning" if pending_confirmation else "neutral"), _kpi("Confirmed Payments", len(completed))],
            "trend": _trend([{"date": _row_date(p, "payment_date", "confirmed_at", "completed_at", "created_at"), "amount": _amount(p, "amount")} for p in completed], "amount", start=filters.get("from"), end=filters.get("to"), label="Confirmed Payments Trend"),
            "tables": [_table("Payment Details", [("date", "Date", "date"), ("reference", "Reference", "text"), ("from", "From", "text"), ("to", "To", "text"), ("for", "For", "text"), ("amount", "Amount", "money"), ("mode", "Mode", "text"), ("status", "Status", "status")], rows)],
        }

    # Orders
    rows = []
    for order in mongo.db.avpl_ufc_orders.find({"centre_uid": scope.get("centre_uid")}):
        if filters.get("farmer") or filters.get("mitra"):
            continue
        if not _in_range(order, filters, "received_at", "dispatched_at", "approved_at", "created_at") or not _filter_status(order, filters):
            continue
        if not _row_product_matches(order, filters.get("product")) or not _row_search_match(order, filters.get("q"), ("order_number", "product_name", "status")):
            continue
        qty, unit = _scoped_transaction_qty(order, filters.get("product"))
        rows.append({"date": _row_date(order, "created_at"), "type": "AVPL → UFC", "reference": order.get("order_number") or "", "party": "AVPL", "product": _row_product_summary(order, filters.get("product")), "quantity": qty, "unit": unit, "value": _scoped_transaction_value(order, filters.get("product"), "total_amount"), "status": _status(order.get("status"))})
    for order in data["ufc_orders"]:
        qty, unit = _scoped_transaction_qty(order, filters.get("product"))
        rows.append({"date": _row_date(order, "created_at"), "type": "UFC → Farmer", "reference": order.get("order_number") or "", "party": order.get("farmer_name") or "Farmer", "product": _row_product_summary(order, filters.get("product")), "quantity": qty, "unit": unit, "value": _scoped_transaction_value(order, filters.get("product"), "total_amount", "grand_total"), "status": _status(order.get("status"))})
    for order in mongo.db.farmer_produce_marketplace_orders.find({"buyer_type": "ufc", "buyer_key": scope.get("centre_uid")}):
        if not _in_range(order, filters, "received_at", "created_at") or not _filter_status(order, filters):
            continue
        if not _row_product_matches(order, filters.get("product")) or not _row_search_match(order, filters.get("q"), ("order_number", "seller_farmer_name", "product_name", "status")):
            continue
        if filters.get("farmer") and not _row_matches_farmer(order, data["farmer_object_ids"], data["farmer_strings"], ("seller_farmer_user_id", "seller_farmer_user_id_str")):
            continue
        if filters.get("mitra"):
            seller_id = str(order.get("seller_farmer_user_id") or order.get("seller_farmer_user_id_str") or "")
            farmer = next((f for f in data.get("all_farmers", []) if str(_farmer_user_id(f) or "") == seller_id), None)
            if not farmer or _clean(farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid")) != filters.get("mitra"):
                continue
        qty, unit = _scoped_transaction_qty(order, filters.get("product"))
        rows.append({"date": _row_date(order, "created_at"), "type": "Farmer → UFC", "reference": order.get("order_number") or "", "party": order.get("seller_farmer_name") or "Farmer", "product": _row_product_summary(order, filters.get("product")), "quantity": qty, "unit": unit, "value": _scoped_transaction_value(order, filters.get("product"), "total_amount", "grand_total"), "status": _status(order.get("status"))})
    rows.sort(key=lambda r: r.get("date") or date.min, reverse=True)
    status_counts = defaultdict(int)
    for row in rows:
        status_counts[row["status"]] += 1
    return {**common,
        "section": "orders", "title": "Orders", "subtitle": filters["period_label"],
        "kpis": [_kpi("Orders", len(rows)), _kpi("Requested", status_counts.get("Requested", 0)), _kpi("Approved", status_counts.get("Approved", 0)), _kpi("Dispatched", status_counts.get("Dispatched", 0)), _kpi("Received / Delivered", status_counts.get("Received", 0) + status_counts.get("Delivered", 0)), _kpi("Cancelled", status_counts.get("Cancelled", 0))],
        "trend": None,
        "tables": [_table("Order Details", [("date", "Date", "date"), ("type", "Order Type", "text"), ("reference", "Reference", "text"), ("party", "Party", "text"), ("product", "Product", "text"), ("quantity", "Qty", "qty_with_unit"), ("value", "Value", "money"), ("status", "Status", "status")], rows)],
    }


def _build_farmer_report(scope, section, filters):
    data = _load_farmer_data(scope, filters)
    farmer = data.get("farmer") or {}
    sales = _sale_rows_for_farmers(data)
    sales_value = sum(_num(r["sales_value"]) for r in sales)
    sales_received = sum(_num(r.get("received")) for r in sales)
    payments = data["payments"]
    actor_key = scope.get("user_id_str") or str(_farmer_user_id(farmer) or "")
    money_received = sum(_amount(p, "amount") for p in payments if p.get("status") == "completed" and str(p.get("payee_key") or "") == actor_key)
    money_paid = sum(_amount(p, "amount") for p in payments if p.get("status") == "completed" and str(p.get("payer_key") or "") == actor_key)
    # Sales rows are already line-aware and proportionally allocate settlement for product filters.
    receivable = sum(_num(r.get("outstanding")) for r in sales)
    payable = sum(_scoped_settlement_values(r, filters.get("product"), "total_amount", "grand_total", "amount")[2] for r in data["purchases"])
    payable += sum(_scoped_settlement_values(r, filters.get("product"), "total_amount", "grand_total", "amount")[2] for r in data["market_purchases"])
    purchase_rows = []
    for row in data["purchases"]:
        qty, unit = _scoped_transaction_qty(row, filters.get("product"))
        purchase_rows.append({"date": _row_date(row, "purchase_date", "created_at"), "source": "My UFC", "reference": row.get("purchase_number") or row.get("order_number") or "", "product": _row_product_summary(row, filters.get("product")), "quantity": qty, "unit": unit, "value": _scoped_transaction_value(row, filters.get("product"), "total_amount", "grand_total"), "payment": _status(row.get("payment_status"))})
    for row in data["market_purchases"]:
        qty, unit = _scoped_transaction_qty(row, filters.get("product"))
        purchase_rows.append({"date": _row_date(row, "received_at", "created_at"), "source": row.get("seller_farmer_name") or "Farmer", "reference": row.get("purchase_number") or row.get("order_number") or "", "product": _row_product_summary(row, filters.get("product")), "quantity": qty, "unit": unit, "value": _scoped_transaction_value(row, filters.get("product"), "total_amount", "grand_total"), "payment": _status(row.get("payment_status"))})
    purchase_rows.sort(key=lambda r: r.get("date") or date.min, reverse=True)
    purchase_value = sum(r["value"] for r in purchase_rows)
    purchase_paid = sum(_scoped_settlement_values(r, filters.get("product"), "total_amount", "grand_total")[1] for r in data["purchases"])
    purchase_paid += sum(_scoped_settlement_values(r, filters.get("product"), "total_amount", "grand_total")[1] for r in data["market_purchases"])

    common = {
        "scope_label": _farmer_name(farmer), "nav": FARMER_SECTIONS, "filters": filters,
        "filter_options": _filter_options(scope, filters, data),
        "show_filters": {"farmer": False, "mitra": False, "product": True, "status": section == "payments", "q": section == "payments"},
        "exportable": True,
        "notice": "Sales Value is what you sold. Money Received is only confirmed money received. They can be different while a payment is pending.",
    }
    if section == "overview":
        stock_rows = []
        for lot in data["stock"]:
            if not _product_match(lot.get("product_name"), filters.get("product")):
                continue
            available = max(_amount(lot, "available_quantity") - _amount(lot, "reserved_quantity") - _amount(lot, "damaged_quantity") - _amount(lot, "blocked_quantity"), 0)
            stock_rows.append({"product": lot.get("product_name") or "Produce", "available": available, "unit": _unit(lot.get("unit_code"))})
        return {**common,
            "section": section, "title": "My Reports", "subtitle": filters["period_label"],
            "kpis": [_kpi("Produced", _quantity_summary(data["productions"], "quantity_produced", "unit_code"), kind="text"), _kpi("Sold", _quantity_summary(sales, "quantity", "unit_code"), kind="text"), _kpi("Sales Value", round(sales_value, 2), kind="money"), _kpi("Money Received", round(sales_received, 2), kind="money"), _kpi("Pending to Receive", round(receivable, 2), kind="money", tone="warning" if receivable else "neutral"), _kpi("Inputs Purchased", round(purchase_value, 2), kind="money")],
            "trend": _trend(sales, "sales_value", start=filters.get("from"), end=filters.get("to"), label="My Sales Trend"),
            "tables": [_table("Current Produce Stock", [("product", "Produce", "text"), ("available", "Available", "qty_with_unit")], stock_rows)],
        }
    if section == "production":
        rows = [{"date": _row_date(r, "harvest_date", "created_at"), "reference": r.get("production_number") or "", "product": r.get("product_name") or "Produce", "quantity": _amount(r, "quantity_produced", "quantity"), "unit": _unit(r.get("unit_code")), "variety": r.get("variety") or "-", "estimated_cost": _amount(r, "estimated_cost")} for r in data["productions"]]
        return {**common,
            "section": section, "title": "Production", "subtitle": filters["period_label"],
            "kpis": [_kpi("Production Entries", len(rows)), _kpi("Produced", _quantity_summary(data["productions"], "quantity_produced", "unit_code"), kind="text"), _kpi("Produce Types", len({_clean(r["product"]).lower() for r in rows})), _kpi("Estimated Cost", round(sum(r["estimated_cost"] for r in rows), 2), kind="money")],
            "trend": None,
            "tables": [_table("Production Details", [("date", "Date", "date"), ("reference", "Reference", "text"), ("product", "Produce", "text"), ("quantity", "Quantity", "qty_with_unit"), ("variety", "Variety", "text"), ("estimated_cost", "Estimated Cost", "money")], rows)],
        }
    if section == "sales":
        rows = [{**r, "date": r.get("date")} for r in sorted(sales, key=lambda x: x.get("date") or date.min, reverse=True)]
        return {**common,
            "section": section, "title": "Sales", "subtitle": filters["period_label"],
            "kpis": [_kpi("Sales Value", round(sales_value, 2), kind="money"), _kpi("Money Received", round(sales_received, 2), kind="money"), _kpi("Pending", round(receivable, 2), kind="money", tone="warning" if receivable else "neutral"), _kpi("Sales", len(rows)), _kpi("Quantity Sold", _quantity_summary(rows, "quantity", "unit_code"), kind="text")],
            "trend": _trend(rows, "sales_value", start=filters.get("from"), end=filters.get("to"), label="Sales Trend"),
            "tables": [_table("Sales Details", [("date", "Date", "date"), ("source", "Sold Through", "text"), ("reference", "Reference", "text"), ("product_name", "Produce", "text"), ("quantity", "Qty", "qty_with_unit"), ("sales_value", "Sales Value", "money")], rows)],
        }
    if section == "purchases":
        return {**common,
            "section": section, "title": "Purchases", "subtitle": filters["period_label"],
            "kpis": [_kpi("Purchase Value", round(purchase_value, 2), kind="money"), _kpi("Purchases", len(purchase_rows)), _kpi("Money Paid", round(purchase_paid, 2), kind="money"), _kpi("Outstanding", round(payable, 2), kind="money", tone="warning" if payable else "neutral")],
            "trend": _trend([{"date": r["date"], "value": r["value"]} for r in purchase_rows], "value", start=filters.get("from"), end=filters.get("to"), label="Purchase Trend"),
            "tables": [_table("Purchase Details", [("date", "Date", "date"), ("source", "Bought From", "text"), ("reference", "Reference", "text"), ("product", "Product", "text"), ("quantity", "Qty", "qty_with_unit"), ("value", "Value", "money"), ("payment", "Payment", "status")], purchase_rows)],
        }
    rows = [{"date": _row_date(p, "payment_date", "confirmed_at", "completed_at", "created_at"), "reference": p.get("payment_number") or p.get("reference") or "", "from": p.get("payer_name") or p.get("payer_key") or "-", "to": p.get("payee_name") or p.get("payee_key") or "-", "for": p.get("source_label") or p.get("invoice_number") or "Payment", "amount": _amount(p, "amount"), "mode": _clean(p.get("payment_mode")).replace("_", " ").title() or "-", "status": _status(p.get("status"))} for p in payments]
    pending_conf = sum(_amount(p, "amount") for p in payments if p.get("status") == "pending_confirmation")
    return {**common,
        "section": "payments", "title": "Payments", "subtitle": filters["period_label"],
        "kpis": [_kpi("Money Received", round(money_received, 2), kind="money"), _kpi("Money Paid", round(money_paid, 2), kind="money"), _kpi("To Receive", round(receivable, 2), kind="money"), _kpi("To Pay", round(payable, 2), kind="money"), _kpi("Awaiting Confirmation", round(pending_conf, 2), kind="money", tone="warning" if pending_conf else "neutral")],
        "trend": None,
        "tables": [_table("Payment Details", [("date", "Date", "date"), ("reference", "Reference", "text"), ("from", "From", "text"), ("to", "To", "text"), ("for", "For", "text"), ("amount", "Amount", "money"), ("mode", "Mode", "text"), ("status", "Status", "status")], rows)],
    }


def _build_mitra_report(scope, section, filters):
    # Force own Mitra scope even if a query string attempts to change it.
    filters = {**filters, "mitra": scope.get("mitra_uid") or ""}
    data_filters = {**filters, "_farmer_search_only": section == "farmers"}
    data = _load_centre_data(scope, data_filters)
    farmer_rows = _centre_farmer_summary(data)
    mitra_rows = _mitra_earning_rows(scope, filters, _mapped_mitras(scope), data.get("farmers"))
    own = mitra_rows[0] if mitra_rows else {"transactions": 0, "business_value": 0.0, "earnings": 0.0, "avpl_earnings": 0.0, "farmer_earnings": 0.0}
    active_ids = _active_farmer_ids(data)
    farmer_sales_value = sum(r["sales_value"] for r in farmer_rows)
    common = {
        "scope_label": _clean((scope.get("mitra") or {}).get("name") or scope.get("mitra_uid") or "My Mitra Report"),
        "nav": MITRA_SECTIONS, "filters": filters, "filter_options": _filter_options(scope, filters, data),
        "show_filters": {"farmer": True, "mitra": False, "product": True, "status": False, "q": section == "farmers"},
        "exportable": True,
        "notice": "Earnings uses the commission/bonus amount already stored by the current business rules. There is no separate Mitra commission settlement ledger yet, so Paid/Pending commission is not fabricated in this report.",
    }
    if section == "overview":
        return {**common,
            "section": section, "title": "My Performance", "subtitle": filters["period_label"],
            "kpis": [_kpi("Farmers", len(data["farmers"])), _kpi("Active Farmers", len(active_ids)), _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money"), _kpi("Business Generated", round(own["business_value"], 2), kind="money"), _kpi("My Earnings", round(own["earnings"], 2), kind="money"), _kpi("Transactions", own["transactions"])],
            "trend": None,
            "tables": [_table("Farmer Activity", [("farmer_name", "Farmer", "text"), ("activity", "Activity", "text"), ("sales_value", "Sales Value", "money"), ("received", "Money Received", "money"), ("pending", "Pending", "money"), ("last_activity_display", "Last Activity", "text")], farmer_rows[:12])],
        }
    if section == "farmers":
        return {**common,
            "section": section, "title": "My Farmers", "subtitle": filters["period_label"],
            "kpis": [_kpi("Farmers", len(data["farmers"])), _kpi("Active", len(active_ids)), _kpi("Farmers Who Sold", sum(1 for r in farmer_rows if r["sales_value"] > 0)), _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money")],
            "trend": None,
            "tables": [_table("Farmer-wise Activity", [("farmer_name", "Farmer", "text"), ("activity", "Activity", "text"), ("produce_sold_display", "Produce Sold", "text"), ("sales_value", "Sales Value", "money"), ("received", "Received", "money"), ("pending", "Pending", "money"), ("last_activity_display", "Last Activity", "text")], farmer_rows)],
        }
    if section == "business":
        sales = _sale_rows_for_farmers(data)
        return {**common,
            "section": section, "title": "Business", "subtitle": filters["period_label"],
            "kpis": [_kpi("Business Generated", round(own["business_value"], 2), kind="money"), _kpi("Farmer Sales", round(farmer_sales_value, 2), kind="money"), _kpi("Transactions", own["transactions"]), _kpi("Farmers Active", len(active_ids))],
            "trend": _trend(sales, "sales_value", start=filters.get("from"), end=filters.get("to"), label="Mapped Farmer Sales Trend"),
            "tables": [_table("Farmer Sales", [("date", "Date", "date"), ("farmer_name", "Farmer", "text"), ("product_name", "Produce", "text"), ("quantity", "Qty", "qty_with_unit"), ("sales_value", "Sales Value", "money"), ("source", "Channel", "text")], sales)],
        }
    return {**common,
        "section": "earnings", "title": "My Earnings", "subtitle": filters["period_label"],
        "kpis": [_kpi("Total Earnings", round(own["earnings"], 2), kind="money"), _kpi("AVPL Product Earnings", round(own["avpl_earnings"], 2), kind="money"), _kpi("Farmer-side Earnings", round(own["farmer_earnings"], 2), kind="money"), _kpi("Eligible Transactions", own["transactions"]), _kpi("Business Base", round(own["business_value"], 2), kind="money")],
        "trend": None,
        "tables": [_table("Earnings Summary", [("mitra_name", "Mitra", "text"), ("transactions", "Transactions", "number"), ("business_value", "Business Generated", "money"), ("avpl_earnings", "AVPL Earnings", "money"), ("farmer_earnings", "Farmer-side Earnings", "money"), ("earnings", "Total Earnings", "money")], mitra_rows)],
    }


def build_operational_report(user_id, role, section, filters, *, centre_uid_hint="", mitra_uid_hint=""):
    scope = _resolve_actor(user_id, role, centre_uid_hint, mitra_uid_hint)
    if scope["role"] not in OPERATIONAL_REPORT_ROLES:
        raise PermissionError("This operational report is not available for your role.")
    if scope["role"] in {"ufc_admin", "ufc_mitra"} and not scope.get("centre_uid"):
        raise ValueError("Your UFC Centre mapping is missing. Update the profile mapping before opening Reports.")
    if scope["role"] == "farmer" and not scope.get("farmer"):
        raise ValueError("Farmer profile was not found.")

    allowed = dict(CENTRE_SECTIONS if scope["role"] == "ufc_admin" else FARMER_SECTIONS if scope["role"] == "farmer" else MITRA_SECTIONS)
    section = section if section in allowed else "overview"
    if scope["role"] == "ufc_admin":
        report = _build_centre_report(scope, section, filters)
    elif scope["role"] == "farmer":
        report = _build_farmer_report(scope, section, filters)
    else:
        report = _build_mitra_report(scope, section, filters)
    report["scope"] = scope
    report["section_label"] = allowed.get(section, "Overview")
    report["generated_on"] = business_today().strftime("%d %b %Y")
    return report


def export_rows(report):
    """Return flat export payload using the same filtered report already rendered on screen."""
    kpis = []
    for kpi in report.get("kpis") or []:
        value = kpi.get("value")
        if kpi.get("kind") == "money":
            value = f"₹{_money(value):,.2f}"
        kpis.append((kpi.get("label") or "", value))
    tables = []
    for table in report.get("tables") or []:
        headers = [column[1] for column in table.get("columns") or []]
        rows = []
        for row in table.get("rows") or []:
            rendered = []
            for key, _label, kind in table.get("columns") or []:
                value = row.get(key)
                if kind in {"qty_with_unit"}:
                    unit = row.get("unit") or row.get("unit_code") or ""
                    value = f"{_format_qty_number(value)} {unit}".strip()
                elif kind in {"money", "money_optional"}:
                    value = "" if value is None and kind == "money_optional" else f"₹{_money(value):,.2f}"
                elif kind == "date":
                    parsed = _date_value(value)
                    value = parsed.strftime("%d %b %Y") if parsed else "-"
                rendered.append(value if value not in (None, "") else "-")
            rows.append(rendered)
        tables.append({"title": table.get("title") or "Details", "headers": headers, "rows": rows})
    applied = [
        ("Period", report.get("filters", {}).get("period_label") or ""),
    ]
    options = report.get("filter_options") or {}
    filters = report.get("filters") or {}
    for key, label in (("farmer", "Farmer"), ("mitra", "Mitra"), ("product", "Product"), ("status", "Status")):
        value = filters.get(key)
        if value and value != "all":
            if key == "farmer":
                value = next((x["label"] for x in options.get("farmers", []) if x["value"] == value), value)
            elif key == "mitra":
                value = next((x["label"] for x in options.get("mitras", []) if x["value"] == value), value)
            applied.append((label, value))
    if filters.get("q"):
        applied.append(("Search", filters["q"]))
    return {
        "title": report.get("title") or "Report",
        "scope_label": report.get("scope_label") or "UnnatFarm",
        "subtitle": report.get("subtitle") or "",
        "generated_on": report.get("generated_on") or "",
        "applied_filters": applied,
        "kpis": kpis,
        "tables": tables,
        "notice": report.get("notice") or "",
    }
