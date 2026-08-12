from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.extensions import mongo
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
    year = date.today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_ufc_order:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"UFC-AVPL-{year}-{sequence:05d}"


def _next_purchase_number(centre_uid):
    year = date.today().year
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
        return datetime.strptime(expiry, "%Y-%m-%d").date() < date.today()
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


def _serialize_order(order):
    if not order:
        return None
    row = dict(order)
    row["id"] = str(row.get("_id") or "")
    row["product_id_str"] = str(row.get("source_product_id") or "")
    row["avpl_sale_id_str"] = str(row.get("avpl_sale_id") or "")
    row["avpl_sales_invoice_id_str"] = str(row.get("avpl_sales_invoice_id") or "")
    row["invoice_grand_total_display"] = _money(row.get("invoice_grand_total"))
    row["status_label"] = ORDER_STATUS_LABELS.get(
        str(row.get("status") or "requested"),
        str(row.get("status") or "requested").replace("_", " ").title(),
    )
    row["requested_quantity_display"] = _qty(row.get("requested_quantity"))
    row["approved_quantity_display"] = _qty(row.get("approved_quantity"))
    row["dispatched_quantity_display"] = _qty(row.get("dispatched_quantity"))
    row["received_quantity_display"] = _qty(row.get("received_quantity"))
    row["unit_price_display"] = _money(row.get("unit_price"))
    row["total_amount_display"] = _money(row.get("total_amount"))
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


def create_ufc_order_request(actor_user_id, centre_uid_hint, product_id, quantity, note=""):
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


