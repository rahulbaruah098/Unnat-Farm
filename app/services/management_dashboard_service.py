from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from time import monotonic

from bson import ObjectId

from app.extensions import mongo
from app.services.stage10_reporting_service import (
    build_reconciliation_report,
    build_system_health,
    resolve_report_scope,
)
from app.utils.timezone import business_today


TOLERANCE = Decimal("0.02")
_HEALTH_CACHE = {}
_HEALTH_TTL_SECONDS = 45


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


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _active_entity():
    return mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }) or mongo.db.accounting_entities.find_one({
        "entity_type": "avpl",
        "status": "active",
        "is_deleted": {"$ne": True},
    })


def _invoice_balance(row):
    row = row or {}
    total = _dec(row.get("settlement_total") if row.get("settlement_total") is not None else (
        row.get("grand_total") or row.get("total_amount") or row.get("invoice_total")
    ))
    paid_raw = row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount")
    paid = max(_dec(paid_raw), Decimal("0"))
    if paid > total:
        paid = total
    computed = max(total - paid, Decimal("0"))
    stored = row.get("outstanding_amount")
    outstanding = computed if stored in (None, "") else max(_dec(stored), Decimal("0"))
    # Never allow stale outstanding to exceed the arithmetic balance.
    if outstanding > computed:
        outstanding = computed
    if computed <= TOLERANCE:
        outstanding = Decimal("0")
    return total, paid, outstanding


def _sum_balance(collection, query=None):
    total = paid = outstanding = Decimal("0")
    for row in mongo.db[collection].find(query or {}):
        if str(row.get("status") or "").lower() in {"cancelled", "voided"}:
            continue
        t, p, o = _invoice_balance(row)
        total += t
        paid += p
        outstanding += o
    return total, paid, outstanding


def _month_match(value, start_date):
    if not value:
        return False
    if isinstance(value, datetime):
        date_value = value.date()
    else:
        try:
            date_value = datetime.fromisoformat(str(value)[:19]).date()
        except Exception:
            try:
                date_value = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            except Exception:
                return False
    return date_value >= start_date


def _sum_month(collection, query, fields, date_fields=("created_at",)):
    first = business_today().replace(day=1)
    total = Decimal("0")
    for row in mongo.db[collection].find(query or {}):
        date_value = next((row.get(field) for field in date_fields if row.get(field)), None)
        if not _month_match(date_value, first):
            continue
        value = next((row.get(field) for field in fields if row.get(field) not in (None, "")), 0)
        total += _dec(value)
    return float(total)


def _stock_product_count():
    pipeline = [
        {"$match": {"status": {"$ne": "cancelled"}}},
        {"$group": {"_id": {"$ifNull": ["$product_key", "$product_name"]}, "qty": {"$sum": {"$ifNull": ["$available_quantity", 0]}}}},
        {"$match": {"qty": {"$gt": 0}}},
        {"$count": "count"},
    ]
    try:
        result = list(mongo.db.avpl_inventory_lots.aggregate(pipeline))
        return int(result[0]["count"]) if result else 0
    except Exception:
        return mongo.db.avpl_inventory_lots.count_documents({"status": {"$ne": "cancelled"}, "available_quantity": {"$gt": 0}})


def _recent_activity(limit=6):
    rows = []
    for payment in mongo.db.payments.find({"status": {"$in": ["completed", "pending_confirmation"]}}).sort("created_at", -1).limit(limit):
        rows.append({
            "at": payment.get("created_at") or payment.get("payment_date"),
            "title": "Payment " + ("awaiting confirmation" if payment.get("status") == "pending_confirmation" else "recorded"),
            "detail": f"{payment.get('payment_number') or 'Payment'} · ₹{_num(payment.get('amount')):,.2f}",
            "tone": "warning" if payment.get("status") == "pending_confirmation" else "good",
        })
    for order in mongo.db.avpl_ufc_orders.find({}).sort("updated_at", -1).limit(limit):
        rows.append({
            "at": order.get("updated_at") or order.get("created_at"),
            "title": "UFC order " + str(order.get("status") or "updated").replace("_", " ").title(),
            "detail": f"{order.get('order_number') or 'UFC Order'} · {order.get('centre_name') or order.get('centre_uid') or 'UFC'}",
            "tone": "neutral",
        })
    rows.sort(key=lambda row: row.get("at") or datetime.min, reverse=True)
    return rows[:limit]


