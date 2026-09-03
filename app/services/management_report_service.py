from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from bson import ObjectId

from app.extensions import mongo
from app.services.stage10_reporting_service import (
    build_financial_report,
    build_gst_report,
    build_inventory_report,
    build_management_overview,
    build_transaction_chains,
    resolve_report_scope,
)
from app.utils.timezone import business_today


MANAGEMENT_REPORT_ROLES = {"avpl_admin", "accounts"}
AVPL_NAV = [
    ("overview", "Overview"), ("procurement", "Procurement"), ("ufcs", "UFCs"),
    ("farmers", "Farmers"), ("mitras", "Mitras"), ("revenue", "Revenue"),
    ("sales", "Sales"), ("inventory", "Inventory"), ("payments", "Payments"),
    ("gst", "GST"), ("accounting", "Accounting"),
]
ACCOUNTS_NAV = [
    ("overview", "Overview"), ("purchases", "Purchases"), ("payables", "Payables"),
    ("sales", "Sales"), ("receivables", "Receivables"), ("payments", "Payments"),
    ("gst", "GST"), ("accounting", "Accounting"),
]
PERIODS = [
    ("today", "Today"), ("7d", "Last 7 Days"), ("this_month", "This Month"),
    ("last_month", "Last Month"), ("3m", "Last 3 Months"), ("fy", "This Financial Year"),
    ("all", "All Time"), ("custom", "Custom"),
]
STATUS_LABELS = {
    "requested": "Requested", "approved": "Approved", "dispatched": "Dispatched",
    "received": "Received", "completed": "Completed", "cancelled": "Cancelled",
    "unpaid": "Unpaid", "partially_paid": "Partially Paid", "paid": "Paid",
    "pending_confirmation": "Awaiting Confirmation", "processing": "Processing",
    "posted": "Posted", "prepared": "Prepared", "ready": "Ready",
}


def _dec(value, default="0"):
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _num(value):
    return float(_dec(value))


def _clean(value):
    return str(value or "").strip()


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _first(row, *fields, default=None):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            return value
    return default


