from __future__ import annotations
from app.utils.timezone import business_today

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from app.extensions import mongo
from app.utils.helpers import now_utc


LISTING_COLLECTION = "ufc_farmer_marketplace_listings"
AUDIT_COLLECTION = "ufc_farmer_marketplace_audit"
UFC_LOT_COLLECTION = "ufc_inventory_lots"


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _decimal(value, default="0"):
    try:
        parsed = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return parsed if parsed.is_finite() else Decimal(default)


def _money(value):
    return f"{_decimal(value).quantize(Decimal('0.01')):.2f}"


def _qty(value):
    number = _decimal(value)
    text = f"{number.quantize(Decimal('0.0001')):f}".rstrip("0").rstrip(".")
    return text or "0"


def _clean_text(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _is_active_user(user):
    return not (
        user.get("active", True) is False
        or user.get("is_active", True) is False
        or str(user.get("status") or "").strip().lower() == "inactive"
    )


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


def _ensure_indexes():
    definitions = [
        (
            mongo.db[LISTING_COLLECTION],
            [("centre_uid", ASCENDING), ("source_product_id", ASCENDING)],
            {"name": "ufc_farmer_listing_centre_product_unique", "unique": True},
        ),
        (
            mongo.db[LISTING_COLLECTION],
            [("centre_uid", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
            {"name": "ufc_farmer_listing_centre_status_idx"},
        ),
        (
            mongo.db[AUDIT_COLLECTION],
            [("centre_uid", ASCENDING), ("created_at", DESCENDING)],
            {"name": "ufc_farmer_marketplace_audit_centre_idx"},
        ),
    ]
    for collection, keys, options in definitions:
        try:
            collection.create_index(keys, **options)
        except Exception:
            # Local/dev Mongo installations may restrict index changes. Do not
            # make the operational page unavailable because of that.
            pass


# ---------------------------------------------------------------------------
# Identity and ownership resolution
# ---------------------------------------------------------------------------


def _get_user(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Invalid authenticated user.")
    user = mongo.db.users.find_one({"_id": oid})
    if not user:
        raise ValueError("Authenticated user was not found.")
    if not _is_active_user(user):
        raise PermissionError("Inactive users cannot use the marketplace.")
    user["resolved_role"] = str(user.get("role") or "").strip().lower()
    user["resolved_name"] = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or user.get("phone")
        or user["resolved_role"].replace("_", " ").title()
    )
    return user


def _resolve_ufc_admin(actor_user_id, centre_uid_hint=None):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "ufc_admin":
        raise PermissionError("Only UFC Admin can manage the Farmer Marketplace.")

    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor["_id"]})
        or {}
    )
    centre_uid = _clean_text(
        master.get("centre_uid")
        or actor.get("centre_uid")
        or actor.get("mapped_centre_uid")
        or centre_uid_hint,
        80,
    )
    hinted = _clean_text(centre_uid_hint, 80)
    if hinted and centre_uid and hinted != centre_uid:
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
    return actor, centre_uid, centre_name


