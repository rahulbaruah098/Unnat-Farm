from __future__ import annotations
from app.utils.timezone import business_today

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.extensions import mongo
from app.services.commerce_receipt_service import (
    normalize_receipt_lines,
    proportional_amount,
    receipt_label,
    summarize_receipt,
)
from app.utils.helpers import now_utc


ORDER_COLLECTION = "avpl_ufc_orders"
AVPL_LOT_COLLECTION = "avpl_inventory_lots"
AVPL_MOVEMENT_COLLECTION = "avpl_stock_movements"
PUBLICATION_COLLECTION = "avpl_marketplace_publications"
UFC_LOT_COLLECTION = "ufc_inventory_lots"
UFC_MOVEMENT_COLLECTION = "ufc_stock_movements"
UFC_PURCHASE_COLLECTION = "ufc_purchase_entries"

AVPL_REVIEW_ROLES = {"super_admin", "avpl_admin", "accounts"}
AVPL_ACTION_ROLES = {"super_admin", "avpl_admin"}
UFC_ORDER_ROLE = "ufc_admin"

ORDER_STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Order Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "dispatched": "Dispatched",
    "received": "Received",
}

PAYMENT_TERM_LABELS = {
    "cod": "Pay on Delivery",
    "credit": "Credit / Pay Later",
    "prepaid_online": "Prepaid / Online (Coming Soon)",
}
PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
    "not_recorded": "Not Recorded Yet",
}


def _decimal(value, default="0"):
    try:
        parsed = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return parsed if parsed.is_finite() else Decimal(default)


def _qty(value):
    number = _decimal(value)
    text = f"{number.quantize(Decimal('0.0001')):f}".rstrip("0").rstrip(".")
    return text or "0"


def _money(value):
    return f"{_decimal(value).quantize(Decimal('0.01')):.2f}"


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _clean_text(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _user_is_active(user):
    return not (
        user.get("active", True) is False
        or user.get("is_active", True) is False
        or str(user.get("status") or "").lower() == "inactive"
    )


def _active_avpl_entity():
    return mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    })