def _reserve_avpl_stock(order, approved_quantity, actor):
    needed = _decimal(approved_quantity)
    allocations = []
    reserved_updates = []
    today_iso = date.today().strftime("%Y-%m-%d")

    for lot in _candidate_avpl_lots(order["accounting_entity_id"], order["source_product_id"]):
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
                {
                    "$or": [
                        {"expiry_date": {"$exists": False}},
                        {"expiry_date": None},
                        {"expiry_date": ""},
                        {"expiry_date": {"$gte": today_iso}},
                    ]
                },
                {
                    "$expr": {
                        "$gte": [
                            {
                                "$subtract": [
                                    {"$ifNull": ["$available_quantity", 0]},
                                    {
                                        "$add": [
                                            {"$ifNull": ["$reserved_quantity", 0]},
                                            {"$ifNull": ["$damaged_quantity", 0]},
                                            {"$ifNull": ["$blocked_quantity", 0]},
                                        ]
                                    },
                                ]
                            },
                            take_float,
                        ]
                    }
                },
            ],
        }
        result = mongo.db[AVPL_LOT_COLLECTION].update_one(
            query,
            {
                "$inc": {"reserved_quantity": take_float},
                "$set": {"updated_at": now_utc(), "last_reservation_order_id": order["_id"]},
            },
        )
        if result.modified_count != 1:
            # Concurrent order/adjustment changed this lot. Skip it and continue.
            continue

        reserved_updates.append((lot["_id"], take_float))
        allocations.append({
            "inventory_lot_id": lot["_id"],
            "inventory_lot_id_str": str(lot["_id"]),
            "quantity": take_float,
            "quantity_display": _qty(take),
            "warehouse_code": lot.get("warehouse_code") or "AVPL-MAIN",
            "warehouse_name": lot.get("warehouse_name") or "AVPL Main Warehouse",
            "warehouse_bin": lot.get("warehouse_bin") or "",
            "batch_number": lot.get("batch_number") or "",
            "barcode": lot.get("barcode") or "",
            "manufacturing_date": lot.get("manufacturing_date") or "",
            "expiry_date": lot.get("expiry_date") or "",
            "unit_code": lot.get("unit_code") or order.get("unit_code") or "Unit",
        })
        needed -= take

    if needed > 0:
        for lot_id, quantity_value in reserved_updates:
            mongo.db[AVPL_LOT_COLLECTION].update_one(
                {"_id": lot_id, "reserved_quantity": {"$gte": quantity_value}},
                {"$inc": {"reserved_quantity": -quantity_value}, "$set": {"updated_at": now_utc()}},
            )
        raise RuntimeError(
            "AVPL stock changed while this order was being approved. Refresh the order and try again."
        )

    timestamp = now_utc()
    for allocation in allocations:
        source_key = f"UFC-RESERVE:{order['_id']}:{allocation['inventory_lot_id']}"
        mongo.db[AVPL_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {
                "$setOnInsert": {
                    "source_posting_key": source_key,
                    "movement_uid": uuid4().hex,
                    "accounting_entity_id": order["accounting_entity_id"],
                    "accounting_entity_id_str": str(order["accounting_entity_id"]),
                    "source_document_type": "ufc_order",
                    "source_document_id": order["_id"],
                    "source_document_id_str": str(order["_id"]),
                    "source_document_number": order.get("order_number") or "",
                    "source_product_id": order["source_product_id"],
                    "source_product_id_str": str(order["source_product_id"]),
                    "product_code": order.get("product_code") or "",
                    "product_name": order.get("product_name") or "Product",
                    "movement_type": "reservation",
                    "direction": "reserve",
                    "quantity": allocation["quantity"],
                    "quantity_display": allocation["quantity_display"],
                    "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                    "warehouse_code": allocation.get("warehouse_code") or "",
                    "warehouse_name": allocation.get("warehouse_name") or "",
                    "warehouse_bin": allocation.get("warehouse_bin") or "",
                    "barcode": allocation.get("barcode") or "",
                    "batch_number": allocation.get("batch_number") or "",
                    "manufacturing_date": allocation.get("manufacturing_date") or "",
                    "expiry_date": allocation.get("expiry_date") or "",
                    "movement_date": date.today().strftime("%Y-%m-%d"),
                    "reason": f"Reserved for UFC order {order.get('order_number') or ''} ({order.get('centre_uid') or ''}).",
                    "posted_by": actor["_id"],
                    "posted_by_name": actor.get("resolved_name") or "",
                    "posted_at": timestamp,
                    "created_at": timestamp,
                }
            },
            upsert=True,
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
                "movement_date": date.today().strftime("%Y-%m-%d"),
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
    today_iso = date.today().strftime("%Y-%m-%d")
    try:
        for allocation in allocations:
            lot_id = _to_object_id(allocation.get("inventory_lot_id"))
            quantity_value = float(_decimal(allocation.get("quantity")))
            if not lot_id or quantity_value <= 0:
                raise RuntimeError("The reservation contains an invalid stock lot.")

            result = mongo.db[AVPL_LOT_COLLECTION].update_one(
                {
                    "_id": lot_id,
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
                {
                    "$inc": {
                        "available_quantity": -quantity_value,
                        "reserved_quantity": -quantity_value,
                        "issued_quantity": quantity_value,
                    },
                    "$set": {"updated_at": now_utc(), "last_dispatch_order_id": oid},
                },
            )
            if result.modified_count != 1:
                raise RuntimeError(
                    "A reserved batch is no longer dispatchable (stock changed or expired). Cancel/release this order and approve it again."
                )
            moved.append((lot_id, quantity_value))
    except Exception:
        for lot_id, quantity_value in reversed(moved):
            mongo.db[AVPL_LOT_COLLECTION].update_one(
                {"_id": lot_id},
                {"$inc": {
                    "available_quantity": quantity_value,
                    "reserved_quantity": quantity_value,
                    "issued_quantity": -quantity_value,
                }, "$set": {"updated_at": now_utc()}},
            )
        raise

    timestamp = now_utc()
    for allocation in allocations:
        source_key = f"UFC-DISPATCH:{oid}:{allocation.get('inventory_lot_id')}"
        mongo.db[AVPL_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {"$setOnInsert": {
                "source_posting_key": source_key,
                "movement_uid": uuid4().hex,
                "accounting_entity_id": order.get("accounting_entity_id"),
                "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
                "source_document_type": "ufc_order_dispatch",
                "source_document_id": oid,
                "source_document_id_str": str(oid),
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
                "warehouse_bin": allocation.get("warehouse_bin") or "",
                "barcode": allocation.get("barcode") or "",
                "batch_number": allocation.get("batch_number") or "",
                "manufacturing_date": allocation.get("manufacturing_date") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": date.today().strftime("%Y-%m-%d"),
                "reason": f"Dispatched to UFC {order.get('centre_uid') or ''} against order {order.get('order_number') or ''}.",
                "posted_by": actor["_id"],
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }},
            upsert=True,
        )

    dispatch_quantity = sum(_decimal(a.get("quantity")) for a in allocations)
    mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "status": "approved", "stock_dispatched": {"$ne": True}},
        {"$set": {
            "status": "dispatched",
            "stock_reserved": False,
            "stock_dispatched": True,
            "reserved_quantity": 0.0,
            "dispatched_quantity": float(dispatch_quantity),
            "dispatch_allocations": allocations,
            "dispatch_note": _clean_text(dispatch_note, 1000),
            "transporter_name": _clean_text(transporter, 120),
            "vehicle_number": _clean_text(vehicle_number, 30).upper(),
            "dispatched_by": actor["_id"],
            "dispatched_by_name": actor.get("resolved_name") or "",
            "dispatched_at": timestamp,
            "updated_at": timestamp,
        }},
    )
    _sync_legacy_product_quantity(order["accounting_entity_id"], order["source_product_id"])
    _append_history(
        oid,
        action="dispatch_order",
        actor=actor,
        note=dispatch_note or "AVPL physically dispatched the reserved goods to UFC.",
        from_status="approved",
        to_status="dispatched",
    )
    _notify_user(
        order.get("requested_by"),
        "AVPL Order Dispatched",
        f"Order {order.get('order_number')} has been dispatched. Confirm receipt after the goods physically arrive at your UFC.",
        "ufc_admin",
    )

    # Stage 5 financial documents are deliberately decoupled from physical stock.
    # Dispatch must remain valid even if invoice numbering/accounting configuration
    # temporarily needs repair. The financial sync is idempotent and can be retried.
    financial_result = None
    financial_warning = None
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

    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "financial": financial_result,
        "financial_warning": financial_warning,
        "message": "Order dispatched. AVPL physical stock has been reduced.",
    }


