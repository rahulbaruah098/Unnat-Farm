from __future__ import annotations
from app.utils.timezone import business_today

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from bson import ObjectId
from pymongo import DESCENDING

from app.extensions import mongo


MANAGEMENT_ROLES = {"super_admin", "avpl_admin", "accounts"}
ALL_REPORT_ROLES = MANAGEMENT_ROLES | {"ufc_admin", "farmer"}
TOLERANCE = Decimal("0.02")
MAX_REPORT_ROWS = 2500


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


def _money(value: Any) -> str:
    return f"{_dec(value).quantize(Decimal('0.01')):.2f}"


def _qty(value: Any) -> str:
    number = _dec(value).quantize(Decimal("0.0001"))
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _as_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except Exception:
        return None


def _first(doc, *fields, default=None):
    for field in fields:
        value = doc.get(field)
        if value not in (None, ""):
            return value
    return default


def _date_in_range(doc, start_date=None, end_date=None, fields=("created_at",)):
    if not start_date and not end_date:
        return True
    value = _as_date(_first(doc, *fields))
    if not value:
        return True
    if start_date and value < start_date:
        return False
    if end_date and value > end_date:
        return False
    return True


def _matches_search(doc, search, fields):
    term = str(search or "").strip().lower()
    if not term:
        return True
    for field in fields:
        value = doc.get(field)
        if isinstance(value, dict):
            value = " ".join(str(v) for v in value.values())
        if term in str(value or "").lower():
            return True
    return False


def _resolve_user(user_id):
    oid = _oid(user_id)
    if not oid:
        return {}
    return mongo.db.users.find_one({"_id": oid}) or {}


def resolve_report_scope(user_id, role=None, centre_uid_hint="", mitra_uid_hint=""):
    user = _resolve_user(user_id)
    role = str(role or user.get("role") or "").strip()
    centre_uid = str(
        centre_uid_hint
        or user.get("centre_uid")
        or user.get("mapped_centre_uid")
        or user.get("center_uid")
        or user.get("mapped_center_uid")
        or ""
    ).strip()
    mitra_uid = str(mitra_uid_hint or user.get("mitra_uid") or user.get("mapped_mitra_uid") or "").strip()
    user_oid = user.get("_id") or _oid(user_id)
    user_str = str(user_oid or user_id or "").strip()
    farmer = {}

    if role == "ufc_admin" and not centre_uid:
        master = mongo.db.ufc_admin_master.find_one({"$or": [{"linked_user_id": user_oid}, {"linked_user_id": user_str}]}) or {}
        centre_uid = str(master.get("centre_uid") or master.get("mapped_centre_uid") or "").strip()

    if role == "ufc_mitra":
        master = mongo.db.ufc_mitra_master.find_one({"$or": [{"linked_user_id": user_oid}, {"linked_user_id": user_str}, {"mitra_uid": mitra_uid}]}) or {}
        mitra_uid = str(mitra_uid or master.get("mitra_uid") or "").strip()
        centre_uid = str(centre_uid or master.get("mapped_centre_uid") or master.get("centre_uid") or "").strip()

    if role == "farmer":
        clauses = []
        if user_oid:
            clauses.extend([{"linked_user_id": user_oid}, {"linked_user_id": user_str}])
        phone = str(user.get("phone") or user.get("contact_no") or "").strip()
        if phone:
            clauses.append({"contact_no": phone})
        if clauses:
            farmer = mongo.db.farmer_master.find_one({"$or": clauses}) or {}
        centre_uid = str(centre_uid or farmer.get("centre_uid") or farmer.get("mapped_centre_uid") or "").strip()
        mitra_uid = str(mitra_uid or farmer.get("mitra_uid") or farmer.get("mapped_mitra_uid") or "").strip()

    return {
        "role": role,
        "is_management": role in MANAGEMENT_ROLES,
        "user": user,
        "user_id": user_oid,
        "user_id_str": user_str,
        "centre_uid": centre_uid,
        "mitra_uid": mitra_uid,
        "farmer": farmer,
        "farmer_id": farmer.get("_id"),
        "farmer_id_str": str(farmer.get("_id") or ""),
        "farmer_phone": str(farmer.get("contact_no") or user.get("phone") or "").strip(),
    }


def parse_report_filters(args):
    start_date = _as_date(args.get("from"))
    end_date = _as_date(args.get("to"))
    if start_date and end_date and end_date < start_date:
        start_date, end_date = end_date, start_date
    return {
        "from": start_date,
        "to": end_date,
        "from_text": start_date.isoformat() if start_date else "",
        "to_text": end_date.isoformat() if end_date else "",
        "q": str(args.get("q") or "").strip(),
        "status": str(args.get("status") or "all").strip().lower(),
        "centre_uid": str(args.get("centre_uid") or "").strip(),
    }


def _scope_query(scope, *, centre_field="centre_uid", farmer_field="farmer_user_id", mitra_field="mitra_uid"):
    if scope["is_management"]:
        return {}
    if scope["role"] == "ufc_admin":
        return {centre_field: scope.get("centre_uid") or "__NO_CENTRE__"}
    if scope["role"] == "ufc_mitra":
        if mitra_field and scope.get("mitra_uid"):
            return {mitra_field: scope["mitra_uid"]}
        return {centre_field: scope.get("centre_uid") or "__NO_CENTRE__"}
    if scope["role"] == "farmer":
        values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]
        return {farmer_field: {"$in": values or ["__NO_USER__"]}}
    return {}


def _collection_rows(collection_name, query=None, projection=None, sort_field="created_at", limit=MAX_REPORT_ROWS):
    try:
        cursor = mongo.db[collection_name].find(query or {}, projection).sort(sort_field, DESCENDING).limit(limit)
        return list(cursor)
    except Exception:
        try:
            return list(mongo.db[collection_name].find(query or {}, projection).limit(limit))
        except Exception:
            return []


def _sum_rows(rows, *fields):
    total = Decimal("0")
    for row in rows:
        total += _dec(_first(row, *fields, default=0))
    return float(total)


def _count_status(collection, query, statuses):
    q = dict(query or {})
    q["status"] = {"$in": list(statuses)}
    return mongo.db[collection].count_documents(q)


def _inventory_scope(scope):
    if scope["is_management"] or scope["role"] in {"sales_nelocals", "sales_unnatfarm"}:
        return "AVPL", "avpl_inventory_lots", {}
    if scope["role"] in {"ufc_admin", "ufc_mitra"}:
        return "UFC", "ufc_inventory_lots", {"centre_uid": scope.get("centre_uid") or "__NO_CENTRE__"}
    if scope["role"] == "farmer":
        values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]
        return "Farmer Produce", "farmer_produce_lots", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}
    return "Inventory", "avpl_inventory_lots", {"_id": None}