def _health(actor_user_id, role):
    # Reconciliation is intentionally deep. Cache the dashboard pulse briefly so
    # normal navigation does not rescan every operational collection on each click.
    cache_key = str(role or "management")
    cached = _HEALTH_CACHE.get(cache_key)
    now = monotonic()
    if cached and now - cached.get("at", 0) < _HEALTH_TTL_SECONDS:
        return dict(cached.get("value") or {})
    try:
        scope = resolve_report_scope(actor_user_id, role=role)
        health = build_system_health(scope, {})
        value = {
            "score": health["summary"]["score"],
            "critical": health["summary"]["critical"],
            "attention": health["summary"]["attention"],
            "reconciliation_issues": health["reconciliation"]["summary"]["issues"],
            "reconciliation_critical": health["reconciliation"]["summary"]["critical"],
        }
        _HEALTH_CACHE[cache_key] = {"at": now, "value": value}
        return dict(value)
    except Exception:
        return {"score": 0, "critical": 0, "attention": 0, "reconciliation_issues": 0, "reconciliation_critical": 0}


def _payment_pending_summary():
    rows = list(mongo.db.payments.find({"status": "pending_confirmation"}))
    return len(rows), float(sum((_dec(row.get("amount")) for row in rows), Decimal("0")))


def _avpl_dashboard(actor_user_id):
    db = mongo.db
    entity = _active_entity()
    entity_filter = {"accounting_entity_id": entity["_id"]} if entity else {}

    supplier_total, supplier_paid, supplier_due = _sum_balance(
        "avpl_supplier_invoices",
        {**entity_filter, "status": {"$ne": "cancelled"}},
    )
    ufc_total, ufc_received, ufc_due = _sum_balance(
        "avpl_sales_invoices",
        {**entity_filter, "status": {"$ne": "cancelled"}},
    )
    farmer_total, farmer_paid, farmer_due = _sum_balance(
        "farmer_marketplace_payables",
        {"buyer_type": "avpl", "status": {"$ne": "cancelled"}},
    )

    ufc_review = db.avpl_ufc_orders.count_documents({"status": "requested"})
    farmer_review = db.farmer_produce_marketplace_orders.count_documents({"buyer_type": "avpl", "status": "requested"})
    goods_to_receive = (
        db.avpl_purchase_orders.count_documents({"status": {"$in": ["approved", "partially_received"]}})
        + db.farmer_produce_marketplace_orders.count_documents({"buyer_type": "avpl", "status": "dispatched"})
    )
    purchases_to_finalize = db.avpl_supplier_invoices.count_documents({
        **entity_filter,
        "status": {"$ne": "cancelled"},
        "$or": [
            {"posting_status": {"$nin": ["posted", "cancelled"]}},
            {"payable_posted": {"$ne": True}},
        ],
    })
    pending_count, pending_amount = _payment_pending_summary()
    health = _health(actor_user_id, "avpl_admin")

    actions = []
    if ufc_review + farmer_review:
        actions.append({"label": "Orders need review", "value": ufc_review + farmer_review, "detail": "UFC / Farmer purchase requests", "target": "orders", "tone": "warning"})
    if goods_to_receive:
        actions.append({"label": "Goods waiting to be received", "value": goods_to_receive, "detail": "Supplier / Farmer dispatches", "target": "receiving", "tone": "warning"})
    if purchases_to_finalize:
        actions.append({"label": "Purchases need finalization", "value": purchases_to_finalize, "detail": "Received purchases not fully posted", "target": "finalize", "tone": "warning"})
    if pending_count:
        actions.append({"label": "Payments need confirmation", "value": pending_count, "detail": f"₹{pending_amount:,.2f} reported", "target": "payments", "tone": "warning"})
    if health["reconciliation_issues"]:
        actions.append({"label": "Reconciliation issues", "value": health["reconciliation_issues"], "detail": "Review mismatched linked records", "target": "reconciliation", "tone": "danger" if health["reconciliation_critical"] else "warning"})
    if health["critical"]:
        actions.append({"label": "System health blockers", "value": health["critical"], "detail": "Critical checks need attention", "target": "health", "tone": "danger"})

    sales_month = _sum_month("avpl_sales_invoices", {**entity_filter, "status": {"$ne": "cancelled"}}, ("settlement_total", "grand_total", "total_amount"), ("invoice_date", "created_at"))
    farmer_purchase_month = _sum_month("farmer_marketplace_purchase_entries", {"buyer_type": "avpl"}, ("total_amount", "grand_total"), ("received_at", "created_at"))

    return {
        "role": "avpl_admin",
        "title": "AVPL Operations",
        "subtitle": "What needs attention now, with business and system control in one place.",
        "kpis": [
            {"label": "Orders to Review", "value": ufc_review + farmer_review, "kind": "count", "target": "orders", "note": "UFC + Farmer requests"},
            {"label": "Goods to Receive", "value": goods_to_receive, "kind": "count", "target": "receiving", "note": "Physical receipt pending"},
            {"label": "Purchases to Finalize", "value": purchases_to_finalize, "kind": "count", "target": "finalize", "note": "Posting / payable pending"},
            {"label": "Sales This Month", "value": sales_month, "kind": "money", "target": "reports", "note": "AVPL issued sales"},
            {"label": "Money to Collect", "value": float(ufc_due), "kind": "money", "target": "payments", "note": "Confirmed receivables"},
            {"label": "Money to Pay", "value": float(supplier_due + farmer_due), "kind": "money", "target": "payments", "note": "Supplier + Farmer payables"},
        ],
        "attention": actions[:7],
        "snapshot": [
            {"label": "Active UFCs", "value": db.ufc_admin_master.count_documents({})},
            {"label": "Farmers", "value": db.farmer_master.count_documents({})},
            {"label": "Mitras", "value": db.ufc_mitra_master.count_documents({})},
            {"label": "Products in Stock", "value": _stock_product_count()},
            {"label": "Farmer Produce Bought", "value": farmer_purchase_month, "kind": "money"},
            {"label": "Payments Awaiting Confirmation", "value": pending_count},
        ],
        "health": health,
        "recent": _recent_activity(),
        "setup_required": not bool(entity),
    }


