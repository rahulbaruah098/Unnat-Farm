from __future__ import annotations
from app.utils.timezone import business_today

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.extensions import mongo
from app.services.accounting_product_mapping_service import get_product_accounting_mapping_for_posting
from app.services.avpl_ufc_sales_service import _resolve_gst_state
from app.utils.helpers import now_utc


ORDER_COLLECTION = "ufc_farmer_orders"
LISTING_COLLECTION = "ufc_farmer_marketplace_listings"
UFC_LOT_COLLECTION = "ufc_inventory_lots"
UFC_MOVEMENT_COLLECTION = "ufc_stock_movements"
SALE_COLLECTION = "ufc_farmer_sales"
INVOICE_COLLECTION = "ufc_farmer_sales_invoices"
RECEIVABLE_COLLECTION = "ufc_farmer_receivables"
FARMER_PAYABLE_COLLECTION = "farmer_payables"
FARMER_PURCHASE_COLLECTION = "farmer_purchase_entries"
AUDIT_COLLECTION = "ufc_farmer_order_audit"

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.0001")

ORDER_STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "delivered": "Delivered",
}
PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
    "not_recorded": "Not Recorded Yet",
}

GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAYMENT_TERM_LABELS = {
    "cod": "Pay on Delivery",
    "credit": "Credit / Pay Later",
    "prepaid_online": "Prepaid / Online (Coming Soon)",
}


def _decimal(value, default="0"):
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _money(value):
    return f"{_decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def _qty(value):
    number = _decimal(value).quantize(QTY_QUANTUM)
    return f"{number:f}".rstrip("0").rstrip(".") or "0"


def _clean(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _date_iso(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return business_today().isoformat()


def _active_user(user):
    return not (
        user.get("active", True) is False
        or user.get("is_active", True) is False
        or str(user.get("status") or "").strip().lower() == "inactive"
    )


def _ensure_indexes():
    definitions = [
        (ORDER_COLLECTION, [("order_number", ASCENDING)], {"unique": True, "name": "ufc_farmer_order_number_unique"}),
        (ORDER_COLLECTION, [("request_token", ASCENDING)], {"unique": True, "name": "ufc_farmer_request_token_unique", "partialFilterExpression": {"request_token": {"$exists": True, "$gt": ""}}}),
        (ORDER_COLLECTION, [("centre_uid", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)], {"name": "ufc_farmer_order_centre_status_idx"}),
        (ORDER_COLLECTION, [("farmer_user_id", ASCENDING), ("created_at", DESCENDING)], {"name": "ufc_farmer_order_farmer_idx"}),
        (UFC_MOVEMENT_COLLECTION, [("source_posting_key", ASCENDING)], {"unique": True, "name": "ufc_stock_movement_source_unique"}),
        (SALE_COLLECTION, [("ufc_farmer_order_id", ASCENDING)], {"unique": True, "name": "ufc_farmer_sale_order_unique"}),
        (SALE_COLLECTION, [("sale_number", ASCENDING)], {"unique": True, "name": "ufc_farmer_sale_number_unique"}),
        (INVOICE_COLLECTION, [("ufc_farmer_order_id", ASCENDING)], {"unique": True, "name": "ufc_farmer_invoice_order_unique"}),
        (INVOICE_COLLECTION, [("document_number", ASCENDING)], {"unique": True, "name": "ufc_farmer_invoice_number_unique"}),
        (RECEIVABLE_COLLECTION, [("ufc_farmer_order_id", ASCENDING)], {"unique": True, "name": "ufc_farmer_receivable_order_unique"}),
        (FARMER_PAYABLE_COLLECTION, [("ufc_farmer_order_id", ASCENDING)], {"unique": True, "name": "farmer_payable_order_unique"}),
        (FARMER_PURCHASE_COLLECTION, [("ufc_farmer_order_id", ASCENDING)], {"unique": True, "name": "farmer_purchase_order_unique"}),
        (AUDIT_COLLECTION, [("ufc_farmer_order_id", ASCENDING), ("created_at", DESCENDING)], {"name": "ufc_farmer_order_audit_idx"}),
    ]
    for collection_name, keys, options in definitions:
        try:
            mongo.db[collection_name].create_index(keys, **options)
        except Exception:
            pass


def _get_user(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Invalid authenticated user.")
    user = mongo.db.users.find_one({"_id": oid})
    if not user:
        raise ValueError("Authenticated user was not found.")
    if not _active_user(user):
        raise PermissionError("Inactive users cannot perform this action.")
    user["resolved_role"] = str(user.get("role") or "").strip().lower()
    user["resolved_name"] = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or user.get("phone")
        or user["resolved_role"].replace("_", " ").title()
    )
    return user


def _resolve_farmer(actor_user_id):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "farmer":
        raise PermissionError("Only Farmers can place UFC Marketplace orders.")

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.farmer_master.find_one({"linked_user_id": actor["_id"]})
        or mongo.db.farmer_master.find_one({"contact_no": actor.get("phone")})
        or {}
    )
    centre_uid = _clean(
        farmer.get("centre_uid")
        or farmer.get("mapped_centre_uid")
        or actor.get("mapped_centre_uid")
        or actor.get("centre_uid"),
        80,
    )
    if not centre_uid:
        raise ValueError("Your Farmer profile is not mapped to a UFC Centre yet.")

    centre = (
        mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid})
        or mongo.db.ufc_centre_master.find_one({"centre_uid": centre_uid})
        or mongo.db.users.find_one({"centre_uid": centre_uid, "role": "ufc_admin"})
        or {}
    )
    centre_name = (
        centre.get("name_of_enterprise")
        or centre.get("enterprise_name")
        or centre.get("centre_name")
        or centre.get("name")
        or centre_uid
    )
    farmer_name = farmer.get("name") or actor.get("resolved_name") or "Farmer"
    return actor, farmer, centre_uid, centre_name, farmer_name


def _resolve_ufc_admin(actor_user_id, centre_uid_hint=None):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "ufc_admin":
        raise PermissionError("Only UFC Admin can manage Farmer orders.")
    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor["_id"]})
        or {}
    )
    centre_uid = _clean(
        master.get("centre_uid")
        or actor.get("centre_uid")
        or actor.get("mapped_centre_uid")
        or centre_uid_hint,
        80,
    )
    hint = _clean(centre_uid_hint, 80)
    if hint and centre_uid and hint != centre_uid:
        raise PermissionError("Your session Centre UID does not match your UFC Admin profile. Please log in again.")
    if not centre_uid:
        raise ValueError("This UFC Admin is not linked to a Centre UID.")
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or master or {}
    centre_name = (
        centre.get("name_of_enterprise")
        or centre.get("enterprise_name")
        or centre.get("centre_name")
        or centre.get("name")
        or centre_uid
    )
    return actor, centre, centre_uid, centre_name


def _next_number(counter_key, prefix, digits=5):
    year = business_today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"{counter_key}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"{prefix}-{year}-{sequence:0{digits}d}"


def _next_centre_number(counter_key, centre_uid, prefix, digits=5):
    year = business_today().year
    safe_centre = "".join(ch for ch in str(centre_uid or "UFC").upper() if ch.isalnum())[:14] or "UFC"
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"{counter_key}:{safe_centre}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"{safe_centre}-{prefix}-{year}-{sequence:0{digits}d}"


def _lot_expired(lot):
    expiry = lot.get("expiry_date")
    if not expiry:
        return False
    try:
        if isinstance(expiry, datetime):
            expiry_date = expiry.date()
        elif isinstance(expiry, date):
            expiry_date = expiry
        else:
            expiry_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
        return expiry_date < business_today()
    except Exception:
        return False


def _lot_saleable(lot):
    if str(lot.get("status") or "").lower() in {"cancelled", "expired"} or _lot_expired(lot):
        return Decimal("0")
    physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
    reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
    damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
    blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
    return max(physical - reserved - damaged - blocked, Decimal("0"))


def _product_saleable(centre_uid, product_id):
    product_oid = _to_object_id(product_id)
    if not product_oid:
        return Decimal("0")
    total = Decimal("0")
    for lot in mongo.db[UFC_LOT_COLLECTION].find({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
        "status": {"$ne": "cancelled"},
    }):
        total += _lot_saleable(lot)
    return total


def _candidate_ufc_lots(centre_uid, product_id):
    product_oid = _to_object_id(product_id)
    rows = list(mongo.db[UFC_LOT_COLLECTION].find({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
        "status": {"$nin": ["cancelled", "expired"]},
        "available_quantity": {"$gt": 0},
    }))
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("expiry_date") or "9999-12-31")[:10],
            row.get("created_at") or datetime.min,
            str(row.get("_id")),
        ),
    )


def _append_history(order_id, *, action, actor, note="", from_status=None, to_status=None):
    item = {
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "note": _clean(note, 1000),
        "actor_user_id": actor.get("_id"),
        "actor_name": actor.get("resolved_name") or actor.get("name") or "User",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "created_at": now_utc(),
    }
    mongo.db[ORDER_COLLECTION].update_one({"_id": order_id}, {"$push": {"history": item}, "$set": {"updated_at": now_utc()}})
    mongo.db[AUDIT_COLLECTION].insert_one({"ufc_farmer_order_id": order_id, **item})