def _ensure_indexes():
    definitions = [
        (
            mongo.db[ORDER_COLLECTION],
            [("order_number", ASCENDING)],
            {"name": "avpl_ufc_order_number_unique", "unique": True},
        ),
        (
            mongo.db[ORDER_COLLECTION],
            [("centre_uid", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
            {"name": "avpl_ufc_order_centre_status_idx"},
        ),
        (
            mongo.db[ORDER_COLLECTION],
            [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
            {"name": "avpl_ufc_order_entity_status_idx"},
        ),
        (
            mongo.db[UFC_LOT_COLLECTION],
            [("lot_key", ASCENDING)],
            {"name": "ufc_inventory_lot_key_unique", "unique": True},
        ),
        (
            mongo.db[UFC_LOT_COLLECTION],
            [("centre_uid", ASCENDING), ("source_product_id", ASCENDING), ("expiry_date", ASCENDING)],
            {"name": "ufc_inventory_centre_product_expiry_idx"},
        ),
        (
            mongo.db[UFC_MOVEMENT_COLLECTION],
            [("source_posting_key", ASCENDING)],
            {"name": "ufc_stock_movement_source_unique", "unique": True},
        ),
        (
            mongo.db[UFC_PURCHASE_COLLECTION],
            [("avpl_ufc_order_id", ASCENDING)],
            {"name": "ufc_purchase_order_unique", "unique": True},
        ),
        (
            mongo.db[UFC_PURCHASE_COLLECTION],
            [("centre_uid", ASCENDING), ("purchase_date", DESCENDING)],
            {"name": "ufc_purchase_centre_date_idx"},
        ),
    ]
    for collection, keys, options in definitions:
        try:
            collection.create_index(keys, **options)
        except Exception:
            # Do not make operational pages unavailable in restricted/local environments.
            pass


def _get_user(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Invalid authenticated user.")
    user = mongo.db.users.find_one({"_id": oid})
    if not user:
        raise ValueError("Authenticated user was not found.")
    if not _user_is_active(user):
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


def _get_avpl_actor(user_id, *, action=False):
    actor = _get_user(user_id)
    allowed = AVPL_ACTION_ROLES if action else AVPL_REVIEW_ROLES
    if actor["resolved_role"] not in allowed:
        raise PermissionError("You are not authorized to manage UFC orders for AVPL.")
    return actor


def _get_ufc_actor(user_id, centre_uid_hint=None):
    actor = _get_user(user_id)
    if actor["resolved_role"] != UFC_ORDER_ROLE:
        raise PermissionError("Only UFC Admin can request products from AVPL.")

    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor["_id"]})
    )
    authoritative_centre_uid = _clean_text(
        (master or {}).get("centre_uid")
        or actor.get("centre_uid")
        or actor.get("mapped_centre_uid"),
        80,
    )
    hinted_centre_uid = _clean_text(centre_uid_hint, 80)
    if hinted_centre_uid and authoritative_centre_uid and hinted_centre_uid != authoritative_centre_uid:
        raise PermissionError("Your session Centre UID does not match your UFC Admin profile. Please log in again.")
    centre_uid = authoritative_centre_uid or hinted_centre_uid
    if not centre_uid:
        raise ValueError("This UFC Admin is not linked to a valid Centre UID.")

    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or master or {}
    centre_name = (
        centre.get("name_of_enterprise")
        or centre.get("enterprise_name")
        or centre.get("centre_name")
        or centre.get("name")
        or centre_uid
    )
    return actor, centre_uid, centre_name


def _next_order_number():
    year = business_today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_ufc_order:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"UFC-AVPL-{year}-{sequence:05d}"


def _next_purchase_number(centre_uid):
    year = business_today().year
    safe_centre = "".join(ch for ch in str(centre_uid or "UFC").upper() if ch.isalnum())[:14] or "UFC"
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"ufc_purchase:{safe_centre}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"{safe_centre}-PUR-{year}-{sequence:05d}"


def _is_published(entity_id, product_id):
    return mongo.db[PUBLICATION_COLLECTION].find_one({
        "accounting_entity_id": entity_id,
        "source_product_id": product_id,
        "status": "published",
        "scope": "all_active_ufc",
    }) is not None


def _lot_expired(lot):
    expiry = str(lot.get("expiry_date") or "")[:10]
    if not expiry:
        return False
    try:
        return datetime.strptime(expiry, "%Y-%m-%d").date() < business_today()
    except ValueError:
        return False


def _lot_saleable(lot):
    if str(lot.get("status") or "").lower() in {"cancelled", "expired"} or _lot_expired(lot):
        return Decimal("0")
    physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
    reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
    damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
    blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
    return max(physical - reserved - damaged - blocked, Decimal("0"))


def _product_saleable(entity_id, product_id):
    total = Decimal("0")
    for lot in mongo.db[AVPL_LOT_COLLECTION].find({
        "accounting_entity_id": entity_id,
        "source_product_id": product_id,
        "status": {"$ne": "cancelled"},
    }):
        total += _lot_saleable(lot)
    return total


def _line_id():
    return uuid4().hex[:12]


def _order_items(order):
    """Return normalized AVPL→UFC order lines while preserving legacy orders.

    New commerce orders persist an ``items`` array. Historical Stage-4 records
    remain single-line documents, so this helper presents both shapes through
    one internal interface without rewriting old data.
    """
    raw_items = order.get("items") or []
    if raw_items:
        return [dict(item or {}) for item in raw_items if isinstance(item, dict)]
    return [{
        "line_id": "legacy",
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "category": order.get("category") or "",
        "product_role": order.get("product_role") or "",
        "unit_code": order.get("unit_code") or "Unit",
        "requested_quantity": order.get("requested_quantity") or 0,
        "approved_quantity": order.get("approved_quantity") or 0,
        "reserved_quantity": order.get("reserved_quantity") or 0,
        "dispatched_quantity": order.get("dispatched_quantity") or 0,
        "received_quantity": order.get("received_quantity") or 0,
        "unit_price": order.get("unit_price") or 0,
        "line_total": order.get("total_amount") or 0,
        "status": order.get("status") or "requested",
        "reservation_allocations": order.get("reservation_allocations") or [],
    }]


def _serialize_order_item(item):
    row = dict(item or {})
    row["source_product_id_str"] = str(row.get("source_product_id") or row.get("source_product_id_str") or "")
    row["requested_quantity_display"] = _qty(row.get("requested_quantity"))
    row["approved_quantity_display"] = _qty(row.get("approved_quantity"))
    row["dispatched_quantity_display"] = _qty(row.get("dispatched_quantity"))
    row["received_quantity_display"] = _qty(row.get("received_quantity"))
    row["physically_received_quantity_display"] = _qty(row.get("physically_received_quantity") if row.get("physically_received_quantity") is not None else row.get("received_quantity"))
    row["accepted_quantity_display"] = _qty(row.get("accepted_quantity") if row.get("accepted_quantity") is not None else row.get("received_quantity"))
    row["damaged_quantity_display"] = _qty(row.get("damaged_quantity"))
    row["rejected_quantity_display"] = _qty(row.get("rejected_quantity"))
    row["missing_quantity_display"] = _qty(row.get("missing_quantity"))
    row["unit_price_display"] = _money(row.get("unit_price"))
    row["line_total_display"] = _money(row.get("line_total"))
    line_status = str(row.get("status") or "requested")
    row["status_label"] = {
        "requested": "Requested",
        "approved": "Approved",
        "partially_approved": "Partially Approved",
        "rejected": "Not Approved",
        "dispatched": "Dispatched",
        "received": "Received",
    }.get(line_status, line_status.replace("_", " ").title())
    return row


def _primary_item(order):
    items = _order_items(order)
    return items[0] if items else {}


def _copy_line_to_legacy_fields(document, item):
    """Mirror the first line into legacy fields so old screens/reports remain safe."""
    item = item or {}
    document.update({
        "source_product_id": item.get("source_product_id"),
        "source_product_id_str": str(item.get("source_product_id") or ""),
        "product_name": item.get("product_name") or "Multiple products",
        "product_code": item.get("product_code") or "",
        "category": item.get("category") or "",
        "product_role": item.get("product_role") or "",
        "unit_code": item.get("unit_code") or "Unit",
        "requested_quantity": float(_decimal(item.get("requested_quantity"))),
        "approved_quantity": float(_decimal(item.get("approved_quantity"))),
        "reserved_quantity": float(_decimal(item.get("reserved_quantity"))),
        "dispatched_quantity": float(_decimal(item.get("dispatched_quantity"))),
        "received_quantity": float(_decimal(item.get("received_quantity"))),
        "unit_price": float(_decimal(item.get("unit_price"))),
    })
    return document


def _serialize_order(order):
    if not order:
        return None
    row = dict(order)
    row["id"] = str(row.get("_id") or "")
    row["product_id_str"] = str(row.get("source_product_id") or "")
    row["avpl_sale_id_str"] = str(row.get("avpl_sale_id") or "")
    row["avpl_sales_invoice_id_str"] = str(row.get("avpl_sales_invoice_id") or "")
    row["invoice_grand_total_display"] = _money(row.get("invoice_grand_total"))
    status = str(row.get("status") or "requested")
    if status == "approved" and row.get("approval_scope") == "partial":
        row["status_label"] = "Partially Approved"
    else:
        row["status_label"] = ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title())
    row["requested_quantity_display"] = _qty(row.get("requested_quantity"))
    row["approved_quantity_display"] = _qty(row.get("approved_quantity"))
    row["dispatched_quantity_display"] = _qty(row.get("dispatched_quantity"))
    row["received_quantity_display"] = _qty(row.get("received_quantity"))
    row["unit_price_display"] = _money(row.get("unit_price"))
    row["total_amount_display"] = _money(row.get("total_amount"))
    row["payment_term_label"] = PAYMENT_TERM_LABELS.get(str(row.get("payment_term") or "credit"), str(row.get("payment_term") or "credit").replace("_", " ").title())
    row["payment_status_label"] = PAYMENT_STATUS_LABELS.get(str(row.get("payment_status") or "unpaid"), str(row.get("payment_status") or "unpaid").replace("_", " ").title())
    row["amount_paid_display"] = _money(row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount"))
    row["outstanding_amount_display"] = _money(row.get("outstanding_amount") if row.get("outstanding_amount") is not None else row.get("invoice_grand_total") or row.get("total_amount"))
    row["items"] = [_serialize_order_item(item) for item in _order_items(row)]
    row["item_count"] = len(row["items"])
    row["is_multi_item_order"] = row.get("is_multi_item_order") is True or row["item_count"] > 1
    row["product_summary"] = (
        row["items"][0].get("product_name") or "Product"
        if row["item_count"] == 1
        else f"{row['item_count']} products"
    )
    row["approved_item_count"] = sum(1 for item in row["items"] if _decimal(item.get("approved_quantity")) > 0)
    row["dispatched_item_count"] = sum(1 for item in row["items"] if _decimal(item.get("dispatched_quantity")) > 0)
    row["received_item_count"] = sum(1 for item in row["items"] if _decimal(item.get("physically_received_quantity") if item.get("physically_received_quantity") is not None else item.get("received_quantity")) > 0)
    row["accepted_item_count"] = sum(1 for item in row["items"] if _decimal(item.get("accepted_quantity") if item.get("accepted_quantity") is not None else item.get("received_quantity")) > 0)
    row["discrepancy_item_count"] = sum(1 for item in row["items"] if _decimal(item.get("discrepancy_quantity")) > 0)
    row["receipt_status"] = row.get("receipt_status") or ("full" if row.get("status") == "received" and not row["discrepancy_item_count"] else ("discrepancy" if row.get("status") == "received" else "none"))
    row["receipt_status_label"] = receipt_label(row["receipt_status"])
    row["accepted_value_display"] = _money(row.get("accepted_goods_total") if row.get("accepted_goods_total") is not None else row.get("settlement_total") or 0)
    row["receipt_adjustment_display"] = _money(row.get("receipt_adjustment_amount") or 0)
    return row


def _append_history(order_id, *, action, actor, note="", from_status=None, to_status=None):
    history = {
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "note": _clean_text(note, 1000),
        "actor_user_id": actor.get("_id") if actor else None,
        "actor_user_id_str": str(actor.get("_id") or "") if actor else "",
        "actor_name": actor.get("resolved_name") if actor else "System",
        "actor_role": actor.get("resolved_role") if actor else "system",
        "at": now_utc(),
    }
    mongo.db[ORDER_COLLECTION].update_one({"_id": order_id}, {"$push": {"history": history}})


def _notify_user(user_id, title, message, role=""):
    if not user_id:
        return
    mongo.db.notifications.insert_one({
        "to_user_id": str(user_id),
        "role": role or "",
        "title": title,
        "message": message,
        "status": "unread",
        "created_at": now_utc(),
    })


def _notify_avpl_admins(title, message):
    users = mongo.db.users.find({
        "role": {"$in": ["avpl_admin", "super_admin"]},
        "active": {"$ne": False},
        "is_active": {"$ne": False},
        "status": {"$ne": "inactive"},
    }, {"_id": 1, "role": 1})
    for user in users:
        _notify_user(user.get("_id"), title, message, user.get("role") or "avpl_admin")


def create_ufc_order_request(actor_user_id, centre_uid_hint, product_id, quantity, note="", payment_term="credit"):
    _ensure_indexes()
    actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    entity = _active_avpl_entity()
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is unavailable.")

    product_oid = _to_object_id(product_id)
    if not product_oid:
        raise ValueError("Select a valid AVPL product.")
    quantity_value = _decimal(quantity)
    if quantity_value <= 0:
        raise ValueError("Requested quantity must be greater than zero.")

    product = mongo.db.products.find_one({
        "_id": product_oid,
        "is_deleted": {"$ne": True},
        "is_active": {"$ne": False},
        "status": {"$nin": ["disabled", "deleted"]},
        "unnatfarm_eligible": {"$ne": False},
    })
    if not product:
        raise ValueError("This AVPL product is not active.")
    if not _is_published(entity["_id"], product_oid):
        raise ValueError("This product is no longer published to UFC Centres.")

    saleable = _product_saleable(entity["_id"], product_oid)
    if saleable <= 0:
        raise ValueError("This product is currently out of stock at AVPL.")
    if quantity_value > saleable:
        raise ValueError(
            f"Only {_qty(saleable)} {product.get('base_unit_code') or product.get('base_unit_name') or 'units'} are currently saleable at AVPL."
        )

    payment_term = _clean_text(payment_term, 40).lower() or "credit"
    if payment_term == "prepaid_online":
        raise ValueError("Online prepaid payment is coming soon. Choose Pay on Delivery or Credit / Pay Later for now.")
    if payment_term not in {"cod", "credit"}:
        raise ValueError("Select Pay on Delivery or Credit / Pay Later.")

    timestamp = now_utc()
    document = {
        "order_number": _next_order_number(),
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "requested_by": actor["_id"],
        "requested_by_str": str(actor["_id"]),
        "requested_by_name": actor.get("resolved_name") or centre_name,
        "source_product_id": product_oid,
        "source_product_id_str": str(product_oid),
        "product_name": product.get("name") or product.get("product_name") or "Product",
        "product_code": product.get("product_code") or "",
        "category": product.get("category") or "",
        "product_role": product.get("product_role") or product.get("type") or "",
        "unit_code": product.get("base_unit_code") or product.get("base_unit_name") or "Unit",
        "requested_quantity": float(quantity_value),
        "approved_quantity": 0.0,
        "reserved_quantity": 0.0,
        "dispatched_quantity": 0.0,
        "received_quantity": 0.0,
        "unit_price": 0.0,
        "total_amount": 0.0,
        "request_note": _clean_text(note, 1000),
        "status": "requested",
        "stock_reserved": False,
        "stock_dispatched": False,
        "ufc_stock_posted": False,
        "purchase_entry_created": False,
        "accounting_status": "not_posted",
        "payment_term": payment_term,
        "payment_term_label": PAYMENT_TERM_LABELS.get(payment_term, payment_term.replace("_", " ").title()),
        "payment_status": "not_recorded",
        "financial_sync_status": "not_applicable",
        "financial_sync_error": None,
        "avpl_sale_id": None,
        "avpl_sale_number": "",
        "avpl_sales_invoice_id": None,
        "avpl_sales_invoice_number": "",
        "history": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[ORDER_COLLECTION].insert_one(document)
    document["_id"] = result.inserted_id
    _append_history(
        result.inserted_id,
        action="request_order",
        actor=actor,
        note=document.get("request_note") or "UFC requested product from AVPL.",
        to_status="requested",
    )
    _notify_avpl_admins(
        "New UFC Order Request",
        f"{centre_name} ({centre_uid}) requested {_qty(quantity_value)} {document['unit_code']} of {document['product_name']}.",
    )
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": result.inserted_id})),
        "message": "Order request sent to AVPL successfully.",
    }


def create_ufc_cart_order_request(actor_user_id, centre_uid_hint, items, note="", payment_term="credit", idempotency_key=""):
    """Create one AVPL order containing multiple product lines.

    The server re-reads Product Master, publication state and saleable inventory
    for every line. Browser cart totals are never trusted. Duplicate products are
    merged before validation so one checkout cannot oversubscribe a SKU by
    submitting it twice.
    """
    _ensure_indexes()
    actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    entity = _active_avpl_entity()
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is unavailable.")
    if not isinstance(items, list):
        raise ValueError("Your cart is invalid. Refresh the Marketplace and try again.")
    if not items:
        raise ValueError("Your cart is empty.")
    if len(items) > 40:
        raise ValueError("A single order can contain at most 40 products.")

    payment_term = _clean_text(payment_term, 40).lower() or "credit"
    if payment_term == "prepaid_online":
        raise ValueError("Online prepaid payment is coming soon. Choose Pay on Delivery or Credit / Pay Later for now.")
    if payment_term not in {"cod", "credit"}:
        raise ValueError("Select Pay on Delivery or Credit / Pay Later.")

    merged = {}
    for raw in items:
        if not isinstance(raw, dict):
            raise ValueError("One cart line is invalid.")
        oid = _to_object_id(raw.get("product_id") or raw.get("source_product_id"))
        qty = _decimal(raw.get("quantity"))
        if not oid or qty <= 0:
            raise ValueError("Every cart product must have a valid quantity greater than zero.")
        key = str(oid)
        if key not in merged:
            merged[key] = {"product_id": oid, "quantity": Decimal("0")}
        merged[key]["quantity"] += qty

    lines = []
    for merged_row in merged.values():
        product_oid = merged_row["product_id"]
        requested = merged_row["quantity"]
        product = mongo.db.products.find_one({
            "_id": product_oid,
            "is_deleted": {"$ne": True},
            "is_active": {"$ne": False},
            "status": {"$nin": ["disabled", "deleted"]},
            "unnatfarm_eligible": {"$ne": False},
        })
        if not product:
            raise ValueError("One product in your cart is no longer active. Remove it and try again.")
        if not _is_published(entity["_id"], product_oid):
            raise ValueError(f"{product.get('name') or 'A product'} is no longer published to UFC Centres.")
        saleable = _product_saleable(entity["_id"], product_oid)
        unit_code = product.get("base_unit_code") or product.get("base_unit_name") or "Unit"
        if saleable <= 0:
            raise ValueError(f"{product.get('name') or 'A product'} is currently out of stock at AVPL.")
        if requested > saleable:
            raise ValueError(f"Only {_qty(saleable)} {unit_code} of {product.get('name') or 'this product'} is currently saleable at AVPL.")
        lines.append({
            "line_id": _line_id(),
            "source_product_id": product_oid,
            "source_product_id_str": str(product_oid),
            "product_name": product.get("name") or product.get("product_name") or "Product",
            "product_code": product.get("product_code") or "",
            "category": product.get("category") or "",
            "product_role": product.get("product_role") or product.get("type") or "",
            "unit_code": unit_code,
            "requested_quantity": float(requested),
            "approved_quantity": 0.0,
            "reserved_quantity": 0.0,
            "dispatched_quantity": 0.0,
            "received_quantity": 0.0,
            "unit_price": 0.0,
            "line_total": 0.0,
            "status": "requested",
            "reservation_allocations": [],
        })

    token = _clean_text(idempotency_key, 120) or f"AVPLCART-{uuid4().hex.upper()}"
    existing = mongo.db[ORDER_COLLECTION].find_one({"checkout_token": token})
    if existing:
        if str(existing.get("requested_by") or "") != str(actor.get("_id") or ""):
            raise PermissionError("This checkout token belongs to another user.")
        return {"order": _serialize_order(existing), "message": "This cart order was already submitted."}

    timestamp = now_utc()
    document = {
        "order_number": _next_order_number(),
        "checkout_token": token,
        "commerce_version": 2,
        "is_multi_item_order": len(lines) > 1,
        "item_count": len(lines),
        "items": lines,
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "requested_by": actor["_id"],
        "requested_by_str": str(actor["_id"]),
        "requested_by_name": actor.get("resolved_name") or centre_name,
        "total_amount": 0.0,
        "request_note": _clean_text(note, 1000),
        "status": "requested",
        "approval_scope": "pending",
        "stock_reserved": False,
        "stock_dispatched": False,
        "ufc_stock_posted": False,
        "purchase_entry_created": False,
        "accounting_status": "not_posted",
        "payment_term": payment_term,
        "payment_term_label": PAYMENT_TERM_LABELS.get(payment_term, payment_term.replace("_", " ").title()),
        "payment_status": "not_recorded",
        "financial_sync_status": "not_applicable",
        "financial_sync_error": None,
        "avpl_sale_id": None,
        "avpl_sale_number": "",
        "avpl_sales_invoice_id": None,
        "avpl_sales_invoice_number": "",
        "history": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _copy_line_to_legacy_fields(document, lines[0])
    try:
        result = mongo.db[ORDER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except Exception:
        existing = mongo.db[ORDER_COLLECTION].find_one({"checkout_token": token})
        if existing:
            return {"order": _serialize_order(existing), "message": "This cart order was already submitted."}
        raise

    _append_history(
        document["_id"], action="request_cart_order", actor=actor,
        note=document.get("request_note") or f"UFC requested {len(lines)} product line(s) from AVPL.",
        to_status="requested",
    )
    _notify_avpl_admins(
        "New UFC Cart Order",
        f"{centre_name} ({centre_uid}) requested {len(lines)} product line(s) in order {document.get('order_number')}.",
    )
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": document["_id"]})), "message": f"Order request sent to AVPL with {len(lines)} product line(s)."}


def _candidate_avpl_lots(entity_id, product_id):
    rows = list(mongo.db[AVPL_LOT_COLLECTION].find({
        "accounting_entity_id": entity_id,
        "source_product_id": product_id,
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


def _reserve_avpl_stock(order, approved_quantity, actor, item=None):
    """Reserve FEFO AVPL stock for one order line.

    ``item`` is optional to keep historical single-line orders fully compatible.
    New allocations carry their own product snapshot so later dispatch/receipt
    does not depend on the order-level legacy mirror.
    """
    effective = dict(order)
    if item:
        effective.update(item)
    needed = _decimal(approved_quantity)
    allocations = []
    reserved_updates = []
    today_iso = business_today().strftime("%Y-%m-%d")
    product_id = effective.get("source_product_id")
    line_id = (item or {}).get("line_id") or "legacy"

    for lot in _candidate_avpl_lots(order["accounting_entity_id"], product_id):
        if needed <= 0:
            break
        saleable = _lot_saleable(lot)
        if saleable <= 0:
            continue
        take = min(needed, saleable)
        take_float = float(take)
        query = {
            "_id": lot["_id"],
            "accounting_entity_id": order["accounting_entity_id"],
            "status": {"$nin": ["cancelled", "expired"]},
            "$and": [
                {"$or": [
                    {"expiry_date": {"$exists": False}}, {"expiry_date": None},
                    {"expiry_date": ""}, {"expiry_date": {"$gte": today_iso}},
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
        }
        result = mongo.db[AVPL_LOT_COLLECTION].update_one(
            query,
            {"$inc": {"reserved_quantity": take_float}, "$set": {"updated_at": now_utc(), "last_reservation_order_id": order["_id"]}},
        )
        if result.modified_count != 1:
            continue
        reserved_updates.append((lot["_id"], take_float))
        allocations.append({
            "line_id": line_id,
            "inventory_lot_id": lot["_id"],
            "inventory_lot_id_str": str(lot["_id"]),
            "source_product_id": product_id,
            "source_product_id_str": str(product_id or ""),
            "product_code": effective.get("product_code") or "",
            "product_name": effective.get("product_name") or "Product",
            "category": effective.get("category") or "",
            "product_role": effective.get("product_role") or "",
            "unit_price": float(_decimal(effective.get("unit_price"))),
            "quantity": take_float,
            "quantity_display": _qty(take),
            "warehouse_code": lot.get("warehouse_code") or "AVPL-MAIN",
            "warehouse_name": lot.get("warehouse_name") or "AVPL Main Warehouse",
            "warehouse_bin": lot.get("warehouse_bin") or "",
            "batch_number": lot.get("batch_number") or "",
            "barcode": lot.get("barcode") or "",
            "manufacturing_date": lot.get("manufacturing_date") or "",
            "expiry_date": lot.get("expiry_date") or "",
            "unit_code": lot.get("unit_code") or effective.get("unit_code") or "Unit",
        })
        needed -= take

    if needed > 0:
        for lot_id, quantity_value in reserved_updates:
            mongo.db[AVPL_LOT_COLLECTION].update_one(
                {"_id": lot_id, "reserved_quantity": {"$gte": quantity_value}},
                {"$inc": {"reserved_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}},
            )
        raise RuntimeError("AVPL stock changed while this order was being approved. Refresh the order and try again.")

    timestamp = now_utc()
    for allocation in allocations:
        source_key = f"UFC-RESERVE:{order['_id']}:{line_id}:{allocation['inventory_lot_id']}"
        mongo.db[AVPL_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {"$setOnInsert": {
                "source_posting_key": source_key,
                "movement_uid": uuid4().hex,
                "accounting_entity_id": order["accounting_entity_id"],
                "accounting_entity_id_str": str(order["accounting_entity_id"]),
                "source_document_type": "ufc_order",
                "source_document_id": order["_id"],
                "source_document_id_str": str(order["_id"]),
                "source_document_number": order.get("order_number") or "",
                "line_id": line_id,
                "source_product_id": allocation.get("source_product_id"),
                "source_product_id_str": allocation.get("source_product_id_str") or "",
                "product_code": allocation.get("product_code") or "",
                "product_name": allocation.get("product_name") or "Product",
                "movement_type": "reservation",
                "direction": "reserve",
                "quantity": allocation["quantity"],
                "quantity_display": allocation["quantity_display"],
                "unit_code": allocation.get("unit_code") or effective.get("unit_code") or "Unit",
                "warehouse_code": allocation.get("warehouse_code") or "",
                "warehouse_name": allocation.get("warehouse_name") or "",
                "warehouse_bin": allocation.get("warehouse_bin") or "",
                "barcode": allocation.get("barcode") or "",
                "batch_number": allocation.get("batch_number") or "",
                "manufacturing_date": allocation.get("manufacturing_date") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().strftime("%Y-%m-%d"),
                "reason": f"Reserved for UFC order {order.get('order_number') or ''} ({order.get('centre_uid') or ''}).",
                "posted_by": actor["_id"],
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }}, upsert=True,
        )
    return allocations


def approve_ufc_order(actor_user_id, order_id, approved_quantity, unit_price, note="", credit_period_days=0):
    _ensure_indexes()
    actor = _get_avpl_actor(actor_user_id, action=True)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order request.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order request was not found.")
    if order.get("status") != "requested":
        raise ValueError("Only a requested order can be approved.")

    qty = _decimal(approved_quantity)
    requested = _decimal(order.get("requested_quantity"))
    price = _decimal(unit_price)
    if qty <= 0:
        raise ValueError("Approved quantity must be greater than zero.")
    if qty > requested:
        raise ValueError("Approved quantity cannot exceed the UFC requested quantity.")
    if price <= 0:
        raise ValueError("Enter an AVPL sale price greater than zero before approving the order.")
    try:
        credit_days = int(str(credit_period_days or 0).strip() or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Credit Days must be a whole number.") from exc
    if credit_days < 0 or credit_days > 365:
        raise ValueError("Credit Days must be between 0 and 365.")

    product = mongo.db.products.find_one({
        "_id": order.get("source_product_id"),
        "is_deleted": {"$ne": True},
        "is_active": {"$ne": False},
        "status": {"$nin": ["disabled", "deleted"]},
    })
    if not product:
        raise ValueError("The linked AVPL Product Master is no longer active.")

    allocations = _reserve_avpl_stock(order, qty, actor)
    timestamp = now_utc()
    total = qty * price
    result = mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "status": "requested", "stock_reserved": {"$ne": True}},
        {
            "$set": {
                "approved_quantity": float(qty),
                "reserved_quantity": float(qty),
                "unit_price": float(price),
                "total_amount": float(total),
                "credit_period_days": credit_days,
                "approval_note": _clean_text(note, 1000),
                "reservation_allocations": allocations,
                "stock_reserved": True,
                "status": "approved",
                "approved_by": actor["_id"],
                "approved_by_name": actor.get("resolved_name") or "",
                "approved_at": timestamp,
                "updated_at": timestamp,
            }
        },
    )
    if result.modified_count != 1:
        # Roll back reservations if an unexpected concurrent state transition occurred.
        for allocation in allocations:
            mongo.db[AVPL_LOT_COLLECTION].update_one(
                {
                    "_id": allocation["inventory_lot_id"],
                    "reserved_quantity": {"$gte": allocation["quantity"]},
                },
                {"$inc": {"reserved_quantity": -allocation["quantity"]}, "$set": {"updated_at": now_utc()}},
            )
        raise RuntimeError("The order changed while approval was being saved. Refresh and try again.")

    _append_history(
        oid,
        action="approve_order",
        actor=actor,
        note=note or f"Approved {_qty(qty)} {order.get('unit_code') or 'units'}.",
        from_status="requested",
        to_status="approved",
    )
    _notify_user(
        order.get("requested_by"),
        "AVPL Order Approved",
        f"Your order {order.get('order_number')} for {order.get('product_name')} has been approved.",
        "ufc_admin",
    )
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "message": "UFC order approved and AVPL stock reserved successfully.",
    }


def approve_ufc_cart_order(actor_user_id, order_id, approvals, note="", credit_period_days=0):
    """Approve/reject AVPL cart lines atomically from the operator's perspective."""
    _ensure_indexes()
    actor = _get_avpl_actor(actor_user_id, action=True)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order request.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order request was not found.")
    items = _order_items(order)
    if not order.get("items"):
        raise ValueError("This is a single-product historical order. Use the normal approval form.")
    if order.get("status") != "requested":
        raise ValueError("Only a requested order can be approved.")
    if not isinstance(approvals, list):
        raise ValueError("Approval lines are invalid.")
    try:
        credit_days = int(str(credit_period_days or 0).strip() or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Credit Days must be a whole number.") from exc
    if credit_days < 0 or credit_days > 365:
        raise ValueError("Credit Days must be between 0 and 365.")

    approval_map = {str(row.get("line_id") or ""): row for row in approvals if isinstance(row, dict)}
    if not approval_map:
        raise ValueError("Enter approval quantities for the order lines.")

    prepared = []
    all_allocations = []
    try:
        for item in items:
            line_id = str(item.get("line_id") or "")
            request_row = approval_map.get(line_id, {})
            approved = _decimal(request_row.get("approved_quantity"))
            requested = _decimal(item.get("requested_quantity"))
            price = _decimal(request_row.get("unit_price"))
            if approved < 0 or approved > requested:
                raise ValueError(f"Approved quantity for {item.get('product_name') or 'a product'} must be between 0 and {_qty(requested)} {item.get('unit_code') or 'units'}.")
            if approved > 0 and price <= 0:
                raise ValueError(f"Enter a selling price for {item.get('product_name') or 'each approved product'}.")
            product = mongo.db.products.find_one({"_id": item.get("source_product_id"), "is_deleted": {"$ne": True}, "is_active": {"$ne": False}, "status": {"$nin": ["disabled", "deleted"]}})
            if approved > 0 and not product:
                raise ValueError(f"{item.get('product_name') or 'A product'} is no longer active in Product Master.")
            line = dict(item)
            line["approved_quantity"] = float(approved)
            line["reserved_quantity"] = float(approved)
            line["unit_price"] = float(price) if approved > 0 else 0.0
            line["line_total"] = float((approved * price).quantize(Decimal('0.01'))) if approved > 0 else 0.0
            line["status"] = "rejected" if approved <= 0 else ("approved" if approved == requested else "partially_approved")
            if approved > 0:
                allocations = _reserve_avpl_stock(order, approved, actor, item=line)
                line["reservation_allocations"] = allocations
                all_allocations.extend(allocations)
            else:
                line["reservation_allocations"] = []
            prepared.append(line)
    except Exception:
        for allocation in all_allocations:
            lot_id = _to_object_id(allocation.get("inventory_lot_id"))
            qty = float(_decimal(allocation.get("quantity")))
            if lot_id and qty > 0:
                mongo.db[AVPL_LOT_COLLECTION].update_one({"_id": lot_id, "reserved_quantity": {"$gte": qty}}, {"$inc": {"reserved_quantity": -qty}, "$set": {"updated_at": now_utc()}})
        if all_allocations:
            keys = [f"UFC-RESERVE:{oid}:{a.get('line_id') or 'legacy'}:{a.get('inventory_lot_id')}" for a in all_allocations]
            mongo.db[AVPL_MOVEMENT_COLLECTION].delete_many({"source_posting_key": {"$in": keys}})
        raise

    approved_lines = [line for line in prepared if _decimal(line.get("approved_quantity")) > 0]
    if not approved_lines:
        timestamp = now_utc()
        result = mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "status": "requested"}, {"$set": {
            "items": prepared, "status": "rejected", "approval_scope": "none", "rejection_reason": _clean_text(note, 1000) or "No order line was approved.",
            "rejected_by": actor["_id"], "rejected_by_name": actor.get("resolved_name") or "", "rejected_at": timestamp, "updated_at": timestamp,
        }})
        if result.modified_count != 1:
            raise RuntimeError("The order changed while approval was being saved. Refresh and try again.")
        _append_history(oid, action="reject_cart_order", actor=actor, note=note or "No cart line was approved.", from_status="requested", to_status="rejected")
        _notify_user(order.get("requested_by"), "AVPL Order Not Approved", f"No product line in order {order.get('order_number')} was approved.", "ufc_admin")
        return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "message": "No order line was approved; the order was rejected."}

    total = sum((_decimal(line.get("line_total")) for line in prepared), Decimal("0"))
    partial = len(approved_lines) != len(prepared) or any(line.get("status") == "partially_approved" for line in prepared)
    timestamp = now_utc()
    primary = approved_lines[0]
    update = {
        "items": prepared,
        "item_count": len(prepared),
        "approved_item_count": len(approved_lines),
        "approval_scope": "partial" if partial else "full",
        "total_amount": float(total),
        "credit_period_days": credit_days,
        "approval_note": _clean_text(note, 1000),
        "reservation_allocations": all_allocations,
        "stock_reserved": True,
        "status": "approved",
        "approved_by": actor["_id"],
        "approved_by_name": actor.get("resolved_name") or "",
        "approved_at": timestamp,
        "updated_at": timestamp,
    }
    _copy_line_to_legacy_fields(update, primary)
    result = mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "status": "requested", "stock_reserved": {"$ne": True}}, {"$set": update})
    if result.modified_count != 1:
        for allocation in all_allocations:
            lot_id = _to_object_id(allocation.get("inventory_lot_id")); qty = float(_decimal(allocation.get("quantity")))
            if lot_id and qty > 0:
                mongo.db[AVPL_LOT_COLLECTION].update_one({"_id": lot_id, "reserved_quantity": {"$gte": qty}}, {"$inc": {"reserved_quantity": -qty}, "$set": {"updated_at": now_utc()}})
        raise RuntimeError("The order changed while approval was being saved. Refresh and try again.")

    _append_history(oid, action="approve_cart_order", actor=actor, note=note or f"Approved {len(approved_lines)} of {len(prepared)} product line(s).", from_status="requested", to_status="approved")
    _notify_user(order.get("requested_by"), "AVPL Cart Order Approved", f"Order {order.get('order_number')} has {len(approved_lines)} approved product line(s).", "ufc_admin")
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "message": "Cart order approved and AVPL stock reserved line by line."}


def reject_ufc_order(actor_user_id, order_id, reason=""):
    actor = _get_avpl_actor(actor_user_id, action=True)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order request.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order request was not found.")
    if order.get("status") != "requested":
        raise ValueError("Only a requested order can be rejected.")
    reason = _clean_text(reason, 1000)
    timestamp = now_utc()
    mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "status": "requested"},
        {"$set": {
            "status": "rejected",
            "rejection_reason": reason,
            "rejected_by": actor["_id"],
            "rejected_by_name": actor.get("resolved_name") or "",
            "rejected_at": timestamp,
            "updated_at": timestamp,
        }},
    )
    _append_history(
        oid,
        action="reject_order",
        actor=actor,
        note=reason or "Rejected by AVPL Admin.",
        from_status="requested",
        to_status="rejected",
    )
    _notify_user(
        order.get("requested_by"),
        "AVPL Order Rejected",
        f"Your order {order.get('order_number')} for {order.get('product_name')} was rejected by AVPL.",
        "ufc_admin",
    )
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "message": "UFC order request rejected.",
    }


def cancel_approved_ufc_order(actor_user_id, order_id, reason=""):
    """Cancel an approved but undispatched order and release reserved stock."""
    actor = _get_avpl_actor(actor_user_id, action=True)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order request.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order request was not found.")
    if order.get("status") != "approved" or order.get("stock_dispatched") is True:
        raise ValueError("Only an approved, undispatched order can be cancelled.")

    allocations = order.get("reservation_allocations") or []
    released = []
    for allocation in allocations:
        lot_id = _to_object_id(allocation.get("inventory_lot_id"))
        quantity_value = float(_decimal(allocation.get("quantity")))
        if not lot_id or quantity_value <= 0:
            continue
        result = mongo.db[AVPL_LOT_COLLECTION].update_one(
            {"_id": lot_id, "reserved_quantity": {"$gte": quantity_value}},
            {"$inc": {"reserved_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}},
        )
        if result.modified_count != 1:
            # restore what was already released before refusing to alter order status
            for prev_lot_id, prev_qty in released:
                mongo.db[AVPL_LOT_COLLECTION].update_one(
                    {"_id": prev_lot_id},
                    {"$inc": {"reserved_quantity": prev_qty}, "$set": {"updated_at": now_utc()}},
                )
            raise RuntimeError("Reserved stock changed unexpectedly. Refresh and try cancellation again.")
        released.append((lot_id, quantity_value))

    timestamp = now_utc()
    for allocation in allocations:
        source_key = f"UFC-RELEASE:{oid}:{allocation.get('inventory_lot_id')}"
        mongo.db[AVPL_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {"$setOnInsert": {
                "source_posting_key": source_key,
                "movement_uid": uuid4().hex,
                "accounting_entity_id": order.get("accounting_entity_id"),
                "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
                "source_document_type": "ufc_order",
                "source_document_id": oid,
                "source_document_id_str": str(oid),
                "source_document_number": order.get("order_number") or "",
                "source_product_id": order.get("source_product_id"),
                "source_product_id_str": str(order.get("source_product_id") or ""),
                "product_code": order.get("product_code") or "",
                "product_name": order.get("product_name") or "Product",
                "movement_type": "reservation_release",
                "direction": "release",
                "quantity": float(_decimal(allocation.get("quantity"))),
                "quantity_display": _qty(allocation.get("quantity")),
                "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                "warehouse_code": allocation.get("warehouse_code") or "",
                "warehouse_name": allocation.get("warehouse_name") or "",
                "warehouse_bin": allocation.get("warehouse_bin") or "",
                "batch_number": allocation.get("batch_number") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().strftime("%Y-%m-%d"),
                "reason": _clean_text(reason, 1000) or f"Reservation released for cancelled UFC order {order.get('order_number') or ''}.",
                "posted_by": actor["_id"],
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }},
            upsert=True,
        )

    mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "status": "approved"},
        {"$set": {
            "status": "cancelled",
            "reserved_quantity": 0.0,
            "stock_reserved": False,
            "cancellation_reason": _clean_text(reason, 1000),
            "cancelled_by": actor["_id"],
            "cancelled_by_name": actor.get("resolved_name") or "",
            "cancelled_at": timestamp,
            "updated_at": timestamp,
        }},
    )
    _append_history(
        oid,
        action="cancel_order",
        actor=actor,
        note=reason or "Approved order cancelled before dispatch; reservation released.",
        from_status="approved",
        to_status="cancelled",
    )
    _notify_user(
        order.get("requested_by"),
        "AVPL Order Cancelled",
        f"Approved order {order.get('order_number')} was cancelled before dispatch.",
        "ufc_admin",
    )
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "message": "Order cancelled and reserved AVPL stock released.",
    }


