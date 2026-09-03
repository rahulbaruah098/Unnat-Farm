from __future__ import annotations
from app.utils.timezone import business_today

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.utils.helpers import now_utc
from app.services.commerce_receipt_service import normalize_receipt_lines, summarize_receipt, receipt_label, receipt_issue_summary


LISTING_COLLECTION = "farmer_produce_marketplace_listings"
ORDER_COLLECTION = "farmer_produce_marketplace_orders"
SALE_COLLECTION = "farmer_marketplace_sales"
INVOICE_COLLECTION = "farmer_marketplace_sales_invoices"
RECEIVABLE_COLLECTION = "farmer_marketplace_receivables"
PURCHASE_COLLECTION = "farmer_marketplace_purchase_entries"
PAYABLE_COLLECTION = "farmer_marketplace_payables"
BUYER_STOCK_COLLECTION = "farmer_marketplace_buyer_stock_lots"
AUDIT_COLLECTION = "farmer_marketplace_audit"
NOTIFICATION_COLLECTION = "notifications"

FARMER_LOT_COLLECTION = "farmer_produce_lots"
FARMER_MOVEMENT_COLLECTION = "farmer_produce_movements"

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.001")
EPSILON = Decimal("0.0004")

PAYMENT_TERMS = {
    "pay_on_receipt": "Pay on Receipt",
    "credit": "Credit / Pay Later",
}
ORDER_STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "dispatched": "Dispatched",
    "received": "Received",
}
BUYER_ROLE_LABELS = {
    "farmer": "Farmer",
    "ufc_admin": "UFC Centre",
    "avpl_admin": "AVPL",
    "super_admin": "AVPL",
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
    number = _decimal(value).quantize(QTY_QUANTUM, rounding=ROUND_HALF_UP)
    text = f"{number:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _clean(value, maximum=500):
    return " ".join(str(value or "").split())[:maximum]


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _date_iso(value, fallback=None):
    fallback = fallback or business_today()
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()
    if raw:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
        except Exception:
            pass
    return fallback.isoformat()


def _next_number(counter_key, prefix, digits=6):
    year = business_today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"{counter_key}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"{prefix}-{year}-{sequence:0{digits}d}"


def _ensure_indexes():
    # Buyer stock used to allow exactly one lot per order. Multi-item orders need
    # one stock lot per product line. Migrate the old index once; legacy rows are
    # still readable and remain grouped by farmer_marketplace_order_id.
    try:
        info = mongo.db[BUYER_STOCK_COLLECTION].index_information()
        if "farmer_market_buyer_stock_order_unique" in info:
            mongo.db[BUYER_STOCK_COLLECTION].drop_index("farmer_market_buyer_stock_order_unique")
    except Exception:
        pass
    definitions = [
        (LISTING_COLLECTION, [("listing_number", ASCENDING)], {"unique": True, "name": "farmer_market_listing_number_unique"}),
        (LISTING_COLLECTION, [("farmer_user_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)], {"name": "farmer_market_listing_owner_idx"}),
        (LISTING_COLLECTION, [("centre_uid", ASCENDING), ("status", ASCENDING), ("product_name", ASCENDING)], {"name": "farmer_market_listing_centre_idx"}),
        (ORDER_COLLECTION, [("order_number", ASCENDING)], {"unique": True, "name": "farmer_market_order_number_unique"}),
        (ORDER_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "farmer_market_order_idempotency_unique"}),
        (ORDER_COLLECTION, [("seller_farmer_user_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)], {"name": "farmer_market_order_seller_idx"}),
        (ORDER_COLLECTION, [("buyer_key", ASCENDING), ("created_at", DESCENDING)], {"name": "farmer_market_order_buyer_idx"}),
        (SALE_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"unique": True, "name": "farmer_market_sale_order_unique"}),
        (SALE_COLLECTION, [("sale_number", ASCENDING)], {"unique": True, "name": "farmer_market_sale_number_unique"}),
        (INVOICE_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"unique": True, "name": "farmer_market_invoice_order_unique"}),
        (INVOICE_COLLECTION, [("document_number", ASCENDING)], {"unique": True, "name": "farmer_market_invoice_number_unique"}),
        (RECEIVABLE_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"unique": True, "name": "farmer_market_receivable_order_unique"}),
        (PURCHASE_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"unique": True, "name": "farmer_market_purchase_order_unique"}),
        (PAYABLE_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"unique": True, "name": "farmer_market_payable_order_unique"}),
        (BUYER_STOCK_COLLECTION, [("farmer_marketplace_order_id", ASCENDING)], {"name": "farmer_market_buyer_stock_order_idx"}),
        (BUYER_STOCK_COLLECTION, [("stock_key", ASCENDING)], {"unique": True, "name": "farmer_market_buyer_stock_key_unique", "partialFilterExpression": {"stock_key": {"$exists": True, "$type": "string"}}}),
        (AUDIT_COLLECTION, [("entity_type", ASCENDING), ("entity_id", ASCENDING), ("created_at", DESCENDING)], {"name": "farmer_market_audit_entity_idx"}),
    ]
    for collection_name, keys, options in definitions:
        try: mongo.db[collection_name].create_index(keys, **options)
        except Exception: pass


def _get_user(actor_user_id):
    oid = _to_object_id(actor_user_id)
    if not oid:
        raise ValueError("Please login again.")
    user = mongo.db.users.find_one({"_id": oid}) or {}
    if not user:
        raise ValueError("Your account was not found.")
    if user.get("active", True) is False or user.get("is_active", True) is False or str(user.get("status") or "").lower() == "inactive":
        raise PermissionError("Inactive accounts cannot use the Farmer Produce Market.")
    role = str(user.get("role") or "").strip().lower()
    user["resolved_role"] = role
    user["resolved_name"] = user.get("name") or user.get("full_name") or user.get("username") or user.get("phone") or role.replace("_", " ").title()
    return user


def _get_farmer(actor_user_id):
    user = _get_user(actor_user_id)
    if user.get("resolved_role") != "farmer":
        raise PermissionError("Only Farmers can create produce listings.")
    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(user["_id"])})
        or mongo.db.farmer_master.find_one({"linked_user_id": user["_id"]})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )
    if not farmer:
        raise ValueError("Complete the Farmer profile before selling produce.")
    return {
        "user": user,
        "user_id": user["_id"],
        "user_id_str": str(user["_id"]),
        "farmer_master_id": farmer.get("_id"),
        "master": farmer,
        "name": farmer.get("name") or user.get("name") or "Farmer",
        "phone": farmer.get("contact_no") or user.get("phone") or "",
        "centre_uid": farmer.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid") or "",
        "mitra_uid": farmer.get("mitra_uid") or user.get("mapped_mitra_uid") or user.get("mitra_uid") or "",
        "state": farmer.get("state") or user.get("state") or "",
        "district": farmer.get("district") or user.get("district") or "",
        "block": farmer.get("block") or user.get("block") or "",
        "village": farmer.get("village") or user.get("village") or "",
    }


def _resolve_ufc_uid(user):
    uid = _clean(user.get("centre_uid") or user.get("mapped_centre_uid"), 80)
    if uid:
        return uid
    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(user.get("_id"))})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": user.get("_id")})
        or {}
    )
    return _clean(master.get("centre_uid") or master.get("mapped_centre_uid"), 80)


def _buyer_snapshot(user):
    role = user.get("resolved_role") or ""
    if role == "farmer":
        farmer = (
            mongo.db.farmer_master.find_one({"linked_user_id": str(user.get("_id"))})
            or mongo.db.farmer_master.find_one({"linked_user_id": user.get("_id")})
            or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
            or {}
        )
        return {
            "role": "farmer",
            "type": "farmer",
            "key": str(user.get("_id")),
            "user_id": user.get("_id"),
            "user_id_str": str(user.get("_id")),
            "name": farmer.get("name") or user.get("resolved_name") or "Farmer",
            "phone": farmer.get("contact_no") or user.get("phone") or "",
            "centre_uid": farmer.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid") or "",
            "state": farmer.get("state") or user.get("state") or "",
            "district": farmer.get("district") or user.get("district") or "",
            "village": farmer.get("village") or user.get("village") or "",
        }
    if role == "ufc_admin":
        centre_uid = _resolve_ufc_uid(user)
        if not centre_uid:
            raise ValueError("This UFC Admin is not linked to a Centre UID.")
        centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or {}
        return {
            "role": "ufc_admin",
            "type": "ufc",
            "key": centre_uid,
            "user_id": user.get("_id"),
            "user_id_str": str(user.get("_id")),
            "name": centre.get("business_name") or centre.get("centre_name") or centre.get("trade_name") or centre_uid,
            "phone": centre.get("contact_number") or centre.get("phone") or user.get("phone") or "",
            "centre_uid": centre_uid,
            "state": centre.get("state") or user.get("state") or "",
            "district": centre.get("district") or "",
            "village": centre.get("village") or "",
        }
    if role in {"avpl_admin", "super_admin"}:
        entity = mongo.db.accounting_entities.find_one({"entity_code": "AVPL", "entity_type": "avpl", "status": "active"}) or {}
        return {
            "role": role,
            "type": "avpl",
            "key": "AVPL",
            "user_id": user.get("_id"),
            "user_id_str": str(user.get("_id")),
            "name": entity.get("legal_name") or entity.get("trade_name") or "AVPL",
            "phone": entity.get("phone") or user.get("phone") or "",
            "centre_uid": "",
            "state": entity.get("state_name") or entity.get("state") or "",
            "district": entity.get("district") or "",
            "village": "",
        }
    raise PermissionError("This account cannot place Farmer Produce Market orders.")


def _audit(actor, action, entity_type, entity_id, note=""):
    mongo.db[AUDIT_COLLECTION].insert_one({
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_id_str": str(entity_id or ""),
        "actor_user_id": actor.get("_id") if isinstance(actor, dict) else None,
        "actor_name": actor.get("resolved_name") if isinstance(actor, dict) else "",
        "actor_role": actor.get("resolved_role") if isinstance(actor, dict) else "",
        "note": _clean(note, 1000),
        "created_at": now_utc(),
    })


def _notify(to_user_id, role, title, message):
    if not to_user_id:
        return
    mongo.db[NOTIFICATION_COLLECTION].insert_one({
        "to_user_id": str(to_user_id),
        "role": role,
        "title": _clean(title, 120),
        "message": _clean(message, 500),
        "status": "unread",
        "created_at": now_utc(),
    })


def _farmer_stock_snapshot(farmer_user_id, product_key):
    oid = _to_object_id(farmer_user_id)
    query_owner = oid if oid else farmer_user_id
    lots = list(mongo.db[FARMER_LOT_COLLECTION].find({
        "farmer_user_id": query_owner,
        "product_key": product_key,
        "status": "active",
    }).sort([("harvest_date", ASCENDING), ("created_at", ASCENDING), ("_id", ASCENDING)]))
    physical = Decimal("0")
    reserved = Decimal("0")
    sample = {}
    for lot in lots:
        sample = sample or lot
        physical += max(_decimal(lot.get("available_quantity")), Decimal("0"))
        reserved += max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
    saleable = max(physical - reserved, Decimal("0"))
    return {
        "lots": lots,
        "sample": sample,
        "physical": physical,
        "reserved": reserved,
        "saleable": saleable,
    }


def _listing_live_values(listing):
    stock = _farmer_stock_snapshot(listing.get("farmer_user_id"), listing.get("product_key"))
    listed = max(_decimal(listing.get("listed_quantity")), Decimal("0"))
    fulfilled = max(_decimal(listing.get("fulfilled_quantity")), Decimal("0"))
    reserved_listing = max(_decimal(listing.get("reserved_quantity")), Decimal("0"))
    listing_remaining = max(listed - fulfilled - reserved_listing, Decimal("0"))
    available_to_order = min(stock["saleable"], listing_remaining)
    return stock, listed, fulfilled, reserved_listing, max(available_to_order, Decimal("0"))


def _serialize_package_options(options):
    result = []
    for idx, item in enumerate(options or []):
        size = _decimal(item.get("quantity_per_bag"))
        price = _decimal(item.get("price_per_bag"))
        if size <= 0 or price <= 0:
            continue
        result.append({
            "index": idx,
            "label": _clean(item.get("label") or f"{_qty(size)} unit bag", 80),
            "quantity_per_bag": float(size),
            "quantity_per_bag_display": _qty(size),
            "price_per_bag": float(price),
            "price_per_bag_display": _money(price),
        })
    return result