def _resolve_farmer(actor_user_id):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "farmer":
        raise PermissionError("Only Farmers can open the Farmer Marketplace.")

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.farmer_master.find_one({"linked_user_id": actor["_id"]})
        or mongo.db.farmer_master.find_one({"contact_no": actor.get("phone")})
        or {}
    )
    centre_uid = _clean_text(
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


# ---------------------------------------------------------------------------
# Stock source of truth
# ---------------------------------------------------------------------------


def _stock_for_product(centre_uid, product_id):
    product_oid = _to_object_id(product_id)
    if not product_oid:
        return None

    physical = Decimal("0")
    reserved = Decimal("0")
    damaged = Decimal("0")
    blocked = Decimal("0")
    purchase_cost_total = Decimal("0")
    received_total = Decimal("0")
    batch_count = 0
    product_name = "Product"
    product_code = ""
    category = ""
    product_role = ""
    unit_code = "Unit"

    found = False
    for lot in mongo.db[UFC_LOT_COLLECTION].find({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
        "status": {"$ne": "cancelled"},
    }):
        found = True
        batch_count += 1
        lot_physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        lot_reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
        lot_damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
        lot_blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
        lot_expired = lot_physical if _lot_expired(lot) else Decimal("0")

        physical += lot_physical
        reserved += lot_reserved
        damaged += lot_damaged
        blocked += lot_blocked + lot_expired
        purchase_cost_total += _decimal(lot.get("purchase_cost_total"))
        received_total += max(_decimal(lot.get("received_quantity")), Decimal("0"))

        product_name = lot.get("product_name") or product_name
        product_code = lot.get("product_code") or product_code
        category = lot.get("category") or category
        product_role = lot.get("product_role") or product_role
        unit_code = lot.get("unit_code") or unit_code

    if not found:
        return None

    saleable = max(physical - reserved - damaged - blocked, Decimal("0"))
    wac = purchase_cost_total / received_total if received_total > 0 else Decimal("0")

    return {
        "product_id": str(product_oid),
        "source_product_id": product_oid,
        "product_name": product_name,
        "product_code": product_code or "-",
        "category": category or "-",
        "product_role": str(product_role or "-").replace("_", " ").title(),
        "unit_code": unit_code,
        "physical": physical,
        "reserved": reserved,
        "damaged": damaged,
        "blocked": blocked,
        "saleable": saleable,
        "batch_count": batch_count,
        "wac": wac,
        "physical_quantity": _qty(physical),
        "reserved_quantity": _qty(reserved),
        "blocked_quantity": _qty(blocked + damaged),
        "saleable_quantity": _qty(saleable),
        "weighted_average_cost": _money(wac),
    }


def _all_stock_rows(centre_uid):
    ids = mongo.db[UFC_LOT_COLLECTION].distinct(
        "source_product_id",
        {"centre_uid": centre_uid, "status": {"$ne": "cancelled"}},
    )
    rows = []
    for product_id in ids:
        row = _stock_for_product(centre_uid, product_id)
        if row:
            rows.append(row)
    rows.sort(key=lambda item: str(item.get("product_name") or "").lower())
    return rows


def _product_master(product_id):
    oid = _to_object_id(product_id)
    if not oid:
        return {}
    return mongo.db.products.find_one({"_id": oid}) or {}


def _image_value(product):
    return (
        product.get("image_name")
        or product.get("picture")
        or product.get("product_image")
        or product.get("image")
        or product.get("image_url")
        or product.get("file_path")
        or product.get("filename")
        or ""
    )


# ---------------------------------------------------------------------------
# UFC setup and publication actions
# ---------------------------------------------------------------------------


def _audit(centre_uid, actor, action, product_id, details=None):
    mongo.db[AUDIT_COLLECTION].insert_one({
        "audit_uid": uuid4().hex,
        "centre_uid": centre_uid,
        "actor_user_id": actor.get("_id"),
        "actor_name": actor.get("resolved_name") or "UFC Admin",
        "action": action,
        "source_product_id": _to_object_id(product_id),
        "details": details or {},
        "created_at": now_utc(),
    })


def save_product_selling_setup(
    actor_user_id,
    centre_uid_hint,
    product_id,
    *,
    selling_price,
    min_order_quantity=1,
    max_order_quantity=0,
    notes="",
):
    _ensure_indexes()
    actor, centre_uid, centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    stock = _stock_for_product(centre_uid, product_id)
    if not stock:
        raise ValueError("This product is not part of your UFC stock.")

    price = _decimal(selling_price)
    minimum = _decimal(min_order_quantity, "1")
    maximum = _decimal(max_order_quantity, "0")

    if price <= 0:
        raise ValueError("Farmer selling price must be greater than zero.")
    if minimum <= 0:
        raise ValueError("Minimum order quantity must be greater than zero.")
    if maximum < 0:
        raise ValueError("Maximum order quantity cannot be negative.")
    if maximum > 0 and maximum < minimum:
        raise ValueError("Maximum order quantity cannot be lower than the minimum order quantity.")

    product_oid = stock["source_product_id"]
    product = _product_master(product_oid)
    existing = mongo.db[LISTING_COLLECTION].find_one({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
    }) or {}
    status = existing.get("status") if existing.get("status") in {"published", "unpublished"} else "draft"
    timestamp = now_utc()

    update = {
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "source_product_id": product_oid,
        "source_product_id_str": str(product_oid),
        "product_name": stock.get("product_name") or product.get("name") or "Product",
        "product_code": stock.get("product_code") or product.get("product_code") or "",
        "category": stock.get("category") or product.get("category") or "",
        "product_role": stock.get("product_role") or product.get("product_role") or product.get("type") or "",
        "unit_code": stock.get("unit_code") or product.get("base_unit") or product.get("unit") or "Unit",
        "selling_price": float(price),
        "min_order_quantity": float(minimum),
        "max_order_quantity": float(maximum),
        "notes": _clean_text(notes, 500),
        "status": status,
        "updated_by": actor["_id"],
        "updated_by_name": actor.get("resolved_name") or "UFC Admin",
        "updated_at": timestamp,
    }
    mongo.db[LISTING_COLLECTION].update_one(
        {"centre_uid": centre_uid, "source_product_id": product_oid},
        {
            "$set": update,
            "$setOnInsert": {
                "listing_uid": uuid4().hex,
                "created_by": actor["_id"],
                "created_at": timestamp,
            },
        },
        upsert=True,
    )
    _audit(
        centre_uid,
        actor,
        "save_selling_setup",
        product_oid,
        {
            "selling_price": float(price),
            "min_order_quantity": float(minimum),
            "max_order_quantity": float(maximum),
        },
    )
    return mongo.db[LISTING_COLLECTION].find_one({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
    })


def bulk_update_publication(actor_user_id, centre_uid_hint, product_ids, action):
    _ensure_indexes()
    actor, centre_uid, _centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    action = str(action or "").strip().lower()
    if action not in {"publish", "unpublish"}:
        raise ValueError("Invalid marketplace action.")

    product_oids = []
    seen = set()
    for value in product_ids or []:
        oid = _to_object_id(value)
        if oid and oid not in seen:
            product_oids.append(oid)
            seen.add(oid)
    if not product_oids:
        raise ValueError("Select at least one UFC stock product.")

    # Pre-validate all rows before changing anything so a bulk action never
    # half-publishes because one selected product has incomplete setup.
    prepared = []
    errors = []
    for oid in product_oids:
        stock = _stock_for_product(centre_uid, oid)
        if not stock:
            errors.append(f"{oid}: not found in UFC stock")
            continue
        listing = mongo.db[LISTING_COLLECTION].find_one({
            "centre_uid": centre_uid,
            "source_product_id": oid,
        })
        if action == "publish":
            if not listing or _decimal(listing.get("selling_price")) <= 0:
                errors.append(f"{stock.get('product_name')}: set the Farmer selling price first")
                continue
        prepared.append((oid, stock, listing or {}))

    if errors:
        raise ValueError("Cannot publish selected products. " + "; ".join(errors[:8]))

    timestamp = now_utc()
    changed = 0
    for oid, stock, listing in prepared:
        if action == "publish":
            mongo.db[LISTING_COLLECTION].update_one(
                {"centre_uid": centre_uid, "source_product_id": oid},
                {"$set": {
                    "status": "published",
                    "published_at": timestamp,
                    "published_by": actor["_id"],
                    "published_by_name": actor.get("resolved_name") or "UFC Admin",
                    "updated_at": timestamp,
                }},
            )
            _audit(centre_uid, actor, "publish_to_farmers", oid, {
                "saleable_quantity_at_publication": float(stock.get("saleable") or 0),
            })
        else:
            mongo.db[LISTING_COLLECTION].update_one(
                {"centre_uid": centre_uid, "source_product_id": oid},
                {"$set": {
                    "status": "unpublished",
                    "unpublished_at": timestamp,
                    "unpublished_by": actor["_id"],
                    "unpublished_by_name": actor.get("resolved_name") or "UFC Admin",
                    "updated_at": timestamp,
                }},
            )
            _audit(centre_uid, actor, "remove_from_farmer_marketplace", oid)
        changed += 1

    return {
        "changed": changed,
        "action": action,
        "message": (
            f"{changed} product(s) published to mapped Farmers."
            if action == "publish"
            else f"{changed} product(s) removed from the Farmer Marketplace."
        ),
    }


def get_ufc_marketplace_setup(actor_user_id, centre_uid_hint, *, search="", status_filter="all"):
    _ensure_indexes()
    _actor, centre_uid, centre_name = _resolve_ufc_admin(actor_user_id, centre_uid_hint)
    stock_rows = _all_stock_rows(centre_uid)
    product_ids = [row["source_product_id"] for row in stock_rows]
    listings = {
        str(item.get("source_product_id")): item
        for item in mongo.db[LISTING_COLLECTION].find({
            "centre_uid": centre_uid,
            "source_product_id": {"$in": product_ids} if product_ids else {"$in": []},
        })
    }
    product_docs = {
        str(item["_id"]): item
        for item in mongo.db.products.find({"_id": {"$in": product_ids}})
    } if product_ids else {}

    rows = []
    for stock in stock_rows:
        key = str(stock["source_product_id"])
        listing = listings.get(key) or {}
        product = product_docs.get(key) or {}
        selling_price = _decimal(listing.get("selling_price"))
        wac = stock.get("wac") or Decimal("0")
        margin = selling_price - wac if selling_price > 0 else Decimal("0")
        margin_percent = (margin / selling_price * Decimal("100")) if selling_price > 0 else Decimal("0")
        listing_status = listing.get("status") or "not_configured"
        rows.append({
            **stock,
            "listing_id": str(listing.get("_id") or ""),
            "listing_status": listing_status,
            "is_published": listing_status == "published",
            "selling_price": _money(selling_price) if selling_price > 0 else "",
            "selling_price_value": float(selling_price) if selling_price > 0 else 0,
            "min_order_quantity": _qty(listing.get("min_order_quantity") or 1),
            "max_order_quantity": _qty(listing.get("max_order_quantity") or 0),
            "notes": listing.get("notes") or "",
            "margin_amount": _money(margin),
            "margin_percent": _money(margin_percent),
            "availability_status": "in_stock" if stock.get("saleable", Decimal("0")) > 0 else "out_of_stock",
            "image": _image_value(product),
        })

    text = _clean_text(search, 120).lower()
    status_filter = str(status_filter or "all").strip().lower()
    if text:
        rows = [
            row for row in rows
            if text in str(row.get("product_name") or "").lower()
            or text in str(row.get("product_code") or "").lower()
            or text in str(row.get("category") or "").lower()
        ]
    if status_filter == "published":
        rows = [row for row in rows if row.get("is_published")]
    elif status_filter == "not_published":
        rows = [row for row in rows if not row.get("is_published")]
    elif status_filter == "needs_price":
        rows = [row for row in rows if not row.get("selling_price_value")]
    elif status_filter == "out_of_stock":
        rows = [row for row in rows if row.get("availability_status") == "out_of_stock"]

    all_listing_values = list(listings.values())
    published_count = sum(1 for item in all_listing_values if item.get("status") == "published")
    needs_price = sum(
        1 for stock in stock_rows
        if _decimal((listings.get(str(stock["source_product_id"])) or {}).get("selling_price")) <= 0
    )
    out_of_stock = sum(1 for stock in stock_rows if stock.get("saleable", Decimal("0")) <= 0)

    return {
        "rows": rows,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "query": search or "",
        "selected_status": status_filter or "all",
        "summary": {
            "stock_products": len(stock_rows),
            "published": published_count,
            "needs_price": needs_price,
            "out_of_stock": out_of_stock,
        },
    }


# ---------------------------------------------------------------------------
# Farmer-facing read model
# ---------------------------------------------------------------------------


def get_farmer_marketplace(actor_user_id, *, search=""):
    _ensure_indexes()
    actor, farmer, centre_uid, centre_name, farmer_name = _resolve_farmer(actor_user_id)
    listings = list(
        mongo.db[LISTING_COLLECTION].find({
            "centre_uid": centre_uid,
            "status": "published",
        }).sort([("published_at", DESCENDING), ("product_name", ASCENDING)])
    )

    product_ids = [item.get("source_product_id") for item in listings if isinstance(item.get("source_product_id"), ObjectId)]
    product_docs = {
        str(item["_id"]): item
        for item in mongo.db.products.find({
            "_id": {"$in": product_ids},
            "is_deleted": {"$ne": True},
            "is_active": {"$ne": False},
            "status": {"$nin": ["disabled", "deleted"]},
        })
    } if product_ids else {}

    rows = []
    for listing in listings:
        product_id = listing.get("source_product_id")
        product = product_docs.get(str(product_id))
        if not product:
            continue
        stock = _stock_for_product(centre_uid, product_id)
        if not stock:
            continue
        saleable = stock.get("saleable") or Decimal("0")
        minimum = max(_decimal(listing.get("min_order_quantity"), "1"), Decimal("0.0001"))
        maximum = max(_decimal(listing.get("max_order_quantity"), "0"), Decimal("0"))
        effective_max = saleable if maximum <= 0 else min(saleable, maximum)
        rows.append({
            "listing_id": str(listing.get("_id") or ""),
            "product_id": str(product_id),
            "_id": str(product_id),
            "product_name": listing.get("product_name") or product.get("name") or "Product",
            "name": listing.get("product_name") or product.get("name") or "Product",
            "product_code": listing.get("product_code") or product.get("product_code") or "-",
            "category": listing.get("category") or product.get("category") or "-",
            "product_role": listing.get("product_role") or product.get("product_role") or product.get("type") or "-",
            "unit_code": listing.get("unit_code") or stock.get("unit_code") or "Unit",
            "selling_price": float(_decimal(listing.get("selling_price"))),
            "selling_price_display": _money(listing.get("selling_price")),
            "min_order_quantity": float(minimum),
            "min_order_quantity_display": _qty(minimum),
            "max_order_quantity": float(maximum),
            "max_order_quantity_display": _qty(maximum),
            "effective_max_quantity": float(effective_max),
            "effective_max_quantity_display": _qty(effective_max),
            "physical_quantity": stock.get("physical_quantity"),
            "reserved_quantity": stock.get("reserved_quantity"),
            "saleable_quantity": stock.get("saleable_quantity"),
            "saleable_quantity_value": float(saleable),
            "availability_status": "in_stock" if saleable > 0 else "out_of_stock",
            "notes": listing.get("notes") or "",
            "image": _image_value(product),
            "picture": _image_value(product),
            "centre_uid": centre_uid,
            "centre_name": centre_name,
            # Stage 7 enables transaction-controlled ordering. The token is
            # unique to this rendered form so a double-click/retry cannot
            # accidentally create two identical orders.
            "ordering_enabled": saleable > 0,
            "request_token": uuid4().hex,
        })

    text = _clean_text(search, 120).lower()
    if text:
        rows = [
            row for row in rows
            if text in str(row.get("product_name") or "").lower()
            or text in str(row.get("product_code") or "").lower()
            or text in str(row.get("category") or "").lower()
        ]

    return {
        "rows": rows,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "farmer_name": farmer_name,
        "farmer_id": str(farmer.get("_id") or ""),
        "query": search or "",
        "summary": {
            "published": len(rows),
            "in_stock": sum(1 for row in rows if row.get("availability_status") == "in_stock"),
            "out_of_stock": sum(1 for row in rows if row.get("availability_status") == "out_of_stock"),
        },
    }