def _sync_legacy_product_quantity(entity_id, product_id):
    total = Decimal("0")
    for lot in mongo.db[AVPL_LOT_COLLECTION].find({
        "accounting_entity_id": entity_id,
        "source_product_id": product_id,
        "status": {"$ne": "cancelled"},
    }, {"available_quantity": 1}):
        total += max(_decimal(lot.get("available_quantity")), Decimal("0"))
    mongo.db.products.update_one(
        {"_id": product_id},
        {"$set": {
            "available_quantity": float(total),
            "legacy_stock_mirror_updated_at": now_utc(),
            "legacy_stock_mirror_source": "stage4_avpl_ufc_dispatch",
        }},
    )


def dispatch_ufc_order(actor_user_id, order_id, dispatch_note="", transporter="", vehicle_number=""):
    _ensure_indexes()
    actor = _get_avpl_actor(actor_user_id, action=True)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order request.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order request was not found.")
    if order.get("status") != "approved" or order.get("stock_reserved") is not True:
        raise ValueError("Only an approved order with reserved stock can be dispatched.")
    if order.get("stock_dispatched") is True:
        return {"order": _serialize_order(order), "message": "This order is already dispatched."}

    allocations = order.get("reservation_allocations") or []
    if not allocations:
        raise RuntimeError("No reserved stock allocation exists for this order.")

    moved = []
    today_iso = business_today().strftime("%Y-%m-%d")
    try:
        for allocation in allocations:
            lot_id = _to_object_id(allocation.get("inventory_lot_id"))
            quantity_value = float(_decimal(allocation.get("quantity")))
            if not lot_id or quantity_value <= 0:
                raise RuntimeError("The reservation contains an invalid stock lot.")
            result = mongo.db[AVPL_LOT_COLLECTION].update_one(
                {
                    "_id": lot_id, "status": {"$nin": ["cancelled", "expired"]},
                    "available_quantity": {"$gte": quantity_value}, "reserved_quantity": {"$gte": quantity_value},
                    "$or": [{"expiry_date": {"$exists": False}}, {"expiry_date": None}, {"expiry_date": ""}, {"expiry_date": {"$gte": today_iso}}],
                },
                {"$inc": {"available_quantity": -quantity_value, "reserved_quantity": -quantity_value, "issued_quantity": quantity_value}, "$set": {"updated_at": now_utc(), "last_dispatch_order_id": oid}},
            )
            if result.modified_count != 1:
                raise RuntimeError("A reserved batch is no longer dispatchable (stock changed or expired). Cancel/release this order and approve it again.")
            moved.append((lot_id, quantity_value))
    except Exception:
        for lot_id, quantity_value in reversed(moved):
            mongo.db[AVPL_LOT_COLLECTION].update_one({"_id": lot_id}, {"$inc": {"available_quantity": quantity_value, "reserved_quantity": quantity_value, "issued_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}})
        raise

    timestamp = now_utc()
    for allocation in allocations:
        line_id = allocation.get("line_id") or "legacy"
        source_key = f"UFC-DISPATCH:{oid}:{line_id}:{allocation.get('inventory_lot_id')}"
        mongo.db[AVPL_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {"$setOnInsert": {
                "source_posting_key": source_key, "movement_uid": uuid4().hex,
                "accounting_entity_id": order.get("accounting_entity_id"), "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
                "source_document_type": "ufc_order_dispatch", "source_document_id": oid, "source_document_id_str": str(oid), "source_document_number": order.get("order_number") or "",
                "line_id": line_id,
                "source_product_id": allocation.get("source_product_id") or order.get("source_product_id"),
                "source_product_id_str": str(allocation.get("source_product_id") or order.get("source_product_id") or ""),
                "product_code": allocation.get("product_code") or order.get("product_code") or "",
                "product_name": allocation.get("product_name") or order.get("product_name") or "Product",
                "movement_type": "sale", "direction": "out",
                "quantity": float(_decimal(allocation.get("quantity"))), "quantity_display": _qty(allocation.get("quantity")),
                "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                "warehouse_code": allocation.get("warehouse_code") or "", "warehouse_name": allocation.get("warehouse_name") or "", "warehouse_bin": allocation.get("warehouse_bin") or "",
                "barcode": allocation.get("barcode") or "", "batch_number": allocation.get("batch_number") or "", "manufacturing_date": allocation.get("manufacturing_date") or "", "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().strftime("%Y-%m-%d"),
                "reason": f"Dispatched to UFC {order.get('centre_uid') or ''} against order {order.get('order_number') or ''}.",
                "posted_by": actor["_id"], "posted_by_name": actor.get("resolved_name") or "", "posted_at": timestamp, "created_at": timestamp,
            }}, upsert=True,
        )

    items = _order_items(order)
    dispatched_items = []
    approved_line_ids = set()
    for item in items:
        line = dict(item)
        approved = _decimal(line.get("approved_quantity"))
        if approved > 0:
            line["dispatched_quantity"] = float(approved)
            line["reserved_quantity"] = 0.0
            line["status"] = "dispatched"
            approved_line_ids.add(str(line.get("line_id") or "legacy"))
        dispatched_items.append(line)
    primary = next((line for line in dispatched_items if _decimal(line.get("dispatched_quantity")) > 0), dispatched_items[0] if dispatched_items else {})
    update = {
        "status": "dispatched", "stock_reserved": False, "stock_dispatched": True,
        "reservation_allocations": allocations, "dispatch_allocations": allocations,
        "items": dispatched_items if order.get("items") else order.get("items"),
        "dispatched_item_count": len(approved_line_ids),
        "dispatch_note": _clean_text(dispatch_note, 1000), "transporter_name": _clean_text(transporter, 120), "vehicle_number": _clean_text(vehicle_number, 30).upper(),
        "dispatched_by": actor["_id"], "dispatched_by_name": actor.get("resolved_name") or "", "dispatched_at": timestamp, "updated_at": timestamp,
    }
    if order.get("items"):
        _copy_line_to_legacy_fields(update, primary)
    else:
        update["reserved_quantity"] = 0.0
        update["dispatched_quantity"] = float(sum((_decimal(a.get("quantity")) for a in allocations), Decimal("0")))
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "status": "approved", "stock_dispatched": {"$ne": True}}, {"$set": update})

    product_ids = {a.get("source_product_id") or order.get("source_product_id") for a in allocations}
    for product_id in product_ids:
        if product_id:
            _sync_legacy_product_quantity(order["accounting_entity_id"], product_id)
    _append_history(oid, action="dispatch_order", actor=actor, note=dispatch_note or "AVPL physically dispatched the reserved goods to UFC.", from_status="approved", to_status="dispatched")
    _notify_user(order.get("requested_by"), "AVPL Order Dispatched", f"Order {order.get('order_number')} has been dispatched. Confirm receipt after the goods physically arrive at your UFC.", "ufc_admin")

    financial_result = None; financial_warning = None
    try:
        from app.services.avpl_ufc_sales_service import ensure_sales_documents_for_order
        financial_result = ensure_sales_documents_for_order(actor["_id"], oid)
    except Exception as exc:
        financial_warning = str(exc)
        try:
            from app.services.avpl_ufc_sales_service import mark_sales_sync_error
            mark_sales_sync_error(oid, financial_warning)
        except Exception:
            pass
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "financial": financial_result, "financial_warning": financial_warning, "message": "Order dispatched. AVPL physical stock has been reduced line by line."}