def serialize_listing(listing, viewer_user_id=None):
    if not listing:
        return None
    stock, listed, fulfilled, reserved_listing, available = _listing_live_values(listing)
    listing_remaining = max(listed - fulfilled - reserved_listing, Decimal("0"))
    row = dict(listing)
    row["id"] = str(row.get("_id") or "")
    row["farmer_user_id_str"] = str(row.get("farmer_user_id") or row.get("farmer_user_id_str") or "")
    row["listed_quantity_display"] = _qty(listed)
    row["fulfilled_quantity_display"] = _qty(fulfilled)
    row["reserved_quantity_display"] = _qty(reserved_listing)
    row["listing_remaining"] = float(listing_remaining)
    row["listing_remaining_display"] = _qty(listing_remaining)
    row["physical_stock_display"] = _qty(stock["physical"])
    row["stock_reserved_display"] = _qty(stock["reserved"])
    row["farm_stock_available"] = float(stock["saleable"])
    row["farm_stock_available_display"] = _qty(stock["saleable"])
    row["available_to_order"] = float(available)
    row["available_to_order_display"] = _qty(available)
    # For the edit form, the farmer changes only the quantity that should remain
    # open for NEW orders. Historical sold/reserved quantities remain untouched.
    row["edit_available_quantity"] = float(available)
    row["edit_available_quantity_display"] = _qty(available)
    row["loose_price_display"] = _money(row.get("loose_price")) if row.get("loose_price") not in (None, "") else ""
    row["min_order_quantity_display"] = _qty(row.get("min_order_quantity"))
    row["package_options"] = _serialize_package_options(row.get("package_options"))
    for package in row["package_options"]:
        size = _decimal(package.get("quantity_per_bag"))
        package["available_bags"] = int(available // size) if size > 0 else 0
    row["status_label"] = str(row.get("status") or "draft").replace("_", " ").title()
    row["is_available"] = row.get("status") == "published" and available > EPSILON
    row["is_owner"] = bool(viewer_user_id and row["farmer_user_id_str"] == str(viewer_user_id))
    return row


def get_listing_form_context(actor_user_id, listing_id=None):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    stock_rows = []
    pipeline = {}
    for lot in mongo.db[FARMER_LOT_COLLECTION].find({"farmer_user_id": profile["user_id"], "status": "active"}).sort([("product_name", ASCENDING), ("harvest_date", ASCENDING)]):
        key = lot.get("product_key") or ""
        if not key:
            continue
        row = pipeline.setdefault(key, {
            "product_key": key,
            "product_name": lot.get("product_name") or "Produce",
            "unit_code": lot.get("unit_code") or "KG",
            "physical": Decimal("0"),
            "reserved": Decimal("0"),
            "varieties": set(),
            "grades": set(),
        })
        row["physical"] += max(_decimal(lot.get("available_quantity")), Decimal("0"))
        row["reserved"] += max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
        if lot.get("variety"):
            row["varieties"].add(str(lot.get("variety")))
        if lot.get("grade"):
            row["grades"].add(str(lot.get("grade")))
    for row in pipeline.values():
        row["saleable"] = max(row["physical"] - row["reserved"], Decimal("0"))
        row["saleable_quantity"] = float(row["saleable"])
        row["saleable_display"] = _qty(row["saleable"])
        row["physical_display"] = _qty(row["physical"])
        row["reserved_display"] = _qty(row["reserved"])
        row["variety_text"] = ", ".join(sorted(row["varieties"]))
        row["grade_text"] = ", ".join(sorted(row["grades"]))
        for field in ["physical", "reserved", "saleable", "varieties", "grades"]:
            row.pop(field, None)
        if _decimal(row["saleable_display"]) > 0:
            stock_rows.append(row)
    stock_rows.sort(key=lambda x: str(x.get("product_name") or "").lower())

    listing = None
    if listing_id:
        oid = _to_object_id(listing_id)
        if not oid:
            raise ValueError("Invalid listing reference.")
        raw = mongo.db[LISTING_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]})
        if not raw:
            raise ValueError("Listing was not found.")
        listing = serialize_listing(raw, profile["user_id"])
    return {
        "farmer": profile,
        "stock_rows": stock_rows,
        "listing": listing,
        "today": business_today().isoformat(),
    }


def _parse_package_options(package_options, unit_code):
    parsed = []
    for item in package_options or []:
        size = _decimal(item.get("quantity_per_bag"))
        price = _decimal(item.get("price_per_bag"))
        label = _clean(item.get("label"), 80)
        if size <= 0 and price <= 0 and not label:
            continue
        if size <= 0:
            raise ValueError("Enter how much produce is inside each bag / pack.")
        if price <= 0:
            raise ValueError("Enter the selling price for each bag / pack.")
        if size > Decimal("1000000"):
            raise ValueError("Bag / pack size is unusually large. Please check it.")
        parsed.append({
            "label": label or f"{_qty(size)} {unit_code} Bag",
            "quantity_per_bag": float(size),
            "price_per_bag": float(price),
        })
        if len(parsed) >= 4:
            break
    return parsed


def save_listing(actor_user_id, product_key, listed_quantity, selling_mode, *, loose_price=None, min_order_quantity=1, package_options=None, title="", description="", grade="", variety="", images=None, publish=True, listing_id=None, available_quantity=None):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    product_key = _clean(product_key, 250)
    if not product_key:
        raise ValueError("Choose produce from My Produce Stock.")
    stock = _farmer_stock_snapshot(profile["user_id"], product_key)
    sample = stock.get("sample") or {}
    if not sample:
        raise ValueError("Produce stock was not found. Add Production / Harvest first.")
    quantity = _decimal(listed_quantity)
    desired_available = None if available_quantity in (None, "") else _decimal(available_quantity)
    unit_code = str(sample.get("unit_code") or "KG")
    mode = str(selling_mode or "loose").strip().lower()
    if mode not in {"loose", "bag", "both"}:
        raise ValueError("Choose Per Unit, Bags / Packs, or Both.")
    loose = _decimal(loose_price)
    if mode in {"loose", "both"} and loose <= 0:
        raise ValueError(f"Enter the selling price per {unit_code}.")
    packages = _parse_package_options(package_options, unit_code)
    if mode in {"bag", "both"} and not packages:
        raise ValueError("Add at least one bag / pack size and price.")
    minimum = max(_decimal(min_order_quantity), Decimal("0"))
    if mode in {"loose", "both"} and minimum <= 0:
        minimum = Decimal("1")

    existing = None
    existing_fulfilled = Decimal("0")
    existing_reserved = Decimal("0")
    existing_images = []
    if listing_id:
        oid = _to_object_id(listing_id)
        existing = mongo.db[LISTING_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]}) if oid else None
        if not existing:
            raise ValueError("Listing was not found.")
        if existing.get("product_key") != product_key:
            raise ValueError("The produce item of an existing listing cannot be changed. Create a new listing instead.")
        existing_fulfilled = max(_decimal(existing.get("fulfilled_quantity")), Decimal("0"))
        existing_reserved = max(_decimal(existing.get("reserved_quantity")), Decimal("0"))
        existing_images = list(existing.get("images") or [])
        if desired_available is not None:
            if desired_available < 0:
                raise ValueError("Quantity available for new orders cannot be negative.")
            if desired_available > stock["saleable"] + EPSILON:
                raise ValueError(f"You currently have only {_qty(stock['saleable'])} {unit_code} available in My Stock.")
            # Stored listed_quantity is the lifetime listing cap. The edit UI is
            # intentionally simpler: farmer edits only what remains for new orders.
            quantity = existing_fulfilled + existing_reserved + desired_available
        if quantity + EPSILON < existing_fulfilled + existing_reserved:
            raise ValueError(f"This listing already has {_qty(existing_fulfilled + existing_reserved)} {unit_code} sold/reserved. Listed quantity cannot be lower than that.")

    if quantity <= 0:
        raise ValueError("Quantity for sale must be greater than zero.")

    # Publication never reserves physical stock. At save time, however, a listing
    # cannot advertise more NEW quantity than the farmer currently has saleable.
    maximum_offer = stock["saleable"] + existing_reserved + existing_fulfilled
    if quantity > maximum_offer + EPSILON:
        raise ValueError(f"You currently have only {_qty(stock['saleable'])} {unit_code} available in My Stock. Reduce the quantity for sale.")

    final_images = [str(x) for x in (images or []) if x]
    if not final_images:
        final_images = existing_images
    final_images = final_images[:4]
    if publish and not final_images:
        raise ValueError("Add at least one clear product photo before publishing. You can save a draft without a photo.")
    timestamp = now_utc()
    payload = {
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_master_id": profile["farmer_master_id"],
        "farmer_name": profile["name"],
        "farmer_phone": profile["phone"],
        "centre_uid": profile["centre_uid"],
        "mitra_uid": profile["mitra_uid"],
        "state": profile["state"],
        "district": profile["district"],
        "block": profile["block"],
        "village": profile["village"],
        "product_key": product_key,
        "product_name": sample.get("product_name") or "Produce",
        "unit_code": unit_code,
        "title": _clean(title, 150) or sample.get("product_name") or "Produce",
        "description": _clean(description, 1200),
        "grade": _clean(grade, 80) or sample.get("grade") or "",
        "variety": _clean(variety, 120) or sample.get("variety") or "",
        "listed_quantity": float(quantity),
        "selling_mode": mode,
        "loose_price": float(loose) if mode in {"loose", "both"} else 0.0,
        "min_order_quantity": float(minimum) if mode in {"loose", "both"} else 0.0,
        "package_options": packages,
        "images": final_images,
        "status": "published" if publish else "draft",
        "published_at": timestamp if publish else None,
        "updated_at": timestamp,
    }
    if existing:
        mongo.db[LISTING_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": payload})
        listing = mongo.db[LISTING_COLLECTION].find_one({"_id": existing["_id"]}) or {**existing, **payload}
        action = "update_listing"
    else:
        payload.update({
            "listing_number": _next_number("farmer_marketplace_listing", "FML"),
            "fulfilled_quantity": 0.0,
            "reserved_quantity": 0.0,
            "created_at": timestamp,
        })
        result = mongo.db[LISTING_COLLECTION].insert_one(payload)
        payload["_id"] = result.inserted_id
        listing = payload
        action = "create_listing"
    _audit(profile["user"], action, "listing", listing["_id"], f"{listing.get('product_name')} · {_qty(quantity)} {unit_code} · {listing.get('status')}.")
    return {"listing": serialize_listing(listing, profile["user_id"]), "message": "Produce published to Farmer Produce Market." if publish else "Listing saved as draft."}


def set_listing_status(actor_user_id, listing_id, status):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    oid = _to_object_id(listing_id)
    if not oid:
        raise ValueError("Invalid listing reference.")
    listing = mongo.db[LISTING_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]})
    if not listing:
        raise ValueError("Listing was not found.")
    target = str(status or "").strip().lower()
    if target not in {"published", "paused", "closed"}:
        raise ValueError("Invalid listing status.")
    if target == "closed" and _decimal(listing.get("reserved_quantity")) > EPSILON:
        raise ValueError("This listing has an approved order. Complete or cancel that order before closing the listing.")
    if target == "published":
        if not (listing.get("images") or []):
            raise ValueError("Add at least one clear product photo before publishing this listing.")
        _, _, _, _, available = _listing_live_values(listing)
        if available <= EPSILON:
            raise ValueError("No saleable produce is available for this listing.")
    update = {"status": target, "updated_at": now_utc()}
    if target == "published":
        update["published_at"] = now_utc()
    mongo.db[LISTING_COLLECTION].update_one({"_id": oid}, {"$set": update})
    _audit(profile["user"], f"listing_{target}", "listing", oid, f"Listing changed to {target}.")
    return {"message": "Listing is live." if target == "published" else ("Listing paused." if target == "paused" else "Listing closed.")}