def _notify_user(user_id, title, message, role=""):
    if not user_id:
        return
    mongo.db.notifications.insert_one({
        "to_user_id": str(user_id),
        "role": role,
        "title": title,
        "message": message,
        "status": "unread",
        "created_at": now_utc(),
    })


def _notify_centre_admins(centre_uid, title, message):
    seen = set()
    users = mongo.db.users.find({"role": "ufc_admin", "$or": [{"centre_uid": centre_uid}, {"mapped_centre_uid": centre_uid}]})
    for user in users:
        key = str(user.get("_id") or "")
        if key and key not in seen and _active_user(user):
            seen.add(key)
            _notify_user(key, title, message, "ufc_admin")


def _farmer_snapshot(actor, farmer, farmer_name):
    gstin = str(farmer.get("gst_number") or farmer.get("gstin") or actor.get("gstin") or "").strip().upper()
    state_name, state_code = _resolve_gst_state(
        farmer.get("state") or actor.get("state") or "",
        farmer.get("state_code") or actor.get("state_code") or "",
        gstin,
    )
    return {
        "farmer_user_id": actor.get("_id"),
        "farmer_user_id_str": str(actor.get("_id") or ""),
        "farmer_master_id": farmer.get("_id"),
        "farmer_master_id_str": str(farmer.get("_id") or ""),
        "name": farmer_name,
        "contact_no": farmer.get("contact_no") or actor.get("phone") or "",
        "email": farmer.get("email") or actor.get("email") or "",
        "gstin": gstin,
        "state": state_name or farmer.get("state") or actor.get("state") or "",
        "state_code": state_code,
        "district": farmer.get("district") or actor.get("district") or "",
        "block": farmer.get("block") or actor.get("block") or "",
        "village": farmer.get("village") or actor.get("village") or "",
        "address": farmer.get("address") or actor.get("address") or "",
    }


def _valid_gstin(value):
    return bool(GSTIN_PATTERN.fullmatch(str(value or "").strip().upper()))


def _centre_snapshot(centre, centre_uid, centre_name):
    centre = dict(centre or {})
    linked_user = {}
    linked_user_id = centre.get("linked_user_id")
    if linked_user_id:
        linked_user = (
            mongo.db.users.find_one({"_id": _to_object_id(linked_user_id)})
            or mongo.db.users.find_one({"_id": linked_user_id})
            or {}
        )
    if not linked_user:
        linked_user = (
            mongo.db.users.find_one({"role": "ufc_admin", "centre_uid": centre_uid})
            or mongo.db.users.find_one({"role": "ufc_admin", "mapped_centre_uid": centre_uid})
            or {}
        )

    gstin = str(centre.get("gst_number") or centre.get("gstin") or linked_user.get("gstin") or "").strip().upper()
    gstin_valid = _valid_gstin(gstin)
    explicit_registered = centre.get("gst_registered")
    if explicit_registered is None:
        explicit_registered = centre.get("is_gst_registered")
    if isinstance(explicit_registered, str):
        explicit_registered = explicit_registered.strip().lower() in {"1", "true", "yes", "on", "registered"}
    gst_registered = bool(gstin_valid and explicit_registered is not False)
    gst_warning = ""
    if gstin and not gstin_valid:
        gst_warning = "GSTIN is present in the UFC master but is not a valid 15-character GSTIN. GST is not charged until the master data is corrected."
    elif explicit_registered is True and not gstin:
        gst_warning = "UFC is marked GST-registered but no valid GSTIN is available. GST is not charged until the master data is corrected."

    state_name, state_code = _resolve_gst_state(
        centre.get("state") or linked_user.get("state") or "",
        centre.get("state_code") or linked_user.get("state_code") or "",
        gstin if gstin_valid else "",
    )
    return {
        "centre_uid": centre_uid,
        "legal_name": centre_name,
        "owner_name": centre.get("name_of_owner") or centre.get("name") or linked_user.get("name") or "",
        "gstin": gstin,
        "gstin_valid": gstin_valid,
        "gst_registered": gst_registered,
        "gst_configuration_warning": gst_warning,
        "pan": str(centre.get("pan_number") or centre.get("pan") or linked_user.get("pan") or "").strip().upper(),
        "state": state_name or centre.get("state") or linked_user.get("state") or "",
        "state_code": state_code,
        "district": centre.get("district") or linked_user.get("district") or "",
        "block": centre.get("block") or linked_user.get("block") or "",
        "village": centre.get("village") or linked_user.get("village") or "",
        "address": centre.get("address") or linked_user.get("address") or "",
        "phone": centre.get("contact_no") or centre.get("phone") or linked_user.get("phone") or "",
        "email": centre.get("email") or linked_user.get("email") or "",
    }