def _ufc_lot_key(centre_uid, product_id, allocation):
    batch = allocation.get("batch_number") or "NO-BATCH"
    expiry = allocation.get("expiry_date") or "NO-EXPIRY"
    return ":".join([str(centre_uid), str(product_id), str(batch), str(expiry)])


def _accepted_dispatch_allocations(order, receipt_lines):
    """Return dispatch-lot slices for accepted quantities only."""
    accepted_by_line = {
        str(line.get("line_id") or "legacy"): _decimal(line.get("accepted_quantity"))
        for line in (receipt_lines or [])
        if isinstance(line, dict) and line.get("receipt_applicable")
    }
    remaining = dict(accepted_by_line)
    selected = []
    allocations = order.get("dispatch_allocations") or order.get("reservation_allocations") or []
    for allocation in allocations:
        line_id = str(allocation.get("line_id") or "legacy")
        need = max(remaining.get(line_id, Decimal("0")), Decimal("0"))
        if need <= 0:
            continue
        available = max(_decimal(allocation.get("quantity")), Decimal("0"))
        take = min(need, available)
        if take <= 0:
            continue
        row = dict(allocation)
        row["quantity"] = float(take)
        row["quantity_display"] = _qty(take)
        selected.append(row)
        remaining[line_id] = need - take
    unresolved = {line_id: qty for line_id, qty in remaining.items() if qty > Decimal("0.0001")}
    if unresolved:
        raise RuntimeError("Accepted receipt quantity could not be matched to the dispatched AVPL stock lots. Refresh and try again.")
    return selected