def get_my_listings(actor_user_id, search=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    q = _clean(search, 120)
    query = {"farmer_user_id": profile["user_id"]}
    if q:
        query["$or"] = [
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"items.product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"title": {"$regex": re.escape(q), "$options": "i"}},
            {"listing_number": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = [serialize_listing(x, profile["user_id"]) for x in mongo.db[LISTING_COLLECTION].find(query).sort("updated_at", DESCENDING)]
    return {
        "farmer": profile,
        "rows": rows,
        "query": q,
        "summary": {
            "total": len(rows),
            "live": sum(1 for r in rows if r.get("status") == "published"),
            "available": sum(1 for r in rows if r.get("is_available")),
            "orders": mongo.db[ORDER_COLLECTION].count_documents({"seller_farmer_user_id": profile["user_id"], "status": {"$in": ["requested", "approved", "dispatched"]}}),
        },
    }


def _market_visibility_query(user):
    role = user.get("resolved_role") or ""
    query = {"status": "published"}
    if role == "ufc_admin":
        centre_uid = _resolve_ufc_uid(user)
        if not centre_uid:
            raise ValueError("This UFC Admin is not linked to a Centre UID.")
        query["centre_uid"] = centre_uid
    elif role in {"farmer", "avpl_admin", "super_admin", "accounts"}:
        pass
    else:
        raise PermissionError("You do not have access to the Farmer Produce Market.")
    return query


def get_marketplace(actor_user_id, search="", only_available=False):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    query = _market_visibility_query(user)
    q = _clean(search, 120)
    if q:
        query["$or"] = [
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"items.product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"title": {"$regex": re.escape(q), "$options": "i"}},
            {"farmer_name": {"$regex": re.escape(q), "$options": "i"}},
            {"variety": {"$regex": re.escape(q), "$options": "i"}},
            {"grade": {"$regex": re.escape(q), "$options": "i"}},
            {"description": {"$regex": re.escape(q), "$options": "i"}},
            {"village": {"$regex": re.escape(q), "$options": "i"}},
            {"block": {"$regex": re.escape(q), "$options": "i"}},
            {"district": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = []
    for listing in mongo.db[LISTING_COLLECTION].find(query).sort("published_at", DESCENDING).limit(300):
        row = serialize_listing(listing, user.get("_id"))
        if only_available and not row.get("is_available"):
            continue
        rows.append(row)
    return {
        "viewer": {"role": user.get("resolved_role"), "name": user.get("resolved_name"), "user_id": str(user.get("_id")), "centre_uid": _resolve_ufc_uid(user) if user.get("resolved_role") == "ufc_admin" else ""},
        "rows": rows,
        "query": q,
        "order_token": f"FMORD-{uuid4().hex.upper()}",
        "payment_terms": PAYMENT_TERMS,
        "available_only": bool(only_available),
        "summary": {"listing_count": len(rows), "available_count": sum(1 for r in rows if r.get("is_available"))},
    }


def get_listing(actor_user_id, listing_id):
    user = _get_user(actor_user_id)
    oid = _to_object_id(listing_id)
    if not oid:
        raise ValueError("Invalid listing reference.")
    query = _market_visibility_query(user)
    query["_id"] = oid
    listing = mongo.db[LISTING_COLLECTION].find_one(query)
    if not listing:
        # Owner may inspect a paused listing from My Listings.
        if user.get("resolved_role") == "farmer":
            listing = mongo.db[LISTING_COLLECTION].find_one({"_id": oid, "farmer_user_id": user.get("_id")})
    if not listing:
        raise ValueError("Listing was not found or is not visible to you.")
    return serialize_listing(listing, user.get("_id"))


def _order_items(order):
    raw = order.get("items") or []
    if raw:
        return [dict(x or {}) for x in raw if isinstance(x, dict)]
    return [{
        "line_id": "legacy", "listing_id": order.get("listing_id"), "listing_number": order.get("listing_number") or "",
        "product_key": order.get("product_key") or "", "product_name": order.get("product_name") or "Produce", "variety": order.get("variety") or "", "grade": order.get("grade") or "",
        "unit_code": order.get("unit_code") or "KG", "purchase_mode": order.get("purchase_mode") or "loose", "requested_quantity": order.get("requested_quantity") or 0,
        "base_quantity": order.get("base_quantity") or 0, "dispatched_quantity": order.get("dispatched_quantity") or order.get("base_quantity") or 0, "quantity_description": order.get("quantity_description") or "", "unit_price": order.get("unit_price") or 0,
        "package": order.get("package"), "line_total": order.get("total_amount") or 0, "status": order.get("status") or "requested", "stock_reservations": order.get("stock_reservations") or [],
    }]


def _serialize_order_item(item):
    row=dict(item or {})
    row["listing_id_str"]=str(row.get("listing_id") or "")
    row["base_quantity_display"]=_qty(row.get("base_quantity"))
    row["requested_quantity_display"]=_qty(row.get("requested_quantity"))
    row["dispatched_quantity_display"]=_qty(row.get("dispatched_quantity") if row.get("dispatched_quantity") is not None else row.get("base_quantity"))
    row["physically_received_quantity_display"]=_qty(row.get("physically_received_quantity"))
    row["accepted_quantity_display"]=_qty(row.get("accepted_quantity"))
    row["damaged_quantity_display"]=_qty(row.get("damaged_quantity"))
    row["rejected_quantity_display"]=_qty(row.get("rejected_quantity"))
    row["missing_quantity_display"]=_qty(row.get("missing_quantity"))
    row["unit_price_display"]=_money(row.get("unit_price"))
    row["line_total_display"]=_money(row.get("line_total"))
    return row


def _copy_order_line_to_legacy(document, line):
    line=line or {}; document.update({
        "listing_id":line.get("listing_id"),"listing_number":line.get("listing_number") or "","product_key":line.get("product_key") or "","product_name":line.get("product_name") or "Produce","variety":line.get("variety") or "","grade":line.get("grade") or "","unit_code":line.get("unit_code") or "KG",
        "purchase_mode":line.get("purchase_mode") or "loose","requested_quantity":float(_decimal(line.get("requested_quantity"))),"base_quantity":float(_decimal(line.get("base_quantity"))),"quantity_description":line.get("quantity_description") or "","unit_price":float(_decimal(line.get("unit_price"))),"package":line.get("package"),
    }); return document


def _prepare_order_line(actor_user_id, listing_id, purchase_mode, quantity, package_index=None):
    listing=get_listing(actor_user_id,listing_id)
    if listing.get("status")!="published":raise ValueError("This produce listing is not currently open for orders.")
    if listing.get("is_owner"):raise ValueError("You cannot order your own produce listing.")
    available=_decimal(listing.get("available_to_order"))
    if available<=EPSILON:raise ValueError(f"{listing.get('product_name') or 'This produce'} is currently out of stock.")
    mode=str(purchase_mode or "loose").strip().lower(); listing_mode=listing.get("selling_mode") or "loose"
    if mode=="loose" and listing_mode not in {"loose","both"}:raise ValueError(f"{listing.get('product_name') or 'This listing'} is sold only by bag / pack.")
    if mode=="bag" and listing_mode not in {"bag","both"}:raise ValueError(f"{listing.get('product_name') or 'This listing'} is sold only by loose quantity.")
    if mode=="bag":
        try: idx=int(package_index)
        except Exception: raise ValueError("Choose a bag / pack size.")
        selected=next((x for x in (listing.get("package_options") or []) if int(x.get("index",-1))==idx),None)
        if not selected:raise ValueError("Choose a valid bag / pack size.")
        count=_decimal(quantity)
        if count<=0 or count!=count.to_integral_value():raise ValueError("Number of bags / packs must be a whole number.")
        base_qty=count*_decimal(selected.get("quantity_per_bag")); total=count*_decimal(selected.get("price_per_bag")); rate=_decimal(selected.get("price_per_bag")); requested=count
        package={"label":selected.get("label") or "Bag","quantity_per_bag":float(_decimal(selected.get("quantity_per_bag"))),"price_per_bag":float(_decimal(selected.get("price_per_bag"))),"bag_count":int(count)}
        qty_desc=f"{int(count)} × {selected.get('quantity_per_bag_display')} {listing.get('unit_code')}"
    else:
        base_qty=_decimal(quantity); minimum=max(_decimal(listing.get("min_order_quantity")),Decimal("0"))
        if base_qty<=0:raise ValueError("Order quantity must be greater than zero.")
        if minimum>0 and base_qty+EPSILON<minimum:raise ValueError(f"Minimum order for {listing.get('product_name') or 'this produce'} is {_qty(minimum)} {listing.get('unit_code')}.")
        rate=_decimal(listing.get("loose_price"));total=base_qty*rate;requested=base_qty;package=None;qty_desc=f"{_qty(base_qty)} {listing.get('unit_code')}"
    if base_qty>available+EPSILON:raise ValueError(f"Only {_qty(available)} {listing.get('unit_code')} of {listing.get('product_name') or 'this produce'} is currently available.")
    if total<=0:raise ValueError("This listing does not have a valid selling price.")
    raw=mongo.db[LISTING_COLLECTION].find_one({"_id":_to_object_id(listing_id)}) or {}
    return {
        "line_id":uuid4().hex[:12],"listing_id":raw.get("_id"),"listing_number":raw.get("listing_number") or "","seller_farmer_user_id":raw.get("farmer_user_id"),"seller_farmer_name":raw.get("farmer_name") or "Farmer","seller_farmer_phone":raw.get("farmer_phone") or "","seller_centre_uid":raw.get("centre_uid") or "","seller_state":raw.get("state") or "","seller_district":raw.get("district") or "","seller_village":raw.get("village") or "",
        "product_key":raw.get("product_key") or "","product_name":raw.get("product_name") or "Produce","variety":raw.get("variety") or "","grade":raw.get("grade") or "","unit_code":raw.get("unit_code") or "KG","purchase_mode":mode,
        "requested_quantity":float(requested),"base_quantity":float(base_qty),"quantity_description":qty_desc,"unit_price":float(rate),"package":package,"line_total":float(total.quantize(MONEY_QUANTUM,rounding=ROUND_HALF_UP)),"status":"requested","stock_reservations":[],
    }


def place_cart_orders(actor_user_id, items, *, payment_term="pay_on_receipt", note="", idempotency_key=""):
    _ensure_indexes(); user=_get_user(actor_user_id); buyer=_buyer_snapshot(user)
    if not isinstance(items,list) or not items:raise ValueError("Your cart is empty.")
    if len(items)>50:raise ValueError("A cart can contain at most 50 produce lines.")
    term=str(payment_term or "pay_on_receipt").strip().lower(); term=term if term in PAYMENT_TERMS else "pay_on_receipt"
    # Merge duplicate cart lines before validating stock so the same listing cannot
    # be submitted twice to bypass its current available quantity. Loose and pack
    # variants remain separate because they have different pricing/quantity rules.
    merged={}
    for raw in items:
        if not isinstance(raw,dict):
            continue
        listing_id=str(raw.get("listing_id") or "").strip(); mode=str(raw.get("purchase_mode") or "loose").strip().lower(); package_index=str(raw.get("package_index") if raw.get("package_index") is not None else "")
        qty=_decimal(raw.get("quantity"))
        if not listing_id or qty<=0:
            raise ValueError("Every produce cart line must have a valid quantity greater than zero.")
        key=(listing_id,mode,package_index)
        if key not in merged:
            merged[key]={"listing_id":listing_id,"purchase_mode":mode,"package_index":raw.get("package_index"),"quantity":Decimal("0")}
        merged[key]["quantity"]+=qty
    prepared=[_prepare_order_line(actor_user_id,x.get("listing_id"),x.get("purchase_mode","loose"),x.get("quantity"),x.get("package_index")) for x in merged.values()]
    if not prepared:raise ValueError("Your cart has no valid produce lines.")
    groups={}
    for line in prepared:groups.setdefault(str(line.get("seller_farmer_user_id") or ""),[]).append(line)
    base=_clean(idempotency_key,100) or f"FMCART-{uuid4().hex.upper()}"; created=[]
    for seller_key,lines in groups.items():
        token=f"{base}:{seller_key}"[:120]; existing=mongo.db[ORDER_COLLECTION].find_one({"idempotency_key":token})
        if existing:
            if existing.get("buyer_key")!=buyer.get("key"):raise RuntimeError("This checkout token is already in use.")
            created.append(serialize_order(existing));continue
        first=lines[0]; total=sum((_decimal(x.get("line_total")) for x in lines),Decimal("0"));timestamp=now_utc();doc={
            "order_number":_next_number("farmer_marketplace_order","FMORD"),"idempotency_key":token,"checkout_token":base,"commerce_version":2,"items":lines,"item_count":len(lines),"is_multi_item_order":len(lines)>1,
            "seller_farmer_user_id":first.get("seller_farmer_user_id"),"seller_farmer_user_id_str":str(first.get("seller_farmer_user_id") or ""),"seller_farmer_name":first.get("seller_farmer_name") or "Farmer","seller_farmer_phone":first.get("seller_farmer_phone") or "","seller_centre_uid":first.get("seller_centre_uid") or "","seller_state":first.get("seller_state") or "","seller_district":first.get("seller_district") or "","seller_village":first.get("seller_village") or "",
            "buyer_role":buyer.get("role"),"buyer_type":buyer.get("type"),"buyer_key":buyer.get("key"),"buyer":buyer,"total_amount":float(total),"payment_term":term,"payment_term_label":PAYMENT_TERMS[term],"credit_days":0,"note":_clean(note,800),"status":"requested","status_history":[{"status":"requested","at":timestamp,"by":buyer.get("name") or "Buyer"}],"payment_status":"unpaid","amount_paid":0.0,"outstanding_amount":float(total),"created_at":timestamp,"updated_at":timestamp,
        };_copy_order_line_to_legacy(doc,first)
        try:r=mongo.db[ORDER_COLLECTION].insert_one(doc);doc["_id"]=r.inserted_id
        except DuplicateKeyError:
            old=mongo.db[ORDER_COLLECTION].find_one({"idempotency_key":token})
            if not old:raise RuntimeError("Order could not be saved safely. Refresh and try again.")
            doc=old
        _audit(user,"place_cart_order","order",doc["_id"],f"{buyer.get('name')} requested {len(lines)} produce line(s) from {first.get('seller_farmer_name') or 'Farmer'}.")
        _notify(first.get("seller_farmer_user_id"),"farmer","New produce order",f"{buyer.get('name')} placed {doc.get('order_number')} with {len(lines)} produce item(s). Open Orders Received to review it.")
        created.append(serialize_order(doc))
    return {"orders":created,"order":created[0] if len(created)==1 else None,"order_count":len(created),"seller_count":len(groups),"message":f"Checkout complete. {len(created)} seller-specific order(s) created."}


def place_order(actor_user_id, listing_id, purchase_mode, quantity, *, package_index=None, payment_term="pay_on_receipt", note="", idempotency_key=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    buyer = _buyer_snapshot(user)
    listing = get_listing(actor_user_id, listing_id)
    if listing.get("status") != "published":
        raise ValueError("This produce listing is not currently open for orders.")
    if listing.get("is_owner"):
        raise ValueError("You cannot order your own produce listing.")
    available = _decimal(listing.get("available_to_order"))
    if available <= EPSILON:
        raise ValueError("This produce is currently out of stock.")
    mode = str(purchase_mode or "loose").strip().lower()
    listing_mode = listing.get("selling_mode") or "loose"
    if mode == "loose" and listing_mode not in {"loose", "both"}:
        raise ValueError("This listing is sold only by bag / pack.")
    if mode == "bag" and listing_mode not in {"bag", "both"}:
        raise ValueError("This listing is sold only by loose quantity.")

    if mode == "bag":
        try:
            idx = int(package_index)
        except Exception:
            raise ValueError("Choose a bag / pack size.")
        options = listing.get("package_options") or []
        selected = next((x for x in options if int(x.get("index", -1)) == idx), None)
        if not selected:
            raise ValueError("Choose a valid bag / pack size.")
        bag_count = _decimal(quantity)
        if bag_count <= 0 or bag_count != bag_count.to_integral_value():
            raise ValueError("Number of bags / packs must be a whole number.")
        base_qty = bag_count * _decimal(selected.get("quantity_per_bag"))
        total = bag_count * _decimal(selected.get("price_per_bag"))
        rate = _decimal(selected.get("price_per_bag"))
        order_quantity = bag_count
        package_snapshot = {
            "label": selected.get("label") or "Bag",
            "quantity_per_bag": float(_decimal(selected.get("quantity_per_bag"))),
            "price_per_bag": float(_decimal(selected.get("price_per_bag"))),
            "bag_count": int(bag_count),
        }
        quantity_description = f"{int(bag_count)} × {selected.get('quantity_per_bag_display')} {listing.get('unit_code')}"
    else:
        base_qty = _decimal(quantity)
        minimum = max(_decimal(listing.get("min_order_quantity")), Decimal("0"))
        if base_qty <= 0:
            raise ValueError("Order quantity must be greater than zero.")
        if minimum > 0 and base_qty + EPSILON < minimum:
            raise ValueError(f"Minimum order is {_qty(minimum)} {listing.get('unit_code')}.")
        rate = _decimal(listing.get("loose_price"))
        total = base_qty * rate
        order_quantity = base_qty
        package_snapshot = None
        quantity_description = f"{_qty(base_qty)} {listing.get('unit_code')}"
    if base_qty > available + EPSILON:
        raise ValueError(f"Only {_qty(available)} {listing.get('unit_code')} is currently available to order.")
    if total <= 0:
        raise ValueError("This listing does not have a valid selling price.")

    term = str(payment_term or "pay_on_receipt").strip().lower()
    if term not in PAYMENT_TERMS:
        term = "pay_on_receipt"
    token = _clean(idempotency_key, 120) or f"FMORD-{uuid4().hex.upper()}"
    existing = mongo.db[ORDER_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        if existing.get("buyer_key") != buyer.get("key"):
            raise RuntimeError("This order request token is already in use.")
        return {"order": serialize_order(existing), "message": "This order request was already saved.", "idempotent_replay": True}

    raw_listing = mongo.db[LISTING_COLLECTION].find_one({"_id": _to_object_id(listing_id)}) or {}
    timestamp = now_utc()
    document = {
        "order_number": _next_number("farmer_marketplace_order", "FMORD"),
        "idempotency_key": token,
        "listing_id": raw_listing.get("_id"),
        "listing_number": raw_listing.get("listing_number") or "",
        "seller_farmer_user_id": raw_listing.get("farmer_user_id"),
        "seller_farmer_user_id_str": str(raw_listing.get("farmer_user_id") or ""),
        "seller_farmer_name": raw_listing.get("farmer_name") or "Farmer",
        "seller_farmer_phone": raw_listing.get("farmer_phone") or "",
        "seller_centre_uid": raw_listing.get("centre_uid") or "",
        "seller_state": raw_listing.get("state") or "",
        "seller_district": raw_listing.get("district") or "",
        "seller_village": raw_listing.get("village") or "",
        "buyer_role": buyer.get("role"),
        "buyer_type": buyer.get("type"),
        "buyer_key": buyer.get("key"),
        "buyer": buyer,
        "product_key": raw_listing.get("product_key") or "",
        "product_name": raw_listing.get("product_name") or "Produce",
        "variety": raw_listing.get("variety") or "",
        "grade": raw_listing.get("grade") or "",
        "unit_code": raw_listing.get("unit_code") or "KG",
        "purchase_mode": mode,
        "requested_quantity": float(order_quantity),
        "base_quantity": float(base_qty),
        "quantity_description": quantity_description,
        "unit_price": float(rate),
        "package": package_snapshot,
        "total_amount": float(total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)),
        "payment_term": term,
        "payment_term_label": PAYMENT_TERMS[term],
        "credit_days": 0,
        "note": _clean(note, 800),
        "status": "requested",
        "status_history": [{"status": "requested", "at": timestamp, "by": buyer.get("name") or "Buyer"}],
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(total.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[ORDER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[ORDER_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            return {"order": serialize_order(existing), "message": "This order request was already saved.", "idempotent_replay": True}
        raise RuntimeError("Order could not be saved safely. Refresh and try again.")
    _audit(user, "place_order", "order", document["_id"], f"{buyer.get('name')} requested {quantity_description} {document['product_name']}.")
    _notify(document.get("seller_farmer_user_id"), "farmer", "New produce order", f"{buyer.get('name')} requested {quantity_description} of {document['product_name']}. Open Orders Received to review it.")
    return {"order": serialize_order(document), "message": "Order request sent to the Farmer.", "idempotent_replay": False}


def _assert_seller(user, order):
    if user.get("resolved_role") != "farmer" or str(order.get("seller_farmer_user_id") or "") != str(user.get("_id")):
        raise PermissionError("Only the Farmer selling this produce can perform this action.")


def _assert_buyer(user, order):
    buyer = _buyer_snapshot(user)
    if str(order.get("buyer_key") or "") != str(buyer.get("key") or ""):
        raise PermissionError("This order does not belong to your account.")
    role = user.get("resolved_role")
    if order.get("buyer_type") == "avpl" and role not in {"avpl_admin", "super_admin"}:
        raise PermissionError("This AVPL order does not belong to your account.")
    if order.get("buyer_type") == "ufc" and role != "ufc_admin":
        raise PermissionError("This UFC order does not belong to your account.")
    if order.get("buyer_type") == "farmer" and role != "farmer":
        raise PermissionError("This Farmer order does not belong to your account.")
    return buyer


def _reserve_stock(order):
    all_allocations=[]; reserved_by_line=[]
    try:
        for line in _order_items(order):
            required=_decimal(line.get("base_quantity"))
            if required<=0:raise ValueError("Order quantity is invalid.")
            lots=list(mongo.db[FARMER_LOT_COLLECTION].find({"farmer_user_id":order.get("seller_farmer_user_id"),"product_key":line.get("product_key"),"status":"active","available_quantity":{"$gt":0}}).sort([("harvest_date",ASCENDING),("created_at",ASCENDING),("_id",ASCENDING)]))
            total_saleable=sum((max(_decimal(l.get("available_quantity"))-_decimal(l.get("reserved_quantity")),Decimal("0")) for l in lots),Decimal("0"))
            if total_saleable+EPSILON<required:raise ValueError(f"Only {_qty(total_saleable)} {line.get('unit_code')} of {line.get('product_name') or 'produce'} is saleable now.")
            remaining=required; line_alloc=[]
            for lot in lots:
                if remaining<=EPSILON:break
                available=max(_decimal(lot.get("available_quantity"))-_decimal(lot.get("reserved_quantity")),Decimal("0"));take=min(available,remaining)
                if take<=EPSILON:continue
                current=max(_decimal(lot.get("reserved_quantity")),Decimal("0"))
                result=mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":lot["_id"],"available_quantity":{"$gte":float(current+take)},"status":"active"},{"$inc":{"reserved_quantity":float(take)},"$set":{"updated_at":now_utc()}})
                if result.modified_count!=1:raise RuntimeError("Produce stock changed in another session. Refresh and approve again.")
                a={"line_id":line.get("line_id") or "legacy","listing_id":line.get("listing_id"),"product_key":line.get("product_key") or "","product_name":line.get("product_name") or "Produce","unit_code":line.get("unit_code") or "KG","lot_id":lot["_id"],"lot_number":lot.get("lot_number") or "","quantity":float(take)}
                line_alloc.append(a);all_allocations.append(a);reserved_by_line.append(a);remaining-=take
            if remaining>EPSILON:raise RuntimeError("Produce stock changed while reserving this order. Refresh and try again.")
    except Exception:
        for a in reserved_by_line:mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a["lot_id"]},{"$inc":{"reserved_quantity":-a["quantity"]},"$set":{"updated_at":now_utc()}})
        raise
    return all_allocations


def _release_reservation(order):
    allocations=order.get("stock_reservations") or []
    for a in allocations:
        mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a.get("lot_id")},{"$inc":{"reserved_quantity":-float(_decimal(a.get("quantity")))},"$set":{"updated_at":now_utc()}})
    # Listing reservations must be released per product line, not from the legacy first line only.
    for line in _order_items(order):
        if line.get("listing_id"):
            mongo.db[LISTING_COLLECTION].update_one({"_id":line.get("listing_id")},{"$inc":{"reserved_quantity":-float(_decimal(line.get("base_quantity")))},"$set":{"updated_at":now_utc()}})


def approve_order(actor_user_id, order_id, *, credit_days=0):
    _ensure_indexes();user=_get_user(actor_user_id);oid=_to_object_id(order_id);order=mongo.db[ORDER_COLLECTION].find_one({"_id":oid}) if oid else None
    if not order:raise ValueError("Order was not found.")
    _assert_seller(user,order)
    if order.get("status")=="approved":return {"order":serialize_order(order),"message":"This order is already approved."}
    if order.get("status")!="requested":raise ValueError("Only a Requested order can be approved.")
    for line in _order_items(order):
        listing=mongo.db[LISTING_COLLECTION].find_one({"_id":line.get("listing_id")}) or {}
        if listing.get("status")!="published":raise ValueError(f"Publish {line.get('product_name') or 'the listing'} again before approving this order.")
        *_,available=_listing_live_values(listing)
        if _decimal(line.get("base_quantity"))>available+EPSILON:raise ValueError(f"Only {_qty(available)} {line.get('unit_code')} of {line.get('product_name') or 'produce'} remains available on this listing.")
    reservations=_reserve_stock(order); changed=[]
    try:
        for line in _order_items(order):
            q=_decimal(line.get("base_quantity"));result=mongo.db[LISTING_COLLECTION].update_one({"_id":line.get("listing_id"),"status":"published","$expr":{"$gte":[{"$subtract":[{"$ifNull":["$listed_quantity",0]},{"$add":[{"$ifNull":["$fulfilled_quantity",0]},{"$ifNull":["$reserved_quantity",0]}]}]},float(q)]}},{"$inc":{"reserved_quantity":float(q)},"$set":{"updated_at":now_utc()}})
            if result.modified_count!=1:raise RuntimeError(f"{line.get('product_name') or 'A listing'} changed while approving this order.")
            changed.append((line.get("listing_id"),float(q)))
        try:days=min(max(int(credit_days or 0),0),365)
        except Exception:days=0
        if order.get("payment_term")!="credit":days=0
        timestamp=now_utc();updated_items=[]
        for line in _order_items(order):
            line=dict(line);line["status"]="approved";line["stock_reservations"]=[a for a in reservations if str(a.get("line_id"))==str(line.get("line_id") or "legacy")];updated_items.append(line)
        result=mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"],"status":"requested"},{"$set":{"status":"approved","items":updated_items if order.get("items") else order.get("items",[]),"stock_reservations":reservations,"credit_days":days,"approved_by":user.get("_id"),"approved_at":timestamp,"updated_at":timestamp},"$push":{"status_history":{"status":"approved","at":timestamp,"by":user.get("resolved_name")}}})
        if result.modified_count!=1:raise RuntimeError("Order changed in another session.")
    except Exception:
        for a in reservations:mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a["lot_id"]},{"$inc":{"reserved_quantity":-a["quantity"]},"$set":{"updated_at":now_utc()}})
        for listing_id,q in changed:mongo.db[LISTING_COLLECTION].update_one({"_id":listing_id},{"$inc":{"reserved_quantity":-q},"$set":{"updated_at":now_utc()}})
        raise
    updated=mongo.db[ORDER_COLLECTION].find_one({"_id":order["_id"]}) or order
    _audit(user,"approve_order","order",order["_id"],f"Reserved {len(_order_items(order))} produce line(s).")
    _notify((order.get("buyer") or {}).get("user_id"),order.get("buyer_role") or "","Produce order approved",f"{order.get('seller_farmer_name')} approved {order.get('order_number')}. Your produce is reserved.")
    return {"order":serialize_order(updated),"message":"Order approved. Produce is reserved line by line; physical stock has not been reduced yet."}


def reject_order(actor_user_id, order_id, reason=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid}) if oid else None
    if not order:
        raise ValueError("Order was not found.")
    _assert_seller(user, order)
    if order.get("status") != "requested":
        raise ValueError("Only a Requested order can be rejected.")
    reason = _clean(reason, 600)
    timestamp = now_utc()
    mongo.db[ORDER_COLLECTION].update_one({"_id": order["_id"], "status": "requested"}, {"$set": {"status": "rejected", "rejection_reason": reason, "rejected_at": timestamp, "updated_at": timestamp}, "$push": {"status_history": {"status": "rejected", "at": timestamp, "by": user.get("resolved_name"), "note": reason}}})
    _audit(user, "reject_order", "order", order["_id"], reason)
    _notify((order.get("buyer") or {}).get("user_id"), order.get("buyer_role") or "", "Produce order rejected", f"{order.get('order_number')} was not accepted by the Farmer.")
    return {"message": "Order rejected. Stock was not changed."}


def cancel_order(actor_user_id, order_id, reason=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid}) if oid else None
    if not order:
        raise ValueError("Order was not found.")
    is_seller = user.get("resolved_role") == "farmer" and str(order.get("seller_farmer_user_id") or "") == str(user.get("_id"))
    if not is_seller:
        _assert_buyer(user, order)
    if order.get("status") not in {"requested", "approved"}:
        raise ValueError("Only a Requested or Approved order can be cancelled.")

    reason = _clean(reason, 600)
    timestamp = now_utc()
    previous_status = order.get("status")
    # Change order state first. This makes cancellation idempotent under two
    # browser tabs and guarantees that reserved stock is released only once.
    result = mongo.db[ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "status": previous_status},
        {
            "$set": {
                "status": "cancelled",
                "cancellation_reason": reason,
                "cancelled_at": timestamp,
                "updated_at": timestamp,
            },
            "$push": {
                "status_history": {
                    "status": "cancelled",
                    "at": timestamp,
                    "by": user.get("resolved_name"),
                    "note": reason,
                }
            },
        },
    )
    if result.modified_count != 1:
        latest = mongo.db[ORDER_COLLECTION].find_one({"_id": order["_id"]}) or {}
        if latest.get("status") == "cancelled":
            return {"message": "This order is already cancelled."}
        raise RuntimeError("Order changed in another session. Refresh before cancelling again.")

    if previous_status == "approved":
        try:
            _release_reservation(order)
        except Exception as exc:
            mongo.db[ORDER_COLLECTION].update_one(
                {"_id": order["_id"]},
                {"$set": {
                    "reservation_release_status": "needs_repair",
                    "reservation_release_error": _clean(exc, 500),
                    "updated_at": now_utc(),
                }},
            )
            raise RuntimeError("Order was cancelled, but its stock reservation needs administrator repair. Do not cancel it again.")

    _audit(user, "cancel_order", "order", order["_id"], reason)
    other_user = (order.get("buyer") or {}).get("user_id") if is_seller else order.get("seller_farmer_user_id")
    other_role = order.get("buyer_role") if is_seller else "farmer"
    _notify(other_user, other_role, "Produce order cancelled", f"{order.get('order_number')} was cancelled. Any reservation was released.")
    return {"message": "Order cancelled. Reserved produce was released." if previous_status == "approved" else "Order request cancelled."}


def _consume_reservation(order):
    allocations=order.get("stock_reservations") or []
    if not allocations:raise RuntimeError("This approved order has no stock reservation. Cancel and recreate it instead of deducting stock manually.")
    applied=[];movement_ids=[]
    try:
        for a in allocations:
            qty=_decimal(a.get("quantity"));
            if qty<=0:continue
            result=mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a.get("lot_id"),"available_quantity":{"$gte":float(qty)},"reserved_quantity":{"$gte":float(qty)},"status":"active"},{"$inc":{"available_quantity":-float(qty),"reserved_quantity":-float(qty),"sold_quantity":float(qty)},"$set":{"updated_at":now_utc()}})
            if result.modified_count!=1:raise RuntimeError("Reserved produce stock changed unexpectedly. No dispatch was completed.")
            applied.append(dict(a))
        for a in applied:
            movement=mongo.db[FARMER_MOVEMENT_COLLECTION].insert_one({"farmer_user_id":order.get("seller_farmer_user_id"),"farmer_user_id_str":str(order.get("seller_farmer_user_id") or ""),"line_id":a.get("line_id") or "legacy","product_name":a.get("product_name") or order.get("product_name") or "Produce","product_key":a.get("product_key") or order.get("product_key") or "","unit_code":a.get("unit_code") or order.get("unit_code") or "KG","lot_id":a.get("lot_id"),"lot_number":a.get("lot_number") or "","movement_type":"marketplace_sale_out","quantity":a.get("quantity"),"direction":"out","reference_type":"farmer_marketplace_order","reference_id":order.get("_id"),"reference_number":order.get("order_number") or "","note":f"Farmer Produce Market dispatch to {(order.get('buyer') or {}).get('name') or 'Buyer'}.","created_at":now_utc()});movement_ids.append(movement.inserted_id)
    except Exception:
        for a in applied:mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a.get("lot_id")},{"$inc":{"available_quantity":a.get("quantity"),"reserved_quantity":a.get("quantity"),"sold_quantity":-a.get("quantity")},"$set":{"updated_at":now_utc()}})
        if movement_ids:mongo.db[FARMER_MOVEMENT_COLLECTION].delete_many({"_id":{"$in":movement_ids}})
        raise
    return applied,movement_ids