def _serialize_order(order):
    if not order:
        return None
    row = dict(order)
    row["id"] = str(row.get("_id") or "")
    row["source_product_id_str"] = str(row.get("source_product_id") or "")
    row["farmer_user_id_str"] = str(row.get("farmer_user_id") or "")
    row["sale_id_str"] = str(row.get("ufc_farmer_sale_id") or "")
    row["invoice_id_str"] = str(row.get("ufc_farmer_invoice_id") or "")
    row["purchase_entry_id_str"] = str(row.get("farmer_purchase_entry_id") or "")
    row["status_label"] = ORDER_STATUS_LABELS.get(str(row.get("status") or "requested"), str(row.get("status") or "").replace("_", " ").title())
    row["requested_quantity_display"] = _qty(row.get("requested_quantity"))
    row["approved_quantity_display"] = _qty(row.get("approved_quantity"))
    row["delivered_quantity_display"] = _qty(row.get("delivered_quantity"))
    row["unit_price_display"] = _money(row.get("unit_price"))
    row["taxable_value_display"] = _money(row.get("taxable_value"))
    row["gst_amount_display"] = _money(row.get("gst_amount"))
    row["grand_total_display"] = _money(row.get("grand_total") or row.get("total_amount"))
    row["amount_paid_display"] = _money(row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount"))
    row["outstanding_amount_display"] = _money(row.get("outstanding_amount") if row.get("outstanding_amount") is not None else row.get("grand_total") or row.get("total_amount"))
    row["payment_status_label"] = PAYMENT_STATUS_LABELS.get(str(row.get("payment_status") or "not_recorded"), str(row.get("payment_status") or "").replace("_", " ").title())
    row["payment_term_label"] = PAYMENT_TERM_LABELS.get(str(row.get("payment_term") or "cod"), str(row.get("payment_term") or "cod").replace("_", " ").title())
    return row


def create_farmer_order(actor_user_id, product_id, quantity, *, request_token="", note="", payment_term="cod"):
    _ensure_indexes()
    actor, farmer, centre_uid, centre_name, farmer_name = _resolve_farmer(actor_user_id)
    product_oid = _to_object_id(product_id)
    if not product_oid:
        raise ValueError("Invalid Marketplace product.")

    listing = mongo.db[LISTING_COLLECTION].find_one({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
        "status": "published",
    })
    if not listing:
        raise ValueError("This product is no longer published by your UFC.")

    requested = _decimal(quantity)
    minimum = max(_decimal(listing.get("min_order_quantity"), "1"), Decimal("0"))
    maximum = max(_decimal(listing.get("max_order_quantity")), Decimal("0"))
    price = _decimal(listing.get("selling_price"))
    if requested <= 0:
        raise ValueError("Order quantity must be greater than zero.")
    if minimum > 0 and requested < minimum:
        raise ValueError(f"Minimum order quantity is {_qty(minimum)} {listing.get('unit_code') or 'units'}.")
    if maximum > 0 and requested > maximum:
        raise ValueError(f"Maximum order quantity is {_qty(maximum)} {listing.get('unit_code') or 'units'}.")
    if price <= 0:
        raise ValueError("This product does not have a valid Farmer selling price yet.")

    payment_term = _clean(payment_term, 40).lower() or "cod"
    if payment_term == "prepaid_online":
        raise ValueError("Online prepaid payment is coming soon. Choose Pay on Delivery or Credit / Pay Later for now.")
    if payment_term not in {"cod", "credit"}:
        raise ValueError("Select Pay on Delivery or Credit / Pay Later.")

    saleable = _product_saleable(centre_uid, product_oid)
    if requested > saleable:
        raise ValueError(f"Only {_qty(saleable)} {listing.get('unit_code') or 'units'} is currently available.")

    token = _clean(request_token, 100)
    if token:
        existing = mongo.db[ORDER_COLLECTION].find_one({"request_token": token})
        if existing:
            if str(existing.get("farmer_user_id") or "") != str(actor.get("_id") or ""):
                raise PermissionError("Invalid order request token.")
            return {"order": _serialize_order(existing), "message": "This order was already placed."}

    product = mongo.db.products.find_one({"_id": product_oid}) or {}
    farmer_snapshot = _farmer_snapshot(actor, farmer, farmer_name)
    timestamp = now_utc()
    order_number = _next_centre_number("ufc_farmer_order", centre_uid, "F-ORD")
    total = requested * price
    document = {
        "order_number": order_number,
        "request_token": token or uuid4().hex,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "farmer_user_id": actor["_id"],
        "farmer_user_id_str": str(actor["_id"]),
        "farmer_master_id": farmer.get("_id"),
        "farmer_master_id_str": str(farmer.get("_id") or ""),
        "farmer_name": farmer_name,
        "farmer_contact": farmer_snapshot.get("contact_no") or "",
        "farmer_snapshot": farmer_snapshot,
        "source_product_id": product_oid,
        "source_product_id_str": str(product_oid),
        "marketplace_listing_id": listing.get("_id"),
        "marketplace_listing_id_str": str(listing.get("_id") or ""),
        "product_name": listing.get("product_name") or product.get("name") or product.get("product_name") or "Product",
        "product_code": listing.get("product_code") or product.get("sku") or product.get("product_code") or "",
        "category": listing.get("category") or product.get("category") or "",
        "product_role": listing.get("product_role") or product.get("product_role") or "",
        "unit_code": listing.get("unit_code") or product.get("unit") or "Unit",
        "requested_quantity": float(requested),
        "approved_quantity": 0.0,
        "reserved_quantity": 0.0,
        "delivered_quantity": 0.0,
        "unit_price": float(price),
        "total_amount": float(total),
        "order_note": _clean(note, 1000),
        "status": "requested",
        "stock_reserved": False,
        "stock_delivered": False,
        "financial_sync_status": "not_started",
        "payment_term": payment_term,
        "payment_term_label": PAYMENT_TERM_LABELS.get(payment_term, payment_term.replace("_", " ").title()),
        "payment_status": "not_recorded",
        "amount_paid": 0.0,
        "outstanding_amount": 0.0,
        "history": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[ORDER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except Exception:
        if token:
            existing = mongo.db[ORDER_COLLECTION].find_one({"request_token": token})
            if existing:
                return {"order": _serialize_order(existing), "message": "This order was already placed."}
        raise

    _append_history(document["_id"], action="place_order", actor=actor, note=note or f"Requested {_qty(requested)} {document['unit_code']} of {document['product_name']}.", from_status=None, to_status="requested")
    _notify_centre_admins(centre_uid, "New Farmer Order", f"{farmer_name} placed order {order_number} for {_qty(requested)} {document['unit_code']} of {document['product_name']}.")
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": document["_id"]})), "message": "Order placed successfully. Your UFC will review it."}


def _reserve_stock(order, approved_quantity, actor):
    needed = _decimal(approved_quantity)
    allocations = []
    reserved_updates = []
    today_iso = business_today().isoformat()

    for lot in _candidate_ufc_lots(order.get("centre_uid"), order.get("source_product_id")):
        if needed <= 0:
            break
        saleable = _lot_saleable(lot)
        if saleable <= 0:
            continue
        take = min(needed, saleable)
        take_float = float(take)
        result = mongo.db[UFC_LOT_COLLECTION].update_one(
            {
                "_id": lot["_id"],
                "centre_uid": order.get("centre_uid"),
                "status": {"$nin": ["cancelled", "expired"]},
                "$and": [
                    {"$or": [
                        {"expiry_date": {"$exists": False}},
                        {"expiry_date": None},
                        {"expiry_date": ""},
                        {"expiry_date": {"$gte": today_iso}},
                    ]},
                    {"$expr": {"$gte": [
                        {"$subtract": [
                            {"$ifNull": ["$available_quantity", 0]},
                            {"$add": [
                                {"$ifNull": ["$reserved_quantity", 0]},
                                {"$ifNull": ["$damaged_quantity", 0]},
                                {"$ifNull": ["$blocked_quantity", 0]},
                            ]},
                        ]},
                        take_float,
                    ]}},
                ],
            },
            {"$inc": {"reserved_quantity": take_float}, "$set": {"updated_at": now_utc(), "last_farmer_order_reservation_id": order["_id"]}},
        )
        if result.modified_count != 1:
            continue
        reserved_updates.append((lot["_id"], take_float))
        allocations.append({
            "inventory_lot_id": lot["_id"],
            "inventory_lot_id_str": str(lot["_id"]),
            "quantity": take_float,
            "quantity_display": _qty(take),
            "warehouse_code": lot.get("warehouse_code") or f"{order.get('centre_uid')}-MAIN",
            "warehouse_name": lot.get("warehouse_name") or f"{order.get('centre_name') or order.get('centre_uid')} Main Stock",
            "batch_number": lot.get("batch_number") or "",
            "barcode": lot.get("barcode") or "",
            "manufacturing_date": lot.get("manufacturing_date") or "",
            "expiry_date": lot.get("expiry_date") or "",
            "unit_code": lot.get("unit_code") or order.get("unit_code") or "Unit",
        })
        needed -= take

    if needed > 0:
        for lot_id, quantity_value in reserved_updates:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"_id": lot_id, "reserved_quantity": {"$gte": quantity_value}},
                {"$inc": {"reserved_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}},
            )
        raise RuntimeError("UFC stock changed while this order was being approved. Refresh and try again.")

    timestamp = now_utc()
    for allocation in allocations:
        key = f"FARMER-RESERVE:{order['_id']}:{allocation['inventory_lot_id']}"
        mongo.db[UFC_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": key},
            {"$setOnInsert": {
                "source_posting_key": key,
                "movement_uid": uuid4().hex,
                "centre_uid": order.get("centre_uid"),
                "centre_name": order.get("centre_name") or order.get("centre_uid"),
                "source_document_type": "farmer_order",
                "source_document_id": order["_id"],
                "source_document_id_str": str(order["_id"]),
                "source_document_number": order.get("order_number") or "",
                "source_product_id": order.get("source_product_id"),
                "source_product_id_str": str(order.get("source_product_id") or ""),
                "product_code": order.get("product_code") or "",
                "product_name": order.get("product_name") or "Product",
                "movement_type": "reservation",
                "direction": "reserve",
                "quantity": allocation["quantity"],
                "quantity_display": allocation["quantity_display"],
                "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                "warehouse_code": allocation.get("warehouse_code") or "",
                "warehouse_name": allocation.get("warehouse_name") or "",
                "batch_number": allocation.get("batch_number") or "",
                "barcode": allocation.get("barcode") or "",
                "manufacturing_date": allocation.get("manufacturing_date") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().isoformat(),
                "reason": f"Reserved for Farmer order {order.get('order_number') or ''} ({order.get('farmer_name') or 'Farmer'}).",
                "posted_by": actor.get("_id"),
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }},
            upsert=True,
        )
    return allocations


def approve_farmer_order(actor_user_id, centre_uid_hint, order_id, approved_quantity, note="", payment_due_days=None):
    _ensure_indexes()
    actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid, "centre_uid": centre_uid})
    if not order:
        raise ValueError("Farmer order was not found for your UFC Centre.")
    if order.get("status") != "requested":
        raise ValueError("Only a requested Farmer order can be approved.")

    approved = _decimal(approved_quantity)
    requested = _decimal(order.get("requested_quantity"))
    if approved <= 0:
        raise ValueError("Approved quantity must be greater than zero.")
    if approved > requested:
        raise ValueError("Approved quantity cannot exceed the Farmer requested quantity.")

    listing = mongo.db[LISTING_COLLECTION].find_one({
        "centre_uid": centre_uid,
        "source_product_id": order.get("source_product_id"),
        "status": "published",
    })
    if not listing:
        raise ValueError("This product is no longer published to the Farmer Marketplace. Publish it again or reject the order.")

    allocations = _reserve_stock(order, approved, actor)
    timestamp = now_utc()
    price = _decimal(order.get("unit_price"))
    total = approved * price
    due_days = 0
    if str(order.get("payment_term") or "cod") == "credit":
        raw_due_days = str(payment_due_days if payment_due_days is not None else "").strip()
        if raw_due_days:
            try:
                due_days = int(raw_due_days)
            except (TypeError, ValueError) as exc:
                raise ValueError("Credit days must be a whole number.") from exc
            if due_days < 0 or due_days > 365:
                raise ValueError("Credit days must be between 0 and 365.")
    result = mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "centre_uid": centre_uid, "status": "requested", "stock_reserved": {"$ne": True}},
        {"$set": {
            "status": "approved",
            "approved_quantity": float(approved),
            "reserved_quantity": float(approved),
            "total_amount": float(total),
            "reservation_allocations": allocations,
            "stock_reserved": True,
            "approval_note": _clean(note, 1000),
            "payment_due_days": due_days,
            "approved_by": actor["_id"],
            "approved_by_name": actor.get("resolved_name") or "",
            "approved_at": timestamp,
            "updated_at": timestamp,
        }},
    )
    if result.modified_count != 1:
        for allocation in allocations:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"_id": allocation["inventory_lot_id"], "reserved_quantity": {"$gte": allocation["quantity"]}},
                {"$inc": {"reserved_quantity": -allocation["quantity"]}, "$set": {"updated_at": now_utc()}},
            )
        raise RuntimeError("The order changed while approval was being saved. Refresh and try again.")

    _append_history(oid, action="approve_order", actor=actor, note=note or f"Approved {_qty(approved)} {order.get('unit_code') or 'units'}.", from_status="requested", to_status="approved")
    _notify_user(order.get("farmer_user_id"), "Farmer Order Approved", f"Your order {order.get('order_number')} was approved for {_qty(approved)} {order.get('unit_code') or 'units'}. Your UFC has reserved the stock.", "farmer")
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "message": "Order approved and UFC stock reserved."}