def _apply_ufc_inventory(order, actor, receipt_lines=None):
    receipt_lines = receipt_lines or normalize_receipt_lines(_order_items(order), None)
    allocations = _accepted_dispatch_allocations(order, receipt_lines)
    timestamp = now_utc()
    for index, allocation in enumerate(allocations, start=1):
        quantity_value = float(_decimal(allocation.get("quantity")))
        if quantity_value <= 0:
            continue
        product_id = allocation.get("source_product_id") or order.get("source_product_id")
        product_name = allocation.get("product_name") or order.get("product_name") or "Product"
        product_code = allocation.get("product_code") or order.get("product_code") or ""
        category = allocation.get("category") or order.get("category") or ""
        product_role = allocation.get("product_role") or order.get("product_role") or ""
        unit_code = allocation.get("unit_code") or order.get("unit_code") or "Unit"
        unit_price = _decimal(allocation.get("unit_price"))
        if unit_price <= 0:
            matched = next((x for x in _order_items(order) if str(x.get("line_id") or "legacy") == str(allocation.get("line_id") or "legacy")), {})
            unit_price = _decimal(matched.get("unit_price") or order.get("unit_price"))
        line_id = allocation.get("line_id") or "legacy"
        receipt_key = f"AVPL-ORDER-RECEIPT:{order['_id']}:{line_id}:{allocation.get('inventory_lot_id') or index}"
        lot_key = _ufc_lot_key(order.get("centre_uid"), product_id, allocation)
        existing = mongo.db[UFC_LOT_COLLECTION].find_one({"lot_key": lot_key})
        if existing:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"_id": existing["_id"], "applied_receipt_keys": {"$ne": receipt_key}},
                {"$inc": {"received_quantity": quantity_value, "available_quantity": quantity_value, "purchase_cost_total": quantity_value * float(unit_price)},
                 "$addToSet": {"applied_receipt_keys": receipt_key, "source_order_ids": order["_id"], "source_order_numbers": order.get("order_number") or ""},
                 "$set": {"last_purchase_price": float(unit_price), "last_receipt_at": timestamp, "updated_at": timestamp}},
            )
        else:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"lot_key": lot_key},
                {"$setOnInsert": {
                    "lot_key": lot_key, "centre_uid": order.get("centre_uid"), "centre_name": order.get("centre_name") or order.get("centre_uid"),
                    "source_type": "avpl_purchase", "source_product_id": product_id, "source_product_id_str": str(product_id or ""),
                    "product_name": product_name, "product_code": product_code, "category": category, "product_role": product_role,
                    "unit_code": unit_code, "warehouse_code": f"{str(order.get('centre_uid') or 'UFC').upper()}-MAIN", "warehouse_name": f"{order.get('centre_name') or order.get('centre_uid')} Main Stock",
                    "source_avpl_inventory_lot_id": allocation.get("inventory_lot_id"), "source_avpl_inventory_lot_id_str": str(allocation.get("inventory_lot_id") or ""),
                    "batch_number": allocation.get("batch_number") or "", "manufacturing_date": allocation.get("manufacturing_date") or "", "expiry_date": allocation.get("expiry_date") or "",
                    "received_quantity": quantity_value, "available_quantity": quantity_value, "reserved_quantity": 0.0, "damaged_quantity": 0.0, "blocked_quantity": 0.0, "issued_quantity": 0.0,
                    "purchase_cost_total": quantity_value * float(unit_price), "last_purchase_price": float(unit_price), "status": "available",
                    "applied_receipt_keys": [receipt_key], "source_order_ids": [order["_id"]], "source_order_numbers": [order.get("order_number") or ""],
                    "created_by": actor["_id"], "created_at": timestamp, "last_receipt_at": timestamp, "updated_at": timestamp,
                }}, upsert=True,
            )

        movement_key = f"UFC-RECEIPT:{order['_id']}:{line_id}:{allocation.get('inventory_lot_id') or index}"
        mongo.db[UFC_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": movement_key},
            {"$setOnInsert": {
                "source_posting_key": movement_key, "movement_uid": uuid4().hex,
                "centre_uid": order.get("centre_uid"), "centre_name": order.get("centre_name") or order.get("centre_uid"),
                "source_document_type": "avpl_order_receipt", "source_document_id": order["_id"], "source_document_id_str": str(order["_id"]), "source_document_number": order.get("order_number") or "",
                "line_id": line_id, "source_product_id": product_id, "source_product_id_str": str(product_id or ""), "product_code": product_code, "product_name": product_name,
                "movement_type": "purchase_receipt", "direction": "in", "quantity": quantity_value, "quantity_display": _qty(quantity_value), "unit_code": unit_code,
                "warehouse_code": f"{str(order.get('centre_uid') or 'UFC').upper()}-MAIN", "batch_number": allocation.get("batch_number") or "", "manufacturing_date": allocation.get("manufacturing_date") or "", "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": business_today().strftime("%Y-%m-%d"), "reason": f"Accepted from AVPL against order {order.get('order_number') or ''}.",
                "posted_by": actor["_id"], "posted_by_name": actor.get("resolved_name") or "", "posted_at": timestamp, "created_at": timestamp,
            }}, upsert=True,
        )