def build_inventory_report(scope, filters=None):
    filters = filters or {}
    label, collection_name, query = _inventory_scope(scope)
    if filters.get("centre_uid") and scope["is_management"] and collection_name == "ufc_inventory_lots":
        query["centre_uid"] = filters["centre_uid"]
    query["status"] = {"$nin": ["cancelled"]}
    rows = _collection_rows(collection_name, query, limit=MAX_REPORT_ROWS)
    grouped = {}
    today = business_today()
    expiring_cutoff = today + timedelta(days=30)

    for lot in rows:
        if not _matches_search(lot, filters.get("q"), ("product_name", "product_code", "batch_number", "warehouse_name", "centre_uid")):
            continue
        product_key = str(lot.get("source_product_id") or lot.get("product_key") or lot.get("product_name") or "Unknown")
        row = grouped.setdefault(product_key, {
            "product_name": lot.get("product_name") or "Unknown Product",
            "product_code": lot.get("product_code") or lot.get("product_key") or "-",
            "unit_code": lot.get("unit_code") or "Unit",
            "centre_uid": lot.get("centre_uid") or "",
            "physical": 0.0,
            "reserved": 0.0,
            "saleable": 0.0,
            "damaged": 0.0,
            "blocked": 0.0,
            "expired": 0.0,
            "expiring_30d": 0.0,
            "stock_value": 0.0,
            "batches": 0,
        })
        physical = max(_num(lot.get("available_quantity")), 0)
        reserved = min(max(_num(lot.get("reserved_quantity")), 0), physical)
        damaged = min(max(_num(lot.get("damaged_quantity")), 0), physical)
        blocked = min(max(_num(lot.get("blocked_quantity")), 0), physical)
        expiry = _as_date(lot.get("expiry_date"))
        expired = physical if expiry and expiry < today else 0.0
        expiring = physical if expiry and today <= expiry <= expiring_cutoff else 0.0
        unusable = expired > 0 or str(lot.get("status") or "").lower() in {"expired", "cancelled"}
        saleable = 0.0 if unusable else max(physical - reserved - damaged - blocked, 0)
        unit_cost = _num(_first(lot, "weighted_average_cost", "average_purchase_cost", "last_purchase_price", "unit_cost", default=0))
        if not unit_cost and _num(lot.get("received_quantity")) > 0 and _num(lot.get("purchase_cost_total")) > 0:
            unit_cost = _num(lot.get("purchase_cost_total")) / max(_num(lot.get("received_quantity")), 1)

        row["physical"] += physical
        row["reserved"] += reserved
        row["saleable"] += saleable
        row["damaged"] += damaged
        row["blocked"] += blocked
        row["expired"] += expired
        row["expiring_30d"] += expiring
        row["stock_value"] += physical * unit_cost
        row["batches"] += 1

    result_rows = sorted(grouped.values(), key=lambda x: (-x["stock_value"], x["product_name"]))
    for row in result_rows:
        for key in ("physical", "reserved", "saleable", "damaged", "blocked", "expired", "expiring_30d"):
            row[key] = round(row[key], 4)
        row["stock_value"] = round(row["stock_value"], 2)
        row["stock_value_display"] = _money(row["stock_value"])

    return {
        "scope_label": label,
        "collection": collection_name,
        "rows": result_rows,
        "summary": {
            "products": len(result_rows),
            "physical": round(sum(r["physical"] for r in result_rows), 4),
            "reserved": round(sum(r["reserved"] for r in result_rows), 4),
            "saleable": round(sum(r["saleable"] for r in result_rows), 4),
            "damaged": round(sum(r["damaged"] for r in result_rows), 4),
            "expired": round(sum(r["expired"] for r in result_rows), 4),
            "expiring_30d": round(sum(r["expiring_30d"] for r in result_rows), 4),
            "stock_value": round(sum(r["stock_value"] for r in result_rows), 2),
        },
    }


def _financial_sources_for_scope(scope):
    if scope["is_management"]:
        return [
            ("Supplier Payable", "payable", "avpl_supplier_invoices", {}, "supplier_name"),
            ("Farmer Produce Payable", "payable", "farmer_marketplace_payables", {"buyer_type": "avpl"}, "seller_farmer_name"),
            ("AVPL → UFC Receivable", "receivable", "avpl_receivables", {}, "centre_name"),
            ("UFC → Farmer Receivable", "receivable", "ufc_farmer_receivables", {}, "farmer_name"),
            ("Farmer Produce Receivable", "receivable", "farmer_marketplace_receivables", {}, "buyer_name"),
            ("Farmer Outside Sale Receivable", "receivable", "farmer_external_receivables", {}, "buyer_name"),
        ]
    if scope["role"] in {"ufc_admin", "ufc_mitra"}:
        centre = scope.get("centre_uid") or "__NO_CENTRE__"
        return [
            ("AVPL Payable", "payable", "ufc_payables", {"centre_uid": centre}, "seller_name"),
            ("Farmer Sales Receivable", "receivable", "ufc_farmer_receivables", {"centre_uid": centre}, "farmer_name"),
            ("Farmer Produce Payable", "payable", "farmer_marketplace_payables", {"buyer_type": "ufc", "buyer_key": centre}, "seller_farmer_name"),
        ]
    if scope["role"] == "farmer":
        values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]
        keys = [str(x) for x in values]
        return [
            ("Input Purchase Payable", "payable", "farmer_payables", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, "seller_name"),
            ("Produce Sale Receivable", "receivable", "farmer_marketplace_receivables", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, "buyer_name"),
            ("Produce Purchase Payable", "payable", "farmer_marketplace_payables", {"buyer_type": "farmer", "buyer_key": {"$in": keys or ["__NO_USER__"]}}, "seller_name"),
            ("Outside Sale Receivable", "receivable", "farmer_external_receivables", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, "buyer_name"),
            ("Outside Purchase Payable", "payable", "farmer_external_payables", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, "seller_name"),
        ]
    return []


def _aging_bucket(due_date, outstanding):
    if _dec(outstanding) <= 0:
        return "Paid"
    due = _as_date(due_date)
    if not due:
        return "Current"
    days = (business_today() - due).days
    if days <= 0:
        return "Current"
    if days <= 7:
        return "1–7 days"
    if days <= 30:
        return "8–30 days"
    if days <= 60:
        return "31–60 days"
    return "60+ days"


def build_financial_report(scope, filters=None):
    filters = filters or {}
    rows = []
    for source_label, nature, collection, query, party_field in _financial_sources_for_scope(scope):
        docs = _collection_rows(collection, query, limit=MAX_REPORT_ROWS)
        for doc in docs:
            if str(doc.get("status") or "").lower() in {"cancelled", "voided", "reversed"}:
                continue
            if not _date_in_range(doc, filters.get("from"), filters.get("to"), ("invoice_date", "sale_date", "created_at", "updated_at")):
                continue
            payment_status = str(doc.get("payment_status") or "unpaid").lower()
            if filters.get("status") not in (None, "", "all") and payment_status != filters["status"]:
                continue
            party = _first(doc, party_field, "supplier_name", "centre_name", "farmer_name", "buyer_name", "seller_name", default="-")
            reference = _first(doc, "invoice_number", "document_number", "supplier_invoice_number", "sale_number", "purchase_number", default="-")
            total = _dec(_first(doc, "total_amount", "grand_total", "invoice_total", default=0))
            paid = _dec(_first(doc, "amount_paid", "paid_amount", default=0))
            outstanding = _dec(_first(doc, "outstanding_amount", default=max(total - paid, Decimal("0"))))
            if not _matches_search({"party": party, "reference": reference, **doc}, filters.get("q"), ("party", "reference", "centre_uid", "product_name")):
                continue
            row = {
                "source": source_label,
                "nature": nature,
                "party": party,
                "reference": reference,
                "invoice_date": _first(doc, "invoice_date", "sale_date", "created_at", default=""),
                "due_date": doc.get("due_date") or "",
                "total": float(total),
                "paid": float(paid),
                "outstanding": float(outstanding),
                "payment_status": payment_status,
                "aging": _aging_bucket(doc.get("due_date"), outstanding),
                "centre_uid": doc.get("centre_uid") or (doc.get("buyer") or {}).get("centre_uid") or "",
                "collection": collection,
                "id": str(doc.get("_id") or ""),
            }
            rows.append(row)

    rows.sort(key=lambda x: (x["outstanding"], str(x.get("due_date") or "")), reverse=True)
    buckets = defaultdict(float)
    for row in rows:
        if row["outstanding"] > 0:
            buckets[row["aging"]] += row["outstanding"]
    receivable = sum(row["outstanding"] for row in rows if row["nature"] == "receivable")
    payable = sum(row["outstanding"] for row in rows if row["nature"] == "payable")
    return {
        "rows": rows,
        "summary": {
            "receivable": round(receivable, 2),
            "payable": round(payable, 2),
            "net": round(receivable - payable, 2),
            "open_items": sum(1 for row in rows if row["outstanding"] > 0),
            "overdue_items": sum(1 for row in rows if row["aging"] not in {"Current", "Paid"}),
            "paid_items": sum(1 for row in rows if row["outstanding"] <= 0),
        },
        "aging": {key: round(value, 2) for key, value in buckets.items()},
    }