def _seller_snapshot(order):
    return {
        "farmer_user_id": order.get("seller_farmer_user_id"),
        "farmer_user_id_str": str(order.get("seller_farmer_user_id") or ""),
        "name": order.get("seller_farmer_name") or "Farmer",
        "phone": order.get("seller_farmer_phone") or "",
        "centre_uid": order.get("seller_centre_uid") or "",
        "village": order.get("seller_village") or "",
        "district": order.get("seller_district") or "",
        "state": order.get("seller_state") or "",
    }


def _ensure_sale_documents(order):
    items=[]; total=Decimal("0")
    for line in _order_items(order):
        q=_decimal(line.get("base_quantity"));line_total=_decimal(line.get("line_total")) or q*_decimal(line.get("unit_price"));total+=line_total
        accepted_q = _decimal(line.get("accepted_quantity")) if line.get("accepted_quantity") is not None else (q if order.get("status") == "received" else Decimal("0"))
        accepted_value = _decimal(line.get("accepted_commercial_total")) if line.get("accepted_commercial_total") is not None else accepted_q * _decimal(line.get("unit_price"))
        items.append({
            "line_id":line.get("line_id") or "legacy","listing_id":line.get("listing_id"),"listing_number":line.get("listing_number") or "",
            "product_key":line.get("product_key") or "","product_name":line.get("product_name") or "Produce","variety":line.get("variety") or "","grade":line.get("grade") or "",
            "quantity":float(q),"dispatched_quantity":float(_decimal(line.get("dispatched_quantity")) or q),
            "received_quantity":float(_decimal(line.get("received_quantity"))),"accepted_quantity":float(accepted_q),
            "damaged_quantity":float(_decimal(line.get("damaged_quantity"))),"rejected_quantity":float(_decimal(line.get("rejected_quantity"))),
            "missing_quantity":float(_decimal(line.get("missing_quantity"))),"accepted_commercial_total":float(accepted_value),
            "quantity_description":line.get("quantity_description") or "","unit_code":line.get("unit_code") or "KG",
            "purchase_mode":line.get("purchase_mode") or "loose","package":line.get("package"),
            "unit_price":float(_decimal(line.get("unit_price"))),"line_total":float(line_total)
        })
    first=items[0]
    receipt_finalized = order.get("status") == "received" or bool(order.get("receipt_status"))
    if receipt_finalized:
        settlement_total = sum((_decimal(x.get("accepted_commercial_total")) for x in _order_items(order) if x.get("accepted_commercial_total") is not None), Decimal("0"))
        if settlement_total <= 0 and any(_decimal(x.get("accepted_quantity")) > 0 for x in _order_items(order)):
            settlement_total = sum((_decimal(x.get("accepted_quantity")) * _decimal(x.get("unit_price")) for x in _order_items(order)), Decimal("0"))
    else:
        settlement_total = total
    receipt_adjustment = max(total - settlement_total, Decimal("0"))
    sale=mongo.db[SALE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]})
    if not sale:
        sale={"sale_number":_next_number("farmer_marketplace_sale","FMSALE"),"farmer_marketplace_order_id":order["_id"],"farmer_marketplace_order_id_str":str(order["_id"]),"order_number":order.get("order_number") or "","commerce_version":2 if order.get("items") else 1,"items":items,"item_count":len(items),"listing_id":first.get("listing_id"),"seller_farmer_user_id":order.get("seller_farmer_user_id"),"seller_farmer_user_id_str":str(order.get("seller_farmer_user_id") or ""),"seller_farmer_name":order.get("seller_farmer_name") or "Farmer","seller_centre_uid":order.get("seller_centre_uid") or "","buyer":order.get("buyer") or {},"buyer_role":order.get("buyer_role") or "","buyer_type":order.get("buyer_type") or "","buyer_key":order.get("buyer_key") or "","product_key":first.get("product_key"),"product_name":first.get("product_name"),"variety":first.get("variety"),"grade":first.get("grade"),"unit_code":first.get("unit_code") if len(items)==1 else "MULTI","quantity":first.get("quantity") if len(items)==1 else 0,"quantity_description":first.get("quantity_description") if len(items)==1 else f"{len(items)} produce items","purchase_mode":first.get("purchase_mode") if len(items)==1 else "multi","package":first.get("package") if len(items)==1 else None,"total_amount":float(total),"payment_term":order.get("payment_term") or "pay_on_receipt","credit_days":order.get("credit_days") or 0,"payment_status":"unpaid","amount_paid":0.0,"outstanding_amount":float(total),"status":"completed","sale_date":business_today().isoformat(),"created_at":now_utc(),"updated_at":now_utc()}
        try:r=mongo.db[SALE_COLLECTION].insert_one(sale);sale["_id"]=r.inserted_id
        except DuplicateKeyError:sale=mongo.db[SALE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or sale
    if sale and sale.get("_id"):
        # Repair stale first-line-only records while preserving confirmed payments.
        sale_paid = _decimal(sale.get("amount_paid"))
        sale_outstanding = max(settlement_total - sale_paid, Decimal("0"))
        sale_payment_status = "paid" if sale_outstanding <= EPSILON else ("partially_paid" if sale_paid > EPSILON else "unpaid")
        mongo.db[SALE_COLLECTION].update_one({"_id": sale["_id"]}, {"$set": {
            "commerce_version": 2 if order.get("items") else 1,
            "items": items,
            "item_count": len(items),
            "product_name": first.get("product_name"),
            "product_key": first.get("product_key"),
            "variety": first.get("variety"),
            "grade": first.get("grade"),
            "quantity": first.get("quantity") if len(items) == 1 else 0,
            "quantity_description": first.get("quantity_description") if len(items) == 1 else f"{len(items)} produce items",
            "unit_code": first.get("unit_code") if len(items) == 1 else "MULTI",
            "unit_price": first.get("unit_price") if len(items) == 1 else 0,
            "total_amount": float(total),
            "settlement_total": float(settlement_total),
            "receipt_adjustment_amount": float(receipt_adjustment),
            "receipt_status": order.get("receipt_status") or "",
            "amount_paid": float(min(sale_paid, settlement_total)),
            "outstanding_amount": float(sale_outstanding),
            "payment_status": sale_payment_status,
            "updated_at": now_utc(),
        }})
        sale = mongo.db[SALE_COLLECTION].find_one({"_id": sale["_id"]}) or sale
    invoice=mongo.db[INVOICE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]})
    if not invoice:
        due=business_today()+timedelta(days=max(int(order.get("credit_days") or 0),0)) if order.get("payment_term")=="credit" else business_today()
        invoice={"document_number":_next_number("farmer_marketplace_invoice","FMRCPT"),"document_type":"sales_receipt","document_title":"Farmer Produce Sales Receipt","farmer_marketplace_order_id":order["_id"],"farmer_marketplace_order_id_str":str(order["_id"]),"farmer_marketplace_sale_id":sale.get("_id"),"farmer_marketplace_sale_id_str":str(sale.get("_id") or ""),"order_number":order.get("order_number") or "","sale_number":sale.get("sale_number") or "","commerce_version":2 if order.get("items") else 1,"items":items,"item_count":len(items),"farmer_user_id":order.get("seller_farmer_user_id"),"farmer_user_id_str":str(order.get("seller_farmer_user_id") or ""),"seller":_seller_snapshot(order),"buyer":order.get("buyer") or {},"buyer_role":order.get("buyer_role") or "","buyer_type":order.get("buyer_type") or "","buyer_key":order.get("buyer_key") or "","product_name":first.get("product_name"),"variety":first.get("variety"),"grade":first.get("grade"),"quantity":first.get("quantity") if len(items)==1 else 0,"quantity_description":first.get("quantity_description") if len(items)==1 else f"{len(items)} produce items","unit_code":first.get("unit_code") if len(items)==1 else "MULTI","purchase_mode":first.get("purchase_mode") if len(items)==1 else "multi","package":first.get("package") if len(items)==1 else None,"taxable_value":float(total),"gst_rate":0.0,"gst_amount":0.0,"grand_total":float(total),"tax_note":"Farmer-produced output is issued as a non-GST sales receipt in this workflow. GST must not be charged unless a separately verified Farmer GST registration workflow is enabled.","payment_term":order.get("payment_term") or "pay_on_receipt","payment_term_label":PAYMENT_TERMS.get(order.get("payment_term"),"Pay on Receipt"),"due_date":due.isoformat(),"payment_status":"unpaid","amount_paid":0.0,"paid_amount":0.0,"outstanding_amount":float(total),"payment_version":0,"payment_ids":[],"status":"issued","issued_at":now_utc(),"created_at":now_utc(),"updated_at":now_utc()}
        try:r=mongo.db[INVOICE_COLLECTION].insert_one(invoice);invoice["_id"]=r.inserted_id
        except DuplicateKeyError:invoice=mongo.db[INVOICE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or invoice
    if invoice and invoice.get("_id"):
        # Keep the printed receipt synchronized with every produce line.
        invoice_paid = _decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount"))
        invoice_outstanding = max(settlement_total - invoice_paid, Decimal("0"))
        invoice_payment_status = "paid" if invoice_outstanding <= EPSILON else ("partially_paid" if invoice_paid > EPSILON else "unpaid")
        mongo.db[INVOICE_COLLECTION].update_one({"_id": invoice["_id"]}, {"$set": {
            "commerce_version": 2 if order.get("items") else 1,
            "items": items,
            "item_count": len(items),
            "product_name": first.get("product_name"),
            "variety": first.get("variety"),
            "grade": first.get("grade"),
            "quantity": first.get("quantity") if len(items) == 1 else 0,
            "quantity_description": first.get("quantity_description") if len(items) == 1 else f"{len(items)} produce items",
            "unit_code": first.get("unit_code") if len(items) == 1 else "MULTI",
            "taxable_value": float(total),
            "grand_total": float(total),
            "settlement_total": float(settlement_total),
            "accepted_goods_total": float(settlement_total),
            "receipt_adjustment_amount": float(receipt_adjustment),
            "receipt_status": order.get("receipt_status") or "",
            "receipt_finalized": receipt_finalized,
            "amount_paid": float(min(invoice_paid, settlement_total)),
            "paid_amount": float(invoice_paid),
            "outstanding_amount": float(invoice_outstanding),
            "payment_status": invoice_payment_status,
            "updated_at": now_utc(),
        }})
        invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": invoice["_id"]}) or invoice
    receivable=mongo.db[RECEIVABLE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]})
    if receivable and receivable.get("_id"):
        receivable_paid = _decimal(receivable.get("amount_paid"))
        receivable_outstanding = max(settlement_total - receivable_paid, Decimal("0"))
        mongo.db[RECEIVABLE_COLLECTION].update_one({"_id": receivable["_id"]}, {"$set": {
            "invoice_id": invoice.get("_id"),
            "invoice_id_str": str(invoice.get("_id") or ""),
            "document_number": invoice.get("document_number") or "",
            "total_amount": float(settlement_total),
            "original_total_amount": float(total),
            "receipt_adjustment_amount": float(receipt_adjustment),
            "amount_paid": float(min(receivable_paid, settlement_total)),
            "outstanding_amount": float(receivable_outstanding),
            "payment_status": "paid" if receivable_outstanding <= EPSILON else ("partially_paid" if receivable_paid > EPSILON else "unpaid"),
            "status": "closed" if receivable_outstanding <= EPSILON else "open",
            "updated_at": now_utc(),
        }})
        receivable = mongo.db[RECEIVABLE_COLLECTION].find_one({"_id": receivable["_id"]}) or receivable
    if not receivable:
        receivable={"farmer_marketplace_order_id":order["_id"],"farmer_marketplace_order_id_str":str(order["_id"]),"invoice_id":invoice.get("_id"),"invoice_id_str":str(invoice.get("_id") or ""),"document_number":invoice.get("document_number") or "","farmer_user_id":order.get("seller_farmer_user_id"),"farmer_user_id_str":str(order.get("seller_farmer_user_id") or ""),"farmer_name":order.get("seller_farmer_name") or "Farmer","buyer_key":order.get("buyer_key") or "","buyer_type":order.get("buyer_type") or "","buyer_name":(order.get("buyer") or {}).get("name") or "Buyer","total_amount":float(settlement_total),"original_total_amount":float(total),"receipt_adjustment_amount":float(receipt_adjustment),"amount_paid":0.0,"outstanding_amount":float(settlement_total),"payment_status":"unpaid","status":"open","due_date":invoice.get("due_date") or "","created_at":now_utc(),"updated_at":now_utc()}
        try:r=mongo.db[RECEIVABLE_COLLECTION].insert_one(receivable);receivable["_id"]=r.inserted_id
        except DuplicateKeyError:receivable=mongo.db[RECEIVABLE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or receivable
    mongo.db[SALE_COLLECTION].update_one({"_id":sale.get("_id")},{"$set":{"invoice_id":invoice.get("_id"),"document_number":invoice.get("document_number") or "","receivable_id":receivable.get("_id"),"updated_at":now_utc()}})
    sale = mongo.db[SALE_COLLECTION].find_one({"_id": sale.get("_id")}) or sale
    return sale,invoice,receivable


def dispatch_order(actor_user_id, order_id):
    _ensure_indexes();user=_get_user(actor_user_id);oid=_to_object_id(order_id);order=mongo.db[ORDER_COLLECTION].find_one({"_id":oid}) if oid else None
    if not order:raise ValueError("Order was not found.")
    _assert_seller(user,order)
    if order.get("status")=="dispatched":
        sale,invoice,_=_ensure_sale_documents(order);return {"order":serialize_order(order),"sale":serialize_sale(sale),"invoice":serialize_invoice(invoice),"message":"This order is already dispatched."}
    if order.get("status")!="approved":raise ValueError("Only an Approved order can be dispatched.")
    applied,movement_ids=_consume_reservation(order);changed=[]
    try:
        for line in _order_items(order):
            q=_decimal(line.get("base_quantity"));result=mongo.db[LISTING_COLLECTION].update_one({"_id":line.get("listing_id"),"reserved_quantity":{"$gte":float(q)}},{"$inc":{"reserved_quantity":-float(q),"fulfilled_quantity":float(q)},"$set":{"updated_at":now_utc()}})
            if result.modified_count!=1:raise RuntimeError(f"{line.get('product_name') or 'A listing'} reservation is inconsistent. Dispatch was cancelled safely.")
            changed.append((line.get("listing_id"),float(q)))
        timestamp=now_utc();updated_items=[]
        for line in _order_items(order):
            line=dict(line);line["status"]="dispatched";line["dispatched_quantity"]=float(_decimal(line.get("base_quantity")));updated_items.append(line)
        result=mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"],"status":"approved"},{"$set":{"status":"dispatched","items":updated_items if order.get("items") else order.get("items",[]),"stock_allocations":applied,"dispatched_at":timestamp,"dispatched_by":user.get("_id"),"updated_at":timestamp},"$push":{"status_history":{"status":"dispatched","at":timestamp,"by":user.get("resolved_name")}}})
        if result.modified_count!=1:raise RuntimeError("Order changed in another session. Dispatch was cancelled safely.")
    except Exception:
        for a in applied:mongo.db[FARMER_LOT_COLLECTION].update_one({"_id":a.get("lot_id")},{"$inc":{"available_quantity":a.get("quantity"),"reserved_quantity":a.get("quantity"),"sold_quantity":-a.get("quantity")},"$set":{"updated_at":now_utc()}})
        if movement_ids:mongo.db[FARMER_MOVEMENT_COLLECTION].delete_many({"_id":{"$in":movement_ids}})
        for lid,q in changed:mongo.db[LISTING_COLLECTION].update_one({"_id":lid},{"$inc":{"reserved_quantity":q,"fulfilled_quantity":-q},"$set":{"updated_at":now_utc()}})
        raise
    updated=mongo.db[ORDER_COLLECTION].find_one({"_id":order["_id"]}) or order
    try:
        sale,invoice,receivable=_ensure_sale_documents(updated);mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"]},{"$set":{"sale_id":sale.get("_id"),"invoice_id":invoice.get("_id"),"receivable_id":receivable.get("_id"),"financial_status":"ready","updated_at":now_utc()}});warning=""
    except Exception as exc:
        mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"]},{"$set":{"financial_status":"needs_repair","financial_error":_clean(exc,500),"updated_at":now_utc()}});sale=invoice=receivable={};warning=" Produce was dispatched, but the sales receipt needs repair from the order screen."
    _audit(user,"dispatch_order","order",order["_id"],f"Physical stock reduced for {len(_order_items(order))} produce line(s).")
    _notify((order.get("buyer") or {}).get("user_id"),order.get("buyer_role") or "","Produce dispatched",f"{order.get('order_number')} has been dispatched by {order.get('seller_farmer_name')}. Confirm the quantities you actually receive before payment.")
    message="Order dispatched. Buyer must confirm actual receipt before payment becomes due."
    final=mongo.db[ORDER_COLLECTION].find_one({"_id":order["_id"]}) or updated
    return {"order":serialize_order(final),"sale":serialize_sale(sale),"invoice":serialize_invoice(invoice),"message":message+warning}


def confirm_delivery(actor_user_id, order_id):
    """Seller confirms that dispatched produce has reached the buyer location.

    This is intentionally stored as a delivery acknowledgement flag instead of
    introducing a new order status. Existing reporting and receipt logic continue
    to use the stable requested -> approved -> dispatched -> received lifecycle.
    """
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid}) if oid else None
    if not order:
        raise ValueError("Order was not found.")
    _assert_seller(user, order)
    if order.get("status") == "received":
        return {"order": serialize_order(order), "message": "The buyer has already confirmed receipt of this order."}
    if order.get("status") != "dispatched":
        raise ValueError("Mark the order as dispatched before confirming delivery.")
    if order.get("seller_delivery_confirmed") is True:
        return {"order": serialize_order(order), "message": "Delivery is already marked. Waiting for the buyer to confirm receipt."}

    timestamp = now_utc()
    result = mongo.db[ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "status": "dispatched", "seller_delivery_confirmed": {"$ne": True}},
        {
            "$set": {
                "seller_delivery_confirmed": True,
                "seller_delivered_at": timestamp,
                "seller_delivered_by": user.get("_id"),
                "updated_at": timestamp,
            },
            "$push": {"status_history": {"status": "delivered", "at": timestamp, "by": user.get("resolved_name")}},
        },
    )
    if result.modified_count != 1:
        latest = mongo.db[ORDER_COLLECTION].find_one({"_id": order["_id"]}) or order
        if latest.get("seller_delivery_confirmed") is True:
            return {"order": serialize_order(latest), "message": "Delivery is already marked. Waiting for the buyer to confirm receipt."}
        raise RuntimeError("Order changed in another session. Refresh and try again.")

    _audit(user, "confirm_delivery", "order", order["_id"], "Farmer marked the dispatched produce as delivered to the buyer.")
    _notify((order.get("buyer") or {}).get("user_id"), order.get("buyer_role") or "", "Produce delivered", f"{order.get('seller_farmer_name')} marked {order.get('order_number')} as delivered. Please confirm receipt after checking the goods.")
    updated = mongo.db[ORDER_COLLECTION].find_one({"_id": order["_id"]}) or order
    return {"order": serialize_order(updated), "message": "Delivery marked. Waiting for the buyer to confirm receipt."}