def _accepted_avpl_invoice_lines(order, invoice, receipt_lines):
    invoice_lines = (invoice or {}).get("items") or []
    invoice_by_line = {str(x.get("line_id") or "legacy"): x for x in invoice_lines if isinstance(x, dict)}
    order_by_line = {str(x.get("line_id") or "legacy"): x for x in _order_items(order)}
    rows = []
    totals = {"taxable_value": Decimal("0"), "cgst_amount": Decimal("0"), "sgst_amount": Decimal("0"), "igst_amount": Decimal("0"), "gst_amount": Decimal("0"), "grand_total": Decimal("0")}
    for receipt in receipt_lines or []:
        if not isinstance(receipt, dict) or not receipt.get("receipt_applicable"):
            continue
        accepted = _decimal(receipt.get("accepted_quantity"))
        if accepted <= 0:
            continue
        line_id = str(receipt.get("line_id") or "legacy")
        source = order_by_line.get(line_id, receipt)
        dispatched = _decimal(receipt.get("dispatched_quantity_for_receipt") or source.get("dispatched_quantity") or source.get("approved_quantity"))
        inv_line = invoice_by_line.get(line_id, {})
        price = _decimal(source.get("unit_price"))
        taxable = proportional_amount(inv_line.get("taxable_value", dispatched * price), accepted, dispatched)
        cgst = proportional_amount(inv_line.get("cgst_amount"), accepted, dispatched)
        sgst = proportional_amount(inv_line.get("sgst_amount"), accepted, dispatched)
        igst = proportional_amount(inv_line.get("igst_amount"), accepted, dispatched)
        gst = proportional_amount(inv_line.get("gst_amount", cgst + sgst + igst), accepted, dispatched)
        grand = proportional_amount(inv_line.get("grand_total", taxable + gst), accepted, dispatched)
        if grand <= 0:
            grand = (taxable + cgst + sgst + igst).quantize(Decimal("0.01"))
        totals["taxable_value"] += taxable; totals["cgst_amount"] += cgst; totals["sgst_amount"] += sgst; totals["igst_amount"] += igst; totals["gst_amount"] += gst; totals["grand_total"] += grand
        rows.append({
            "line_id": line_id,
            "source_product_id": source.get("source_product_id"), "source_product_id_str": str(source.get("source_product_id") or ""),
            "product_name": source.get("product_name") or "Product", "product_code": source.get("product_code") or "", "category": source.get("category") or "", "product_role": source.get("product_role") or "",
            "quantity": float(accepted), "accepted_quantity": float(accepted), "quantity_display": _qty(accepted), "unit_code": source.get("unit_code") or "Unit",
            "unit_price": float(price), "unit_price_display": _money(price),
            "taxable_value": float(taxable), "hsn_code": inv_line.get("hsn_code") or "", "taxability_code": inv_line.get("taxability_code") or "",
            "gst_rate": float(_decimal(inv_line.get("gst_rate"))), "cgst_amount": float(cgst), "sgst_amount": float(sgst), "igst_amount": float(igst), "gst_amount": float(gst),
            "line_total": float(grand), "line_total_display": _money(grand),
        })
    return rows, {key: value.quantize(Decimal("0.01")) for key, value in totals.items()}