def _payment_map(invoice_ids):
    ids = [value for value in invoice_ids if value]
    if not ids:
        return defaultdict(list)
    rows = _collection_rows("payments", {"invoice_id": {"$in": ids}}, limit=MAX_REPORT_ROWS)
    result = defaultdict(list)
    for row in rows:
        result[str(row.get("invoice_id") or "")].append(row)
    return result


def _related_by_id(collection, field, ids):
    ids = [x for x in ids if x]
    if not ids:
        return {}
    result = {}
    for row in _collection_rows(collection, {field: {"$in": ids}}, limit=MAX_REPORT_ROWS):
        result[str(row.get(field) or "")] = row
    return result


def build_transaction_chains(scope, filters=None):
    filters = filters or {}
    chains = []
    management = scope["is_management"]
    centre_uid = scope.get("centre_uid")
    user_values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]

    if management:
        po_query = {"status": {"$ne": "cancelled"}}
        pos = _collection_rows("avpl_purchase_orders", po_query, limit=600)
        po_ids = [row.get("_id") for row in pos]
        grns_by_po = defaultdict(list)
        for row in _collection_rows("avpl_goods_receipts", {"purchase_order_id": {"$in": po_ids}}, limit=2000):
            grns_by_po[str(row.get("purchase_order_id") or "")].append(row)
        inv_by_po = defaultdict(list)
        invoices = _collection_rows("avpl_supplier_invoices", {"purchase_order_id": {"$in": po_ids}}, limit=2000)
        for row in invoices:
            inv_by_po[str(row.get("purchase_order_id") or "")].append(row)
        pay_by_invoice = _payment_map([row.get("_id") for row in invoices])
        for po in pos:
            if not _date_in_range(po, filters.get("from"), filters.get("to"), ("order_date", "created_at")):
                continue
            if not _matches_search(po, filters.get("q"), ("po_number", "supplier_name", "status")):
                continue
            po_key = str(po.get("_id") or "")
            grns = grns_by_po.get(po_key, [])
            invs = inv_by_po.get(po_key, [])
            payments = sum(len(pay_by_invoice.get(str(inv.get("_id") or ""), [])) for inv in invs)
            blocking = sum(int(inv.get("blocking_mismatch_count") or 0) for inv in invs)
            complete = bool(grns and invs and blocking == 0 and all(inv.get("payable_posted") is True or inv.get("posting_status") == "posted" for inv in invs))
            chains.append({
                "flow": "Supplier → AVPL", "reference": po.get("po_number") or po_key,
                "party": po.get("supplier_name") or "Supplier", "status": po.get("status") or "-",
                "steps": [f"PO: {po.get('status') or '-'}", f"GRN: {len(grns)}", f"Invoice: {len(invs)}", f"Payments: {payments}"],
                "complete": complete, "warning": f"{blocking} blocking match issue(s)" if blocking else "",
                "date": po.get("order_date") or po.get("created_at"),
            })

    if management or scope["role"] in {"ufc_admin", "ufc_mitra"}:
        query = {"status": {"$ne": "cancelled"}}
        if not management:
            query["centre_uid"] = centre_uid or "__NO_CENTRE__"
        orders = _collection_rows("avpl_ufc_orders", query, limit=800)
        order_ids = [row.get("_id") for row in orders]
        sales = _related_by_id("avpl_ufc_sales", "avpl_ufc_order_id", order_ids)
        invoices = _related_by_id("avpl_sales_invoices", "avpl_ufc_order_id", order_ids)
        purchases = _related_by_id("ufc_purchase_entries", "avpl_ufc_order_id", order_ids)
        paymap = _payment_map([row.get("_id") for row in invoices.values()])
        for order in orders:
            if not _date_in_range(order, filters.get("from"), filters.get("to"), ("created_at", "dispatched_at", "received_at")):
                continue
            if not _matches_search(order, filters.get("q"), ("order_number", "centre_uid", "centre_name", "product_name", "status")):
                continue
            key = str(order.get("_id") or "")
            sale, invoice, purchase = sales.get(key), invoices.get(key), purchases.get(key)
            payments = paymap.get(str((invoice or {}).get("_id") or ""), [])
            terminal = order.get("status") == "received"
            complete = bool(not terminal or (sale and invoice and purchase))
            warning = "" if complete else "Received order is missing sale/invoice/purchase link"
            chains.append({
                "flow": "AVPL → UFC", "reference": order.get("order_number") or key,
                "party": f"{order.get('centre_name') or 'UFC'} ({order.get('centre_uid') or '-'})",
                "status": order.get("status") or "-",
                "steps": [f"Order: {order.get('status') or '-'}", f"Sale: {'Yes' if sale else 'No'}", f"Invoice: {'Yes' if invoice else 'No'}", f"UFC Purchase: {'Yes' if purchase else 'No'}", f"Payments: {len(payments)}"],
                "complete": complete, "warning": warning, "date": order.get("created_at"),
            })

    if management or scope["role"] in {"ufc_admin", "ufc_mitra", "farmer"}:
        query = {"status": {"$ne": "cancelled"}}
        if scope["role"] in {"ufc_admin", "ufc_mitra"}:
            query["centre_uid"] = centre_uid or "__NO_CENTRE__"
        elif scope["role"] == "farmer":
            query["farmer_user_id"] = {"$in": user_values or ["__NO_USER__"]}
        orders = _collection_rows("ufc_farmer_orders", query, limit=800)
        order_ids = [row.get("_id") for row in orders]
        sales = _related_by_id("ufc_farmer_sales", "ufc_farmer_order_id", order_ids)
        invoices = _related_by_id("ufc_farmer_sales_invoices", "ufc_farmer_order_id", order_ids)
        purchases = _related_by_id("farmer_purchase_entries", "ufc_farmer_order_id", order_ids)
        paymap = _payment_map([row.get("_id") for row in invoices.values()])
        for order in orders:
            if not _date_in_range(order, filters.get("from"), filters.get("to"), ("created_at", "delivered_at", "received_at")):
                continue
            if not _matches_search(order, filters.get("q"), ("order_number", "centre_uid", "farmer_name", "product_name", "status")):
                continue
            key = str(order.get("_id") or "")
            sale, invoice, purchase = sales.get(key), invoices.get(key), purchases.get(key)
            payments = paymap.get(str((invoice or {}).get("_id") or ""), [])
            terminal = order.get("status") in {"delivered", "received"}
            complete = bool(not terminal or (sale and invoice and purchase))
            chains.append({
                "flow": "UFC → Farmer", "reference": order.get("order_number") or key,
                "party": order.get("farmer_name") or "Farmer", "status": order.get("status") or "-",
                "steps": [f"Order: {order.get('status') or '-'}", f"Sale: {'Yes' if sale else 'No'}", f"Invoice: {'Yes' if invoice else 'No'}", f"Farmer Purchase: {'Yes' if purchase else 'No'}", f"Payments: {len(payments)}"],
                "complete": complete, "warning": "" if complete else "Delivered order is missing a linked financial/purchase record", "date": order.get("created_at"),
            })

    market_query = {"status": {"$ne": "cancelled"}}
    if scope["role"] == "farmer":
        market_query["$or"] = [
            {"seller_farmer_user_id": {"$in": user_values or ["__NO_USER__"]}},
            {"buyer_key": {"$in": [str(x) for x in user_values] or ["__NO_USER__"]}},
        ]
    elif scope["role"] in {"ufc_admin", "ufc_mitra"}:
        market_query["$or"] = [
            {"buyer_type": "ufc", "buyer.centre_uid": centre_uid or "__NO_CENTRE__"},
            {"centre_uid": centre_uid or "__NO_CENTRE__"},
        ]
    orders = _collection_rows("farmer_produce_marketplace_orders", market_query, limit=800)
    order_ids = [row.get("_id") for row in orders]
    sales = _related_by_id("farmer_marketplace_sales", "farmer_marketplace_order_id", order_ids)
    invoices = _related_by_id("farmer_marketplace_sales_invoices", "farmer_marketplace_order_id", order_ids)
    purchases = _related_by_id("farmer_marketplace_purchase_entries", "farmer_marketplace_order_id", order_ids)
    paymap = _payment_map([row.get("_id") for row in invoices.values()])
    for order in orders:
        if not _date_in_range(order, filters.get("from"), filters.get("to"), ("created_at", "dispatched_at", "received_at")):
            continue
        if not _matches_search(order, filters.get("q"), ("order_number", "seller_farmer_name", "product_name", "buyer_type", "status")):
            continue
        key = str(order.get("_id") or "")
        sale, invoice, purchase = sales.get(key), invoices.get(key), purchases.get(key)
        payments = paymap.get(str((invoice or {}).get("_id") or ""), [])
        terminal = order.get("status") == "received"
        complete = bool(not terminal or (sale and invoice and purchase))
        buyer = order.get("buyer") or {}
        chains.append({
            "flow": "Farmer Produce", "reference": order.get("order_number") or key,
            "party": f"{buyer.get('name') or order.get('buyer_type') or 'Buyer'} / {order.get('seller_farmer_name') or 'Farmer'}",
            "status": order.get("status") or "-",
            "steps": [f"Order: {order.get('status') or '-'}", f"Sale: {'Yes' if sale else 'No'}", f"Invoice: {'Yes' if invoice else 'No'}", f"Buyer Purchase: {'Yes' if purchase else 'No'}", f"Payments: {len(payments)}"],
            "complete": complete, "warning": "" if complete else "Received produce order is missing sale/invoice/purchase link", "date": order.get("created_at"),
        })

    chains.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return {
        "rows": chains,
        "summary": {
            "total": len(chains),
            "complete": sum(1 for row in chains if row["complete"]),
            "needs_attention": sum(1 for row in chains if not row["complete"]),
        },
    }