def repair_financial_documents(actor_user_id, order_id):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid}) if oid else None
    if not order:
        raise ValueError("Order was not found.")
    _assert_seller(user, order)
    if order.get("status") not in {"dispatched", "received"}:
        raise ValueError("Sales documents can be created only after dispatch.")
    sale, invoice, receivable = _ensure_sale_documents(order)
    patch = {
        "sale_id": sale.get("_id"),
        "invoice_id": invoice.get("_id"),
        "receivable_id": receivable.get("_id"),
        "financial_status": "ready",
        "financial_error": "",
        "updated_at": now_utc(),
    }
    # If goods were already received while financial generation was unavailable,
    # rebuild the buyer-side records now without changing stock again.
    if order.get("status") == "received":
        purchase, payable = _ensure_purchase(order, invoice)
        buyer_stock = _ensure_institutional_buyer_stock(order, purchase)
        patch.update({
            "purchase_id": purchase.get("_id"),
            "payable_id": payable.get("_id"),
            "buyer_stock_lot_ids": [x.get("_id") for x in (buyer_stock or []) if x.get("_id")],
            "buyer_stock_lot_id": ((buyer_stock or [{}])[0]).get("_id") if buyer_stock else None,
        })
    mongo.db[ORDER_COLLECTION].update_one({"_id": order["_id"]}, {"$set": patch})
    _audit(user, "repair_financial_documents", "order", order["_id"], "Farmer marketplace sale and purchase documents synchronized.")
    return {"message": "Sales receipt and linked buyer records are ready.", "invoice": serialize_invoice(invoice)}


