from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import ceil
from typing import Any

from bson import ObjectId

from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.timezone import business_today, format_ist_datetime, to_india_datetime


SALES_ROLE = "sales_unnatfarm"
CHANNELS = [
    ("all", "All Sales"),
    ("avpl_ufc", "AVPL → UFC"),
    ("ufc_farmer", "UFC → Farmer"),
    ("ufc_pos", "UFC POS"),
    ("farmer_produce", "Farmer Produce"),
]
PERIODS = [
    ("this_month", "This Month"),
    ("today", "Today"),
    ("7d", "Last 7 Days"),
    ("30d", "Last 30 Days"),
    ("last_month", "Last Month"),
    ("fy", "Financial Year"),
    ("all", "All Time"),
]
PAYMENT_STATUSES = [
    ("all", "All Payments"),
    ("unpaid", "Unpaid"),
    ("partially_paid", "Partially Paid"),
    ("paid", "Paid"),
    ("pending_confirmation", "Awaiting Confirmation"),
    ("not_recorded", "Not Recorded"),
]
FOLLOWUP_STATUSES = [
    ("new", "New"),
    ("contacted", "Contacted"),
    ("follow_up", "Follow-up"),
    ("forwarded", "Forwarded"),
    ("closed", "Closed"),
]


def _clean(value: Any) -> str:
    return str(value or "").strip()


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


def _money(value: Any) -> float:
    return round(_num(value), 2)


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _actor(actor_user_id):
    oid = _oid(actor_user_id)
    user = mongo.db.users.find_one({"_id": oid}) if oid else None
    if not user:
        raise PermissionError("Sales user account was not found.")
    if _clean(user.get("role")).lower() != SALES_ROLE:
        raise PermissionError("This Sales workspace is available only to Sales UnnatFarm users.")
    return user