def _invoice_sources(scope):
    sources = []
    if scope["is_management"]:
        sources.extend([
            ("Supplier Invoice", "avpl_supplier_invoices", {}),
            ("AVPL Sales Invoice", "avpl_sales_invoices", {}),
            ("UFC Farmer Invoice", "ufc_farmer_sales_invoices", {}),
            ("Farmer Market Invoice", "farmer_marketplace_sales_invoices", {}),
            ("Farmer External Sale Invoice", "farmer_external_sales_invoices", {}),
        ])
    elif scope["role"] in {"ufc_admin", "ufc_mitra"}:
        centre = scope.get("centre_uid") or "__NO_CENTRE__"
        sources.extend([
            ("AVPL Sales Invoice", "avpl_sales_invoices", {"centre_uid": centre}),
            ("UFC Farmer Invoice", "ufc_farmer_sales_invoices", {"centre_uid": centre}),
        ])
    elif scope["role"] == "farmer":
        values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]
        sources.extend([
            ("UFC Farmer Invoice", "ufc_farmer_sales_invoices", {"buyer.farmer_user_id": {"$in": values or ["__NO_USER__"]}}),
            ("Farmer Market Invoice", "farmer_marketplace_sales_invoices", {"$or": [{"seller_farmer_user_id": {"$in": values or ["__NO_USER__"]}}, {"buyer_key": {"$in": [str(x) for x in values] or ["__NO_USER__"]}}]}),
            ("Farmer External Sale Invoice", "farmer_external_sales_invoices", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}),
        ])
    return sources


def _stock_reconciliation(scope):
    issues = []
    label, collection_name, query = _inventory_scope(scope)
    rows = _collection_rows(collection_name, query, limit=MAX_REPORT_ROWS)
    for lot in rows:
        available = _dec(lot.get("available_quantity"))
        reserved = _dec(lot.get("reserved_quantity"))
        damaged = _dec(lot.get("damaged_quantity"))
        blocked = _dec(lot.get("blocked_quantity"))
        received = _dec(lot.get("received_quantity"))
        issued = _dec(lot.get("issued_quantity"))
        adjusted_in = _dec(lot.get("adjusted_in_quantity"))
        adjusted_out = _dec(lot.get("adjusted_out_quantity"))
        expected = received + adjusted_in - issued - adjusted_out
        product = lot.get("product_name") or lot.get("product_key") or "Product"
        batch = lot.get("batch_number") or lot.get("lot_number") or "-"
        if available < -TOLERANCE:
            issues.append({"severity": "critical", "type": "Negative stock", "reference": f"{product} / {batch}", "message": f"Available quantity is {_qty(available)}."})
        if reserved - available > TOLERANCE:
            issues.append({"severity": "critical", "type": "Over-reserved stock", "reference": f"{product} / {batch}", "message": f"Reserved {_qty(reserved)} exceeds physical {_qty(available)}."})
        if damaged < 0 or blocked < 0:
            issues.append({"severity": "critical", "type": "Invalid stock classification", "reference": f"{product} / {batch}", "message": "Damaged/blocked quantity is negative."})
        if collection_name in {"avpl_inventory_lots", "ufc_inventory_lots"} and received > 0 and abs(available - expected) > TOLERANCE:
            issues.append({"severity": "warning", "type": "Lot balance mismatch", "reference": f"{product} / {batch}", "message": f"Stored {_qty(available)}; cumulative receipt/issue/adjustment formula gives {_qty(expected)}."})
        if collection_name == "farmer_produce_lots":
            harvested = _dec(_first(lot, "original_quantity", "harvested_quantity", "produced_quantity", "initial_quantity", default=0))
            sold = _dec(lot.get("sold_quantity"))
            lost = _dec(_first(lot, "waste_quantity", "wastage_quantity", "damaged_quantity", "loss_quantity", default=0))
            if harvested > 0:
                expected_farmer = harvested - sold - lost
                if abs(available - expected_farmer) > Decimal("0.05"):
                    issues.append({"severity": "warning", "type": "Produce lot balance mismatch", "reference": f"{product} / {batch}", "message": f"Stored {_qty(available)} differs from production less sale/loss {_qty(expected_farmer)}."})
    return issues