def reject_farmer_order(actor_user_id, centre_uid_hint, order_id, reason=""):
    actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid, "centre_uid": centre_uid})
    if not order:
        raise ValueError("Farmer order was not found for your UFC Centre.")
    if order.get("status") != "requested":
        raise ValueError("Only a requested Farmer order can be rejected.")
    timestamp = now_utc()
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "status": "requested"}, {"$set": {"status": "rejected", "rejection_reason": _clean(reason, 1000), "rejected_by": actor["_id"], "rejected_by_name": actor.get("resolved_name") or "", "rejected_at": timestamp, "updated_at": timestamp}})
    _append_history(oid, action="reject_order", actor=actor, note=reason or "Order rejected by UFC.", from_status="requested", to_status="rejected")
    _notify_user(order.get("farmer_user_id"), "Farmer Order Rejected", f"Your order {order.get('order_number')} was not approved. {(_clean(reason, 200) or '').strip()}", "farmer")
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "message": "Farmer order rejected."}


def _release_reservation(order, actor, reason=""):
    allocations = order.get("reservation_allocations") or []
    for allocation in allocations:
        lot_id = _to_object_id(allocation.get("inventory_lot_id"))
        quantity_value = float(_decimal(allocation.get("quantity")))
        if lot_id and quantity_value > 0:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"_id": lot_id, "reserved_quantity": {"$gte": quantity_value}},
                {"$inc": {"reserved_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}},
            )
            key = f"FARMER-RELEASE:{order['_id']}:{lot_id}"
            mongo.db[UFC_MOVEMENT_COLLECTION].update_one(
                {"source_posting_key": key},
                {"$setOnInsert": {
                    "source_posting_key": key,
                    "movement_uid": uuid4().hex,
                    "centre_uid": order.get("centre_uid"),
                    "centre_name": order.get("centre_name") or order.get("centre_uid"),
                    "source_document_type": "farmer_order_cancellation",
                    "source_document_id": order["_id"],
                    "source_document_id_str": str(order["_id"]),
                    "source_document_number": order.get("order_number") or "",
                    "source_product_id": order.get("source_product_id"),
                    "source_product_id_str": str(order.get("source_product_id") or ""),
                    "product_code": order.get("product_code") or "",
                    "product_name": order.get("product_name") or "Product",
                    "movement_type": "reservation_release",
                    "direction": "release",
                    "quantity": quantity_value,
                    "quantity_display": _qty(quantity_value),
                    "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                    "batch_number": allocation.get("batch_number") or "",
                    "expiry_date": allocation.get("expiry_date") or "",
                    "movement_date": business_today().isoformat(),
                    "reason": _clean(reason, 500) or f"Reservation released for Farmer order {order.get('order_number') or ''}.",
                    "posted_by": actor.get("_id"),
                    "posted_by_name": actor.get("resolved_name") or "",
                    "posted_at": now_utc(),
                    "created_at": now_utc(),
                }},
                upsert=True,
            )


def cancel_farmer_order(actor_user_id, order_id, *, centre_uid_hint=None, reason=""):
    actor = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("Farmer order was not found.")

    if actor.get("resolved_role") == "farmer":
        if str(order.get("farmer_user_id") or "") != str(actor.get("_id") or ""):
            raise PermissionError("You cannot cancel another Farmer's order.")
    elif actor.get("resolved_role") == "ufc_admin":
        _actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
        if str(order.get("centre_uid") or "") != str(centre_uid):
            raise PermissionError("This order does not belong to your UFC Centre.")
    else:
        raise PermissionError("You are not authorized to cancel this order.")

    old_status = str(order.get("status") or "")
    if old_status not in {"requested", "approved"}:
        raise ValueError("Only Requested or Approved orders can be cancelled before delivery.")
    if old_status == "approved" and order.get("stock_reserved") is True:
        _release_reservation(order, actor, reason or "Order cancelled before delivery.")

    timestamp = now_utc()
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "status": old_status}, {"$set": {"status": "cancelled", "stock_reserved": False, "reserved_quantity": 0.0, "cancellation_reason": _clean(reason, 1000), "cancelled_by": actor.get("_id"), "cancelled_by_name": actor.get("resolved_name") or "", "cancelled_at": timestamp, "updated_at": timestamp}})
    _append_history(oid, action="cancel_order", actor=actor, note=reason or "Order cancelled before delivery.", from_status=old_status, to_status="cancelled")

    if actor.get("resolved_role") == "farmer":
        _notify_centre_admins(order.get("centre_uid"), "Farmer Order Cancelled", f"{order.get('farmer_name') or 'Farmer'} cancelled order {order.get('order_number')}.")
    else:
        _notify_user(order.get("farmer_user_id"), "Farmer Order Cancelled", f"Your UFC cancelled order {order.get('order_number')}. {(_clean(reason, 200) or '').strip()}", "farmer")
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "message": "Order cancelled and any reserved UFC stock was released."}