def _ensure_purchase(order, invoice):
    purchase=mongo.db[PURCHASE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]});items=[]
    for line in _order_items(order):items.append({"line_id":line.get("line_id") or "legacy","listing_id":line.get("listing_id"),"product_key":line.get("product_key") or "","product_name":line.get("product_name") or "Produce","variety":line.get("variety") or "","grade":line.get("grade") or "","quantity":line.get("accepted_quantity") if line.get("accepted_quantity") is not None else line.get("base_quantity") or 0,"quantity_description":line.get("quantity_description") or "","unit_code":line.get("unit_code") or "KG","purchase_mode":line.get("purchase_mode") or "loose","package":line.get("package"),"unit_price":line.get("unit_price") or 0,"line_total":line.get("accepted_commercial_total") if line.get("accepted_commercial_total") is not None else line.get("line_total") or 0})
    first=items[0]
    if not purchase:
        purchase={"purchase_number":_next_number("farmer_marketplace_purchase","FMPUR"),"farmer_marketplace_order_id":order["_id"],"farmer_marketplace_order_id_str":str(order["_id"]),"order_number":order.get("order_number") or "","invoice_id":invoice.get("_id"),"document_number":invoice.get("document_number") or "","commerce_version":2 if order.get("items") else 1,"items":items,"item_count":len(items),"buyer_role":order.get("buyer_role") or "","buyer_type":order.get("buyer_type") or "","buyer_key":order.get("buyer_key") or "","buyer":order.get("buyer") or {},"seller_farmer_user_id":order.get("seller_farmer_user_id"),"seller_farmer_name":order.get("seller_farmer_name") or "Farmer","seller_centre_uid":order.get("seller_centre_uid") or "","product_key":first.get("product_key"),"product_name":first.get("product_name"),"variety":first.get("variety"),"grade":first.get("grade"),"quantity":first.get("quantity") if len(items)==1 else 0,"quantity_description":first.get("quantity_description") if len(items)==1 else f"{len(items)} produce items","unit_code":first.get("unit_code") if len(items)==1 else "MULTI","purchase_mode":first.get("purchase_mode") if len(items)==1 else "multi","package":first.get("package") if len(items)==1 else None,"total_amount":invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total") or order.get("total_amount") or 0,"payment_status":invoice.get("payment_status") or "unpaid","amount_paid":invoice.get("amount_paid") or 0,"outstanding_amount":invoice.get("outstanding_amount") if invoice.get("outstanding_amount") is not None else invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total") or 0,"status":"received","received_at":now_utc(),"created_at":now_utc(),"updated_at":now_utc()}
        try:r=mongo.db[PURCHASE_COLLECTION].insert_one(purchase);purchase["_id"]=r.inserted_id
        except DuplicateKeyError:purchase=mongo.db[PURCHASE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or purchase
    if purchase and purchase.get("_id"):
        accepted_total = _decimal(invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total"))
        purchase_paid = _decimal(purchase.get("amount_paid"))
        purchase_outstanding = max(accepted_total - purchase_paid, Decimal("0"))
        mongo.db[PURCHASE_COLLECTION].update_one({"_id": purchase["_id"]}, {"$set": {
            "commerce_version": 2 if order.get("items") else 1,
            "items": items, "item_count": len(items),
            "product_key": first.get("product_key"), "product_name": first.get("product_name"),
            "variety": first.get("variety"), "grade": first.get("grade"),
            "quantity": first.get("quantity") if len(items) == 1 else 0,
            "quantity_description": first.get("quantity_description") if len(items) == 1 else f"{len(items)} produce items",
            "unit_code": first.get("unit_code") if len(items) == 1 else "MULTI",
            "total_amount": float(accepted_total),
            "amount_paid": float(min(purchase_paid, accepted_total)),
            "outstanding_amount": float(purchase_outstanding),
            "payment_status": "paid" if purchase_outstanding <= EPSILON else ("partially_paid" if purchase_paid > EPSILON else "unpaid"),
            "status": "received", "updated_at": now_utc(),
        }})
        purchase = mongo.db[PURCHASE_COLLECTION].find_one({"_id": purchase["_id"]}) or purchase
    payable=mongo.db[PAYABLE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]})
    if not payable:
        total=_decimal(invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total"));payable={"farmer_marketplace_order_id":order["_id"],"farmer_marketplace_order_id_str":str(order["_id"]),"purchase_id":purchase.get("_id"),"invoice_id":invoice.get("_id"),"document_number":invoice.get("document_number") or "","buyer_role":order.get("buyer_role") or "","buyer_type":order.get("buyer_type") or "","buyer_key":order.get("buyer_key") or "","buyer_name":(order.get("buyer") or {}).get("name") or "Buyer","seller_farmer_user_id":order.get("seller_farmer_user_id"),"seller_farmer_name":order.get("seller_farmer_name") or "Farmer","total_amount":float(total),"amount_paid":invoice.get("amount_paid") or 0,"outstanding_amount":invoice.get("outstanding_amount") if invoice.get("outstanding_amount") is not None else float(total),"payment_status":invoice.get("payment_status") or "unpaid","status":"closed" if invoice.get("payment_status")=="paid" else "open","due_date":invoice.get("due_date") or "","created_at":now_utc(),"updated_at":now_utc()}
        try:r=mongo.db[PAYABLE_COLLECTION].insert_one(payable);payable["_id"]=r.inserted_id
        except DuplicateKeyError:payable=mongo.db[PAYABLE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or payable
    if payable and payable.get("_id"):
        accepted_total = _decimal(invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total"))
        payable_paid = _decimal(payable.get("amount_paid"))
        payable_outstanding = max(accepted_total - payable_paid, Decimal("0"))
        mongo.db[PAYABLE_COLLECTION].update_one({"_id": payable["_id"]}, {"$set": {
            "total_amount": float(accepted_total),
            "amount_paid": float(min(payable_paid, accepted_total)),
            "outstanding_amount": float(payable_outstanding),
            "payment_status": "paid" if payable_outstanding <= EPSILON else ("partially_paid" if payable_paid > EPSILON else "unpaid"),
            "status": "closed" if payable_outstanding <= EPSILON else "open",
            "updated_at": now_utc(),
        }})
        payable = mongo.db[PAYABLE_COLLECTION].find_one({"_id": payable["_id"]}) or payable
    return purchase,payable


def _ensure_institutional_buyer_stock(order, purchase):
    if order.get("buyer_type") not in {"avpl","ufc"}:return []
    lots=[]
    for line in _order_items(order):
        line_id=line.get("line_id") or "legacy";stock_key=f"{order['_id']}:{line_id}";existing=mongo.db[BUYER_STOCK_COLLECTION].find_one({"stock_key":stock_key})
        if not existing and line_id=="legacy":existing=mongo.db[BUYER_STOCK_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"],"$or":[{"stock_key":{"$exists":False}},{"stock_key":None}]})
        if existing:
            if not existing.get("stock_key"):mongo.db[BUYER_STOCK_COLLECTION].update_one({"_id":existing["_id"]},{"$set":{"stock_key":stock_key,"line_id":line_id}});existing["stock_key"]=stock_key
            lots.append(existing);continue
        q=float(_decimal(line.get("accepted_quantity") if line.get("accepted_quantity") is not None else line.get("base_quantity")));
        if q <= 0: continue
        document={"stock_key":stock_key,"line_id":line_id,"lot_number":_next_number("farmer_marketplace_buyer_stock","FMIN"),"farmer_marketplace_order_id":order["_id"],"purchase_id":purchase.get("_id"),"buyer_type":order.get("buyer_type") or "","buyer_key":order.get("buyer_key") or "","buyer_name":(order.get("buyer") or {}).get("name") or "Buyer","centre_uid":(order.get("buyer") or {}).get("centre_uid") or "","source_farmer_user_id":order.get("seller_farmer_user_id"),"source_farmer_name":order.get("seller_farmer_name") or "Farmer","product_key":line.get("product_key") or "","product_name":line.get("product_name") or "Produce","variety":line.get("variety") or "","grade":line.get("grade") or "","unit_code":line.get("unit_code") or "KG","original_quantity":q,"available_quantity":q,"status":"active","received_at":now_utc(),"created_at":now_utc(),"updated_at":now_utc(),"inventory_note":"Farmer-produce procurement stock. Kept separate from AVPL/UFC input-product inventory until a controlled product-mapping workflow moves/uses it."}
        try:r=mongo.db[BUYER_STOCK_COLLECTION].insert_one(document);document["_id"]=r.inserted_id;lots.append(document)
        except DuplicateKeyError:
            existing=mongo.db[BUYER_STOCK_COLLECTION].find_one({"stock_key":stock_key})
            if existing:lots.append(existing)
    return lots


def _apply_receipt_settlement(order, receipt_lines, summary):
    """Keep the seller's dispatch receipt intact while making buyer payable equal accepted goods."""
    invoice=mongo.db[INVOICE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or {}
    sale=mongo.db[SALE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or {}
    if not invoice:
        sale,invoice,_=_ensure_sale_documents(order)
    original_total=_decimal(invoice.get("grand_total") or order.get("total_amount"))
    dispatched_value=_decimal(summary.get("dispatched_value"))
    accepted_value=_decimal(summary.get("accepted_value"))
    if dispatched_value > EPSILON and original_total > 0:
        settlement=(original_total * accepted_value / dispatched_value).quantize(MONEY_QUANTUM,rounding=ROUND_HALF_UP)
    else:
        settlement=Decimal("0")
    paid=min(_decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount")),settlement)
    outstanding=max(settlement-paid,Decimal("0"))
    payment_status="paid" if outstanding <= EPSILON else ("partially_paid" if paid > EPSILON else "unpaid")
    patch={"receipt_finalized":True,"receipt_status":summary.get("receipt_status"),"receipt_lines":receipt_lines,"settlement_total":float(settlement),"accepted_goods_total":float(settlement),"receipt_adjustment_amount":float(max(original_total-settlement,Decimal("0"))),"amount_paid":float(paid),"paid_amount":float(paid),"outstanding_amount":float(outstanding),"payment_status":payment_status,"updated_at":now_utc()}
    if invoice.get("_id"):mongo.db[INVOICE_COLLECTION].update_one({"_id":invoice["_id"]},{"$set":patch})
    if sale.get("_id"):mongo.db[SALE_COLLECTION].update_one({"_id":sale["_id"]},{"$set":{k:v for k,v in patch.items() if k not in {"paid_amount","receipt_lines"}}})
    mongo.db[RECEIVABLE_COLLECTION].update_one({"farmer_marketplace_order_id":order["_id"]},{"$set":{"total_amount":float(settlement),"amount_paid":float(paid),"outstanding_amount":float(outstanding),"payment_status":payment_status,"status":"closed" if outstanding<=EPSILON else "open","receipt_adjustment_amount":float(max(original_total-settlement,Decimal("0"))),"updated_at":now_utc()}})
    return mongo.db[INVOICE_COLLECTION].find_one({"_id":invoice.get("_id")}) or {**invoice,**patch}


def receive_order(actor_user_id, order_id, receipt_lines=None, receipt_note=""):
    _ensure_indexes()
    user=_get_user(actor_user_id); oid=_to_object_id(order_id); order=mongo.db[ORDER_COLLECTION].find_one({"_id":oid}) if oid else None
    if not order: raise ValueError("Order was not found.")
    _assert_buyer(user,order)
    if order.get("status")=="received":
        invoice=mongo.db[INVOICE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or {}; purchase=mongo.db[PURCHASE_COLLECTION].find_one({"farmer_marketplace_order_id":order["_id"]}) or {}
        return {"order":serialize_order(order),"purchase":serialize_purchase(purchase),"invoice":serialize_invoice(invoice),"message":"This order is already received."}
    if order.get("status")!="dispatched": raise ValueError("Only a dispatched order can be received.")
    rows=normalize_receipt_lines(_order_items(order),receipt_lines,dispatched_fields=("dispatched_quantity","base_quantity"),allow_legacy_full_receipt=receipt_lines is None)
    summary=summarize_receipt(rows); timestamp=now_utc()
    # Save the buyer's physical fact first. Financial/stock repair can then be retried safely.
    patch={"status":"received","items":rows,"receipt_status":summary.get("receipt_status"),"receipt_note":_clean(receipt_note,1000),"received_item_count":summary.get("received_item_count"),"accepted_item_count":summary.get("accepted_item_count"),"discrepancy_item_count":summary.get("discrepancy_item_count"),"accepted_goods_value":summary.get("accepted_value"),"receipt_adjustment_amount":summary.get("adjustment_value"),"received_at":timestamp,"received_by":user.get("_id"),"received_by_name":user.get("resolved_name") or "","financial_status":"pending","updated_at":timestamp}
    result=mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"],"status":"dispatched"},{"$set":patch,"$push":{"status_history":{"status":"received","at":timestamp,"by":user.get("resolved_name")}}})
    if result.modified_count!=1: raise RuntimeError("Order changed while confirming receipt. Refresh before trying again.")
    received_order=mongo.db[ORDER_COLLECTION].find_one({"_id":order["_id"]}) or {**order,**patch}
    purchase=payable={};buyer_stock=[];invoice={};financial_error=""
    try:
        invoice=_apply_receipt_settlement(received_order,rows,summary)
        # Re-sync seller documents after receipt so their item lines carry the
        # accepted/damaged/rejected/missing facts used by printouts and reports.
        _sale, invoice, _receivable = _ensure_sale_documents(received_order)
        purchase,payable=_ensure_purchase(received_order,invoice)
        # synchronize payable if it already existed before this receipt
        settlement=_decimal(invoice.get("settlement_total") if invoice.get("settlement_total") is not None else invoice.get("grand_total")); paid=_decimal(invoice.get("amount_paid")); outstanding=max(settlement-paid,Decimal("0")); pstatus="paid" if outstanding<=EPSILON else ("partially_paid" if paid>EPSILON else "unpaid")
        mongo.db[PAYABLE_COLLECTION].update_one({"farmer_marketplace_order_id":order["_id"]},{"$set":{"total_amount":float(settlement),"amount_paid":float(paid),"outstanding_amount":float(outstanding),"payment_status":pstatus,"status":"closed" if outstanding<=EPSILON else "open","receipt_adjustment_amount":float(_decimal(invoice.get("receipt_adjustment_amount"))),"updated_at":now_utc()}},upsert=False)
        buyer_stock=_ensure_institutional_buyer_stock(received_order,purchase); financial_status="ready"
    except Exception as exc:
        financial_status="needs_repair"; financial_error=_clean(exc,500)
    mongo.db[ORDER_COLLECTION].update_one({"_id":order["_id"]},{"$set":{"purchase_id":(purchase or {}).get("_id"),"payable_id":(payable or {}).get("_id"),"buyer_stock_lot_ids":[x.get("_id") for x in buyer_stock if x.get("_id")],"buyer_stock_lot_id":((buyer_stock or [{}])[0]).get("_id") if buyer_stock else None,"financial_status":financial_status,"financial_error":financial_error,"updated_at":now_utc()}})
    discrepancy_text=receipt_issue_summary(rows) if summary.get("receipt_status")=="discrepancy" else ""
    audit_note=discrepancy_text or f"Buyer received {summary.get('accepted_item_count',0)} accepted line(s); no discrepancy."
    _audit(user,"receive_order","order",order["_id"],audit_note)
    seller_message=f"{(order.get('buyer') or {}).get('name')} confirmed receipt of {order.get('order_number')}. Payment is based on accepted quantity."
    if discrepancy_text:
        seller_message += f" {discrepancy_text}"
    _notify(order.get("seller_farmer_user_id"),"farmer","Produce received",seller_message)
    final=mongo.db[ORDER_COLLECTION].find_one({"_id":order["_id"]}) or received_order
    message="Receipt confirmed. Only accepted quantity entered buyer stock and payment is based on accepted value." if financial_status=="ready" else "Receipt confirmed safely. Financial documents need repair; receipt quantities were not lost."
    return {"order":serialize_order(final),"purchase":serialize_purchase(purchase),"invoice":serialize_invoice(invoice),"message":message}


def serialize_order(order):
    if not order:return None
    row=dict(order); row["id"]=str(row.get("_id") or ""); row["listing_id_str"]=str(row.get("listing_id") or ""); row["invoice_id_str"]=str(row.get("invoice_id") or ""); row["sale_id_str"]=str(row.get("sale_id") or ""); row["purchase_id_str"]=str(row.get("purchase_id") or "")
    row["total_display"]=_money(row.get("total_amount")); row["amount_paid_display"]=_money(row.get("amount_paid")); row["outstanding_display"]=_money(row.get("outstanding_amount") if row.get("outstanding_amount") is not None else row.get("total_amount")); row["base_quantity_display"]=_qty(row.get("base_quantity")); row["requested_quantity_display"]=_qty(row.get("requested_quantity"))
    row["status_label"]=ORDER_STATUS_LABELS.get(row.get("status"),str(row.get("status") or "").replace("_"," ").title()); row["buyer_role_label"]=BUYER_ROLE_LABELS.get(row.get("buyer_role"),row.get("buyer_type","Buyer").replace("_"," ").title()); row["payment_term_label"]=PAYMENT_TERMS.get(row.get("payment_term"),str(row.get("payment_term") or "").replace("_"," ").title())
    row["items"]=[_serialize_order_item(x) for x in _order_items(row)]; row["item_count"]=len(row["items"]); row["is_multi_item_order"]=row.get("is_multi_item_order") is True or row["item_count"]>1; row["product_summary"]=row["items"][0].get("product_name") if row["item_count"]==1 else f"{row['item_count']} produce items"
    row["dispatched_item_count"]=sum(1 for x in row["items"] if _decimal(x.get("dispatched_quantity") if x.get("dispatched_quantity") is not None else x.get("base_quantity"))>0); row["received_item_count"]=sum(1 for x in row["items"] if _decimal(x.get("physically_received_quantity"))>0); row["accepted_item_count"]=sum(1 for x in row["items"] if _decimal(x.get("accepted_quantity"))>0); row["discrepancy_item_count"]=sum(1 for x in row["items"] if _decimal(x.get("discrepancy_quantity"))>0)
    row["receipt_status_label"]=receipt_label(row.get("receipt_status")) if row.get("receipt_status") else ""; row["accepted_goods_value_display"]=_money(row.get("accepted_goods_value")); row["receipt_adjustment_amount_display"]=_money(row.get("receipt_adjustment_amount"))
    if row.get("status")=="requested":row["next_step_label"]="Waiting for Farmer approval"
    elif row.get("status")=="approved":row["next_step_label"]="Farmer to dispatch produce"
    elif row.get("status")=="dispatched":row["next_step_label"]="Buyer to confirm actual receipt"
    elif row.get("status")=="received":row["next_step_label"]="Payment / settlement"
    else:row["next_step_label"]=row.get("status_label")
    return row


def _serialize_financial_items(items):
    rows = []
    for source in items or []:
        if not isinstance(source, dict):
            continue
        item = dict(source)
        item["quantity_display"] = _qty(item.get("quantity") if item.get("quantity") is not None else item.get("base_quantity"))
        item["base_quantity_display"] = _qty(item.get("base_quantity") if item.get("base_quantity") is not None else item.get("quantity"))
        item["unit_price_display"] = _money(item.get("unit_price"))
        item["line_total_display"] = _money(item.get("line_total") if item.get("line_total") is not None else item.get("total_amount"))
        rows.append(item)
    return rows


def _serialize_commerce_item(item):
    row = dict(item or {})
    row["listing_id_str"] = str(row.get("listing_id") or "")
    row["quantity_display"] = _qty(row.get("quantity") if row.get("quantity") is not None else row.get("base_quantity"))
    row["base_quantity_display"] = _qty(row.get("base_quantity") if row.get("base_quantity") is not None else row.get("quantity"))
    row["unit_price_display"] = _money(row.get("unit_price"))
    row["line_total_display"] = _money(row.get("line_total"))
    row["dispatched_quantity_display"] = _qty(row.get("dispatched_quantity") if row.get("dispatched_quantity") is not None else row.get("quantity"))
    row["received_quantity_display"] = _qty(row.get("received_quantity"))
    row["accepted_quantity_display"] = _qty(row.get("accepted_quantity"))
    row["damaged_quantity_display"] = _qty(row.get("damaged_quantity"))
    row["rejected_quantity_display"] = _qty(row.get("rejected_quantity"))
    row["missing_quantity_display"] = _qty(row.get("missing_quantity"))
    row["accepted_commercial_total_display"] = _money(row.get("accepted_commercial_total"))
    return row


def serialize_sale(sale):
    if not sale:
        return None
    row = dict(sale)
    row["id"] = str(row.get("_id") or "")
    row["invoice_id_str"] = str(row.get("invoice_id") or "")
    row["farmer_marketplace_order_id_str"] = str(row.get("farmer_marketplace_order_id") or "")
    row["total_display"] = _money(row.get("total_amount"))
    row["amount_paid_display"] = _money(row.get("amount_paid"))
    row["outstanding_display"] = _money(row.get("outstanding_amount"))
    row["quantity_display"] = _qty(row.get("quantity"))
    row["payment_status_label"] = str(row.get("payment_status") or "unpaid").replace("_", " ").title()
    row["items"] = [_serialize_commerce_item(x) for x in (row.get("items") or []) if isinstance(x, dict)]
    row["item_count"] = len(row["items"]) or int(row.get("item_count") or 1)
    row["is_multi_item_order"] = row["item_count"] > 1
    row["product_summary"] = row["items"][0].get("product_name") if len(row["items"]) == 1 else (f"{row['item_count']} produce items" if row["item_count"] > 1 else row.get("product_name") or "Produce")
    return row


def serialize_invoice(invoice):
    if not invoice:
        return None
    row = dict(invoice)
    row["id"] = str(row.get("_id") or "")
    row["total_display"] = _money(row.get("settlement_total") if row.get("settlement_total") is not None else row.get("grand_total")); row["original_total_display"] = _money(row.get("grand_total")); row["receipt_adjustment_display"] = _money(row.get("receipt_adjustment_amount")); row["receipt_status_label"] = receipt_label(row.get("receipt_status")) if row.get("receipt_status") else ""
    paid_value = row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount")
    row["paid_display"] = _money(paid_value)
    row["outstanding_display"] = _money(row.get("outstanding_amount"))
    row["quantity_display"] = _qty(row.get("quantity"))
    row["payment_status_label"] = str(row.get("payment_status") or "unpaid").replace("_", " ").title()
    row["items"] = [_serialize_commerce_item(x) for x in (row.get("items") or []) if isinstance(x, dict)]
    row["item_count"] = len(row["items"]) or int(row.get("item_count") or 1)
    row["is_multi_item_order"] = row["item_count"] > 1
    row["product_summary"] = row["items"][0].get("product_name") if len(row["items"]) == 1 else (f"{row['item_count']} produce items" if row["item_count"] > 1 else row.get("product_name") or "Produce")
    return row


def serialize_purchase(purchase):
    if not purchase:
        return None
    row = dict(purchase)
    row["id"] = str(row.get("_id") or "")
    row["invoice_id_str"] = str(row.get("invoice_id") or "")
    row["farmer_marketplace_order_id_str"] = str(row.get("farmer_marketplace_order_id") or "")
    row["total_display"] = _money(row.get("total_amount"))
    row["amount_paid_display"] = _money(row.get("amount_paid"))
    row["outstanding_display"] = _money(row.get("outstanding_amount"))
    row["quantity_display"] = _qty(row.get("quantity"))
    row["payment_status_label"] = str(row.get("payment_status") or "unpaid").replace("_", " ").title()
    row["items"] = [_serialize_commerce_item(x) for x in (row.get("items") or []) if isinstance(x, dict)]
    row["item_count"] = len(row["items"]) or int(row.get("item_count") or 1)
    row["is_multi_item_order"] = row["item_count"] > 1
    row["product_summary"] = row["items"][0].get("product_name") if len(row["items"]) == 1 else (f"{row['item_count']} produce items" if row["item_count"] > 1 else row.get("product_name") or "Produce")
    return row


def _order_access_query(user, side):
    role = user.get("resolved_role") or ""
    if side == "seller":
        if role != "farmer":
            raise PermissionError("Only Farmers can view Orders Received.")
        return {"seller_farmer_user_id": user.get("_id")}
    buyer = _buyer_snapshot(user)
    return {"buyer_key": buyer.get("key")}


def get_orders(actor_user_id, *, side="buyer", search=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    query = _order_access_query(user, side)
    q = _clean(search, 120)
    if q:
        query["$or"] = [
            {"order_number": {"$regex": re.escape(q), "$options": "i"}},
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"items.product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"seller_farmer_name": {"$regex": re.escape(q), "$options": "i"}},
            {"buyer.name": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = [serialize_order(x) for x in mongo.db[ORDER_COLLECTION].find(query).sort("created_at", DESCENDING).limit(200)]
    return {
        "viewer": {"role": user.get("resolved_role"), "name": user.get("resolved_name")},
        "side": side,
        "rows": rows,
        "query": q,
        "summary": {
            "total": len(rows),
            "requested": sum(1 for r in rows if r.get("status") == "requested"),
            "approved": sum(1 for r in rows if r.get("status") == "approved"),
            "dispatched": sum(1 for r in rows if r.get("status") == "dispatched"),
            "received": sum(1 for r in rows if r.get("status") == "received"),
        },
    }


def get_order_detail(actor_user_id, order_id):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(order_id)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid}) if oid else None
    if not order:
        raise ValueError("Order was not found.")
    seller = user.get("resolved_role") == "farmer" and str(order.get("seller_farmer_user_id") or "") == str(user.get("_id"))
    buyer = False
    if not seller:
        try:
            _assert_buyer(user, order)
            buyer = True
        except (PermissionError, ValueError):
            buyer = False
    if not seller and not buyer:
        raise PermissionError("You do not have access to this order.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"farmer_marketplace_order_id": order["_id"]}) or {}
    sale = mongo.db[SALE_COLLECTION].find_one({"farmer_marketplace_order_id": order["_id"]}) or {}
    purchase = mongo.db[PURCHASE_COLLECTION].find_one({"farmer_marketplace_order_id": order["_id"]}) or {}
    listing = mongo.db[LISTING_COLLECTION].find_one({"_id": order.get("listing_id")}) or {}
    return {
        "order": serialize_order(order),
        "invoice": serialize_invoice(invoice),
        "sale": serialize_sale(sale),
        "purchase": serialize_purchase(purchase),
        "listing": serialize_listing(listing, user.get("_id")) if listing else None,
        "is_seller": seller,
        "is_buyer": buyer,
        "payment_token": f"FMPAY-{uuid4().hex.upper()}",
    }


def get_purchases(actor_user_id, search=""):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    buyer = _buyer_snapshot(user)
    query = {"buyer_key": buyer.get("key")}
    q = _clean(search, 120)
    if q:
        query["$or"] = [
            {"purchase_number": {"$regex": re.escape(q), "$options": "i"}},
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"items.product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"seller_farmer_name": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = [serialize_purchase(x) for x in mongo.db[PURCHASE_COLLECTION].find(query).sort("received_at", DESCENDING).limit(200)]
    stock_rows = []
    if buyer.get("type") in {"avpl", "ufc"}:
        for x in mongo.db[BUYER_STOCK_COLLECTION].find({"buyer_key": buyer.get("key"), "status": "active"}).sort("received_at", DESCENDING).limit(200):
            row = dict(x)
            row["id"] = str(row.get("_id") or "")
            row["quantity_display"] = _qty(row.get("available_quantity"))
            stock_rows.append(row)
    return {"buyer": buyer, "rows": rows, "stock_rows": stock_rows, "query": q}


def get_sales(actor_user_id, search=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    q = _clean(search, 120)
    query = {"seller_farmer_user_id": profile["user_id"]}
    if q:
        query["$or"] = [
            {"sale_number": {"$regex": re.escape(q), "$options": "i"}},
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"items.product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"buyer.name": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = [serialize_sale(x) for x in mongo.db[SALE_COLLECTION].find(query).sort("created_at", DESCENDING).limit(200)]
    total = sum((_decimal(x.get("total_amount")) for x in mongo.db[SALE_COLLECTION].find({"seller_farmer_user_id": profile["user_id"], "status": "completed"})), Decimal("0"))
    paid = sum((_decimal(x.get("amount_paid")) for x in mongo.db[SALE_COLLECTION].find({"seller_farmer_user_id": profile["user_id"], "status": "completed"})), Decimal("0"))
    return {"farmer": profile, "rows": rows, "query": q, "summary": {"count": len(rows), "sales_value": _money(total), "received": _money(paid), "outstanding": _money(max(total-paid, Decimal('0')))}}


def get_invoice_print_context(actor_user_id, invoice_id):
    _ensure_indexes()
    user = _get_user(actor_user_id)
    oid = _to_object_id(invoice_id)
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": oid}) if oid else None
    if not invoice:
        raise ValueError("Sales receipt was not found.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": invoice.get("farmer_marketplace_order_id")}) or {}
    seller = user.get("resolved_role") == "farmer" and str(invoice.get("farmer_user_id") or "") == str(user.get("_id"))
    buyer = False
    if not seller and order:
        try:
            _assert_buyer(user, order)
            buyer = True
        except Exception:
            buyer = False
    if not seller and not buyer:
        raise PermissionError("You do not have access to this sales receipt.")
    return {"invoice": serialize_invoice(invoice), "order": serialize_order(order), "seller": invoice.get("seller") or {}, "buyer": invoice.get("buyer") or {}}