def _reconciliation_guidance(issue_type):
    issue_type = str(issue_type or "")
    mapping = {
        "Negative stock": ("Stock quantity fell below zero, usually because an issue/sale/adjustment was recorded without enough accepted stock.", "Review the lot movements and the source order/receipt. Correct through a controlled stock adjustment only after the source document is verified.", "Inventory and margin reports may be wrong until resolved."),
        "Over-reserved stock": ("Open orders reserve more quantity than the lot currently holds.", "Review approved/open orders for the lot. Cancel/release stale reservations or correct the affected order before new dispatches.", "The system may promise stock that cannot actually be delivered."),
        "Invalid stock classification": ("A damaged or blocked stock bucket contains an invalid negative value.", "Review the latest stock adjustment and movement history for this lot, then post a controlled correction.", "Saleable stock may be overstated or understated."),
        "Lot balance mismatch": ("The stored lot balance does not agree with receipt, issue and adjustment movements.", "Compare GRN/receipt, dispatch and stock-adjustment history. Use the authoritative movements to determine the correct balance.", "Stock valuation and availability may be inconsistent."),
        "Produce lot balance mismatch": ("Farmer produce stock does not reconcile with production less sale/loss movements.", "Review production, manual stock adjustments, marketplace reservations and completed sales for this produce lot.", "Farmer stock/listing availability may be inaccurate."),
        "Financial mismatch": ("The payable/settlement amount, confirmed paid amount and stored outstanding do not reconcile.", "Compare completed/reversed payments with the authoritative settlement amount, then correct only the stale financial snapshot.", "Receivable/payable and dashboard totals can be incorrect."),
        "Receipt adjustment mismatch": ("The original invoice value, accepted-goods settlement value and recorded receipt adjustment do not reconcile.", "Open the buyer receipt and compare accepted, damaged, rejected and missing quantities with the invoice receipt adjustment. Repair only the authoritative receipt/financial snapshot; do not alter the original dispatch invoice value.", "A genuine damaged/rejected/missing-goods adjustment may be mistaken for a payment error, or an invalid adjustment may hide a real financial mismatch."),
        "Payment status mismatch": ("The invoice payment status conflicts with its confirmed paid/outstanding amounts.", "Verify completed/reversed payments first, then synchronize payment status from the confirmed settlement balance.", "Users may see Paid and Outstanding at the same time."),
        "Broken transaction chain": ("A completed business step is missing a linked stock, invoice, sale, purchase or payment-side record.", "Open the source transaction and identify the missing linked step. Rebuild only from the authoritative order/receipt; do not create a duplicate transaction manually.", "Audit history, stock or accounting may be incomplete."),
        "Product line mismatch": ("A multi-item order was synchronized into a sale/invoice using only part of its active product lines, usually through a legacy single-line path.", "Open the authoritative order and rebuild/synchronize the linked sale from its item lines before further settlement.", "Sales, stock, invoice totals and product-wise reports can be incomplete or understated."),
        "Orphan payment": ("A payment has no valid source invoice reference.", "Verify the payment reference and original transaction. Link it to the correct invoice only when ownership and amount are proven; otherwise keep it flagged for manual review.", "Payment totals can be counted without a valid receivable/payable source."),
    }
    return mapping.get(issue_type, ("A linked system record does not match the expected business state.", "Open the affected source transaction and compare its order, receipt, stock, invoice and payment history before making a correction.", "Related operational or financial reports may be affected."))


def build_reconciliation_report(scope, filters=None):
    filters = filters or {}
    issues = _stock_reconciliation(scope)
    financial_checked = 0
    documented_adjustments = 0
    documented_adjustment_value = Decimal("0")
    for source_label, collection, query in _invoice_sources(scope):
        rows = _collection_rows(collection, query, limit=MAX_REPORT_ROWS)
        for invoice in rows:
            if str(invoice.get("status") or "").lower() in {"cancelled", "voided"}:
                continue
            financial_checked += 1
            gross_total = _dec(_first(invoice, "grand_total", "total_amount", "invoice_total", default=0))
            settlement_raw = invoice.get("settlement_total")
            settlement_total = _dec(settlement_raw) if settlement_raw not in (None, "") else gross_total
            adjustment_raw = invoice.get("receipt_adjustment_amount")
            receipt_adjustment = _dec(adjustment_raw) if adjustment_raw not in (None, "") else Decimal("0")
            paid = _dec(_first(invoice, "amount_paid", "paid_amount", default=0))
            outstanding = _dec(_first(invoice, "outstanding_amount", default=max(settlement_total - paid, Decimal("0"))))
            difference = settlement_total - paid - outstanding
            ref = _first(invoice, "invoice_number", "document_number", "supplier_invoice_number", "internal_reference", default=str(invoice.get("_id") or ""))

            # Universal receiving intentionally preserves the original dispatch/
            # invoice value for audit while making only buyer-accepted goods
            # payable.  A documented receipt adjustment is therefore a normal
            # business outcome, not a financial mismatch.
            has_receipt_settlement = settlement_raw not in (None, "") or adjustment_raw not in (None, "")
            if has_receipt_settlement:
                adjustment_difference = gross_total - settlement_total - receipt_adjustment
                invalid_adjustment = (
                    settlement_total < -TOLERANCE
                    or receipt_adjustment < -TOLERANCE
                    or settlement_total - gross_total > TOLERANCE
                    or abs(adjustment_difference) > TOLERANCE
                )
                if invalid_adjustment:
                    issues.append({
                        "severity": "critical",
                        "type": "Receipt adjustment mismatch",
                        "reference": f"{source_label} {ref}",
                        "message": (
                            f"Original ₹{_money(gross_total)} does not reconcile to payable ₹{_money(settlement_total)} "
                            f"+ receipt adjustment ₹{_money(receipt_adjustment)} "
                            f"(difference ₹{_money(adjustment_difference)})."
                        ),
                    })
                elif receipt_adjustment > TOLERANCE:
                    documented_adjustments += 1
                    documented_adjustment_value += receipt_adjustment

            if abs(difference) > TOLERANCE:
                issues.append({
                    "severity": "critical",
                    "type": "Financial mismatch",
                    "reference": f"{source_label} {ref}",
                    "message": (
                        f"Payable ₹{_money(settlement_total)} ≠ paid ₹{_money(paid)} "
                        f"+ outstanding ₹{_money(outstanding)} (difference ₹{_money(difference)})."
                    ),
                })
            payment_status = str(invoice.get("payment_status") or "unpaid")
            if outstanding <= TOLERANCE and payment_status not in {"paid", "voided", "reversed"}:
                issues.append({"severity": "warning", "type": "Payment status mismatch", "reference": f"{source_label} {ref}", "message": f"Outstanding is zero but payment status is {payment_status}."})
            if outstanding > TOLERANCE and payment_status == "paid":
                issues.append({"severity": "critical", "type": "Payment status mismatch", "reference": f"{source_label} {ref}", "message": f"Invoice is marked paid but ₹{_money(outstanding)} remains outstanding."})

    chain_report = build_transaction_chains(scope, filters)
    for row in chain_report["rows"]:
        if not row["complete"]:
            issues.append({"severity": "critical", "type": "Broken transaction chain", "reference": f"{row['flow']} {row['reference']}", "message": row.get("warning") or "Required linked record is missing."})

    # Payment orphan check uses the explicit invoice_collection persisted by Stage 8.
    pay_query = {"status": {"$in": ["completed", "processing"]}}
    if scope["role"] == "farmer":
        keys = [scope.get("user_id_str"), scope.get("farmer_phone")]
        pay_query["$or"] = [{"payer_key": {"$in": [x for x in keys if x]}}, {"payee_key": {"$in": [x for x in keys if x]}}]
    payments = _collection_rows("payments", pay_query, limit=MAX_REPORT_ROWS)
    orphan_payments = 0
    for payment in payments:
        collection = str(payment.get("invoice_collection") or "").strip()
        invoice_id = payment.get("invoice_id")
        if not collection or not invoice_id:
            orphan_payments += 1
            issues.append({"severity": "critical", "type": "Orphan payment", "reference": payment.get("payment_number") or str(payment.get("_id") or ""), "message": "Payment is missing its source invoice collection/reference."})
            continue
        if not mongo.db[collection].find_one({"_id": invoice_id}, {"_id": 1}):
            orphan_payments += 1
            issues.append({"severity": "critical", "type": "Orphan payment", "reference": payment.get("payment_number") or str(payment.get("_id") or ""), "message": f"Linked invoice was not found in {collection}."})

    # Multi-item commerce consistency. Detect legacy one-line financial records
    # created from a multi-line order before they distort stock or reports.
    if scope.get("is_management"):
        line_checks = (
            ("AVPL → UFC", "avpl_ufc_orders", "avpl_ufc_sales", "avpl_ufc_order_id", "dispatched"),
            ("UFC → Farmer", "ufc_farmer_orders", "ufc_farmer_sales", "ufc_farmer_order_id", "accepted"),
            ("Farmer Produce", "farmer_produce_marketplace_orders", "farmer_marketplace_sales", "farmer_marketplace_order_id", "accepted"),
        )
        for flow_label, order_collection, sale_collection, link_field, basis in line_checks:
            order_rows = _collection_rows(order_collection, {"status": {"$in": ["dispatched", "received", "completed"]}}, limit=MAX_REPORT_ROWS)
            for order in order_rows:
                source_items = list(order.get("items") or [])
                if not source_items:
                    continue
                if basis == "dispatched":
                    expected = sum(1 for item in source_items if _dec(item.get("dispatched_quantity") if item.get("dispatched_quantity") is not None else item.get("approved_quantity")) > 0)
                else:
                    expected = sum(1 for item in source_items if _dec(item.get("accepted_quantity") if item.get("accepted_quantity") is not None else item.get("received_quantity") or item.get("delivered_quantity") or item.get("approved_quantity")) > 0)
                if expected <= 0:
                    continue
                sale = mongo.db[sale_collection].find_one({link_field: order.get("_id")})
                order_ref = _first(order, "order_number", default=str(order.get("_id") or ""))
                if not sale:
                    if str(order.get("status") or "").lower() in {"received", "completed"}:
                        issues.append({"severity": "critical", "type": "Broken transaction chain", "reference": f"{flow_label} {order_ref}", "message": "Buyer receipt is complete but the linked sale record is missing."})
                    continue
                actual = len(sale.get("items") or []) or int(sale.get("item_count") or (1 if sale.get("product_name") else 0))
                if actual != expected:
                    issues.append({"severity": "critical", "type": "Product line mismatch", "reference": f"{flow_label} {order_ref}", "message": f"Order has {expected} active product line(s) but the linked sale has {actual}."})

    for issue in issues:
        cause, action, impact = _reconciliation_guidance(issue.get("type"))
        issue.setdefault("cause", cause)
        issue.setdefault("recommended_action", action)
        issue.setdefault("impact", impact)
        issue.setdefault("repair_mode", "Review")

    severity_rank = {"critical": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda x: (severity_rank.get(x.get("severity"), 9), x.get("type") or "", x.get("reference") or ""))
    return {
        "issues": issues,
        "summary": {
            "issues": len(issues),
            "critical": sum(1 for x in issues if x["severity"] == "critical"),
            "warnings": sum(1 for x in issues if x["severity"] == "warning"),
            "financial_documents_checked": financial_checked,
            "documented_receipt_adjustments": documented_adjustments,
            "documented_receipt_adjustment_value": float(documented_adjustment_value.quantize(Decimal("0.01"))),
            "transaction_chains_checked": chain_report["summary"]["total"],
            "stock_lots_checked": len(_collection_rows(_inventory_scope(scope)[1], _inventory_scope(scope)[2], projection={"_id": 1}, limit=MAX_REPORT_ROWS)),
            "orphan_payments": orphan_payments,
        },
        "chains": chain_report,
    }