def _apply_delivery_stock(order, actor):
    allocations = order.get("reservation_allocations") or []
    if not allocations:
        raise RuntimeError("No reserved UFC stock allocation exists for this order.")
    moved = []
    today_iso = business_today().isoformat()
    try:
        for allocation in allocations:
            lot_id = _to_object_id(allocation.get("inventory_lot_id"))
            quantity_value = float(_decimal(allocation.get("quantity")))
            if not lot_id or quantity_value <= 0:
                raise RuntimeError("The reservation contains an invalid UFC stock lot.")
            result = mongo.db[UFC_LOT_COLLECTION].update_one(
                {
                    "_id": lot_id,
                    "centre_uid": order.get("centre_uid"),
                    "status": {"$nin": ["cancelled", "expired"]},
                    "available_quantity": {"$gte": quantity_value},
                    "reserved_quantity": {"$gte": quantity_value},
                    "$or": [
                        {"expiry_date": {"$exists": False}},
                        {"expiry_date": None},
                        {"expiry_date": ""},
                        {"expiry_date": {"$gte": today_iso}},
                    ],
                },
                {"$inc": {"available_quantity": -quantity_value, "reserved_quantity": -quantity_value, "issued_quantity": quantity_value}, "$set": {"updated_at": now_utc(), "last_farmer_delivery_order_id": order["_id"]}},
            )
            if result.modified_count != 1:
                raise RuntimeError("A reserved UFC batch is no longer deliverable. Cancel the order to release reservation and place/approve it again.")
            moved.append((lot_id, quantity_value))
    except Exception:
        for lot_id, quantity_value in reversed(moved):
            mongo.db[UFC_LOT_COLLECTION].update_one({"_id": lot_id}, {"$inc": {"available_quantity": quantity_value, "reserved_quantity": quantity_value, "issued_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}})
        raise

    timestamp = now_utc()
    for allocation in allocations:
        key = f"FARMER-DELIVERY:{order['_id']}:{allocation.get('inventory_lot_id')}"
        mongo.db[UFC_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": key},
            {"$setOnInsert": {
                "source_posting_key": key,
                "movement_uid": uuid4().hex,
                "centre_uid": order.get("centre_uid"),
                "centre_name": order.get("centre_name") or order.get("centre_uid"),
                "source_document_type": "farmer_order_delivery",
                "source_document_id": order["_id"],
                "source_document_id_str": str(order["_id"]),
                "source_document_number": order.get("order_number") or "",
                "source_product_id": order.get("source_product_id"),
                "source_product_id_str": str(order.get("source_product_id") or ""),
                "product_code": order.get("product_code") or "",
                "product_name": order.get("product_name") or "Product",
                "movement_type": "sale",
                "direction": "out",
                "quantity": float(_decimal(allocation.get("quantity"))),
                "quantity_display": _qty(allocation.get("quantity")),
                "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                "warehouse_code": allocation.get("warehouse_code") or "",
                "warehouse_name": allocation.get("warehouse_name") or "",
                "batch_number": allocation.get("batch_number") or "",
                "barcode": allocation.get("barcode") or "",
                "manufacturing_date": allocation.get("manufacturing_date") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().isoformat(),
                "reason": f"Delivered to {order.get('farmer_name') or 'Farmer'} against order {order.get('order_number') or ''}.",
                "posted_by": actor.get("_id"),
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }},
            upsert=True,
        )


def _active_avpl_entity_for_mapping():
    return mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    })


def _financial_snapshot(order, centre):
    quantity = _decimal(order.get("delivered_quantity") or order.get("approved_quantity"))
    unit_price = _decimal(order.get("unit_price"))
    taxable_value = (quantity * unit_price).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    seller = _centre_snapshot(centre, order.get("centre_uid"), order.get("centre_name") or order.get("centre_uid"))
    buyer = dict(order.get("farmer_snapshot") or {})

    gst_rate = Decimal("0")
    taxability_code = "NON_GST"
    hsn_code = ""
    mapping_status = "unavailable"
    mapped_gst_rate = Decimal("0")
    entity = _active_avpl_entity_for_mapping()
    if entity:
        try:
            mapping = get_product_accounting_mapping_for_posting(
                entity["_id"],
                order.get("source_product_id"),
                transaction_date=_date_iso(order.get("delivered_at") or business_today()),
                operation="sales",
            )
            hsn = mapping.get("hsn") or {}
            taxability_code = str(hsn.get("taxability_code") or "").upper() or "NON_GST"
            hsn_code = hsn.get("hsn_code") or ""
            if taxability_code == "TAXABLE":
                mapped_gst_rate = _decimal((mapping.get("effective_gst_rate") or {}).get("total_rate"))
            mapping_status = "resolved"
        except Exception:
            # A non-GST UFC may still issue a Sales Receipt. Keep the sale usable
            # while exposing missing product mapping as a document warning.
            if seller.get("gst_registered"):
                raise
    elif seller.get("gst_registered"):
        raise RuntimeError("Product GST mapping cannot be resolved because the AVPL Accounting entity is unavailable.")

    # Product classification (HSN) is independent from whether this particular
    # UFC is legally allowed to collect GST. GST is charged only by a UFC with
    # a valid GST registration.
    if seller.get("gst_registered") and taxability_code == "TAXABLE":
        gst_rate = mapped_gst_rate

    seller_state_name, seller_state_code = _resolve_gst_state(seller.get("state") or "", seller.get("state_code") or "", seller.get("gstin") or "")
    buyer_state_name, buyer_state_code = _resolve_gst_state(buyer.get("state") or "", buyer.get("state_code") or "", buyer.get("gstin") or "")
    if seller.get("gst_registered") and gst_rate > 0:
        if not seller_state_code:
            raise RuntimeError("UFC Centre State is required before issuing a GST invoice.")
        if not buyer_state_code:
            raise RuntimeError("Farmer State is required before issuing a GST invoice.")

    tax_amount = (taxable_value * gst_rate / Decimal("100")).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    cgst = sgst = igst = Decimal("0")
    supply_type = "non_taxable"
    if tax_amount > 0:
        if seller_state_code == buyer_state_code:
            cgst = (tax_amount / Decimal("2")).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            sgst = tax_amount - cgst
            supply_type = "intra_state"
        else:
            igst = tax_amount
            supply_type = "inter_state"

    grand_total = taxable_value + cgst + sgst + igst
    return {
        "seller": seller,
        "buyer": buyer,
        "quantity": quantity,
        "unit_price": unit_price,
        "taxable_value": taxable_value,
        "taxability_code": taxability_code,
        "hsn_code": hsn_code,
        "gst_rate": gst_rate,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "gst_amount": cgst + sgst + igst,
        "grand_total": grand_total,
        "supply_type": supply_type,
        "place_of_supply_state": buyer_state_name or seller_state_name or buyer.get("state") or seller.get("state") or "",
        "place_of_supply_state_code": buyer_state_code or seller_state_code,
        "mapping_status": mapping_status,
        "mapped_gst_rate": mapped_gst_rate,
        "gst_configuration_warning": seller.get("gst_configuration_warning") or "",
        "document_warning": (
            seller.get("gst_configuration_warning")
            or ("Product HSN/GST mapping is not available." if mapping_status != "resolved" else "")
        ),
        "document_type": "tax_invoice" if seller.get("gst_registered") else "sales_receipt",
    }


def _upsert_sale(order, actor):
    existing = mongo.db[SALE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
    if existing:
        return existing
    quantity = _decimal(order.get("delivered_quantity") or order.get("approved_quantity"))
    unit_price = _decimal(order.get("unit_price"))
    timestamp = now_utc()
    document = {
        "sale_number": _next_centre_number("ufc_farmer_sale", order.get("centre_uid"), "SALE", digits=6),
        "ufc_farmer_order_id": order["_id"],
        "ufc_farmer_order_id_str": str(order["_id"]),
        "order_number": order.get("order_number") or "",
        "centre_uid": order.get("centre_uid"),
        "centre_name": order.get("centre_name") or order.get("centre_uid"),
        "farmer_user_id": order.get("farmer_user_id"),
        "farmer_user_id_str": str(order.get("farmer_user_id") or ""),
        "farmer_name": order.get("farmer_name") or "Farmer",
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "quantity": float(quantity),
        "unit_code": order.get("unit_code") or "Unit",
        "unit_price": float(unit_price),
        "base_amount": float(quantity * unit_price),
        "grand_total": float(quantity * unit_price),
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(quantity * unit_price),
        "status": "delivered",
        "sale_date": order.get("delivered_at") or timestamp,
        "created_by": actor.get("_id"),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[SALE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except Exception:
        existing = mongo.db[SALE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
        if existing:
            return existing
        raise


def _upsert_purchase(order, actor):
    existing = mongo.db[FARMER_PURCHASE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
    if existing:
        return existing
    quantity = _decimal(order.get("delivered_quantity") or order.get("approved_quantity"))
    unit_price = _decimal(order.get("unit_price"))
    total = quantity * unit_price
    timestamp = now_utc()
    document = {
        "purchase_number": _next_centre_number("farmer_purchase", order.get("centre_uid"), "F-PUR", digits=6),
        "ufc_farmer_order_id": order["_id"],
        "ufc_farmer_order_id_str": str(order["_id"]),
        "order_number": order.get("order_number") or "",
        "farmer_user_id": order.get("farmer_user_id"),
        "farmer_user_id_str": str(order.get("farmer_user_id") or ""),
        "farmer_master_id": order.get("farmer_master_id"),
        "farmer_name": order.get("farmer_name") or "Farmer",
        "seller_type": "ufc",
        "seller_centre_uid": order.get("centre_uid"),
        "seller_name": order.get("centre_name") or order.get("centre_uid"),
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "quantity": float(quantity),
        "unit_code": order.get("unit_code") or "Unit",
        "unit_price": float(unit_price),
        "total_amount": float(total),
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(total),
        "accounting_status": "not_posted",
        "financial_link_status": "awaiting_invoice",
        "purchase_date": order.get("delivered_at") or timestamp,
        "status": "received",
        "received_by": order.get("farmer_user_id"),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[FARMER_PURCHASE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except Exception:
        existing = mongo.db[FARMER_PURCHASE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
        if existing:
            return existing
        raise


def _upsert_invoice(order, sale, purchase, actor, centre, financial):
    existing = mongo.db[INVOICE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
    if existing:
        # Stage 8 tax hardening: older Stage 7 invoices may have been issued
        # before HSN/GST seller validation was strict. Refresh classification
        # metadata every time. Monetary tax totals are refreshed only while the
        # invoice is completely unsettled; once any payment exists the legal
        # amount is frozen and only non-monetary warnings/classification change.
        paid = _decimal(existing.get("amount_paid") if existing.get("amount_paid") is not None else existing.get("paid_amount"))
        has_payments = bool(existing.get("payment_ids")) or paid > Decimal("0.004")
        document_type = financial.get("document_type") or "sales_receipt"
        updates = {
            "seller": financial.get("seller") or existing.get("seller") or {},
            "buyer": financial.get("buyer") or existing.get("buyer") or {},
            "hsn_code": financial.get("hsn_code") or existing.get("hsn_code") or "",
            "taxability_code": financial.get("taxability_code") or existing.get("taxability_code") or "NON_GST",
            "product_mapping_status": financial.get("mapping_status") or "",
            "mapped_gst_rate": float(financial.get("mapped_gst_rate") or 0),
            "gst_configuration_warning": financial.get("gst_configuration_warning") or "",
            "document_warning": financial.get("document_warning") or "",
            "payment_term": order.get("payment_term") or existing.get("payment_term") or "cod",
            "payment_term_label": PAYMENT_TERM_LABELS.get(order.get("payment_term") or existing.get("payment_term") or "cod", "Pay on Delivery"),
            "payment_due_days": max(int(order.get("payment_due_days") or existing.get("payment_due_days") or 0), 0),
            "updated_at": now_utc(),
        }
        if not has_payments:
            new_total = _decimal(financial.get("grand_total"))
            prior_document_type = str(existing.get("document_type") or "sales_receipt")
            if prior_document_type != document_type:
                corrected_prefix = "SI" if document_type == "tax_invoice" else "RCPT"
                updates.update({
                    "previous_document_number": existing.get("document_number") or "",
                    "document_number": _next_centre_number("ufc_farmer_invoice", order.get("centre_uid"), corrected_prefix, digits=6),
                    "document_corrected_at": now_utc(),
                    "document_correction_reason": "GST registration/tax classification was re-evaluated before any payment settlement.",
                })
            updates.update({
                "document_type": document_type,
                "document_title": "Tax Invoice" if document_type == "tax_invoice" else "Sales Receipt",
                "quantity": float(financial.get("quantity") or 0),
                "unit_price": float(financial.get("unit_price") or 0),
                "taxable_value": float(financial.get("taxable_value") or 0),
                "gst_rate": float(financial.get("gst_rate") or 0),
                "cgst_amount": float(financial.get("cgst") or 0),
                "sgst_amount": float(financial.get("sgst") or 0),
                "igst_amount": float(financial.get("igst") or 0),
                "gst_amount": float(financial.get("gst_amount") or 0),
                "grand_total": float(new_total),
                "supply_type": financial.get("supply_type") or "non_taxable",
                "place_of_supply_state": financial.get("place_of_supply_state") or "",
                "place_of_supply_state_code": financial.get("place_of_supply_state_code") or "",
                "outstanding_amount": float(new_total),
                "payment_status": "unpaid",
                "due_date": (
                    (datetime.strptime(_date_iso(order.get("delivered_at") or business_today()), "%Y-%m-%d").date()
                     + timedelta(days=max(int(order.get("payment_due_days") or 0), 0))).isoformat()
                    if str(order.get("payment_term") or "cod") == "credit"
                    else _date_iso(order.get("delivered_at") or business_today())
                ),
                "tax_hardened_at": now_utc(),
            })
        else:
            updates["tax_hardening_note"] = "Invoice has payment history; monetary GST totals were not rewritten. Review/reissue through controlled finance workflow if correction is legally required."
        mongo.db[INVOICE_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": updates})
        return mongo.db[INVOICE_COLLECTION].find_one({"_id": existing["_id"]}) or existing
    document_type = financial.get("document_type") or "sales_receipt"
    prefix = "SI" if document_type == "tax_invoice" else "RCPT"
    timestamp = now_utc()
    document_number = _next_centre_number("ufc_farmer_invoice", order.get("centre_uid"), prefix, digits=6)
    document = {
        "document_number": document_number,
        "document_type": document_type,
        "document_title": "Tax Invoice" if document_type == "tax_invoice" else "Sales Receipt",
        "ufc_farmer_order_id": order["_id"],
        "ufc_farmer_order_id_str": str(order["_id"]),
        "order_number": order.get("order_number") or "",
        "ufc_farmer_sale_id": sale.get("_id"),
        "ufc_farmer_sale_id_str": str(sale.get("_id") or ""),
        "farmer_purchase_entry_id": purchase.get("_id"),
        "farmer_purchase_entry_id_str": str(purchase.get("_id") or ""),
        "centre_uid": order.get("centre_uid"),
        "seller": financial.get("seller") or {},
        "buyer": financial.get("buyer") or {},
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "hsn_code": financial.get("hsn_code") or "",
        "taxability_code": financial.get("taxability_code") or "NON_GST",
        "quantity": float(financial.get("quantity") or 0),
        "unit_code": order.get("unit_code") or "Unit",
        "unit_price": float(financial.get("unit_price") or 0),
        "taxable_value": float(financial.get("taxable_value") or 0),
        "gst_rate": float(financial.get("gst_rate") or 0),
        "cgst_amount": float(financial.get("cgst") or 0),
        "sgst_amount": float(financial.get("sgst") or 0),
        "igst_amount": float(financial.get("igst") or 0),
        "gst_amount": float(financial.get("gst_amount") or 0),
        "grand_total": float(financial.get("grand_total") or 0),
        "supply_type": financial.get("supply_type") or "non_taxable",
        "place_of_supply_state": financial.get("place_of_supply_state") or "",
        "place_of_supply_state_code": financial.get("place_of_supply_state_code") or "",
        "product_mapping_status": financial.get("mapping_status") or "",
        "mapped_gst_rate": float(financial.get("mapped_gst_rate") or 0),
        "gst_configuration_warning": financial.get("gst_configuration_warning") or "",
        "document_warning": financial.get("document_warning") or "",
        "payment_term": order.get("payment_term") or "cod",
        "payment_term_label": PAYMENT_TERM_LABELS.get(order.get("payment_term") or "cod", "Pay on Delivery"),
        "invoice_date": _date_iso(order.get("delivered_at") or business_today()),
        "due_date": (
            (datetime.strptime(_date_iso(order.get("delivered_at") or business_today()), "%Y-%m-%d").date()
             + timedelta(days=max(int(order.get("payment_due_days") or 0), 0))).isoformat()
            if str(order.get("payment_term") or "cod") == "credit"
            else _date_iso(order.get("delivered_at") or business_today())
        ),
        "payment_due_days": max(int(order.get("payment_due_days") or 0), 0),
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(financial.get("grand_total") or 0),
        "accounting_status": "not_posted",
        "status": "issued",
        "issued_by": actor.get("_id"),
        "issued_by_name": actor.get("resolved_name") or "",
        "issued_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[INVOICE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except Exception:
        existing = mongo.db[INVOICE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
        if existing:
            return existing
        raise


def _upsert_receivable_payable(order, sale, invoice, purchase):
    total = _decimal(invoice.get("grand_total"))
    timestamp = now_utc()
    receivable_doc = {
        "ufc_farmer_order_id": order["_id"],
        "order_number": order.get("order_number") or "",
        "ufc_farmer_sale_id": sale.get("_id"),
        "invoice_id": invoice.get("_id"),
        "document_number": invoice.get("document_number") or "",
        "centre_uid": order.get("centre_uid"),
        "farmer_user_id": order.get("farmer_user_id"),
        "farmer_name": order.get("farmer_name") or "Farmer",
        "amount": float(total),
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "payment_status": invoice.get("payment_status") or "unpaid",
        "status": "open" if _decimal(invoice.get("outstanding_amount"), str(total)) > 0 else "closed",
        "source": "automatic_ufc_farmer_sale",
        "updated_at": timestamp,
    }
    mongo.db[RECEIVABLE_COLLECTION].update_one(
        {"ufc_farmer_order_id": order["_id"]},
        {"$set": receivable_doc, "$setOnInsert": {"created_at": timestamp}},
        upsert=True,
    )
    receivable = mongo.db[RECEIVABLE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})

    payable_doc = {
        "ufc_farmer_order_id": order["_id"],
        "order_number": order.get("order_number") or "",
        "farmer_purchase_entry_id": purchase.get("_id"),
        "invoice_id": invoice.get("_id"),
        "document_number": invoice.get("document_number") or "",
        "farmer_user_id": order.get("farmer_user_id"),
        "farmer_name": order.get("farmer_name") or "Farmer",
        "seller_centre_uid": order.get("centre_uid"),
        "seller_name": order.get("centre_name") or order.get("centre_uid"),
        "amount": float(total),
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "payment_status": invoice.get("payment_status") or "unpaid",
        "status": "open" if _decimal(invoice.get("outstanding_amount"), str(total)) > 0 else "closed",
        "source": "automatic_ufc_farmer_sale",
        "updated_at": timestamp,
    }
    mongo.db[FARMER_PAYABLE_COLLECTION].update_one(
        {"ufc_farmer_order_id": order["_id"]},
        {"$set": payable_doc, "$setOnInsert": {"created_at": timestamp}},
        upsert=True,
    )
    payable = mongo.db[FARMER_PAYABLE_COLLECTION].find_one({"ufc_farmer_order_id": order["_id"]})
    return receivable, payable


def ensure_delivery_documents(actor_user_id, order_id):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("Farmer order was not found.")
    if order.get("status") != "delivered" or order.get("stock_delivered") is not True:
        raise ValueError("Sales documents can only be generated after physical delivery.")
    if actor.get("resolved_role") == "ufc_admin":
        _actor, centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, order.get("centre_uid"))
        if centre_uid != order.get("centre_uid"):
            raise PermissionError("This Farmer order does not belong to your UFC Centre.")
    elif actor.get("resolved_role") in {"super_admin", "avpl_admin", "accounts"}:
        centre = mongo.db.ufc_admin_master.find_one({"centre_uid": order.get("centre_uid")}) or {}
    else:
        raise PermissionError("You are not authorized to generate UFC sales documents.")

    sale = _upsert_sale(order, actor)
    purchase = _upsert_purchase(order, actor)
    # Persist the operational seller/buyer records before GST/invoice work.
    # If financial configuration needs repair, delivery remains complete and
    # neither the UFC Sale nor Farmer Purchase has to be re-entered manually.
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {
        "ufc_farmer_sale_id": sale.get("_id"),
        "ufc_farmer_sale_number": sale.get("sale_number") or "",
        "farmer_purchase_entry_id": purchase.get("_id"),
        "farmer_purchase_number": purchase.get("purchase_number") or "",
        "updated_at": now_utc(),
    }})
    financial = _financial_snapshot(order, centre)
    invoice = _upsert_invoice(order, sale, purchase, actor, centre, financial)
    receivable, payable = _upsert_receivable_payable(order, sale, invoice, purchase)

    total = float(_decimal(invoice.get("grand_total")))
    common = {
        "ufc_farmer_sale_id": sale.get("_id"),
        "ufc_farmer_sale_number": sale.get("sale_number") or "",
        "ufc_farmer_invoice_id": invoice.get("_id"),
        "ufc_farmer_invoice_number": invoice.get("document_number") or "",
        "invoice_document_type": invoice.get("document_type") or "",
        "invoice_date": invoice.get("invoice_date"),
        "taxable_value": float(_decimal(invoice.get("taxable_value"))),
        "gst_rate": float(_decimal(invoice.get("gst_rate"))),
        "gst_amount": float(_decimal(invoice.get("gst_amount"))),
        "grand_total": total,
        "payment_status": invoice.get("payment_status") or "unpaid",
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "financial_sync_status": "linked",
        "financial_sync_error": "",
        "updated_at": now_utc(),
    }
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {**common, "farmer_purchase_entry_id": purchase.get("_id"), "farmer_purchase_number": purchase.get("purchase_number") or ""}})
    mongo.db[SALE_COLLECTION].update_one({"_id": sale.get("_id")}, {"$set": {**common, "invoice_id": invoice.get("_id"), "invoice_number": invoice.get("document_number") or "", "receivable_id": receivable.get("_id") if receivable else None}})
    mongo.db[FARMER_PURCHASE_COLLECTION].update_one({"_id": purchase.get("_id")}, {"$set": {
        "invoice_id": invoice.get("_id"),
        "invoice_id_str": str(invoice.get("_id") or ""),
        "invoice_number": invoice.get("document_number") or "",
        "document_type": invoice.get("document_type") or "",
        "hsn_code": invoice.get("hsn_code") or "",
        "taxable_value": float(_decimal(invoice.get("taxable_value"))),
        "gst_rate": float(_decimal(invoice.get("gst_rate"))),
        "cgst_amount": float(_decimal(invoice.get("cgst_amount"))),
        "sgst_amount": float(_decimal(invoice.get("sgst_amount"))),
        "igst_amount": float(_decimal(invoice.get("igst_amount"))),
        "gst_amount": float(_decimal(invoice.get("gst_amount"))),
        "total_amount": total,
        "payment_status": invoice.get("payment_status") or "unpaid",
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "farmer_payable_id": payable.get("_id") if payable else None,
        "financial_link_status": "linked",
        "updated_at": now_utc(),
    }})
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "sale": sale,
        "invoice": invoice,
        "purchase": purchase,
        "receivable": receivable,
        "farmer_payable": payable,
        "message": "UFC Sale, invoice/receipt and Farmer Purchase were linked successfully.",
    }


def refresh_ufc_farmer_tax_documents(actor_user_id, centre_uid_hint=None):
    """Re-evaluate HSN/GST metadata for delivered Farmer orders of one UFC.

    This is intentionally idempotent. It never touches stock, never creates a
    second sale/purchase, and will not rewrite monetary totals after a payment
    has been applied to an invoice.
    """
    _ensure_indexes()
    actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    delivered = list(mongo.db[ORDER_COLLECTION].find({
        "centre_uid": centre_uid,
        "status": "delivered",
        "stock_delivered": True,
    }).sort("delivered_at", DESCENDING).limit(500))
    refreshed = 0
    warnings = 0
    errors = []
    for order in delivered:
        try:
            result = ensure_delivery_documents(actor["_id"], order["_id"])
            invoice = result.get("invoice") or {}
            refreshed += 1
            if invoice.get("document_warning") or invoice.get("gst_configuration_warning") or invoice.get("tax_hardening_note"):
                warnings += 1
        except Exception as exc:
            errors.append({
                "order_number": order.get("order_number") or str(order.get("_id")),
                "error": _clean(exc, 500),
            })
    return {
        "refreshed": refreshed,
        "warnings": warnings,
        "errors": errors,
        "message": f"Refreshed {refreshed} delivered invoice/receipt document(s)." + (f" {len(errors)} need attention." if errors else ""),
    }


def mark_financial_sync_error(order_id, error_message):
    oid = _to_object_id(order_id)
    if not oid:
        return
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {"financial_sync_status": "repair_required", "financial_sync_error": _clean(error_message, 1500), "updated_at": now_utc()}})


def deliver_farmer_order(actor_user_id, centre_uid_hint, order_id, delivery_note=""):
    _ensure_indexes()
    actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid, "centre_uid": centre_uid})
    if not order:
        raise ValueError("Farmer order was not found for your UFC Centre.")
    if order.get("status") == "delivered" and order.get("stock_delivered") is True:
        return {"order": _serialize_order(order), "message": "This order was already delivered. UFC stock was not deducted again."}
    if order.get("status") != "approved" or order.get("stock_reserved") is not True:
        raise ValueError("Only an approved order with reserved UFC stock can be delivered.")

    _apply_delivery_stock(order, actor)
    quantity = _decimal(order.get("approved_quantity"))
    timestamp = now_utc()
    update_result = mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "centre_uid": centre_uid, "status": "approved", "stock_delivered": {"$ne": True}},
        {"$set": {
            "status": "delivered",
            "stock_reserved": False,
            "stock_delivered": True,
            "reserved_quantity": 0.0,
            "delivered_quantity": float(quantity),
            "delivery_allocations": order.get("reservation_allocations") or [],
            "delivery_note": _clean(delivery_note, 1000),
            "delivered_by": actor["_id"],
            "delivered_by_name": actor.get("resolved_name") or "",
            "delivered_at": timestamp,
            "financial_sync_status": "pending",
            "updated_at": timestamp,
        }},
    )
    if update_result.modified_count != 1:
        # Another worker changed the state after physical lot updates. Restore lot state.
        for allocation in order.get("reservation_allocations") or []:
            lot_id = _to_object_id(allocation.get("inventory_lot_id"))
            quantity_value = float(_decimal(allocation.get("quantity")))
            if lot_id and quantity_value > 0:
                mongo.db[UFC_LOT_COLLECTION].update_one({"_id": lot_id}, {"$inc": {"available_quantity": quantity_value, "reserved_quantity": quantity_value, "issued_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}})
        raise RuntimeError("The order changed while delivery was being saved. UFC stock was restored; refresh and try again.")

    _append_history(oid, action="deliver_order", actor=actor, note=delivery_note or "UFC physically delivered the reserved goods to the Farmer.", from_status="approved", to_status="delivered")

    financial_warning = None
    try:
        ensure_delivery_documents(actor_user_id, oid)
    except Exception as exc:
        financial_warning = str(exc)
        mark_financial_sync_error(oid, financial_warning)

    _notify_user(order.get("farmer_user_id"), "Order Delivered", f"Your UFC marked order {order.get('order_number')} as delivered. Your purchase entry has been created automatically.", "farmer")
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "financial_warning": financial_warning,
        "message": "Order delivered. UFC physical stock was reduced and the Farmer purchase was created automatically.",
    }


def get_order(order_id, *, actor_user_id=None, centre_uid=None):
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid Farmer order.")
    query = {"_id": oid}
    if centre_uid:
        query["centre_uid"] = centre_uid
    order = mongo.db[ORDER_COLLECTION].find_one(query)
    if not order:
        raise ValueError("Farmer order was not found.")
    if actor_user_id:
        actor = _get_user(actor_user_id)
        if actor.get("resolved_role") == "farmer" and str(order.get("farmer_user_id") or "") != str(actor.get("_id") or ""):
            raise PermissionError("You cannot view another Farmer's order.")
        if actor.get("resolved_role") == "ufc_admin":
            _actor, _centre, resolved_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid or order.get("centre_uid"))
            if str(order.get("centre_uid") or "") != resolved_uid:
                raise PermissionError("This order does not belong to your UFC Centre.")
    return _serialize_order(order)


def get_ufc_order_overview(actor_user_id, centre_uid_hint=None, *, search="", status_filter="all"):
    _ensure_indexes()
    _actor, _centre, centre_uid, centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    query = {"centre_uid": centre_uid}
    status = _clean(status_filter, 30).lower()
    if status and status != "all":
        query["status"] = status
    text = _clean(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"order_number": {"$regex": escaped, "$options": "i"}},
            {"farmer_name": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"product_code": {"$regex": escaped, "$options": "i"}},
            {"status": {"$regex": escaped, "$options": "i"}},
        ]
    rows = [_serialize_order(row) for row in mongo.db[ORDER_COLLECTION].find(query).sort("created_at", DESCENDING)]
    counts = {key: mongo.db[ORDER_COLLECTION].count_documents({"centre_uid": centre_uid, "status": key}) for key in ORDER_STATUS_LABELS}
    return {"rows": rows, "centre_uid": centre_uid, "centre_name": centre_name, "query": search or "", "selected_status": status_filter or "all", "summary": {"total": mongo.db[ORDER_COLLECTION].count_documents({"centre_uid": centre_uid}), **counts}}