def _date_value(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    for parser in (
        lambda: datetime.strptime(raw[:10], "%Y-%m-%d").date(),
        lambda: datetime.fromisoformat(raw.replace("Z", "+00:00")).date(),
    ):
        try:
            return parser()
        except Exception:
            pass
    return None


def _period_bounds(period, raw_from="", raw_to=""):
    today = business_today()
    period = _clean(period).lower() or "this_month"
    if period == "all": return None, None, "All Time"
    if period == "today": return today, today, "Today"
    if period == "7d": return today - timedelta(days=6), today, "Last 7 Days"
    if period == "this_month": return today.replace(day=1), today, "This Month"
    if period == "last_month":
        first = today.replace(day=1); end = first - timedelta(days=1)
        return end.replace(day=1), end, "Last Month"
    if period == "3m": return today - timedelta(days=89), today, "Last 3 Months"
    if period == "fy":
        year = today.year if today.month >= 4 else today.year - 1
        return date(year, 4, 1), today, f"FY {year}-{str(year+1)[-2:]}"
    if period == "custom":
        start, end = _date_value(raw_from), _date_value(raw_to)
        if start and end and end < start: start, end = end, start
        return start, end, "Custom"
    return today.replace(day=1), today, "This Month"


def parse_management_filters(args):
    period = _clean(args.get("period") or "this_month")
    start, end, label = _period_bounds(period, args.get("from"), args.get("to"))
    return {
        "period": period, "period_label": label, "from": start, "to": end,
        "from_text": start.isoformat() if start else _clean(args.get("from")),
        "to_text": end.isoformat() if end else _clean(args.get("to")),
        "centre": _clean(args.get("centre")), "farmer": _clean(args.get("farmer")),
        "mitra": _clean(args.get("mitra")), "product": _clean(args.get("product")),
        "status": _clean(args.get("status") or "all").lower(), "q": _clean(args.get("q")),
    }


def _row_date(row):
    for field in ("sale_date", "invoice_date", "payment_date", "received_at", "order_date", "created_at", "updated_at"):
        value = _date_value((row or {}).get(field))
        if value: return value
    return None


def _product_text(row):
    names = []
    for item in (row or {}).get("items") or []:
        if item.get("product_name"): names.append(str(item.get("product_name")))
    if row.get("product_name"): names.append(str(row.get("product_name")))
    return " · ".join(dict.fromkeys(names)) or "-"


def _farmer_key(row):
    return _clean(_first(row, "seller_farmer_user_id_str", "farmer_user_id_str", "farmer_user_id", "seller_farmer_user_id", "farmer_id"))


def _matches(row, filters):
    d = _row_date(row)
    if d and filters.get("from") and d < filters["from"]: return False
    if d and filters.get("to") and d > filters["to"]: return False
    centre = filters.get("centre")
    if centre:
        candidates = {_clean(row.get("centre_uid")), _clean(row.get("seller_centre_uid")), _clean((row.get("buyer") or {}).get("centre_uid"))}
        if centre not in candidates: return False
    farmer = filters.get("farmer")
    if farmer and farmer not in {_farmer_key(row), _clean(row.get("buyer_key"))}: return False
    mitra = filters.get("mitra")
    if mitra and mitra != _clean(row.get("mitra_uid") or row.get("mapped_mitra_uid")): return False
    product = filters.get("product")
    if product and product.lower() not in _product_text(row).lower(): return False
    status = filters.get("status")
    if status and status != "all" and status != _clean(row.get("status") or row.get("payment_status") or row.get("posting_status")).lower(): return False
    q = filters.get("q", "").lower()
    if q:
        haystack = " ".join(str(v) for v in [row.get("order_number"), row.get("po_number"), row.get("invoice_number"), row.get("document_number"), row.get("supplier_name"), row.get("centre_name"), row.get("centre_uid"), row.get("seller_farmer_name"), row.get("farmer_name"), row.get("payment_number"), _product_text(row)] if v not in (None, "")).lower()
        if q not in haystack: return False
    return True


def _money_balance(row):
    total = _dec(_first(row, "settlement_total", "grand_total", "total_amount", "invoice_total", default=0))
    paid = max(_dec(_first(row, "amount_paid", "paid_amount", default=0)), Decimal("0"))
    if paid > total: paid = total
    computed = max(total - paid, Decimal("0"))
    outstanding_raw = row.get("outstanding_amount")
    outstanding = computed if outstanding_raw in (None, "") else min(max(_dec(outstanding_raw), Decimal("0")), computed)
    if computed <= Decimal("0.02"): outstanding = Decimal("0")
    return float(total), float(paid), float(outstanding)


def _format_status(value):
    raw = _clean(value).lower()
    return STATUS_LABELS.get(raw, raw.replace("_", " ").title() if raw else "-")


def _options():
    centres = []
    for row in mongo.db.ufc_admin_master.find({}).sort("centre_uid", 1):
        uid = _clean(row.get("centre_uid")); name = _clean(row.get("name_of_enterprise") or row.get("centre_name") or row.get("name") or uid)
        if uid: centres.append({"value": uid, "label": f"{name} · {uid}"})
    farmers = []
    for row in mongo.db.farmer_master.find({}).sort("name", 1).limit(1500):
        key = _clean(row.get("linked_user_id") or row.get("_id")); name = _clean(row.get("name") or row.get("farmer_name") or row.get("full_name") or row.get("contact_no") or "Farmer")
        if key: farmers.append({"value": key, "label": name})
    mitras = []
    for row in mongo.db.ufc_mitra_master.find({}).sort("mitra_uid", 1):
        uid = _clean(row.get("mitra_uid")); name = _clean(row.get("name") or row.get("mitra_name") or uid)
        if uid: mitras.append({"value": uid, "label": f"{name} · {uid}"})
    products = set()
    for row in mongo.db.products.find({"is_deleted": {"$ne": True}}, {"name": 1, "product_name": 1}).limit(1500):
        value = _clean(row.get("name") or row.get("product_name"))
        if value: products.add(value)
    for row in mongo.db.farmer_produce_marketplace_listings.find({}, {"product_name": 1}).limit(1500):
        if row.get("product_name"): products.add(_clean(row.get("product_name")))
    return {"periods": PERIODS, "centres": centres, "farmers": farmers, "mitras": mitras, "products": sorted(products, key=str.lower), "statuses": [("all", "All Statuses")] + sorted(STATUS_LABELS.items(), key=lambda x: x[1])}


def _kpi(label, value, kind="count", note="", tone="neutral"):
    return {"label": label, "value": value, "kind": kind, "note": note, "tone": tone}


def _table(title, columns, rows, empty="No records for the selected filters."):
    return {"title": title, "columns": columns, "rows": rows, "empty": empty}


def _fetch(collection, query=None, limit=2500):
    return list(mongo.db[collection].find(query or {}).sort("created_at", -1).limit(limit))


def _supplier_invoice_rows(filters):
    rows = []
    for inv in _fetch("avpl_supplier_invoices", {"status": {"$ne": "cancelled"}}):
        if not _matches(inv, filters): continue
        total, paid, outstanding = _money_balance(inv)
        rows.append({"date": _row_date(inv), "supplier": inv.get("supplier_name") or "Supplier", "reference": inv.get("official_purchase_invoice_number") or inv.get("supplier_invoice_number") or inv.get("internal_reference") or "-", "po": inv.get("po_number") or "-", "total": total, "paid": paid, "outstanding": outstanding, "status": _format_status(inv.get("posting_status") or inv.get("payment_status"))})
    return rows


def _farmer_purchase_rows(filters):
    rows = []
    for purchase in _fetch("farmer_marketplace_purchase_entries", {"buyer_type": "avpl", "status": {"$ne": "cancelled"}}):
        if not _matches(purchase, filters):
            continue
        total, paid, outstanding = _money_balance(purchase)
        rows.append({
            "date": _row_date(purchase),
            "supplier": purchase.get("seller_farmer_name") or "Farmer",
            "reference": purchase.get("purchase_number") or purchase.get("document_number") or purchase.get("order_number") or "-",
            "po": purchase.get("order_number") or "-",
            "products": _product_text(purchase),
            "total": total,
            "paid": paid,
            "outstanding": outstanding,
            "status": _format_status(purchase.get("payment_status") or purchase.get("status")),
        })
    return rows


def _financial_rows(filters, scope, nature=None):
    stage_filters = {
        "from": filters.get("from"), "to": filters.get("to"), "q": filters.get("q"),
        "status": filters.get("status"), "centre_uid": filters.get("centre"),
    }
    data = build_financial_report(scope, stage_filters)
    result = []
    for row in data.get("rows") or []:
        if nature and row.get("nature") != nature:
            continue
        if filters.get("centre") and _clean(row.get("centre_uid")) != filters.get("centre"):
            continue
        total = max(_num(row.get("total")), 0.0)
        paid = max(min(_num(row.get("paid")), total), 0.0)
        arithmetic = max(total - paid, 0.0)
        outstanding = min(max(_num(row.get("outstanding")), 0.0), arithmetic)
        if arithmetic <= 0.02:
            outstanding = 0.0
        result.append({
            "date": _date_value(row.get("invoice_date")),
            "due": _date_value(row.get("due_date")),
            "source": row.get("source") or "-",
            "party": row.get("party") or "-",
            "reference": row.get("reference") or "-",
            "total": total, "paid": paid, "outstanding": outstanding,
            "status": _format_status("paid" if outstanding <= 0.02 else row.get("payment_status")),
            "aging": "Paid" if outstanding <= 0.02 else (row.get("aging") or "Current"),
            "nature": row.get("nature") or "",
        })
    result.sort(key=lambda r: (-r["outstanding"], str(r.get("due") or "")))
    return result


def _payment_rows(filters):
    rows = []
    for p in _fetch("payments", {}):
        if not _matches(p, filters):
            continue
        payer = p.get("payer_name") or p.get("payer_display_name") or p.get("payer_key") or "-"
        payee = p.get("payee_name") or p.get("payee_display_name") or p.get("payee_key") or "-"
        payer_role = _clean(p.get("payer_role") or p.get("payer_type")).lower()
        payee_role = _clean(p.get("payee_role") or p.get("payee_type")).lower()
        direction = "Received" if payee_role in {"avpl", "avpl_admin", "accounts"} else ("Paid" if payer_role in {"avpl", "avpl_admin", "accounts"} else "System")
        rows.append({
            "date": _row_date(p), "reference": p.get("payment_number") or p.get("reference") or "-",
            "payer": payer, "payee": payee, "party": f"{payer} → {payee}",
            "source": _clean(p.get("source_type")).replace("_", " ").title(), "amount": _num(p.get("amount")),
            "mode": _clean(p.get("payment_mode")).replace("_", " ").title() or "-",
            "status": _format_status(p.get("status")), "direction": direction,
        })
    return rows


def _ufc_performance(filters):
    centres = { _clean(r.get("centre_uid")): _clean(r.get("name_of_enterprise") or r.get("centre_name") or r.get("name") or r.get("centre_uid")) for r in mongo.db.ufc_admin_master.find({}) if _clean(r.get("centre_uid")) }
    grouped = {uid: {"centre_uid": uid, "centre": name, "orders": 0, "sales": 0.0, "received": 0.0, "outstanding": 0.0, "farmers": 0} for uid, name in centres.items()}
    for farmer in mongo.db.farmer_master.find({}):
        uid = _clean(farmer.get("centre_uid") or farmer.get("mapped_centre_uid"))
        if uid in grouped: grouped[uid]["farmers"] += 1
    for order in _fetch("avpl_ufc_orders", {"status": {"$ne": "cancelled"}}):
        if not _matches(order, filters): continue
        uid = _clean(order.get("centre_uid")); grouped.setdefault(uid, {"centre_uid": uid, "centre": order.get("centre_name") or uid or "UFC", "orders": 0, "sales": 0.0, "received": 0.0, "outstanding": 0.0, "farmers": 0})["orders"] += 1
    for inv in _fetch("avpl_sales_invoices", {"status": {"$ne": "cancelled"}}):
        if not _matches(inv, filters): continue
        uid = _clean(inv.get("centre_uid") or (inv.get("buyer") or {}).get("centre_uid")); row = grouped.setdefault(uid, {"centre_uid": uid, "centre": inv.get("centre_name") or uid or "UFC", "orders": 0, "sales": 0.0, "received": 0.0, "outstanding": 0.0, "farmers": 0})
        total, paid, outstanding = _money_balance(inv); row["sales"] += total; row["received"] += paid; row["outstanding"] += outstanding
    rows = [r for r in grouped.values() if not filters.get("centre") or r["centre_uid"] == filters["centre"]]
    rows.sort(key=lambda r: (-r["sales"], r["centre"]))
    return rows


def _farmer_performance(filters):
    masters = {}
    for farmer in mongo.db.farmer_master.find({}):
        keys = {_clean(farmer.get("_id")), _clean(farmer.get("linked_user_id"))}
        info = {"farmer": _clean(farmer.get("name") or farmer.get("farmer_name") or farmer.get("full_name") or farmer.get("contact_no") or "Farmer"), "centre": _clean(farmer.get("centre_uid") or farmer.get("mapped_centre_uid")), "mitra": _clean(farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid"))}
        for key in keys:
            if key: masters[key] = info
    grouped = {}
    for sale in _fetch("farmer_marketplace_sales", {"status": {"$ne": "cancelled"}}):
        if not _matches(sale, filters): continue
        key = _farmer_key(sale); info = masters.get(key, {}); row = grouped.setdefault(key, {"farmer": sale.get("seller_farmer_name") or info.get("farmer") or "Farmer", "centre": sale.get("seller_centre_uid") or info.get("centre") or "-", "mitra": info.get("mitra") or "-", "transactions": 0, "sales": 0.0, "received": 0.0, "pending": 0.0})
        total, paid, outstanding = _money_balance(sale); row["transactions"] += 1; row["sales"] += total; row["received"] += paid; row["pending"] += outstanding
    rows = list(grouped.values()); rows.sort(key=lambda r: (-r["sales"], r["farmer"]))
    return rows


def _mitra_performance(filters):
    mitras = { _clean(m.get("mitra_uid")): _clean(m.get("name") or m.get("mitra_name") or m.get("mitra_uid")) for m in mongo.db.ufc_mitra_master.find({}) if _clean(m.get("mitra_uid")) }
    farmers_per = defaultdict(int)
    for f in mongo.db.farmer_master.find({}): farmers_per[_clean(f.get("mitra_uid") or f.get("mapped_mitra_uid"))] += 1
    grouped = {uid: {"mitra": name, "mitra_uid": uid, "farmers": farmers_per[uid], "transactions": 0, "business": 0.0, "avpl_earning": 0.0, "farmer_earning": 0.0, "earning": 0.0} for uid, name in mitras.items()}
    for collection, earning_key in (("pos_sales", "avpl_earning"), ("farmer_product_sales", "farmer_earning")):
        for sale in _fetch(collection, {}):
            if not _matches(sale, filters): continue
            uid = _clean(sale.get("mitra_uid"));
            if filters.get("mitra") and uid != filters["mitra"]: continue
            row = grouped.setdefault(uid, {"mitra": uid or "Mitra", "mitra_uid": uid, "farmers": farmers_per[uid], "transactions": 0, "business": 0.0, "avpl_earning": 0.0, "farmer_earning": 0.0, "earning": 0.0})
            business = _num(_first(sale, "grand_total", "total_amount", "amount", default=0)); bonus = _num(sale.get("bonus_amount")); row["transactions"] += 1; row["business"] += business; row[earning_key] += bonus; row["earning"] += bonus
    rows = list(grouped.values()); rows.sort(key=lambda r: (-r["business"], r["mitra"]))
    return rows


def _sales_rows(filters):
    rows = []
    for sale in _fetch("avpl_ufc_sales", {}):
        if not _matches(sale, filters): continue
        total = _num(_first(sale, "settlement_total", "grand_total", "total_amount", default=0)); cogs = _num(sale.get("estimated_cogs")); margin = _num(sale.get("gross_margin_amount")) if sale.get("gross_margin_amount") is not None else total - cogs
        rows.append({"date": _row_date(sale), "reference": sale.get("sale_number") or sale.get("order_number") or "-", "ufc": sale.get("centre_name") or sale.get("centre_uid") or "UFC", "products": _product_text(sale), "sales": total, "cogs": cogs, "margin": margin, "status": _format_status(sale.get("status") or "completed")})
    return rows


def _build_report(role, section, filters, scope):
    nav = AVPL_NAV if role == "avpl_admin" else ACCOUNTS_NAV
    valid = {k for k, _ in nav}
    if section not in valid: raise ValueError("Unknown management report section.")
    title_map = {
        "overview": "Management Overview" if role == "avpl_admin" else "Financial Overview", "procurement": "Procurement", "ufcs": "UFC Performance", "farmers": "Farmer Performance", "mitras": "Mitra Performance", "revenue": "AVPL Revenue", "sales": "Sales", "inventory": "Inventory", "payments": "Payments", "accounting": "Accounting Control", "purchases": "Purchases", "payables": "Payables", "receivables": "Receivables", "gst": "GST / HSN",
    }
    report = {"title": title_map[section], "subtitle": "Filtered, decision-ready information without accounting clutter.", "scope_label": "AVPL · All Centres", "section": section, "nav": nav, "filters": filters, "filter_options": _options(), "show_filters": {"centre": True, "farmer": section in {"farmers", "mitras", "overview"}, "mitra": section in {"mitras", "farmers", "overview"}, "product": section not in {"accounting", "payments", "payables", "receivables"}, "status": section in {"procurement", "purchases", "payables", "sales", "payments", "accounting"}, "q": True}, "kpis": [], "tables": [], "trend": None, "notice": "", "exportable": True, "management_mode": True}
    stage_filters = {"from": filters.get("from"), "to": filters.get("to"), "q": filters.get("q"), "status": filters.get("status"), "centre_uid": filters.get("centre")}

    if section == "overview":
        overview = build_management_overview(scope, stage_filters); k = overview["kpis"]
        payments = _payment_rows(filters)
        if role == "accounts":
            receivables = _financial_rows(filters, scope, "receivable")
            payables = _financial_rows(filters, scope, "payable")
            chains = build_transaction_chains(scope, stage_filters)
            report["kpis"] = [
                _kpi("Receivables", sum(r["outstanding"] for r in receivables), "money", tone="warning"),
                _kpi("Payables", sum(r["outstanding"] for r in payables), "money", tone="warning"),
                _kpi("Payments", sum(r["amount"] for r in payments), "money"),
                _kpi("Awaiting Confirmation", sum(1 for r in payments if r["status"] == "Awaiting Confirmation"), tone="warning"),
                _kpi("Open Items", sum(1 for r in receivables + payables if r["outstanding"] > .02)),
                _kpi("Chains Need Attention", chains["summary"]["needs_attention"], tone="warning"),
            ]
            report["tables"] = [
                _table("Open Receivables", [("source", "Type", "text"), ("party", "Collect From", "text"), ("reference", "Reference", "text"), ("outstanding", "Outstanding", "money"), ("aging", "Age", "text")], [r for r in receivables if r["outstanding"] > .02][:20]),
                _table("Open Payables", [("source", "Type", "text"), ("party", "Pay To", "text"), ("reference", "Reference", "text"), ("outstanding", "Outstanding", "money"), ("aging", "Age", "text")], [r for r in payables if r["outstanding"] > .02][:20]),
                _table("Recent Payments", [("date", "Date", "date"), ("payer", "From", "text"), ("payee", "To", "text"), ("amount", "Amount", "money"), ("status", "Status", "status")], payments[:20]),
            ]
        else:
            report["kpis"] = [_kpi("Supplier Purchases", k["supplier_purchases"], "money"), _kpi("AVPL → UFC Sales", k["avpl_ufc_sales"], "money"), _kpi("Receivables", k["receivables"], "money", tone="warning"), _kpi("Payables", k["payables"], "money", tone="warning"), _kpi("Gross Margin", k["estimated_gross_margin"], "money"), _kpi("Pending Orders", k["pending_orders"])]
            ufc_rows = _ufc_performance(filters)[:25]
            report["tables"] = [_table("UFC Snapshot", [("centre", "UFC", "text"), ("farmers", "Farmers", "text"), ("orders", "Orders", "text"), ("sales", "Sales", "money"), ("received", "Collected", "money"), ("outstanding", "Outstanding", "money")], ufc_rows), _table("Recent Payments", [("date", "Date", "date"), ("payer", "From", "text"), ("payee", "To", "text"), ("source", "Type", "text"), ("amount", "Amount", "money"), ("status", "Status", "status")], payments[:25])]
    elif section in {"procurement", "purchases"}:
        supplier_rows = _supplier_invoice_rows(filters)
        farmer_rows = _farmer_purchase_rows(filters)
        combined = supplier_rows + farmer_rows
        total = sum(r["total"] for r in combined); paid = sum(r["paid"] for r in combined); due = sum(r["outstanding"] for r in combined)
        report["kpis"] = [_kpi("Purchase Value", total, "money"), _kpi("Paid", paid, "money"), _kpi("Outstanding", due, "money", tone="warning"), _kpi("Purchases", len(combined))]
        report["tables"] = [
            _table("Supplier Purchases", [("date", "Date", "date"), ("supplier", "Supplier", "text"), ("po", "PO", "text"), ("reference", "Invoice", "text"), ("total", "Value", "money"), ("paid", "Paid", "money"), ("outstanding", "Outstanding", "money"), ("status", "Status", "status")], supplier_rows),
            _table("Farmer Produce Purchases", [("date", "Date", "date"), ("supplier", "Farmer", "text"), ("po", "Order", "text"), ("reference", "Purchase", "text"), ("products", "Produce", "text"), ("total", "Value", "money"), ("paid", "Paid", "money"), ("outstanding", "Outstanding", "money"), ("status", "Status", "status")], farmer_rows),
        ]
    elif section == "payables":
        rows = _financial_rows(filters, scope, "payable")
        total = sum(r["total"] for r in rows); paid = sum(r["paid"] for r in rows); due = sum(r["outstanding"] for r in rows)
        report["kpis"] = [_kpi("Payable Value", total, "money"), _kpi("Paid", paid, "money"), _kpi("Outstanding", due, "money", tone="warning"), _kpi("Open Items", sum(1 for r in rows if r["outstanding"] > .02))]
        report["tables"] = [_table("Payables", [("date", "Date", "date"), ("source", "Type", "text"), ("party", "Pay To", "text"), ("reference", "Reference", "text"), ("total", "Value", "money"), ("paid", "Paid", "money"), ("outstanding", "Outstanding", "money"), ("aging", "Age", "text"), ("status", "Status", "status")], rows)]
    elif section == "ufcs":
        rows = _ufc_performance(filters); report["kpis"] = [_kpi("UFCs", len(rows)), _kpi("Orders", sum(r["orders"] for r in rows)), _kpi("Sales", sum(r["sales"] for r in rows), "money"), _kpi("Collected", sum(r["received"] for r in rows), "money"), _kpi("Outstanding", sum(r["outstanding"] for r in rows), "money", tone="warning")]; report["tables"] = [_table("UFC-wise Performance", [("centre", "UFC", "text"), ("centre_uid", "UID", "text"), ("farmers", "Farmers", "text"), ("orders", "Orders", "text"), ("sales", "Sales", "money"), ("received", "Collected", "money"), ("outstanding", "Outstanding", "money")], rows)]
    elif section == "farmers":
        rows = _farmer_performance(filters); report["kpis"] = [_kpi("Selling Farmers", len(rows)), _kpi("Transactions", sum(r["transactions"] for r in rows)), _kpi("Farmer Sales", sum(r["sales"] for r in rows), "money"), _kpi("Money Received", sum(r["received"] for r in rows), "money"), _kpi("Pending", sum(r["pending"] for r in rows), "money", tone="warning")]; report["tables"] = [_table("Farmer-wise Performance", [("farmer", "Farmer", "text"), ("centre", "UFC", "text"), ("mitra", "Mitra", "text"), ("transactions", "Sales", "text"), ("sales", "Sales Value", "money"), ("received", "Received", "money"), ("pending", "Pending", "money")], rows)]
    elif section == "mitras":
        rows = _mitra_performance(filters); report["kpis"] = [_kpi("Mitras", len(rows)), _kpi("Farmers Served", sum(r["farmers"] for r in rows)), _kpi("Business Generated", sum(r["business"] for r in rows), "money"), _kpi("Mitra Earnings", sum(r["earning"] for r in rows), "money")]; report["notice"] = "Paid / pending Mitra commission is not shown because the current system does not maintain a dedicated Mitra commission settlement ledger."; report["tables"] = [_table("Mitra-wise Performance", [("mitra", "Mitra", "text"), ("mitra_uid", "UID", "text"), ("farmers", "Farmers", "text"), ("transactions", "Transactions", "text"), ("business", "Business", "money"), ("avpl_earning", "AVPL-side Earning", "money"), ("farmer_earning", "Farmer-side Earning", "money"), ("earning", "Total Earning", "money")], rows)]
    elif section in {"revenue", "sales"}:
        rows = _sales_rows(filters); sales = sum(r["sales"] for r in rows); cogs = sum(r["cogs"] for r in rows); margin = sum(r["margin"] for r in rows)
        report["kpis"] = [_kpi("Sales Revenue", sales, "money"), _kpi("COGS", cogs, "money"), _kpi("Gross Margin", margin, "money"), _kpi("Sales", len(rows))]; report["tables"] = [_table("AVPL Sales", [("date", "Date", "date"), ("reference", "Reference", "text"), ("ufc", "UFC", "text"), ("products", "Products", "text"), ("sales", "Revenue", "money"), ("cogs", "COGS", "money"), ("margin", "Margin", "money"), ("status", "Status", "status")], rows)]
    elif section == "inventory":
        inv = build_inventory_report(scope, stage_filters); rows = [{"product": r.get("product_name"), "code": r.get("product_code"), "physical": r.get("physical"), "reserved": r.get("reserved"), "saleable": r.get("saleable"), "damaged": r.get("damaged"), "expiring": r.get("expiring_30d"), "value": r.get("stock_value")} for r in inv.get("rows", [])]
        report["kpis"] = [_kpi("Stock Value", inv["summary"]["stock_value"], "money"), _kpi("Physical", inv["summary"]["physical"]), _kpi("Reserved", inv["summary"]["reserved"]), _kpi("Saleable", inv["summary"]["saleable"]), _kpi("Expiring ≤30 Days", inv["summary"]["expiring_30d"], tone="warning")]; report["tables"] = [_table("Inventory", [("product", "Product", "text"), ("code", "Code", "text"), ("physical", "Physical", "text"), ("reserved", "Reserved", "text"), ("saleable", "Saleable", "text"), ("damaged", "Damaged", "text"), ("expiring", "Expiring", "text"), ("value", "Stock Value", "money")], rows)]
    elif section == "payments":
        rows = _payment_rows(filters)
        report["kpis"] = [
            _kpi("Payments", len(rows)),
            _kpi("Received by AVPL", sum(r["amount"] for r in rows if r["direction"] == "Received"), "money"),
            _kpi("Paid by AVPL", sum(r["amount"] for r in rows if r["direction"] == "Paid"), "money"),
            _kpi("Awaiting Confirmation", sum(1 for r in rows if r["status"] == "Awaiting Confirmation"), tone="warning"),
        ]
        report["tables"] = [_table("Payments", [("date", "Date", "date"), ("reference", "Reference", "text"), ("payer", "From", "text"), ("payee", "To", "text"), ("source", "Type", "text"), ("amount", "Amount", "money"), ("mode", "Mode", "text"), ("status", "Status", "status")], rows)]
    elif section == "receivables":
        rows = _financial_rows(filters, scope, "receivable")
        total = sum(r["total"] for r in rows); received = sum(r["paid"] for r in rows); due = sum(r["outstanding"] for r in rows)
        report["kpis"] = [_kpi("Receivable Value", total, "money"), _kpi("Collected", received, "money"), _kpi("Outstanding", due, "money", tone="warning"), _kpi("Open Items", sum(1 for r in rows if r["outstanding"] > .02))]
        report["tables"] = [_table("Receivables", [("date", "Date", "date"), ("source", "Type", "text"), ("party", "Collect From", "text"), ("reference", "Reference", "text"), ("total", "Value", "money"), ("paid", "Collected", "money"), ("outstanding", "Outstanding", "money"), ("aging", "Age", "text"), ("status", "Status", "status")], rows)]
    elif section == "gst":
        gst = build_gst_report(scope, stage_filters); report["kpis"] = [_kpi("Documents", gst["summary"]["documents"]), _kpi("Taxable Value", gst["summary"]["taxable"], "money"), _kpi("CGST", gst["summary"]["cgst"], "money"), _kpi("SGST", gst["summary"]["sgst"], "money"), _kpi("IGST", gst["summary"]["igst"], "money")]; report["tables"] = [_table("GST / HSN", [("source", "Source", "text"), ("reference", "Reference", "text"), ("hsn", "HSN", "text"), ("product_name", "Product", "text"), ("taxable", "Taxable", "money"), ("gst", "GST", "money")], gst.get("rows", []))]
    elif section == "accounting":
        chains = build_transaction_chains(scope, stage_filters); rows = [{"flow": r.get("flow"), "reference": r.get("reference"), "party": r.get("party") or r.get("centre_uid") or "-", "amount": r.get("amount") or r.get("value") or 0, "status": "Complete" if r.get("complete") else "Needs Attention", "warning": r.get("warning") or "-"} for r in chains.get("rows", [])]; report["kpis"] = [_kpi("Chains Checked", chains["summary"]["total"]), _kpi("Complete", chains["summary"]["complete"]), _kpi("Needs Attention", chains["summary"]["needs_attention"], tone="warning")]; report["tables"] = [_table("Transaction Chains", [("flow", "Flow", "text"), ("reference", "Reference", "text"), ("party", "Party", "text"), ("amount", "Value", "money"), ("status", "Status", "status"), ("warning", "Issue", "text")], rows)]
    return report


def build_management_report(actor_user_id, role, section, filters):
    role = _clean(role).lower()
    if role not in MANAGEMENT_REPORT_ROLES: raise PermissionError("Management reports are available only to AVPL Admin and Accounts.")
    scope = resolve_report_scope(actor_user_id, role=role)
    return _build_report(role, section, filters, scope)


def export_rows(report):
    def display_value(value, kind):
        if kind == "money": return f"₹{_num(value):,.2f}"
        if kind == "date":
            d = _date_value(value); return d.strftime("%d %b %Y") if d else ""
        return value if value not in (None, "") else "-"
    return {
        "title": report.get("title"), "subtitle": report.get("subtitle"), "scope_label": report.get("scope_label"),
        "generated_on": datetime.now().strftime("%d %b %Y %I:%M %p"),
        "applied_filters": [("Period", report["filters"].get("period_label")), ("UFC", report["filters"].get("centre") or "All"), ("Farmer", report["filters"].get("farmer") or "All"), ("Mitra", report["filters"].get("mitra") or "All"), ("Product", report["filters"].get("product") or "All"), ("Status", report["filters"].get("status") or "All")],
        "kpis": [(k.get("label"), f"₹{_num(k.get('value')):,.2f}" if k.get("kind") == "money" else k.get("value")) for k in report.get("kpis", [])],
        "tables": [{"title": table.get("title"), "headers": [label for _key, label, _kind in table.get("columns", [])], "rows": [[display_value(row.get(key), kind) for key, _label, kind in table.get("columns", [])] for row in table.get("rows", [])]} for table in report.get("tables", [])],
        "notice": report.get("notice") or "",
    }