def build_gst_report(scope, filters=None):
    filters = filters or {}
    rows = []
    sources = _invoice_sources(scope)
    for source_label, collection, query in sources:
        if "Supplier" in source_label and not scope["is_management"]:
            continue
        for doc in _collection_rows(collection, query, limit=MAX_REPORT_ROWS):
            if str(doc.get("status") or "").lower() in {"cancelled", "voided"}:
                continue
            if not _date_in_range(doc, filters.get("from"), filters.get("to"), ("invoice_date", "created_at")):
                continue
            gst_rate = _dec(doc.get("gst_rate"))
            cgst = _dec(doc.get("cgst_amount"))
            sgst = _dec(doc.get("sgst_amount"))
            igst = _dec(doc.get("igst_amount"))
            gst = _dec(_first(doc, "gst_amount", default=cgst + sgst + igst))
            taxable = _dec(_first(doc, "taxable_value", "subtotal", default=0))
            hsn = str(doc.get("hsn_code") or "Unmapped")
            reference = _first(doc, "invoice_number", "document_number", "supplier_invoice_number", "internal_reference", default="-")
            if not _matches_search({"hsn": hsn, "reference": reference, **doc}, filters.get("q"), ("hsn", "reference", "product_name", "centre_uid", "supplier_name")):
                continue
            rows.append({
                "source": source_label, "reference": reference, "invoice_date": doc.get("invoice_date") or "",
                "hsn": hsn, "product_name": doc.get("product_name") or "-", "gst_rate": float(gst_rate),
                "taxable": float(taxable), "cgst": float(cgst), "sgst": float(sgst), "igst": float(igst), "gst": float(gst),
            })
    hsn_map = defaultdict(lambda: {"taxable": 0.0, "cgst": 0.0, "sgst": 0.0, "igst": 0.0, "gst": 0.0, "documents": 0})
    for row in rows:
        h = hsn_map[row["hsn"]]
        h["documents"] += 1
        for key in ("taxable", "cgst", "sgst", "igst", "gst"):
            h[key] += row[key]
    hsn_rows = []
    for hsn, values in hsn_map.items():
        hsn_rows.append({"hsn": hsn, **{k: round(v, 2) if k != "documents" else v for k, v in values.items()}})
    hsn_rows.sort(key=lambda x: -x["taxable"])
    return {
        "rows": rows,
        "hsn_rows": hsn_rows,
        "summary": {
            "documents": len(rows),
            "taxable": round(sum(x["taxable"] for x in rows), 2),
            "cgst": round(sum(x["cgst"] for x in rows), 2),
            "sgst": round(sum(x["sgst"] for x in rows), 2),
            "igst": round(sum(x["igst"] for x in rows), 2),
            "gst": round(sum(x["gst"] for x in rows), 2),
            "non_gst_documents": sum(1 for x in rows if x["gst"] <= 0),
        },
    }