def get_farmer_order_overview(actor_user_id, *, search=""):
    _ensure_indexes()
    actor, _farmer, centre_uid, centre_name, farmer_name = _resolve_farmer(actor_user_id)
    query = {"farmer_user_id": actor["_id"]}
    text = _clean(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"order_number": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"status": {"$regex": escaped, "$options": "i"}},
        ]
    rows = [_serialize_order(row) for row in mongo.db[ORDER_COLLECTION].find(query).sort("created_at", DESCENDING)]
    total = sum((_decimal(row.get("grand_total") or row.get("total_amount")) for row in rows), Decimal("0"))
    return {"rows": rows, "centre_uid": centre_uid, "centre_name": centre_name, "farmer_name": farmer_name, "query": search or "", "summary": {"count": len(rows), "total_value": _money(total)}}


def get_farmer_purchase_overview(actor_user_id, *, search=""):
    _ensure_indexes()
    actor, farmer, centre_uid, centre_name, farmer_name = _resolve_farmer(actor_user_id)
    query = {"farmer_user_id": actor["_id"]}
    text = _clean(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"purchase_number": {"$regex": escaped, "$options": "i"}},
            {"order_number": {"$regex": escaped, "$options": "i"}},
            {"invoice_number": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
        ]
    rows = []
    total = Decimal("0")
    for item in mongo.db[FARMER_PURCHASE_COLLECTION].find(query).sort("purchase_date", DESCENDING):
        row = dict(item)
        row["id"] = str(row.get("_id") or "")
        row["invoice_id_str"] = str(row.get("invoice_id") or "")
        row["quantity_display"] = _qty(row.get("quantity"))
        row["unit_price_display"] = _money(row.get("unit_price"))
        row["taxable_value_display"] = _money(row.get("taxable_value"))
        row["gst_amount_display"] = _money(row.get("gst_amount"))
        row["total_amount_display"] = _money(row.get("total_amount"))
        row["amount_paid_display"] = _money(row.get("amount_paid"))
        row["outstanding_amount_display"] = _money(row.get("outstanding_amount"))
        row["payment_status_label"] = PAYMENT_STATUS_LABELS.get(str(row.get("payment_status") or "not_recorded"), str(row.get("payment_status") or "").replace("_", " ").title())
        row["source_type"] = "ufc_automatic"
        rows.append(row)
        total += _decimal(row.get("total_amount"))

    # Keep older/manual Farmer purchase history visible. Stage 7 does not
    # delete or rewrite legacy transaction records; internal UFC purchases are
    # simply added as the new automatic source.
    farmer_contact = farmer.get("contact_no") or farmer.get("phone") or actor.get("phone") or ""
    legacy_query = {"transaction_type": "input_purchase"}
    owner_filters = []
    if farmer_contact:
        owner_filters.append({"farmer_contact": farmer_contact})
    owner_filters.extend([
        {"farmer_user_id": str(actor["_id"])},
        {"farmer_user_id": actor["_id"]},
        {"user_id": str(actor["_id"])},
    ])
    legacy_query["$or"] = owner_filters
    if text:
        import re
        escaped = re.escape(text)
        legacy_query = {"$and": [legacy_query, {"$or": [
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"status": {"$regex": escaped, "$options": "i"}},
            {"source_reference": {"$regex": escaped, "$options": "i"}},
        ]}]}
    for item in mongo.db.transactions.find(legacy_query).sort("created_at", DESCENDING).limit(100):
        amount = _decimal(item.get("total_amount") or item.get("amount"))
        legacy = {
            **item,
            "id": str(item.get("_id") or ""),
            "purchase_number": item.get("purchase_number") or item.get("source_reference") or "Legacy Purchase",
            "order_number": item.get("order_number") or "—",
            "seller_name": item.get("seller_name") or item.get("source") or "External / Legacy",
            "invoice_id_str": "",
            "quantity_display": _qty(item.get("quantity")),
            "unit_price_display": _money(item.get("unit_price") or item.get("price")),
            "taxable_value_display": _money(item.get("taxable_value") or amount),
            "gst_amount_display": _money(item.get("gst_amount")),
            "total_amount_display": _money(amount),
            "amount_paid_display": _money(item.get("amount_paid")),
            "outstanding_amount_display": _money(item.get("outstanding_amount")),
            "payment_status_label": PAYMENT_STATUS_LABELS.get(str(item.get("payment_status") or "not_recorded"), "Not Recorded Yet"),
            "source_type": "legacy",
            "purchase_date": item.get("created_at"),
        }
        rows.append(legacy)
        total += amount

    rows.sort(key=lambda row: row.get("purchase_date") or row.get("created_at") or datetime.min, reverse=True)
    return {"rows": rows, "centre_uid": centre_uid, "centre_name": centre_name, "farmer_name": farmer_name, "query": search or "", "summary": {"count": len(rows), "total_value": _money(total)}}