def _apply_avpl_receipt_settlement(order, receipt_lines, receipt_summary):
    try:
        from app.services.avpl_ufc_sales_service import (
            INVOICE_COLLECTION, RECEIVABLE_COLLECTION, SALE_COLLECTION, UFC_PAYABLE_COLLECTION,
        )
    except Exception:
        return None
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    if not invoice:
        return None
    accepted_lines, totals = _accepted_avpl_invoice_lines(order, invoice, receipt_lines)
    original_total = _decimal(invoice.get("grand_total"))
    settlement_total = totals["grand_total"]
    paid = max(_decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount")), Decimal("0"))
    outstanding = max(settlement_total - paid, Decimal("0"))
    status = "paid" if outstanding <= Decimal("0.004") else ("partially_paid" if paid > Decimal("0.004") else "unpaid")
    adjustment = max(original_total - settlement_total, Decimal("0"))
    common = {
        "settlement_total": float(settlement_total),
        "accepted_goods_total": float(settlement_total),
        "receipt_adjustment_amount": float(adjustment),
        "receipt_status": receipt_summary.get("receipt_status") or "full",
        "receipt_lines": accepted_lines,
        "payment_status": status,
        "amount_paid": float(min(paid, settlement_total) if settlement_total >= 0 else paid),
        "outstanding_amount": float(outstanding),
        "updated_at": now_utc(),
    }
    mongo.db[INVOICE_COLLECTION].update_one({"_id": invoice["_id"]}, {"$set": {**common, "receipt_finalized": True}})
    sale_id = invoice.get("avpl_ufc_sale_id")
    if sale_id:
        mongo.db[SALE_COLLECTION].update_one({"_id": sale_id}, {"$set": {**common, "status": "received"}})
    mongo.db[RECEIVABLE_COLLECTION].update_one({"avpl_ufc_order_id": order["_id"]}, {"$set": {**common, "amount": float(settlement_total), "status": "closed" if status == "paid" else "open"}})
    mongo.db[UFC_PAYABLE_COLLECTION].update_one({"avpl_ufc_order_id": order["_id"]}, {"$set": {**common, "amount": float(settlement_total), "status": "closed" if status == "paid" else "open"}})
    return mongo.db[INVOICE_COLLECTION].find_one({"_id": invoice["_id"]})


def _create_ufc_purchase_entry(order, actor, receipt_lines=None):
    existing = mongo.db[UFC_PURCHASE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    if existing:
        return existing
    timestamp = now_utc()
    invoice = None
    try:
        from app.services.avpl_ufc_sales_service import INVOICE_COLLECTION
        invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    except Exception:
        invoice = None

    receipt_lines = receipt_lines or normalize_receipt_lines(_order_items(order), None)
    purchase_items, accepted_totals = _accepted_avpl_invoice_lines(order, invoice, receipt_lines)
    if not purchase_items:
        # A fully missing/rejected receipt is still a valid goods-receipt event;
        # keep a zero-value purchase record for audit and settlement clarity.
        for line in receipt_lines:
            if not line.get("receipt_applicable"):
                continue
            purchase_items.append({
                "line_id": line.get("line_id") or "legacy", "source_product_id": line.get("source_product_id"), "source_product_id_str": str(line.get("source_product_id") or ""),
                "product_name": line.get("product_name") or "Product", "product_code": line.get("product_code") or "", "category": line.get("category") or "", "product_role": line.get("product_role") or "",
                "quantity": 0.0, "accepted_quantity": 0.0, "quantity_display": "0", "unit_code": line.get("unit_code") or "Unit", "unit_price": float(_decimal(line.get("unit_price"))), "unit_price_display": _money(line.get("unit_price")),
                "taxable_value": 0.0, "gst_rate": 0.0, "cgst_amount": 0.0, "sgst_amount": 0.0, "igst_amount": 0.0, "gst_amount": 0.0, "line_total": 0.0, "line_total_display": "0.00",
            })
    total = accepted_totals["grand_total"]
    original_total = _decimal((invoice or {}).get("grand_total"), str(total))
    primary = next((x for x in purchase_items if _decimal(x.get("accepted_quantity") if x.get("accepted_quantity") is not None else x.get("quantity")) > 0), purchase_items[0] if purchase_items else _primary_item(order))
    summary = summarize_receipt(receipt_lines)
    document = {
        "purchase_number": _next_purchase_number(order.get("centre_uid")),
        "centre_uid": order.get("centre_uid"), "centre_name": order.get("centre_name") or order.get("centre_uid"),
        "seller_type": "avpl", "seller_name": "AVPL",
        "avpl_ufc_order_id": order["_id"], "avpl_ufc_order_id_str": str(order["_id"]), "avpl_order_number": order.get("order_number") or "",
        "commerce_version": 2 if order.get("items") else 1, "items": purchase_items, "item_count": len(purchase_items) or 1,
        "source_product_id": primary.get("source_product_id"), "source_product_id_str": str(primary.get("source_product_id") or ""),
        "product_name": primary.get("product_name") or "Product", "product_code": primary.get("product_code") or "", "category": primary.get("category") or "", "product_role": primary.get("product_role") or "",
        "quantity": float(_decimal(primary.get("accepted_quantity") if primary.get("accepted_quantity") is not None else primary.get("quantity"))),
        "quantity_display": _qty(primary.get("accepted_quantity") if primary.get("accepted_quantity") is not None else primary.get("quantity")),
        "unit_code": primary.get("unit_code") or "Unit", "unit_price": float(_decimal(primary.get("unit_price"))), "unit_price_display": _money(primary.get("unit_price")),
        "original_invoice_total": float(original_total), "total_amount": float(total), "total_amount_display": _money(total),
        "receipt_adjustment_amount": float(max(original_total-total, Decimal("0"))), "receipt_status": summary.get("receipt_status"), "receipt_lines": receipt_lines,
        "avpl_sales_invoice_id": (invoice or {}).get("_id"), "avpl_sales_invoice_id_str": str((invoice or {}).get("_id") or ""), "avpl_sales_invoice_number": (invoice or {}).get("invoice_number") or "",
        "invoice_date": (invoice or {}).get("invoice_date"), "due_date": (invoice or {}).get("due_date"),
        "hsn_code": (invoice or {}).get("hsn_code") or "", "taxability_code": (invoice or {}).get("taxability_code") or "",
        "taxable_value": float(accepted_totals["taxable_value"]), "gst_rate": float(_decimal((invoice or {}).get("gst_rate"))),
        "cgst_amount": float(accepted_totals["cgst_amount"]), "sgst_amount": float(accepted_totals["sgst_amount"]), "igst_amount": float(accepted_totals["igst_amount"]), "gst_amount": float(accepted_totals["gst_amount"]),
        "purchase_date": timestamp, "status": "received", "accounting_status": "not_posted",
        "payment_status": "unpaid" if total > 0 else "paid", "amount_paid": 0.0, "outstanding_amount": float(total),
        "financial_link_status": "linked" if invoice else "awaiting_avpl_invoice",
        "received_by": actor["_id"], "received_by_name": actor.get("resolved_name") or "", "created_at": timestamp, "updated_at": timestamp,
    }
    result = mongo.db[UFC_PURCHASE_COLLECTION].insert_one(document)
    document["_id"] = result.inserted_id
    return document

def receive_ufc_order(actor_user_id, centre_uid_hint, order_id, receipt_note="", receipt_lines=None):
    _ensure_indexes()
    actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid AVPL order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid, "centre_uid": centre_uid})
    if not order:
        raise ValueError("This AVPL order does not belong to your UFC Centre.")
    if order.get("status") == "received" and order.get("ufc_stock_posted") is True:
        purchase = mongo.db[UFC_PURCHASE_COLLECTION].find_one({"avpl_ufc_order_id": oid})
        return {"order": _serialize_order(order), "purchase": purchase, "message": "This receipt is already confirmed. Stock and payable were not changed again."}
    if order.get("status") != "dispatched" or order.get("stock_dispatched") is not True:
        raise ValueError("Only a physically dispatched AVPL order can be received.")

    receipt_items = normalize_receipt_lines(
        _order_items(order), receipt_lines,
        dispatched_fields=("dispatched_quantity", "approved_quantity"),
        price_field="unit_price",
        line_total_field="line_total",
        allow_legacy_full_receipt=True,
    )
    summary = summarize_receipt(receipt_items)

    # Stock is created strictly from buyer-accepted quantities. Missing, damaged
    # and rejected quantities never enter UFC saleable stock.
    _apply_ufc_inventory(order, actor, receipt_items)
    purchase = _create_ufc_purchase_entry(order, actor, receipt_items)
    adjusted_invoice = _apply_avpl_receipt_settlement(order, receipt_items, summary)

    updated_items = []
    for line in receipt_items:
        row = dict(line)
        if row.get("receipt_applicable"):
            row["status"] = "received" if _decimal(row.get("discrepancy_quantity")) <= Decimal("0.0001") else "received_with_discrepancy"
        updated_items.append(row)
    primary = next((line for line in updated_items if line.get("receipt_applicable")), updated_items[0] if updated_items else {})
    timestamp = now_utc()
    settlement_total = _decimal((adjusted_invoice or {}).get("settlement_total"), str(purchase.get("total_amount") or 0))
    original_total = _decimal((adjusted_invoice or {}).get("grand_total"), str(order.get("invoice_grand_total") or order.get("total_amount") or settlement_total))
    update = {
        "status": "received", "receipt_status": summary.get("receipt_status"), "receipt_finalized": True,
        "ufc_stock_posted": True, "purchase_entry_created": True,
        "ufc_purchase_entry_id": purchase.get("_id"), "ufc_purchase_number": purchase.get("purchase_number") or "",
        "items": updated_items if order.get("items") else order.get("items"),
        "received_item_count": summary.get("received_item_count"), "accepted_item_count": summary.get("accepted_item_count"), "discrepancy_item_count": summary.get("discrepancy_item_count"),
        "accepted_goods_total": float(settlement_total), "settlement_total": float(settlement_total), "receipt_adjustment_amount": float(max(original_total-settlement_total, Decimal("0"))),
        "receipt_note": _clean_text(receipt_note, 1000), "received_by": actor["_id"], "received_by_name": actor.get("resolved_name") or centre_name,
        "received_at": timestamp, "updated_at": timestamp,
    }
    if order.get("items"):
        _copy_line_to_legacy_fields(update, primary)
    else:
        update["received_quantity"] = float(_decimal(primary.get("physically_received_quantity")))
        update["accepted_quantity"] = float(_decimal(primary.get("accepted_quantity")))
        update["damaged_quantity"] = float(_decimal(primary.get("damaged_quantity")))
        update["rejected_quantity"] = float(_decimal(primary.get("rejected_quantity")))
        update["missing_quantity"] = float(_decimal(primary.get("missing_quantity")))
    mongo.db[ORDER_COLLECTION].update_one({"_id": oid, "centre_uid": centre_uid, "status": "dispatched"}, {"$set": update})
    try:
        from app.services.avpl_ufc_sales_service import link_ufc_purchase_financials
        purchase = link_ufc_purchase_financials(oid) or purchase
    except Exception:
        pass

    note = receipt_note or ("UFC accepted all dispatched goods." if summary.get("receipt_status") == "full" else f"UFC receipt recorded with {summary.get('discrepancy_item_count')} discrepant product line(s).")
    _append_history(oid, action="receive_order", actor=actor, note=note, from_status="dispatched", to_status="received")
    _notify_avpl_admins("UFC Confirmed Receipt", f"{centre_name} ({centre_uid}) received order {order.get('order_number')}. Accepted payable value: ₹{_money(settlement_total)}.")
    message = "Goods received. Accepted quantities were added to UFC stock and are now payable."
    if summary.get("receipt_status") == "discrepancy":
        message = f"Receipt saved with discrepancy. Only accepted goods worth ₹{_money(settlement_total)} are payable; missing/damaged/rejected goods were excluded."
    return {"order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})), "purchase": purchase, "message": message}

def get_order(order_id, *, centre_uid=None):
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid order.")
    query = {"_id": oid}
    if centre_uid:
        query["centre_uid"] = str(centre_uid)
    order = mongo.db[ORDER_COLLECTION].find_one(query)
    if not order:
        raise ValueError("Order was not found.")
    return _serialize_order(order)


def _order_search_query(search):
    text = _clean_text(search, 120)
    if not text:
        return None
    import re
    escaped = re.escape(text)
    return {"$or": [
        {"order_number": {"$regex": escaped, "$options": "i"}},
        {"centre_uid": {"$regex": escaped, "$options": "i"}},
        {"centre_name": {"$regex": escaped, "$options": "i"}},
        {"product_name": {"$regex": escaped, "$options": "i"}},
        {"items.product_name": {"$regex": escaped, "$options": "i"}},
        {"product_code": {"$regex": escaped, "$options": "i"}},
        {"items.product_code": {"$regex": escaped, "$options": "i"}},
    ]}


def _order_overview(query, *, status_filter="", search="", page=1, per_page=30):
    status = str(status_filter or "").strip().lower()
    if status and status != "all":
        query["status"] = status
    search_clause = _order_search_query(search)
    if search_clause:
        query.update(search_clause)
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 30), 10), 100)
    collection = mongo.db[ORDER_COLLECTION]
    total = collection.count_documents(query)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    rows = [
        _serialize_order(row)
        for row in collection.find(query)
        .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        .skip((page - 1) * per_page)
        .limit(per_page)
    ]
    counts = {
        status_code: collection.count_documents({
            **{key: value for key, value in query.items() if key not in {"status", "$or"}},
            "status": status_code,
        })
        for status_code in ORDER_STATUS_LABELS
    }
    return {
        "rows": rows,
        "selected_status": status or "all",
        "query": search or "",
        "statuses": ORDER_STATUS_LABELS,
        "counts": counts,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def get_avpl_order_overview(actor_user_id, *, status_filter="", search="", page=1):
    _ensure_indexes()
    _get_avpl_actor(actor_user_id, action=False)
    entity = _active_avpl_entity()
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is unavailable.")
    return _order_overview(
        {"accounting_entity_id": entity["_id"]},
        status_filter=status_filter,
        search=search,
        page=page,
    )


def get_ufc_order_overview(actor_user_id, centre_uid_hint, *, status_filter="", search="", page=1):
    _ensure_indexes()
    _actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    overview = _order_overview(
        {"centre_uid": centre_uid},
        status_filter=status_filter,
        search=search,
        page=page,
    )
    overview["centre_uid"] = centre_uid
    overview["centre_name"] = centre_name
    return overview


def get_ufc_stock_overview(actor_user_id, centre_uid_hint, *, search=""):
    _ensure_indexes()
    _actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    query = {"centre_uid": centre_uid, "status": {"$ne": "cancelled"}}
    text = _clean_text(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"product_code": {"$regex": escaped, "$options": "i"}},
            {"batch_number": {"$regex": escaped, "$options": "i"}},
        ]
    grouped = defaultdict(lambda: {
        "physical": Decimal("0"),
        "reserved": Decimal("0"),
        "damaged": Decimal("0"),
        "blocked": Decimal("0"),
        "cost": Decimal("0"),
        "received": Decimal("0"),
        "batch_count": 0,
        "unit_code": "Unit",
        "product_name": "Product",
        "product_code": "",
        "category": "",
        "product_role": "",
    })
    for lot in mongo.db[UFC_LOT_COLLECTION].find(query).sort([("product_name", 1), ("expiry_date", 1)]):
        key = str(lot.get("source_product_id") or "")
        row = grouped[key]
        physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
        damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
        blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
        expired = physical if _lot_expired(lot) else Decimal("0")
        row["physical"] += physical
        row["reserved"] += reserved
        row["damaged"] += damaged
        row["blocked"] += blocked + expired
        row["cost"] += _decimal(lot.get("purchase_cost_total"))
        row["received"] += max(_decimal(lot.get("received_quantity")), Decimal("0"))
        row["batch_count"] += 1
        row["unit_code"] = lot.get("unit_code") or row["unit_code"]
        row["product_name"] = lot.get("product_name") or row["product_name"]
        row["product_code"] = lot.get("product_code") or row["product_code"]
        row["category"] = lot.get("category") or row["category"]
        row["product_role"] = lot.get("product_role") or row["product_role"]

    rows = []
    total_value = Decimal("0")
    for product_id, row in grouped.items():
        saleable = max(row["physical"] - row["reserved"] - row["damaged"] - row["blocked"], Decimal("0"))
        wac = row["cost"] / row["received"] if row["received"] > 0 else Decimal("0")
        value = row["physical"] * wac
        total_value += value
        rows.append({
            "product_id": product_id,
            "product_name": row["product_name"],
            "product_code": row["product_code"] or "-",
            "category": row["category"] or "-",
            "product_role": str(row["product_role"] or "-").replace("_", " ").title(),
            "unit_code": row["unit_code"],
            "physical_quantity": _qty(row["physical"]),
            "reserved_quantity": _qty(row["reserved"]),
            "saleable_quantity": _qty(saleable),
            "blocked_quantity": _qty(row["blocked"] + row["damaged"]),
            "batch_count": row["batch_count"],
            "weighted_average_cost": _money(wac),
            "stock_value": _money(value),
        })
    rows.sort(key=lambda item: item["product_name"].lower())
    return {
        "rows": rows,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "query": search or "",
        "summary": {
            "product_count": len(rows),
            "stock_value": _money(total_value),
        },
    }