def _accounts_dashboard(actor_user_id):
    db = mongo.db
    entity = _active_entity()
    entity_filter = {"accounting_entity_id": entity["_id"]} if entity else {}

    _, supplier_paid, supplier_due = _sum_balance("avpl_supplier_invoices", {**entity_filter, "status": {"$ne": "cancelled"}})
    _, ufc_received, ufc_due = _sum_balance("avpl_sales_invoices", {**entity_filter, "status": {"$ne": "cancelled"}})
    _, farmer_paid, farmer_due = _sum_balance("farmer_marketplace_payables", {"buyer_type": "avpl", "status": {"$ne": "cancelled"}})
    pending_count, pending_amount = _payment_pending_summary()
    purchases_to_finalize = db.avpl_supplier_invoices.count_documents({
        **entity_filter,
        "status": {"$ne": "cancelled"},
        "$or": [{"posting_status": {"$nin": ["posted", "cancelled"]}}, {"payable_posted": {"$ne": True}}],
    })
    unresolved_posting = db.posting_failures.count_documents({"status": {"$nin": ["resolved", "closed"]}})
    health = _health(actor_user_id, "accounts")

    actions = []
    if purchases_to_finalize:
        actions.append({"label": "Purchases need financial finalization", "value": purchases_to_finalize, "detail": "Check invoice / posting readiness", "target": "finalize", "tone": "warning"})
    if pending_count:
        actions.append({"label": "Payments need confirmation", "value": pending_count, "detail": f"₹{pending_amount:,.2f} reported", "target": "payments", "tone": "warning"})
    if health["reconciliation_issues"]:
        actions.append({"label": "Reconciliation issues", "value": health["reconciliation_issues"], "detail": "Finance or stock chains need review", "target": "reconciliation", "tone": "danger" if health["reconciliation_critical"] else "warning"})
    if unresolved_posting:
        actions.append({"label": "Posting failures", "value": unresolved_posting, "detail": "Unresolved accounting posting errors", "target": "health", "tone": "danger"})
    if health["critical"]:
        actions.append({"label": "Critical health blockers", "value": health["critical"], "detail": "Resolve before closing the period", "target": "health", "tone": "danger"})

    month_start = business_today().replace(day=1)
    money_in_month = money_out_month = Decimal("0")
    for payment in db.payments.find({"status": "completed"}):
        if not _month_match(payment.get("payment_date") or payment.get("created_at"), month_start):
            continue
        source = str(payment.get("source_type") or "")
        amount = _dec(payment.get("amount"))
        if source in {"avpl_ufc_invoice", "farmer_marketplace_invoice"} and str(payment.get("payee_role") or "").lower() in {"avpl_admin", "avpl", ""}:
            money_in_month += amount
        elif source == "supplier_invoice" or str(payment.get("payer_role") or "").lower() in {"avpl_admin", "avpl", "accounts"}:
            money_out_month += amount

    return {
        "role": "accounts",
        "title": "Accounts Control",
        "subtitle": "Financial actions, exceptions and reconciliations that need attention.",
        "kpis": [
            {"label": "Payables", "value": float(supplier_due + farmer_due), "kind": "money", "target": "payments", "note": "Supplier + Farmer"},
            {"label": "Receivables", "value": float(ufc_due), "kind": "money", "target": "payments", "note": "AVPL sales outstanding"},
            {"label": "Payments to Confirm", "value": pending_count, "kind": "count", "target": "payments", "note": f"₹{pending_amount:,.2f} reported"},
            {"label": "Purchases to Finalize", "value": purchases_to_finalize, "kind": "count", "target": "finalize", "note": "Financial posting pending"},
            {"label": "Reconciliation Issues", "value": health["reconciliation_issues"], "kind": "count", "target": "reconciliation", "note": f"{health['reconciliation_critical']} critical"},
            {"label": "Posting Problems", "value": unresolved_posting, "kind": "count", "target": "health", "note": "Unresolved posting failures"},
        ],
        "attention": actions[:7],
        "snapshot": [
            {"label": "Money In This Month", "value": float(money_in_month), "kind": "money"},
            {"label": "Money Out This Month", "value": float(money_out_month), "kind": "money"},
            {"label": "Supplier Paid", "value": float(supplier_paid), "kind": "money"},
            {"label": "UFC Collected", "value": float(ufc_received), "kind": "money"},
            {"label": "Farmer Paid", "value": float(farmer_paid), "kind": "money"},
            {"label": "Health Score", "value": f"{health['score']}%"},
        ],
        "health": health,
        "recent": _recent_activity(),
        "setup_required": not bool(entity),
    }


def get_management_dashboard(actor_user_id, role):
    role = str(role or "").strip().lower()
    if role == "avpl_admin":
        return _avpl_dashboard(actor_user_id)
    if role == "accounts":
        return _accounts_dashboard(actor_user_id)
    raise PermissionError("Management dashboard is available only to AVPL Admin and Accounts.")