def _date_value(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        converted = to_india_datetime(value)
        return converted.date() if converted else value.date()
    if isinstance(value, date):
        return value
    raw = _clean(value)
    if not raw:
        return None
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


def _period_bounds(period: str):
    today = business_today()
    key = _clean(period).lower() or "this_month"
    if key == "all":
        return None, None
    if key == "today":
        return today, today
    if key == "7d":
        return today - timedelta(days=6), today
    if key == "30d":
        return today - timedelta(days=29), today
    if key == "last_month":
        first_this = today.replace(day=1)
        end = first_this - timedelta(days=1)
        return end.replace(day=1), end
    if key == "fy":
        year = today.year if today.month >= 4 else today.year - 1
        return date(year, 4, 1), date(year + 1, 3, 31)
    return today.replace(day=1), today


def _first(row: dict, *fields, default=None):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            return value
    return default


def _product_summary(row: dict) -> str:
    items = [x for x in (row.get("items") or []) if isinstance(x, dict)]
    names = []
    for line in items:
        name = _clean(line.get("product_name") or line.get("name"))
        if name and name not in names:
            names.append(name)
    if not names:
        single = _clean(row.get("product_name") or row.get("name"))
        if single:
            names.append(single)
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        return f"{len(names)} products"
    item_count = int(row.get("item_count") or 0)
    return f"{item_count} products" if item_count > 1 else "Product"


def _line_count(row: dict) -> int:
    items = [x for x in (row.get("items") or []) if isinstance(x, dict)]
    if items:
        return len(items)
    return max(int(row.get("item_count") or 1), 1)


def _display_status(value: Any) -> str:
    raw = _clean(value).lower()
    labels = {
        "requested": "Requested",
        "approved": "Approved",
        "dispatched": "Dispatched",
        "received": "Received",
        "received_with_discrepancy": "Received with Discrepancy",
        "delivered": "Delivered",
        "completed": "Completed",
        "invoiced": "Invoiced",
        "cancelled": "Cancelled",
        "rejected": "Rejected",
        "void": "Voided",
        "voided": "Voided",
        "active": "Active",
    }
    return labels.get(raw, raw.replace("_", " ").title() if raw else "-")


def _payment_label(value: Any) -> str:
    raw = _clean(value).lower()
    labels = {
        "unpaid": "Unpaid",
        "partially_paid": "Partially Paid",
        "paid": "Paid",
        "pending_confirmation": "Awaiting Confirmation",
        "not_recorded": "Not Recorded",
        "confirmed": "Paid",
    }
    return labels.get(raw, raw.replace("_", " ").title() if raw else "Not Recorded")


def _sale_date(row: dict):
    for field in ("sale_date", "received_at", "delivered_at", "dispatched_at", "created_at", "updated_at"):
        parsed = _date_value(row.get(field))
        if parsed:
            return parsed
    return None


def _sale_datetime(row: dict):
    for field in ("sale_date", "received_at", "delivered_at", "dispatched_at", "created_at", "updated_at"):
        value = row.get(field)
        if isinstance(value, datetime):
            converted = to_india_datetime(value)
            return converted.replace(tzinfo=None) if converted else value.replace(tzinfo=None)
        raw = _clean(value)
        if raw:
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
    return datetime.min


def _value_for_sale(row: dict, *, channel: str) -> float:
    # Settlement value is authoritative once buyer receipt has adjusted the
    # payable for damaged/rejected/missing goods. Original invoice/dispatch
    # value remains on its source document for audit.
    if row.get("settlement_total") not in (None, ""):
        return _money(row.get("settlement_total"))
    if channel == "ufc_farmer" and row.get("accepted_goods_total") not in (None, ""):
        return _money(row.get("accepted_goods_total"))
    return _money(_first(row, "grand_total", "total_amount", "amount", default=0))


def _row_common(row: dict, channel: str, channel_label: str, party: str, reference: str, *, sublabel: str = ""):
    value = _value_for_sale(row, channel=channel)
    paid = max(_money(_first(row, "amount_paid", "paid_amount", default=0)), 0)
    outstanding_raw = _first(row, "outstanding_amount", default=None)
    outstanding = max(_money(outstanding_raw), 0) if outstanding_raw not in (None, "") else max(round(value - paid, 2), 0)
    payment_status = _clean(row.get("payment_status")).lower()
    if not payment_status:
        payment_status = "paid" if outstanding <= 0.02 and value > 0 else ("partially_paid" if paid > 0 else "not_recorded")
    return {
        "id": _clean(row.get("_id")),
        "channel": channel,
        "channel_label": channel_label,
        "date": _sale_date(row),
        "date_display": format_ist_datetime(_first(row, "sale_date", "received_at", "delivered_at", "dispatched_at", "created_at", "updated_at"), "%d %b %Y", "-"),
        "sort_datetime": _sale_datetime(row),
        "reference": reference or "-",
        "party": party or "-",
        "party_sublabel": sublabel or "",
        "products": _product_summary(row),
        "line_count": _line_count(row),
        "value": value,
        "paid": paid,
        "outstanding": outstanding,
        "payment_status": payment_status,
        "payment_status_label": _payment_label(payment_status),
        "status": _clean(row.get("status") or row.get("receipt_status") or "completed").lower(),
        "status_label": _display_status(row.get("receipt_status") or row.get("status") or "completed"),
        "receipt_adjustment": _money(row.get("receipt_adjustment_amount")),
        "source_collection": "",
        "source_id": _clean(row.get("_id")),
    }


def _collect_sales(limit_each: int = 1500):
    rows = []

    for source in mongo.db.avpl_ufc_sales.find({}).sort("created_at", -1).limit(limit_each):
        row = _row_common(
            source,
            "avpl_ufc",
            "AVPL → UFC",
            _clean(source.get("centre_name") or source.get("centre_uid") or "UFC"),
            _clean(source.get("sale_number") or source.get("avpl_order_number") or source.get("order_number")),
            sublabel=_clean(source.get("centre_uid")),
        )
        row["source_collection"] = "avpl_ufc_sales"
        rows.append(row)

    for source in mongo.db.ufc_farmer_sales.find({}).sort("created_at", -1).limit(limit_each):
        row = _row_common(
            source,
            "ufc_farmer",
            "UFC → Farmer",
            _clean(source.get("farmer_name") or "Farmer"),
            _clean(source.get("sale_number") or source.get("order_number")),
            sublabel=_clean(source.get("centre_name") or source.get("centre_uid")),
        )
        row["source_collection"] = "ufc_farmer_sales"
        rows.append(row)

    for source in mongo.db.pos_sales.find({"$or": [{"seller_type": "ufc"}, {"seller_type": {"$exists": False}}]}).sort("created_at", -1).limit(limit_each):
        # POS is intentionally retained because it is a legitimate direct UFC
        # sale channel and is also where pre-modernisation Sales data may live.
        party = _clean(source.get("farmer_name") or source.get("buyer_name") or source.get("farmer_phone") or "Farmer / Buyer")
        row = _row_common(
            source,
            "ufc_pos",
            "UFC POS",
            party,
            _clean(source.get("sale_number") or source.get("invoice_number") or source.get("source_reference") or source.get("receipt_number")),
            sublabel=_clean(source.get("centre_uid")),
        )
        row["source_collection"] = "pos_sales"
        rows.append(row)

    for source in mongo.db.farmer_marketplace_sales.find({"status": {"$ne": "cancelled"}}).sort("created_at", -1).limit(limit_each):
        buyer = source.get("buyer") or {}
        buyer_name = _clean(buyer.get("name") or buyer.get("display_name") or source.get("buyer_name") or source.get("buyer_type"))
        farmer_name = _clean(source.get("seller_farmer_name") or "Farmer")
        party = f"{farmer_name} → {buyer_name}" if buyer_name else farmer_name
        row = _row_common(
            source,
            "farmer_produce",
            "Farmer Produce",
            party,
            _clean(source.get("sale_number") or source.get("order_number")),
            sublabel=_clean(source.get("seller_centre_uid")),
        )
        row["source_collection"] = "farmer_marketplace_sales"
        rows.append(row)

    rows.sort(key=lambda item: item.get("sort_datetime") or datetime.min, reverse=True)
    return rows


def _activity_row(source: dict, channel: str, channel_label: str, party: str, reference: str, *, sublabel: str = ""):
    row = _row_common(source, channel, channel_label, party, reference, sublabel=sublabel)
    # Orders may not yet have a Sale document. Prefer their own order totals and
    # keep payment as Not Recorded until a real settlement record exists.
    row["status"] = _clean(source.get("status") or "requested").lower()
    row["status_label"] = _display_status(source.get("receipt_status") or source.get("status") or "requested")
    return row


def _collect_activity(limit_each: int = 1500):
    rows = []
    for source in mongo.db.avpl_ufc_orders.find({}).sort("created_at", -1).limit(limit_each):
        row = _activity_row(
            source, "avpl_ufc", "AVPL → UFC",
            _clean(source.get("centre_name") or source.get("centre_uid") or "UFC"),
            _clean(source.get("order_number")), sublabel=_clean(source.get("centre_uid")),
        )
        row["value"] = _money(_first(source, "settlement_total", "accepted_goods_total", "order_total", "grand_total", "total_amount", default=row.get("value")))
        row["source_collection"] = "avpl_ufc_orders"
        rows.append(row)

    for source in mongo.db.ufc_farmer_orders.find({}).sort("created_at", -1).limit(limit_each):
        row = _activity_row(
            source, "ufc_farmer", "UFC → Farmer",
            _clean(source.get("farmer_name") or "Farmer"),
            _clean(source.get("order_number")), sublabel=_clean(source.get("centre_name") or source.get("centre_uid")),
        )
        row["value"] = _money(_first(source, "settlement_total", "accepted_goods_total", "grand_total", "total_amount", "order_total", default=row.get("value")))
        row["source_collection"] = "ufc_farmer_orders"
        rows.append(row)

    for source in mongo.db.farmer_produce_marketplace_orders.find({}).sort("created_at", -1).limit(limit_each):
        buyer = source.get("buyer") or {}
        buyer_name = _clean(buyer.get("name") or buyer.get("display_name") or source.get("buyer_name") or source.get("buyer_type"))
        farmer_name = _clean(source.get("seller_farmer_name") or "Farmer")
        party = f"{farmer_name} → {buyer_name}" if buyer_name else farmer_name
        row = _activity_row(
            source, "farmer_produce", "Farmer Produce", party,
            _clean(source.get("order_number")), sublabel=_clean(source.get("seller_centre_uid")),
        )
        row["value"] = _money(_first(source, "settlement_total", "accepted_goods_total", "total_amount", "grand_total", default=row.get("value")))
        row["source_collection"] = "farmer_produce_marketplace_orders"
        rows.append(row)

    # POS has no separate order object; the sale itself is its authoritative
    # operational activity record.
    for source in mongo.db.pos_sales.find({"$or": [{"seller_type": "ufc"}, {"seller_type": {"$exists": False}}]}).sort("created_at", -1).limit(limit_each):
        party = _clean(source.get("farmer_name") or source.get("buyer_name") or source.get("farmer_phone") or "Farmer / Buyer")
        row = _activity_row(
            source, "ufc_pos", "UFC POS", party,
            _clean(source.get("sale_number") or source.get("invoice_number") or source.get("source_reference") or source.get("receipt_number")),
            sublabel=_clean(source.get("centre_uid")),
        )
        row["source_collection"] = "pos_sales"
        rows.append(row)

    rows.sort(key=lambda item: item.get("sort_datetime") or datetime.min, reverse=True)
    return rows


def _filter_sales(rows, *, period="this_month", channel="all", payment_status="all", q=""):
    start, end = _period_bounds(period)
    channel_key = _clean(channel).lower() or "all"
    payment_key = _clean(payment_status).lower() or "all"
    needle = _clean(q).lower()
    result = []
    for row in rows:
        when = row.get("date")
        if when and start and when < start:
            continue
        if when and end and when > end:
            continue
        if channel_key != "all" and row.get("channel") != channel_key:
            continue
        if payment_key != "all" and row.get("payment_status") != payment_key:
            continue
        if needle:
            haystack = " ".join(
                _clean(row.get(key))
                for key in ("channel_label", "reference", "party", "party_sublabel", "products", "status_label", "payment_status_label")
            ).lower()
            if needle not in haystack:
                continue
        result.append(row)
    return result


def _paginate(rows, page=1, per_page=25):
    try:
        page = max(int(page or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(per_page or 25)
    except (TypeError, ValueError):
        per_page = 25
    per_page = per_page if per_page in {25, 50, 100} else 25
    total = len(rows)
    total_pages = max(ceil(total / per_page), 1)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    return rows[start:start + per_page], {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def _summary(rows):
    return {
        "sales_count": len(rows),
        "sales_value": round(sum(_num(row.get("value")) for row in rows), 2),
        "received": round(sum(_num(row.get("paid")) for row in rows), 2),
        "outstanding": round(sum(_num(row.get("outstanding")) for row in rows), 2),
        "receipt_adjustments": round(sum(_num(row.get("receipt_adjustment")) for row in rows), 2),
    }


def get_sales_overview(actor_user_id, *, period="this_month", channel="all", payment_status="all", q="", page=1, per_page=25):
    _actor(actor_user_id)
    all_rows = _collect_sales()
    filtered = _filter_sales(all_rows, period=period, channel=channel, payment_status=payment_status, q=q)
    page_rows, pagination = _paginate(filtered, page=page, per_page=per_page)
    return {
        "rows": page_rows,
        "summary": _summary(filtered),
        "channels": CHANNELS,
        "periods": PERIODS,
        "payment_statuses": PAYMENT_STATUSES,
        "selected_period": _clean(period) or "this_month",
        "selected_channel": _clean(channel) or "all",
        "selected_payment": _clean(payment_status) or "all",
        "q": _clean(q),
        "pagination": pagination,
    }


def get_sales_activity(actor_user_id, *, period="this_month", channel="all", q="", page=1, per_page=25):
    _actor(actor_user_id)
    rows = _filter_sales(_collect_activity(), period=period, channel=channel, payment_status="all", q=q)
    page_rows, pagination = _paginate(rows, page=page, per_page=per_page)
    status_counts = {}
    for row in rows:
        key = row.get("status_label") or "Other"
        status_counts[key] = status_counts.get(key, 0) + 1
    in_progress = sum(1 for row in rows if row.get("status") in {"requested", "approved", "dispatched", "processing"})
    completed = sum(1 for row in rows if row.get("status") in {"received", "delivered", "completed", "invoiced"})
    return {
        "rows": page_rows,
        "summary": {
            "activity_count": len(rows),
            "business_value": round(sum(_num(r.get("value")) for r in rows), 2),
            "in_progress": in_progress,
            "completed": completed,
        },
        "status_counts": status_counts,
        "channels": CHANNELS,
        "periods": PERIODS,
        "selected_period": _clean(period) or "this_month",
        "selected_channel": _clean(channel) or "all",
        "q": _clean(q),
        "pagination": pagination,
    }


def _finance_lead_matches(lead, q):
    needle = _clean(q).lower()
    if not needle:
        return True
    values = [
        lead.get("farmer_name"), lead.get("farmer_mobile"), lead.get("farmer_address"),
        lead.get("centre_uid"), lead.get("mitra_uid"), lead.get("purpose"),
        lead.get("status"), lead.get("sales_followup_status"), lead.get("amount"),
        lead.get("total_transaction"), lead.get("sales_followup_note"),
    ]
    return needle in " ".join(_clean(v) for v in values).lower()


def get_finance_leads(actor_user_id, *, q="", followup="all", page=1, per_page=25):
    _actor(actor_user_id)
    query = {"visible_to_roles": SALES_ROLE}
    leads = list(mongo.db.financial_assistance_leads.find(query).sort("created_at", -1).limit(2000))
    followup_key = _clean(followup).lower() or "all"
    rows = []
    for lead in leads:
        status = _clean(lead.get("sales_followup_status") or "new").lower()
        if followup_key != "all" and status != followup_key:
            continue
        if not _finance_lead_matches(lead, q):
            continue
        row = dict(lead)
        row["id"] = str(row.get("_id") or "")
        row["followup_status"] = status
        row["followup_status_label"] = dict(FOLLOWUP_STATUSES).get(status, status.replace("_", " ").title())
        row["finance_status_label"] = _display_status(row.get("status") or "new")
        row["amount_value"] = _money(row.get("amount"))
        row["transaction_value"] = _money(row.get("total_transaction"))
        row["created_at_display"] = format_ist_datetime(row.get("created_at"), "%d %b %Y", "-")
        row["updated_at_display"] = format_ist_datetime(row.get("sales_followup_at"), "%d %b %Y, %I:%M %p", "-")
        rows.append(row)
    page_rows, pagination = _paginate(rows, page=page, per_page=per_page)
    return {
        "rows": page_rows,
        "q": _clean(q),
        "selected_followup": followup_key,
        "followup_statuses": [("all", "All Follow-up")] + FOLLOWUP_STATUSES,
        "pagination": pagination,
        "summary": {
            "total": len(rows),
            "open": sum(1 for row in rows if row.get("followup_status") != "closed"),
            "new": sum(1 for row in rows if row.get("followup_status") == "new"),
            "follow_up": sum(1 for row in rows if row.get("followup_status") in {"contacted", "follow_up"}),
            "forwarded": sum(1 for row in rows if row.get("followup_status") == "forwarded"),
        },
    }


def update_finance_lead_followup(actor_user_id, lead_id, *, followup_status, note=""):
    actor = _actor(actor_user_id)
    oid = _oid(lead_id)
    if not oid:
        raise ValueError("Invalid finance lead reference.")
    lead = mongo.db.financial_assistance_leads.find_one({"_id": oid, "visible_to_roles": SALES_ROLE})
    if not lead:
        raise ValueError("Finance lead was not found or is not available to Sales UnnatFarm.")
    allowed = {key for key, _ in FOLLOWUP_STATUSES}
    status = _clean(followup_status).lower()
    if status not in allowed:
        raise ValueError("Choose a valid Sales follow-up status.")
    clean_note = _clean(note)[:1000]
    timestamp = now_utc()
    history = {
        "status": status,
        "note": clean_note,
        "actor_user_id": actor.get("_id"),
        "actor_name": actor.get("name") or actor.get("username") or "Sales UnnatFarm",
        "at": timestamp,
    }
    mongo.db.financial_assistance_leads.update_one(
        {"_id": oid},
        {
            "$set": {
                "sales_followup_status": status,
                "sales_followup_note": clean_note,
                "sales_followup_by": actor.get("_id"),
                "sales_followup_by_name": history["actor_name"],
                "sales_followup_at": timestamp,
                "updated_at": timestamp,
            },
            "$push": {"sales_followup_history": history},
        },
    )
    return {"message": "Finance lead follow-up updated.", "status": status}


def get_sales_dashboard(actor_user_id):
    _actor(actor_user_id)
    month_rows = _filter_sales(_collect_sales(), period="this_month", channel="all", payment_status="all", q="")
    avpl_rows = [r for r in month_rows if r.get("channel") == "avpl_ufc"]
    ufc_farmer_rows = [r for r in month_rows if r.get("channel") in {"ufc_farmer", "ufc_pos"}]
    produce_rows = [r for r in month_rows if r.get("channel") == "farmer_produce"]
    finance = get_finance_leads(actor_user_id, followup="all", page=1, per_page=100)

    requested_avpl = mongo.db.avpl_ufc_orders.count_documents({"status": "requested"})
    farmer_pending = mongo.db.ufc_farmer_orders.count_documents({"status": {"$in": ["requested", "approved", "dispatched"]}})
    produce_pending = mongo.db.farmer_produce_marketplace_orders.count_documents({"status": {"$in": ["requested", "approved", "dispatched"]}})
    outstanding_count = sum(1 for r in month_rows if _num(r.get("outstanding")) > 0.02)

    attention = []
    if finance["summary"]["new"]:
        attention.append({"label": "New finance leads", "value": finance["summary"]["new"], "target": "finance"})
    if requested_avpl:
        attention.append({"label": "UFC orders awaiting AVPL review", "value": requested_avpl, "target": "activity"})
    if farmer_pending:
        attention.append({"label": "Farmer input orders in progress", "value": farmer_pending, "target": "activity"})
    if produce_pending:
        attention.append({"label": "Farmer produce orders in progress", "value": produce_pending, "target": "activity"})
    if outstanding_count:
        attention.append({"label": "Sales with money outstanding", "value": outstanding_count, "target": "sales"})

    return {
        "kpis": [
            {"label": "AVPL → UFC", "value": round(sum(r["value"] for r in avpl_rows), 2), "kind": "money", "note": "This month"},
            {"label": "UFC → Farmer", "value": round(sum(r["value"] for r in ufc_farmer_rows), 2), "kind": "money", "note": "Orders + POS this month"},
            {"label": "Farmer Produce", "value": round(sum(r["value"] for r in produce_rows), 2), "kind": "money", "note": "This month"},
            {"label": "Open Finance Leads", "value": finance["summary"].get("open", 0), "kind": "count", "note": "Sales follow-up"},
        ],
        "attention": attention[:5],
        "recent": month_rows[:8],
        "snapshot": {
            "sales_records": len(month_rows),
            "received": round(sum(r["paid"] for r in month_rows), 2),
            "outstanding": round(sum(r["outstanding"] for r in month_rows), 2),
            "receipt_adjustments": round(sum(r["receipt_adjustment"] for r in month_rows), 2),
        },
    }