def get_ufc_purchase_overview(actor_user_id, centre_uid_hint, *, search=""):
    _ensure_indexes()
    _actor, centre_uid, centre_name = _get_ufc_actor(actor_user_id, centre_uid_hint)
    query = {"centre_uid": centre_uid}
    text = _clean_text(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"purchase_number": {"$regex": escaped, "$options": "i"}},
            {"avpl_order_number": {"$regex": escaped, "$options": "i"}},
            {"avpl_sales_invoice_number": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"items.product_name": {"$regex": escaped, "$options": "i"}},
        ]
    rows = []
    total_value = Decimal("0")
    for item in mongo.db[UFC_PURCHASE_COLLECTION].find(query).sort("purchase_date", DESCENDING):
        total_value += _decimal(item.get("total_amount"))
        serialized_items = []
        for source in item.get("items") or []:
            if not isinstance(source, dict):
                continue
            line = dict(source)
            line["quantity_display"] = _qty(line.get("quantity") if line.get("quantity") is not None else line.get("received_quantity"))
            line["unit_price_display"] = _money(line.get("unit_price"))
            line["taxable_value_display"] = _money(line.get("taxable_value"))
            line["gst_amount_display"] = _money(line.get("gst_amount"))
            line["line_total_display"] = _money(line.get("line_total") if line.get("line_total") is not None else line.get("grand_total"))
            serialized_items.append(line)
        item_count = len(serialized_items) or int(item.get("item_count") or 1)
        rows.append({
            **item,
            "id": str(item.get("_id") or ""),
            "avpl_sales_invoice_id_str": str(item.get("avpl_sales_invoice_id") or ""),
            "ufc_payable_id_str": str(item.get("ufc_payable_id") or ""),
            "quantity_display": item.get("quantity_display") or _qty(item.get("quantity")),
            "unit_price_display": item.get("unit_price_display") or _money(item.get("unit_price")),
            "taxable_value_display": _money(item.get("taxable_value")),
            "gst_amount_display": _money(item.get("gst_amount")),
            "total_amount_display": item.get("total_amount_display") or _money(item.get("total_amount")),
            "amount_paid_display": _money(item.get("amount_paid")),
            "outstanding_amount_display": _money(item.get("outstanding_amount")),
            "items": serialized_items,
            "item_count": item_count,
            "is_multi_item_order": item_count > 1,
            "product_summary": (serialized_items[0].get("product_name") if len(serialized_items) == 1 else (f"{item_count} products" if item_count > 1 else item.get("product_name") or "Product")),
        })
    return {
        "rows": rows,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "query": search or "",
        "summary": {"count": len(rows), "total_value": _money(total_value)},
    }