def build_management_overview(scope, filters=None):
    filters = filters or {}
    inventory = build_inventory_report(scope, filters)
    financial = build_financial_report(scope, filters)
    chains = build_transaction_chains(scope, filters)

    if scope["is_management"]:
        supplier_invoices = _collection_rows("avpl_supplier_invoices", {"status": {"$ne": "cancelled"}}, limit=MAX_REPORT_ROWS)
        avpl_sales = _collection_rows("avpl_ufc_sales", {}, limit=MAX_REPORT_ROWS)
        ufc_sales = _collection_rows("ufc_farmer_sales", {}, limit=MAX_REPORT_ROWS)
        pos_sales = _collection_rows("pos_sales", {}, limit=MAX_REPORT_ROWS)
        produce_sales = _collection_rows("farmer_marketplace_sales", {}, limit=MAX_REPORT_ROWS)
        supplier_purchase = _sum_rows([x for x in supplier_invoices if _date_in_range(x, filters.get("from"), filters.get("to"), ("invoice_date", "created_at"))], "grand_total", "total_amount")
        avpl_ufc_sales = _sum_rows([x for x in avpl_sales if _date_in_range(x, filters.get("from"), filters.get("to"), ("sale_date", "created_at"))], "grand_total", "total_amount")
        ufc_farmer_sales = _sum_rows([x for x in (ufc_sales + pos_sales) if _date_in_range(x, filters.get("from"), filters.get("to"), ("sale_date", "created_at"))], "grand_total", "total_amount", "amount")
        farmer_produce_sales = _sum_rows([x for x in produce_sales if _date_in_range(x, filters.get("from"), filters.get("to"), ("sale_date", "created_at"))], "grand_total", "total_amount")
        cogs = _sum_rows(avpl_sales, "estimated_cogs")
        margin = _sum_rows(avpl_sales, "gross_margin_amount")
        pending_orders = (
            _count_status("avpl_purchase_orders", {}, {"approved", "partially_received", "draft", "pending_approval"})
            + _count_status("avpl_ufc_orders", {}, {"requested", "approved", "dispatched"})
            + _count_status("ufc_farmer_orders", {}, {"requested", "approved", "delivered"})
            + _count_status("farmer_produce_marketplace_orders", {}, {"requested", "approved", "dispatched"})
        )
        awaiting_dispatch = (
            _count_status("avpl_ufc_orders", {}, {"approved"})
            + _count_status("ufc_farmer_orders", {}, {"approved"})
            + _count_status("farmer_produce_marketplace_orders", {}, {"approved"})
        )
        awaiting_receipt = _count_status("avpl_ufc_orders", {}, {"dispatched"}) + _count_status("farmer_produce_marketplace_orders", {}, {"dispatched"})
    elif scope["role"] in {"ufc_admin", "ufc_mitra"}:
        centre = scope.get("centre_uid") or "__NO_CENTRE__"
        avpl_purchases = _collection_rows("ufc_purchase_entries", {"centre_uid": centre}, limit=MAX_REPORT_ROWS)
        ufc_sales = _collection_rows("ufc_farmer_sales", {"centre_uid": centre}, limit=MAX_REPORT_ROWS)
        pos_sales = _collection_rows("pos_sales", {"centre_uid": centre}, limit=MAX_REPORT_ROWS)
        supplier_purchase = _sum_rows(avpl_purchases, "total_amount", "grand_total")
        avpl_ufc_sales = 0.0
        ufc_farmer_sales = _sum_rows(ufc_sales + pos_sales, "grand_total", "total_amount", "amount")
        produce_sales = _collection_rows("farmer_marketplace_sales", {"centre_uid": centre}, limit=MAX_REPORT_ROWS)
        farmer_produce_sales = _sum_rows(produce_sales, "grand_total", "total_amount")
        cogs = margin = 0.0
        pending_orders = _count_status("avpl_ufc_orders", {"centre_uid": centre}, {"requested", "approved", "dispatched"}) + _count_status("ufc_farmer_orders", {"centre_uid": centre}, {"requested", "approved", "delivered"})
        awaiting_dispatch = _count_status("ufc_farmer_orders", {"centre_uid": centre}, {"approved"})
        awaiting_receipt = _count_status("avpl_ufc_orders", {"centre_uid": centre}, {"dispatched"})
    elif scope["role"] == "farmer":
        values = [x for x in [scope.get("user_id"), scope.get("user_id_str")] if x]
        purchases = _collection_rows("farmer_purchase_entries", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, limit=MAX_REPORT_ROWS)
        produce_sales = _collection_rows("farmer_marketplace_sales", {"seller_farmer_user_id": {"$in": values or ["__NO_USER__"]}}, limit=MAX_REPORT_ROWS)
        supplier_purchase = _sum_rows(purchases, "grand_total", "total_amount")
        avpl_ufc_sales = ufc_farmer_sales = 0.0
        farmer_produce_sales = _sum_rows(produce_sales, "grand_total", "total_amount")
        cogs = margin = 0.0
        pending_orders = _count_status("ufc_farmer_orders", {"farmer_user_id": {"$in": values or ["__NO_USER__"]}}, {"requested", "approved", "delivered"}) + _count_status("farmer_produce_marketplace_orders", {"seller_farmer_user_id": {"$in": values or ["__NO_USER__"]}}, {"requested", "approved", "dispatched"})
        awaiting_dispatch = _count_status("farmer_produce_marketplace_orders", {"seller_farmer_user_id": {"$in": values or ["__NO_USER__"]}}, {"approved"})
        awaiting_receipt = _count_status("farmer_produce_marketplace_orders", {"buyer_key": {"$in": [str(x) for x in values] or ["__NO_USER__"]}}, {"dispatched"})
    else:
        supplier_purchase = avpl_ufc_sales = ufc_farmer_sales = farmer_produce_sales = cogs = margin = 0.0
        pending_orders = awaiting_dispatch = awaiting_receipt = 0

    payments = _collection_rows("payments", {"status": "completed"}, limit=MAX_REPORT_ROWS)
    if scope["role"] == "ufc_admin":
        centre = scope.get("centre_uid") or ""
        payments = [p for p in payments if centre and centre in {str(p.get("payer_key") or ""), str(p.get("payee_key") or ""), str(p.get("centre_uid") or "")}]
    if scope["role"] == "farmer":
        keys = {scope.get("user_id_str"), scope.get("farmer_phone")}
        payments = [p for p in payments if str(p.get("payer_key") or "") in keys or str(p.get("payee_key") or "") in keys]
    payment_value = _sum_rows(payments, "amount")

    return {
        "kpis": {
            "supplier_purchases": round(supplier_purchase, 2),
            "avpl_ufc_sales": round(avpl_ufc_sales, 2),
            "ufc_farmer_sales": round(ufc_farmer_sales, 2),
            "farmer_produce_sales": round(farmer_produce_sales, 2),
            "receivables": financial["summary"]["receivable"],
            "payables": financial["summary"]["payable"],
            "stock_value": inventory["summary"]["stock_value"],
            "saleable_stock": inventory["summary"]["saleable"],
            "pending_orders": pending_orders,
            "awaiting_dispatch": awaiting_dispatch,
            "awaiting_receipt": awaiting_receipt,
            "payment_value": round(payment_value, 2),
            "estimated_cogs": round(cogs, 2),
            "estimated_gross_margin": round(margin, 2),
            "expiring_30d": inventory["summary"]["expiring_30d"],
        },
        "inventory": inventory,
        "financial": financial,
        "chains": chains,
    }