def _ufc_lot_key(centre_uid, product_id, allocation):
    batch = allocation.get("batch_number") or "NO-BATCH"
    expiry = allocation.get("expiry_date") or "NO-EXPIRY"
    return ":".join([
        str(centre_uid),
        str(product_id),
        str(batch),
        str(expiry),
    ])


def _apply_ufc_inventory(order, actor):
    allocations = order.get("dispatch_allocations") or order.get("reservation_allocations") or []
    timestamp = now_utc()
    for index, allocation in enumerate(allocations, start=1):
        quantity_value = float(_decimal(allocation.get("quantity")))
        if quantity_value <= 0:
            continue
        receipt_key = f"AVPL-ORDER-RECEIPT:{order['_id']}:{allocation.get('inventory_lot_id') or index}"
        lot_key = _ufc_lot_key(order.get("centre_uid"), order.get("source_product_id"), allocation)
        existing = mongo.db[UFC_LOT_COLLECTION].find_one({"lot_key": lot_key})
        if existing:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"_id": existing["_id"], "applied_receipt_keys": {"$ne": receipt_key}},
                {
                    "$inc": {
                        "received_quantity": quantity_value,
                        "available_quantity": quantity_value,
                        "purchase_cost_total": quantity_value * float(_decimal(order.get("unit_price"))),
                    },
                    "$addToSet": {
                        "applied_receipt_keys": receipt_key,
                        "source_order_ids": order["_id"],
                        "source_order_numbers": order.get("order_number") or "",
                    },
                    "$set": {
                        "last_purchase_price": float(_decimal(order.get("unit_price"))),
                        "last_receipt_at": timestamp,
                        "updated_at": timestamp,
                    },
                },
            )
        else:
            mongo.db[UFC_LOT_COLLECTION].update_one(
                {"lot_key": lot_key},
                {"$setOnInsert": {
                    "lot_key": lot_key,
                    "centre_uid": order.get("centre_uid"),
                    "centre_name": order.get("centre_name") or order.get("centre_uid"),
                    "source_product_id": order.get("source_product_id"),
                    "source_product_id_str": str(order.get("source_product_id") or ""),
                    "product_code": order.get("product_code") or "",
                    "product_name": order.get("product_name") or "Product",
                    "category": order.get("category") or "",
                    "product_role": order.get("product_role") or "",
                    "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                    "warehouse_code": f"{str(order.get('centre_uid') or 'UFC').upper()}-MAIN",
                    "warehouse_name": f"{order.get('centre_name') or order.get('centre_uid') or 'UFC'} Main Stock",
                    "source_avpl_warehouse_code": allocation.get("warehouse_code") or "",
                    "barcode": allocation.get("barcode") or "",
                    "batch_number": allocation.get("batch_number") or "",
                    "manufacturing_date": allocation.get("manufacturing_date") or "",
                    "expiry_date": allocation.get("expiry_date") or "",
                    "received_quantity": quantity_value,
                    "available_quantity": quantity_value,
                    "reserved_quantity": 0.0,
                    "damaged_quantity": 0.0,
                    "blocked_quantity": 0.0,
                    "issued_quantity": 0.0,
                    "purchase_cost_total": quantity_value * float(_decimal(order.get("unit_price"))),
                    "last_purchase_price": float(_decimal(order.get("unit_price"))),
                    "status": "available",
                    "applied_receipt_keys": [receipt_key],
                    "source_order_ids": [order["_id"]],
                    "source_order_numbers": [order.get("order_number") or ""],
                    "created_by": actor["_id"],
                    "created_at": timestamp,
                    "last_receipt_at": timestamp,
                    "updated_at": timestamp,
                }},
                upsert=True,
            )

        movement_key = f"UFC-RECEIPT:{order['_id']}:{allocation.get('inventory_lot_id') or index}"
        mongo.db[UFC_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": movement_key},
            {"$setOnInsert": {
                "source_posting_key": movement_key,
                "movement_uid": uuid4().hex,
                "centre_uid": order.get("centre_uid"),
                "centre_name": order.get("centre_name") or order.get("centre_uid"),
                "source_document_type": "avpl_order_receipt",
                "source_document_id": order["_id"],
                "source_document_id_str": str(order["_id"]),
                "source_document_number": order.get("order_number") or "",
                "source_product_id": order.get("source_product_id"),
                "source_product_id_str": str(order.get("source_product_id") or ""),
                "product_code": order.get("product_code") or "",
                "product_name": order.get("product_name") or "Product",
                "movement_type": "purchase_receipt",
                "direction": "in",
                "quantity": quantity_value,
                "quantity_display": _qty(quantity_value),
                "unit_code": allocation.get("unit_code") or order.get("unit_code") or "Unit",
                "warehouse_code": f"{str(order.get('centre_uid') or 'UFC').upper()}-MAIN",
                "batch_number": allocation.get("batch_number") or "",
                "manufacturing_date": allocation.get("manufacturing_date") or "",
                "expiry_date": allocation.get("expiry_date") or "",
                "movement_date": date.today().strftime("%Y-%m-%d"),
                "reason": f"Received from AVPL against order {order.get('order_number') or ''}.",
                "posted_by": actor["_id"],
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }},
            upsert=True,
        )