def get_ufc_sales_overview(actor_user_id, centre_uid_hint=None, *, search="", payment_status="all"):
    _ensure_indexes()
    _actor, _centre, centre_uid, centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    query = {"centre_uid": centre_uid}
    selected_payment = _clean(payment_status, 30).lower()
    if selected_payment and selected_payment != "all":
        query["payment_status"] = selected_payment
    text = _clean(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"sale_number": {"$regex": escaped, "$options": "i"}},
            {"invoice_number": {"$regex": escaped, "$options": "i"}},
            {"order_number": {"$regex": escaped, "$options": "i"}},
            {"farmer_name": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
        ]
    rows = []
    total = Decimal("0")
    outstanding = Decimal("0")
    for item in mongo.db[SALE_COLLECTION].find(query).sort("sale_date", DESCENDING):
        row = dict(item)
        row["id"] = str(row.get("_id") or "")
        row["invoice_id_str"] = str(row.get("invoice_id") or "")
        row["quantity_display"] = _qty(row.get("quantity"))
        row["unit_price_display"] = _money(row.get("unit_price"))
        row["grand_total_display"] = _money(row.get("grand_total"))
        row["outstanding_amount_display"] = _money(row.get("outstanding_amount"))
        row["payment_status_label"] = PAYMENT_STATUS_LABELS.get(str(row.get("payment_status") or "unpaid"), str(row.get("payment_status") or "").replace("_", " ").title())
        total += _decimal(row.get("grand_total"))
        outstanding += _decimal(row.get("outstanding_amount"))
        rows.append(row)
    return {"rows": rows, "centre_uid": centre_uid, "centre_name": centre_name, "query": search or "", "selected_payment": payment_status or "all", "summary": {"count": len(rows), "total_sales": _money(total), "outstanding": _money(outstanding)}}