def build_system_health(scope, filters=None):
    reconciliation = build_reconciliation_report(scope, filters)
    db = mongo.db
    checks = []

    def add(name, ok, detail, severity="warning", category="System", action="Review the related setup or transaction and resolve the underlying issue before continuing."):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": "ok" if ok else severity, "category": category, "recommended_action": "No action required." if ok else action})

    # Master data / setup readiness.
    active_entity = db.accounting_entities.find_one({"entity_type": "avpl", "status": "active", "is_deleted": {"$ne": True}})
    add("Active AVPL accounting entity", bool(active_entity), "Required for procurement, invoicing and accounting mapping.", "critical", "Accounting", "Open Accounting Setup and activate the AVPL accounting entity.")
    fy_query = {"status": "open", "is_open": True, "is_locked": {"$ne": True}, "is_deleted": {"$ne": True}}
    if active_entity and active_entity.get("_id"):
        fy_query["accounting_entity_id"] = active_entity["_id"]
    open_fy = db.financial_years.find_one(fy_query)
    add("Open financial year", bool(open_fy), "Official invoice numbering requires an open, unlocked Financial Year for the active AVPL entity.", "critical", "Accounting", "Open or create the correct financial year before posting new financial documents.")
    mapping_count = db.accounting_product_mappings.count_documents({"status": {"$in": ["active", "approved"]}, "is_deleted": {"$ne": True}}) + db.product_accounting_mappings.count_documents({"status": {"$in": ["active", "approved"]}, "is_deleted": {"$ne": True}})
    add("Product accounting mappings", mapping_count > 0 or db.products.count_documents({}) == 0, f"{mapping_count} active/approved mapping record(s) found.", "warning", "GST / Accounting", "Review Product Accounting Mapping and complete missing HSN/ledger mappings.")
    documented_adjustments = int(reconciliation["summary"].get("documented_receipt_adjustments") or 0)
    adjustment_note = f" {documented_adjustments} documented receipt adjustment(s) recognized as valid business outcomes." if documented_adjustments else ""
    add("No critical reconciliation errors", reconciliation["summary"]["critical"] == 0, f"{reconciliation['summary']['critical']} critical reconciliation issue(s).{adjustment_note}", "critical", "Reconciliation", "Open Reconciliation, start with Critical issues, and repair the authoritative source chain before posting further changes.")
    add("No orphan payments", reconciliation["summary"]["orphan_payments"] == 0, f"{reconciliation['summary']['orphan_payments']} orphan payment(s).", "critical", "Payments", "Open Reconciliation and link each payment to its verified source invoice, or keep it under manual review.")
    posting_failures = db.posting_failures.count_documents({"status": {"$nin": ["resolved", "closed"]}})
    add("No unresolved posting failures", posting_failures == 0, f"{posting_failures} unresolved posting failure(s).", "critical", "Accounting", "Review the posting failure record and fix the source validation/mapping before retrying the posting.")
    pending_validations = db.validations.count_documents({"status": "pending"})
    add("Validation queue reviewed", pending_validations == 0, f"{pending_validations} pending profile/user validation(s).", "warning", "Users", "Review the pending validation queue.")
    pending_stock_adjustments = db.avpl_stock_adjustments.count_documents({"status": "submitted"})
    add("Stock adjustment queue reviewed", pending_stock_adjustments == 0, f"{pending_stock_adjustments} submitted stock adjustment(s) await controlled approval.", "warning", "Inventory", "Review submitted stock adjustments and approve/reject them with supporting evidence.")
    failed_payments = db.payments.count_documents({"status": "failed"})
    add("No failed payment records requiring review", failed_payments == 0, f"{failed_payments} failed payment attempt(s) retained for audit.", "warning", "Payments", "Review failed payment attempts and confirm whether the payer should retry or the record should remain audit-only.")

    return {
        "checks": checks,
        "reconciliation": reconciliation,
        "summary": {
            "total": len(checks),
            "healthy": sum(1 for x in checks if x["ok"]),
            "attention": sum(1 for x in checks if not x["ok"]),
            "critical": sum(1 for x in checks if not x["ok"] and x["severity"] == "critical"),
            "score": round(100 * sum(1 for x in checks if x["ok"]) / max(len(checks), 1)),
        },
    }


def build_uat_checklist(scope):
    steps = [
        ("01", "Foundation", "Create/verify AVPL accounting entity, financial year, users, UFC profiles and product/accounting masters."),
        ("02", "Supplier → AVPL PO", "Create a supplier Purchase Order. In streamlined mode it should be immediately approved and printable."),
        ("03", "GRN / Stock Receipt", "Receive against the PO. Accepted quantity must increase AVPL physical stock exactly once."),
        ("04", "Supplier Invoice", "Record invoice; three-way match must pass before posting. Verify payable and printable purchase invoice."),
        ("05", "AVPL Marketplace", "Publish selected stocked products. Publication must not reduce physical stock."),
        ("06", "UFC Order", "UFC requests product; AVPL approval reserves stock without reducing physical quantity."),
        ("07", "AVPL Dispatch", "Dispatch once; AVPL physical/reserved stock must reduce correctly and sale/invoice/receivable must exist."),
        ("08", "UFC Receipt", "UFC confirms physical receipt; UFC stock and purchase entry must be created exactly once."),
        ("09", "UFC → Farmer", "Publish UFC stock, Farmer orders, UFC approves/delivers, Farmer receives; linked sale/purchase/invoice must agree."),
        ("10", "Input Payment", "Record full, partial and later settlement. Both receivable/payable sides must stay synchronized."),
        ("11", "Farmer Harvest", "Record production; Farmer Produce stock should reflect harvest and movement history."),
        ("12", "Farmer Produce Listing", "Publish loose and/or bag options. Listing must not reduce physical produce stock."),
        ("13", "Farmer Produce Order", "Test Farmer→Farmer, Farmer→UFC and Farmer→AVPL buying where applicable; self-order must be blocked."),
        ("14", "Produce Dispatch/Receipt", "Approval reserves base quantity; dispatch reduces once; receipt creates buyer purchase without falsifying harvest."),
        ("15", "Produce Payment", "Test full/partial/no-payment/credit and payment reversal with audit retained."),
        ("16", "Returns/Cancellation Safety", "Cancel eligible approved orders and verify reservation release occurs exactly once; posted history must remain."),
        ("17", "Reconciliation", "Run Stage 10 reconciliation. Stock, invoice arithmetic and linked transaction chains should have zero critical issues."),
        ("18", "Permissions", "Log in as Super Admin, AVPL, Accounts, UFC A, UFC B, Mitra and Farmer; verify private records are correctly scoped."),
        ("19", "Performance", "Test search/pagination on realistic volumes and confirm dashboards/reports remain responsive with Stage 10 indexes."),
        ("20", "Audit / Go-Live", "Review audit trails, unresolved failures and System Health. Go live only after critical health checks are green."),
    ]
    return {
        "steps": [{"number": n, "title": title, "description": desc} for n, title, desc in steps],
        "scope": scope,
    }


def report_rows_for_csv(report_name, scope, filters=None):
    filters = filters or {}
    name = str(report_name or "").strip().lower()
    if name == "inventory":
        report = build_inventory_report(scope, filters)
        return ["Product", "Code", "Unit", "Physical", "Reserved", "Saleable", "Damaged", "Expired", "Expiring 30d", "Stock Value"], [
            [r["product_name"], r["product_code"], r["unit_code"], r["physical"], r["reserved"], r["saleable"], r["damaged"], r["expired"], r["expiring_30d"], r["stock_value"]] for r in report["rows"]
        ]
    if name == "financial":
        report = build_financial_report(scope, filters)
        return ["Source", "Nature", "Party", "Reference", "Invoice Date", "Due Date", "Total", "Paid", "Outstanding", "Payment Status", "Aging"], [
            [r["source"], r["nature"], r["party"], r["reference"], r["invoice_date"], r["due_date"], r["total"], r["paid"], r["outstanding"], r["payment_status"], r["aging"]] for r in report["rows"]
        ]
    if name == "transactions":
        report = build_transaction_chains(scope, filters)
        return ["Flow", "Reference", "Party", "Status", "Complete", "Warning", "Steps"], [
            [r["flow"], r["reference"], r["party"], r["status"], "Yes" if r["complete"] else "No", r["warning"], " | ".join(r["steps"])] for r in report["rows"]
        ]
    if name == "gst":
        report = build_gst_report(scope, filters)
        return ["Source", "Reference", "Invoice Date", "HSN", "Product", "GST Rate", "Taxable", "CGST", "SGST", "IGST", "GST"], [
            [r["source"], r["reference"], r["invoice_date"], r["hsn"], r["product_name"], r["gst_rate"], r["taxable"], r["cgst"], r["sgst"], r["igst"], r["gst"]] for r in report["rows"]
        ]
    if name == "reconciliation":
        report = build_reconciliation_report(scope, filters)
        return ["Severity", "Type", "Reference", "Message"], [[x["severity"], x["type"], x["reference"], x["message"]] for x in report["issues"]]
    raise ValueError("Unknown Stage 10 report export.")