def _create_ufc_purchase_entry(order, actor):
    existing = mongo.db[UFC_PURCHASE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    if existing:
        return existing
    timestamp = now_utc()
    quantity_value = _decimal(order.get("dispatched_quantity") or order.get("approved_quantity"))
    price = _decimal(order.get("unit_price"))
    total = quantity_value * price
    invoice = None
    try:
        from app.services.avpl_ufc_sales_service import INVOICE_COLLECTION
        invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
        if invoice:
            total = _decimal(invoice.get("grand_total"), str(total))
    except Exception:
        invoice = None
    document = {
        "purchase_number": _next_purchase_number(order.get("centre_uid")),
        "centre_uid": order.get("centre_uid"),
        "centre_name": order.get("centre_name") or order.get("centre_uid"),
        "seller_type": "avpl",
        "seller_name": "AVPL",
        "avpl_ufc_order_id": order["_id"],
        "avpl_ufc_order_id_str": str(order["_id"]),
        "avpl_order_number": order.get("order_number") or "",
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "category": order.get("category") or "",
        "product_role": order.get("product_role") or "",
        "quantity": float(quantity_value),
        "quantity_display": _qty(quantity_value),
        "unit_code": order.get("unit_code") or "Unit",
        "unit_price": float(price),
        "unit_price_display": _money(price),
        "total_amount": float(total),
        "total_amount_display": _money(total),
        "avpl_sales_invoice_id": (invoice or {}).get("_id"),
        "avpl_sales_invoice_id_str": str((invoice or {}).get("_id") or ""),
        "avpl_sales_invoice_number": (invoice or {}).get("invoice_number") or "",
        "invoice_date": (invoice or {}).get("invoice_date"),
        "due_date": (invoice or {}).get("due_date"),
        "hsn_code": (invoice or {}).get("hsn_code") or "",
        "taxability_code": (invoice or {}).get("taxability_code") or "",
        "taxable_value": float(_decimal((invoice or {}).get("taxable_value"))),
        "gst_rate": float(_decimal((invoice or {}).get("gst_rate"))),
        "cgst_amount": float(_decimal((invoice or {}).get("cgst_amount"))),
        "sgst_amount": float(_decimal((invoice or {}).get("sgst_amount"))),
        "igst_amount": float(_decimal((invoice or {}).get("igst_amount"))),
        "gst_amount": float(_decimal((invoice or {}).get("gst_amount"))),
        "purchase_date": timestamp,
        "status": "received",
        "accounting_status": "not_posted",
        "payment_status": "unpaid" if invoice else "not_recorded",
        "amount_paid": 0.0,
        "outstanding_amount": float(total),
        "financial_link_status": "linked" if invoice else "awaiting_avpl_invoice",
        "received_by": actor["_id"],
        "received_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    result = mongo.db[UFC_PURCHASE_COLLECTION].insert_one(document)
    document["_id"] = result.inserted_id
    return document


def receive_ufc_order(actor_user_id, centre_uid_hint, order_id, receipt_note=""):
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
        return {
            "order": _serialize_order(order),
            "purchase": purchase,
            "message": "This order was already received and added to UFC stock.",
        }
    if order.get("status") != "dispatched" or order.get("stock_dispatched") is not True:
        raise ValueError("Only a physically dispatched AVPL order can be received.")

    # Both operations are idempotent. If a local server stops midway, pressing
    # Confirm Receipt again safely finishes the missing pieces without double stock.
    _apply_ufc_inventory(order, actor)
    purchase = _create_ufc_purchase_entry(order, actor)
    try:
        from app.services.avpl_ufc_sales_service import link_ufc_purchase_financials
        purchase = link_ufc_purchase_financials(oid) or purchase
    except Exception:
        # Receipt/stock is the physical source of truth and must not be rolled back
        # by a recoverable financial-linking problem.
        pass
    received_qty = _decimal(order.get("dispatched_quantity") or order.get("approved_quantity"))
    timestamp = now_utc()
    mongo.db[ORDER_COLLECTION].update_one(
        {"_id": oid, "centre_uid": centre_uid, "status": "dispatched"},
        {"$set": {
            "status": "received",
            "received_quantity": float(received_qty),
            "ufc_stock_posted": True,
            "purchase_entry_created": True,
            "ufc_purchase_entry_id": purchase.get("_id"),
            "ufc_purchase_number": purchase.get("purchase_number") or "",
            "receipt_note": _clean_text(receipt_note, 1000),
            "received_by": actor["_id"],
            "received_by_name": actor.get("resolved_name") or centre_name,
            "received_at": timestamp,
            "updated_at": timestamp,
        }},
    )
    try:
        from app.services.avpl_ufc_sales_service import link_ufc_purchase_financials
        purchase = link_ufc_purchase_financials(oid) or purchase
    except Exception:
        pass

    _append_history(
        oid,
        action="receive_order",
        actor=actor,
        note=receipt_note or "UFC confirmed physical receipt; purchase entry and UFC stock were created.",
        from_status="dispatched",
        to_status="received",
    )
    _notify_avpl_admins(
        "UFC Confirmed Receipt",
        f"{centre_name} ({centre_uid}) received order {order.get('order_number')} - {_qty(received_qty)} {order.get('unit_code') or 'units'} of {order.get('product_name')}.",
    )
    return {
        "order": _serialize_order(mongo.db[ORDER_COLLECTION].find_one({"_id": oid})),
        "purchase": purchase,
        "message": "Goods received. UFC purchase entry and UFC stock were created successfully.",
    }


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
        {"product_code": {"$regex": escaped, "$options": "i"}},
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
        ]
    rows = []
    total_value = Decimal("0")
    for item in mongo.db[UFC_PURCHASE_COLLECTION].find(query).sort("purchase_date", DESCENDING):
        total_value += _decimal(item.get("total_amount"))
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
        })
    return {
        "rows": rows,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "query": search or "",
        "summary": {"count": len(rows), "total_value": _money(total_value)},
    }