def get_invoice(invoice_id, *, actor_user_id):
    oid = _to_object_id(invoice_id)
    if not oid:
        raise ValueError("Invalid invoice/receipt.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": oid})
    if not invoice:
        raise ValueError("Invoice/receipt was not found.")
    actor = _get_user(actor_user_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": invoice.get("ufc_farmer_order_id")}) or {}
    if actor.get("resolved_role") == "farmer":
        if str(order.get("farmer_user_id") or "") != str(actor.get("_id") or ""):
            raise PermissionError("You cannot view another Farmer's invoice/receipt.")
    elif actor.get("resolved_role") == "ufc_admin":
        _actor, _centre, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, order.get("centre_uid"))
        if centre_uid != order.get("centre_uid"):
            raise PermissionError("This invoice/receipt does not belong to your UFC Centre.")
    elif actor.get("resolved_role") not in {"super_admin", "avpl_admin", "accounts"}:
        raise PermissionError("You are not authorized to view this invoice/receipt.")
    row = dict(invoice)
    row["id"] = str(row.get("_id") or "")
    for key in ["quantity", "unit_price", "taxable_value", "gst_rate", "cgst_amount", "sgst_amount", "igst_amount", "gst_amount", "grand_total", "amount_paid", "outstanding_amount"]:
        row[f"{key}_display"] = _qty(row.get(key)) if key == "quantity" else _money(row.get(key))
    row["payment_status_label"] = PAYMENT_STATUS_LABELS.get(str(row.get("payment_status") or "unpaid"), str(row.get("payment_status") or "").replace("_", " ").title())
    return row


def get_invoice_print_context(invoice_id, *, actor_user_id):
    invoice = get_invoice(invoice_id, actor_user_id=actor_user_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": _to_object_id(invoice.get("ufc_farmer_order_id"))}) or {}
    sale = mongo.db[SALE_COLLECTION].find_one({"ufc_farmer_order_id": order.get("_id")}) or {}
    purchase = mongo.db[FARMER_PURCHASE_COLLECTION].find_one({"ufc_farmer_order_id": order.get("_id")}) or {}
    return {"invoice": invoice, "order": _serialize_order(order), "sale": sale, "purchase": purchase, "seller": invoice.get("seller") or {}, "buyer": invoice.get("buyer") or {}}
