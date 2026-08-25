from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
import json
import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.services.accounting_product_mapping_service import get_product_accounting_mapping_for_posting
from app.services.avpl_ufc_sales_service import _resolve_gst_state
from app.utils.helpers import now_utc
from app.utils.timezone import business_today


POS_SALE_COLLECTION = "pos_sales"
POS_INVOICE_COLLECTION = "pos_invoices"
POS_RECEIVABLE_COLLECTION = "pos_receivables"
POS_MOVEMENT_COLLECTION = "pos_stock_movements"

UFC_INPUT_LOT_COLLECTION = "ufc_inventory_lots"
UFC_INPUT_MOVEMENT_COLLECTION = "ufc_stock_movements"
UFC_OUTPUT_LOT_COLLECTION = "farmer_marketplace_buyer_stock_lots"
FARMER_MARKET_ORDER_COLLECTION = "farmer_produce_marketplace_orders"
UFC_FARMER_LISTING_COLLECTION = "ufc_farmer_marketplace_listings"

FARMER_LOT_COLLECTION = "farmer_produce_lots"
FARMER_MOVEMENT_COLLECTION = "farmer_produce_movements"
FARMER_PRODUCTION_COLLECTION = "farmer_production_entries"

MONEY = Decimal("0.01")
QTY = Decimal("0.001")
EPS = Decimal("0.0004")
GSTIN_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")

PAYMENT_TERMS = {
    "pay_now": "Pay Now",
    "credit": "Credit / Pay Later",
}

UFC_BUYER_TYPES = {
    "registered_farmer": "Registered Farmer",
    "walk_in": "Walk-in Customer",
    "trader": "Trader / Wholesaler",
    "other": "Other Buyer",
}

FARMER_BUYER_TYPES = {
    "trader": "Trader / Wholesaler",
    "local_buyer": "Local Buyer",
    "mapped_ufc": "My UFC Centre",
    "other": "Other Buyer",
}


def _decimal(value, default="0"):
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _money(value):
    return f"{_decimal(value).quantize(MONEY, rounding=ROUND_HALF_UP):.2f}"


def _qty(value):
    number = _decimal(value).quantize(QTY, rounding=ROUND_HALF_UP)
    text = f"{number:f}".rstrip("0").rstrip(".")
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


def _date_iso(value=None):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or "").strip()[:10]
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return business_today().isoformat()


def _ensure_indexes():
    definitions = [
        (POS_SALE_COLLECTION, [("sale_number", ASCENDING)], {"name": "pos_sale_number_v2_unique", "unique": True, "partialFilterExpression": {"sale_number": {"$exists": True, "$gt": ""}}}),
        (POS_SALE_COLLECTION, [("idempotency_key", ASCENDING)], {"name": "pos_sale_token_v2_unique", "unique": True, "partialFilterExpression": {"idempotency_key": {"$exists": True, "$gt": ""}}}),
        (POS_SALE_COLLECTION, [("seller_type", ASCENDING), ("seller_key", ASCENDING), ("created_at", DESCENDING)], {"name": "pos_seller_date_idx"}),
        (POS_INVOICE_COLLECTION, [("pos_sale_id", ASCENDING)], {"name": "pos_invoice_sale_unique", "unique": True}),
        (POS_INVOICE_COLLECTION, [("document_number", ASCENDING)], {"name": "pos_invoice_number_unique", "unique": True}),
        (POS_RECEIVABLE_COLLECTION, [("invoice_id", ASCENDING)], {"name": "pos_receivable_invoice_unique", "unique": True}),
        (POS_MOVEMENT_COLLECTION, [("posting_key", ASCENDING)], {"name": "pos_stock_posting_unique", "unique": True}),
    ]
    for collection, keys, options in definitions:
        try:
            mongo.db[collection].create_index(keys, **options)
        except Exception:
            pass


def _next_number(counter_key, prefix, digits=6):
    year = business_today().year
    row = mongo.db.system_counters.find_one_and_update(
        {"_id": f"{counter_key}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((row or {}).get("sequence") or 1)
    return f"{prefix}-{year}-{sequence:0{digits}d}"


def _get_user(actor_user_id):
    oid = _to_object_id(actor_user_id)
    if not oid:
        raise ValueError("Please login again.")
    user = mongo.db.users.find_one({"_id": oid}) or {}
    if not user:
        raise ValueError("Your account was not found.")
    if user.get("active", True) is False or user.get("is_active", True) is False or str(user.get("status") or "").lower() == "inactive":
        raise PermissionError("Inactive users cannot use POS.")
    user["resolved_role"] = str(user.get("role") or "").strip().lower()
    user["resolved_name"] = user.get("name") or user.get("full_name") or user.get("username") or user.get("phone") or "User"
    return user


def _valid_gstin(value):
    return bool(GSTIN_PATTERN.fullmatch(str(value or "").strip().upper()))


def _resolve_ufc(actor_user_id, centre_uid_hint=None):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "ufc_admin":
        raise PermissionError("Only UFC Admin can use the UFC POS.")
    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor["_id"]})
        or {}
    )
    centre_uid = _clean(master.get("centre_uid") or actor.get("centre_uid") or actor.get("mapped_centre_uid") or centre_uid_hint, 80)
    if not centre_uid:
        raise ValueError("This UFC Admin is not linked to a Centre UID.")
    hint = _clean(centre_uid_hint, 80)
    if hint and hint != centre_uid:
        raise PermissionError("Your session Centre UID does not match your UFC profile. Please log in again.")
    centre_name = master.get("name_of_enterprise") or master.get("enterprise_name") or master.get("centre_name") or master.get("name") or centre_uid
    gstin = str(master.get("gst_number") or master.get("gstin") or actor.get("gstin") or "").strip().upper()
    valid_gstin = _valid_gstin(gstin)
    explicit_registered = master.get("gst_registered")
    if explicit_registered is None:
        explicit_registered = master.get("is_gst_registered")
    if isinstance(explicit_registered, str):
        explicit_registered = explicit_registered.strip().lower() in {"1", "true", "yes", "on", "registered"}
    gst_registered = bool(valid_gstin and explicit_registered is not False)
    state_name, state_code = _resolve_gst_state(
        master.get("state") or actor.get("state") or "",
        master.get("state_code") or actor.get("state_code") or "",
        gstin if valid_gstin else "",
    )
    seller = {
        "type": "ufc",
        "key": centre_uid,
        "centre_uid": centre_uid,
        "legal_name": centre_name,
        "owner_name": master.get("name_of_owner") or master.get("name") or actor.get("resolved_name") or "",
        "gstin": gstin,
        "gst_registered": gst_registered,
        "state": state_name or master.get("state") or actor.get("state") or "",
        "state_code": state_code,
        "district": master.get("district") or actor.get("district") or "",
        "block": master.get("block") or actor.get("block") or "",
        "village": master.get("village") or actor.get("village") or "",
        "address": master.get("address") or actor.get("address") or "",
        "phone": master.get("contact_no") or master.get("phone") or actor.get("phone") or "",
        "email": master.get("email") or actor.get("email") or "",
        "pan": str(master.get("pan_number") or master.get("pan") or actor.get("pan") or "").strip().upper(),
    }
    if gstin and not valid_gstin:
        seller["gst_warning"] = "The UFC GSTIN in the Centre profile is invalid, so GST is not charged until the profile is corrected."
    elif explicit_registered is True and not gstin:
        seller["gst_warning"] = "The UFC is marked GST-registered but no valid GSTIN is available."
    else:
        seller["gst_warning"] = ""
    return actor, master, centre_uid, centre_name, seller


def _resolve_farmer(actor_user_id):
    actor = _get_user(actor_user_id)
    if actor.get("resolved_role") != "farmer":
        raise PermissionError("Only Farmers can use Farmer POS.")
    master = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.farmer_master.find_one({"linked_user_id": actor["_id"]})
        or mongo.db.farmer_master.find_one({"contact_no": actor.get("phone")})
        or {}
    )
    if not master:
        raise ValueError("Complete the Farmer profile before using POS.")
    profile = {
        "user_id": actor["_id"],
        "user_id_str": str(actor["_id"]),
        "farmer_master_id": master.get("_id"),
        "name": master.get("name") or actor.get("resolved_name") or "Farmer",
        "phone": master.get("contact_no") or actor.get("phone") or "",
        "centre_uid": master.get("centre_uid") or actor.get("mapped_centre_uid") or actor.get("centre_uid") or "",
        "mitra_uid": master.get("mitra_uid") or actor.get("mapped_mitra_uid") or actor.get("mitra_uid") or "",
        "state": master.get("state") or actor.get("state") or "",
        "district": master.get("district") or actor.get("district") or "",
        "block": master.get("block") or actor.get("block") or "",
        "village": master.get("village") or actor.get("village") or "",
        "address": master.get("address") or actor.get("address") or "",
    }
    return actor, master, profile


def _active_avpl_entity():
    return mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    })


def _lot_expired(lot):
    value = lot.get("expiry_date")
    if not value:
        return False
    if isinstance(value, datetime):
        expiry = value.date()
    elif isinstance(value, date):
        expiry = value
    else:
        try:
            expiry = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except Exception:
            return False
    return expiry < business_today()


def _input_saleable(lot):
    physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
    reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
    damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
    blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
    if _lot_expired(lot):
        return Decimal("0")
    return max(physical - reserved - damaged - blocked, Decimal("0"))


def _input_unit_cost(lot):
    received = _decimal(lot.get("received_quantity"))
    total_cost = _decimal(lot.get("purchase_cost_total"))
    if received > EPS and total_cost >= 0:
        return total_cost / received
    return max(_decimal(lot.get("last_purchase_price")), Decimal("0"))


def _mapped_farmers(centre_uid):
    rows = []
    for farmer in mongo.db.farmer_master.find({"centre_uid": centre_uid, "approval_status": "approved"}).sort("name", 1):
        row = dict(farmer)
        row["id"] = str(row.get("_id") or "")
        row["linked_user_id_str"] = str(row.get("linked_user_id") or "")
        gstin = str(row.get("gstin") or row.get("gst_number") or "").strip().upper()
        state_name, state_code = _resolve_gst_state(
            row.get("state") or "",
            row.get("state_code") or "",
            gstin if _valid_gstin(gstin) else "",
        )
        row["gst_state"] = state_name or row.get("state") or ""
        row["gst_state_code"] = state_code or ""
        rows.append(row)
    return rows


def _mitra_bonus_percentage(mitra_uid, category):
    """Preserve the existing UFC Mitra bonus rule for AVPL-derived input sales."""
    for query in [
        {"mitra_uid": mitra_uid, "bonus_type": "avpl_product_sale", "category": category},
        {"mitra_uid": None, "bonus_type": "avpl_product_sale", "category": category},
        {"mitra_uid": mitra_uid, "bonus_type": "avpl_product_sale", "category": "all"},
        {"mitra_uid": None, "bonus_type": "avpl_product_sale", "category": "all"},
    ]:
        setting = mongo.db.mitra_bonus_settings.find_one(query)
        if setting:
            return max(_decimal(setting.get("percentage"), "2"), Decimal("0"))
    return Decimal("2")


def _input_catalog(centre_uid):
    lots = list(mongo.db[UFC_INPUT_LOT_COLLECTION].find({
        "centre_uid": centre_uid,
        "status": {"$nin": ["cancelled", "expired"]},
        "available_quantity": {"$gt": 0},
    }))
    grouped = {}
    product_ids = set()
    for lot in lots:
        pid = lot.get("source_product_id")
        if not pid:
            continue
        product_ids.add(pid)
        key = str(pid)
        row = grouped.setdefault(key, {
            "physical": Decimal("0"), "saleable": Decimal("0"), "cost_value": Decimal("0"),
            "product_name": lot.get("product_name") or "Product", "product_code": lot.get("product_code") or "",
            "unit_code": lot.get("unit_code") or "Unit", "category": lot.get("category") or "",
            "product_role": lot.get("product_role") or "input", "barcodes": set(), "batch_count": 0,
            "last_purchase_price": Decimal("0"),
        })
        physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        saleable = _input_saleable(lot)
        unit_cost = _input_unit_cost(lot)
        row["physical"] += physical
        row["saleable"] += saleable
        row["cost_value"] += saleable * unit_cost
        row["batch_count"] += 1
        row["last_purchase_price"] = max(row["last_purchase_price"], _decimal(lot.get("last_purchase_price")))
        if lot.get("barcode"):
            row["barcodes"].add(str(lot.get("barcode")))

    product_map = {row["_id"]: row for row in mongo.db.products.find({"_id": {"$in": list(product_ids)}})} if product_ids else {}
    listings = list(mongo.db[UFC_FARMER_LISTING_COLLECTION].find({"centre_uid": centre_uid, "source_product_id": {"$in": list(product_ids)}})) if product_ids else []
    listing_by_product = {str(row.get("source_product_id")): row for row in listings}

    items = []
    for pid, row in grouped.items():
        if row["saleable"] <= EPS:
            continue
        product = product_map.get(_to_object_id(pid)) or {}
        listing = listing_by_product.get(pid) or {}
        default_price = _decimal(listing.get("selling_price"))
        if default_price <= 0:
            default_price = _decimal(product.get("selling_price") or product.get("sale_price") or product.get("price"))
        if default_price <= 0:
            default_price = row["last_purchase_price"]
        wac = row["cost_value"] / row["saleable"] if row["saleable"] > EPS else Decimal("0")
        items.append({
            "stock_key": f"input:{pid}",
            "source_type": "input",
            "source_label": "Input Stock",
            "badge": "INPUT",
            "product_id": pid,
            "product_name": product.get("name") or row["product_name"],
            "product_code": product.get("product_code") or product.get("sku") or row["product_code"],
            "category": product.get("category") or row["category"],
            "product_role": product.get("product_role") or row["product_role"],
            "unit_code": row["unit_code"],
            "available_quantity": float(row["saleable"]),
            "available_display": _qty(row["saleable"]),
            "physical_display": _qty(row["physical"]),
            "default_price": float(default_price),
            "default_price_display": _money(default_price),
            "unit_cost": float(wac),
            "unit_cost_display": _money(wac),
            "barcode": next(iter(sorted(row["barcodes"])), ""),
            "batch_count": row["batch_count"],
            "mapping_product_id": pid,
        })
    return items


def _output_catalog(centre_uid):
    lots = list(mongo.db[UFC_OUTPUT_LOT_COLLECTION].find({
        "buyer_type": "ufc",
        "$or": [{"buyer_key": centre_uid}, {"centre_uid": centre_uid}],
        "status": "active",
        "available_quantity": {"$gt": 0},
    }).sort([("received_at", ASCENDING), ("created_at", ASCENDING)]))
    order_ids = [lot.get("farmer_marketplace_order_id") for lot in lots if lot.get("farmer_marketplace_order_id")]
    order_map = {row["_id"]: row for row in mongo.db[FARMER_MARKET_ORDER_COLLECTION].find({"_id": {"$in": order_ids}})} if order_ids else {}

    grouped = {}
    names = set()
    for lot in lots:
        product_key = _clean(lot.get("product_key") or f"{lot.get('product_name')}:{lot.get('unit_code')}", 220)
        unit = lot.get("unit_code") or "KG"
        variety_key = _clean(lot.get("variety"), 120).casefold()
        grade_key = _clean(lot.get("grade"), 80).casefold()
        group_key = f"{product_key}|{unit}|{variety_key}|{grade_key}"
        order = order_map.get(lot.get("farmer_marketplace_order_id")) or {}
        qty = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        unit_cost = _decimal(order.get("unit_price"))
        if unit_cost <= 0:
            base_qty = _decimal(order.get("base_quantity"))
            total = _decimal(order.get("total_amount"))
            if base_qty > EPS:
                unit_cost = total / base_qty
        row = grouped.setdefault(group_key, {
            "product_key": product_key,
            "product_name": lot.get("product_name") or "Produce",
            "variety": lot.get("variety") or "",
            "grade": lot.get("grade") or "",
            "unit_code": unit,
            "available": Decimal("0"),
            "cost_value": Decimal("0"),
            "latest_price": Decimal("0"),
            "lot_count": 0,
            "farmers": set(),
        })
        row["available"] += qty
        row["cost_value"] += qty * max(unit_cost, Decimal("0"))
        row["latest_price"] = unit_cost if unit_cost > 0 else row["latest_price"]
        row["lot_count"] += 1
        if lot.get("source_farmer_name"):
            row["farmers"].add(str(lot.get("source_farmer_name")))
        names.add(row["product_name"])

    active_products = list(mongo.db.products.find({
        "name": {"$in": list(names)},
        "is_deleted": {"$ne": True},
        "is_active": {"$ne": False},
    })) if names else []
    by_name = {}
    for p in active_products:
        by_name.setdefault(str(p.get("name") or "").casefold(), []).append(p)

    items = []
    for group_key, row in grouped.items():
        if row["available"] <= EPS:
            continue
        wac = row["cost_value"] / row["available"] if row["available"] > EPS else Decimal("0")
        default_price = row["latest_price"] if row["latest_price"] > 0 else wac
        matches = by_name.get(str(row["product_name"]).casefold(), [])
        mapping_product_id = str(matches[0]["_id"]) if len(matches) == 1 else ""
        items.append({
            "stock_key": f"output:{group_key}",
            "source_type": "output",
            "source_label": "Farmer Produce Stock",
            "badge": "OUTPUT",
            "product_id": "",
            "product_key": row["product_key"],
            "product_name": row["product_name"],
            "product_code": "",
            "category": "Farmer Produce",
            "product_role": "output",
            "variety": row["variety"],
            "grade": row["grade"],
            "unit_code": row["unit_code"],
            "available_quantity": float(row["available"]),
            "available_display": _qty(row["available"]),
            "physical_display": _qty(row["available"]),
            "default_price": float(default_price),
            "default_price_display": _money(default_price),
            "unit_cost": float(wac),
            "unit_cost_display": _money(wac),
            "barcode": "",
            "batch_count": row["lot_count"],
            "source_farmer_names": ", ".join(sorted(row["farmers"]))[:180],
            "mapping_product_id": mapping_product_id,
        })
    return items



def _catalog_tax_preview(catalog_item, seller):
    """Return non-mutating GST/HSN metadata for POS display.

    The checkout preview uses this classification only for display. The sale
    still recalculates tax server-side through _line_tax_for_ufc(), so browser
    values can never override the statutory calculation.
    """
    mapping_product_id = _to_object_id(catalog_item.get("mapping_product_id"))
    hsn_code = ""
    taxability_code = "UNMAPPED"
    mapped_rate = Decimal("0")
    warning = ""
    if mapping_product_id:
        entity = _active_avpl_entity()
        if entity:
            try:
                mapping = get_product_accounting_mapping_for_posting(
                    entity["_id"],
                    mapping_product_id,
                    transaction_date=business_today().isoformat(),
                    operation="sales",
                )
                hsn = mapping.get("hsn") or {}
                hsn_code = str(hsn.get("hsn_code") or "")
                taxability_code = str(hsn.get("taxability_code") or "NON_GST").upper()
                if taxability_code == "TAXABLE":
                    mapped_rate = max(_decimal((mapping.get("effective_gst_rate") or {}).get("total_rate")), Decimal("0"))
            except Exception as exc:
                warning = _clean(exc, 300) or "Product GST mapping is not available."
        else:
            warning = "AVPL Accounting product mapping is unavailable."
    else:
        warning = "No unique Product Master GST mapping is available for this Farmer Produce stock."

    charged_rate = mapped_rate if seller.get("gst_registered") and taxability_code == "TAXABLE" else Decimal("0")
    if taxability_code == "TAXABLE":
        classification = f"GST {_qty(mapped_rate)}%"
    elif taxability_code in {"EXEMPT", "NIL_RATED", "NIL RATED", "NON_GST"}:
        classification = taxability_code.replace("_", " ").title()
    else:
        classification = "GST mapping unavailable"

    if seller.get("gst_registered"):
        charge_label = f"Charge {_qty(charged_rate)}%" if charged_rate > 0 else "No GST charged"
    else:
        charge_label = "UFC not GST-registered · GST not charged"

    return {
        "hsn_code": hsn_code,
        "taxability_code": taxability_code,
        "mapped_gst_rate": float(mapped_rate),
        "mapped_gst_rate_display": _qty(mapped_rate),
        "preview_gst_rate": float(charged_rate),
        "preview_gst_rate_display": _qty(charged_rate),
        "gst_classification_label": classification,
        "gst_charge_label": charge_label,
        "gst_preview_warning": warning,
    }


def _farmer_catalog(profile):
    lots = list(mongo.db[FARMER_LOT_COLLECTION].find({
        "farmer_user_id": profile["user_id"],
        "status": "active",
        "available_quantity": {"$gt": 0},
    }).sort([("harvest_date", ASCENDING), ("created_at", ASCENDING)]))
    production_ids = [lot.get("production_entry_id") for lot in lots if lot.get("production_entry_id")]
    production_map = {row["_id"]: row for row in mongo.db[FARMER_PRODUCTION_COLLECTION].find({"_id": {"$in": production_ids}})} if production_ids else {}
    grouped = {}
    for lot in lots:
        product_key = lot.get("product_key") or _clean(f"{lot.get('product_name')}:{lot.get('unit_code')}", 220)
        variety = _clean(lot.get("variety"), 120)
        grade = _clean(lot.get("grade"), 80)
        group_key = f"{product_key}|{variety.casefold()}|{grade.casefold()}"
        available = max(_decimal(lot.get("available_quantity")) - _decimal(lot.get("reserved_quantity")), Decimal("0"))
        if available <= EPS:
            continue
        production = production_map.get(lot.get("production_entry_id")) or {}
        original = max(_decimal(lot.get("original_quantity")), Decimal("0"))
        batch_cost = max(_decimal(production.get("estimated_cost")), Decimal("0"))
        unit_cost = batch_cost / original if original > EPS else Decimal("0")
        row = grouped.setdefault(group_key, {
            "product_name": lot.get("product_name") or "Produce",
            "product_key": product_key,
            "variety": variety,
            "grade": grade,
            "unit_code": lot.get("unit_code") or "KG",
            "available": Decimal("0"), "cost_value": Decimal("0"), "lot_count": 0,
        })
        row["available"] += available
        row["cost_value"] += available * unit_cost
        row["lot_count"] += 1
    items = []
    for key, row in grouped.items():
        wac = row["cost_value"] / row["available"] if row["available"] > EPS else Decimal("0")
        items.append({
            "stock_key": f"farmer:{key}",
            "source_type": "farmer_output",
            "source_label": "My Produce Stock",
            "badge": "PRODUCE",
            "product_key": row["product_key"],
            "product_name": row["product_name"],
            "variety": row["variety"],
            "grade": row["grade"],
            "unit_code": row["unit_code"],
            "available_quantity": float(row["available"]),
            "available_display": _qty(row["available"]),
            "default_price": 0.0,
            "default_price_display": "0.00",
            "unit_cost": float(wac),
            "unit_cost_display": _money(wac),
            "batch_count": row["lot_count"],
            "barcode": "",
        })
    return sorted(items, key=lambda x: x["product_name"].casefold())


def _sale_rows(query, limit=60):
    rows = []
    for sale in mongo.db[POS_SALE_COLLECTION].find(query).sort("created_at", DESCENDING).limit(limit):
        item = serialize_sale(sale)
        rows.append(item)
    return rows


def get_ufc_pos_context(actor_user_id, centre_uid_hint=None, search=""):
    _ensure_indexes()
    actor, master, centre_uid, centre_name, seller = _resolve_ufc(actor_user_id, centre_uid_hint)
    catalog = _input_catalog(centre_uid) + _output_catalog(centre_uid)
    for item in catalog:
        item.update(_catalog_tax_preview(item, seller))
    text = _clean(search, 120).casefold()
    if text:
        catalog = [item for item in catalog if text in " ".join([
            str(item.get("product_name") or ""), str(item.get("product_code") or ""),
            str(item.get("source_label") or ""), str(item.get("barcode") or ""),
        ]).casefold()]
    sales = _sale_rows({"seller_type": "ufc", "seller_key": centre_uid})
    total_due = sum((_decimal(s.get("outstanding_amount")) for s in sales if s.get("status") == "completed"), Decimal("0"))
    today = business_today().isoformat()
    return {
        "seller": seller,
        "centre_uid": centre_uid,
        "centre_name": centre_name,
        "catalog": catalog,
        "farmers": _mapped_farmers(centre_uid),
        "buyer_types": UFC_BUYER_TYPES,
        "payment_terms": PAYMENT_TERMS,
        "sales": sales,
        "sale_token": f"UFC-POS-{uuid4().hex.upper()}",
        "payment_token": f"UFC-POS-PAY-{uuid4().hex.upper()}",
        "today": today,
        "query": search or "",
        "summary": {
            "products": len(catalog),
            "input_products": sum(1 for x in catalog if x.get("source_type") == "input"),
            "output_products": sum(1 for x in catalog if x.get("source_type") == "output"),
            "outstanding": _money(total_due),
        },
    }


def get_farmer_pos_context(actor_user_id, search=""):
    _ensure_indexes()
    actor, master, profile = _resolve_farmer(actor_user_id)
    catalog = _farmer_catalog(profile)
    text = _clean(search, 120).casefold()
    if text:
        catalog = [x for x in catalog if text in f"{x.get('product_name','')} {x.get('variety','')} {x.get('grade','')}".casefold()]
    mapped_ufc = {}
    if profile.get("centre_uid"):
        centre = mongo.db.ufc_admin_master.find_one({"centre_uid": profile["centre_uid"]}) or mongo.db.ufc_centre_master.find_one({"centre_uid": profile["centre_uid"]}) or {}
        mapped_ufc = {
            "centre_uid": profile["centre_uid"],
            "name": centre.get("name_of_enterprise") or centre.get("enterprise_name") or centre.get("centre_name") or centre.get("name") or profile["centre_uid"],
        }
    sales = _sale_rows({"seller_type": "farmer", "seller_key": profile["user_id_str"]})
    total_due = sum((_decimal(s.get("outstanding_amount")) for s in sales if s.get("status") == "completed"), Decimal("0"))
    return {
        "farmer": profile,
        "catalog": catalog,
        "mapped_ufc": mapped_ufc,
        "buyer_types": FARMER_BUYER_TYPES,
        "payment_terms": PAYMENT_TERMS,
        "sales": sales,
        "sale_token": f"FARMER-POS-{uuid4().hex.upper()}",
        "payment_token": f"FARMER-POS-PAY-{uuid4().hex.upper()}",
        "today": business_today().isoformat(),
        "query": search or "",
        "summary": {"products": len(catalog), "outstanding": _money(total_due)},
    }


def _parse_cart(cart_json):
    try:
        raw = json.loads(cart_json or "[]")
    except Exception as exc:
        raise ValueError("The POS cart could not be read. Refresh and add the products again.") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("Add at least one product to the cart.")
    if len(raw) > 50:
        raise ValueError("A single POS bill can contain at most 50 line items.")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = _clean(item.get("stock_key"), 300)
        qty = _decimal(item.get("quantity"))
        rate = _decimal(item.get("unit_price"))
        discount = _decimal(item.get("discount_percent"))
        if not key:
            raise ValueError("One cart item is missing its stock reference. Remove it and add it again.")
        if qty <= 0:
            raise ValueError("Every cart quantity must be greater than zero.")
        if rate <= 0:
            raise ValueError("Every selling price must be greater than zero.")
        if discount < 0 or discount > 100:
            raise ValueError("Discount percentage must be between 0 and 100.")
        rows.append({"stock_key": key, "quantity": qty, "unit_price": rate, "discount_percent": discount})
    if not rows:
        raise ValueError("Add at least one valid product to the cart.")
    return rows


def _registered_farmer_snapshot(centre_uid, farmer_id):
    oid = _to_object_id(farmer_id)
    farmer = mongo.db.farmer_master.find_one({"_id": oid, "centre_uid": centre_uid, "approval_status": "approved"}) if oid else None
    if not farmer:
        raise ValueError("Choose a valid Farmer mapped to this UFC Centre.")
    linked_user = mongo.db.users.find_one({"_id": _to_object_id(farmer.get("linked_user_id"))}) or {}
    return {
        "type": "registered_farmer",
        "key": str(farmer.get("linked_user_id") or farmer.get("_id")),
        "farmer_master_id": farmer.get("_id"),
        "farmer_master_id_str": str(farmer.get("_id")),
        "farmer_user_id": _to_object_id(farmer.get("linked_user_id")),
        "farmer_user_id_str": str(farmer.get("linked_user_id") or ""),
        "name": farmer.get("name") or linked_user.get("name") or "Farmer",
        "phone": farmer.get("contact_no") or linked_user.get("phone") or "",
        "state": farmer.get("state") or linked_user.get("state") or "",
        "district": farmer.get("district") or linked_user.get("district") or "",
        "block": farmer.get("block") or linked_user.get("block") or "",
        "village": farmer.get("village") or linked_user.get("village") or "",
        "address": farmer.get("address") or linked_user.get("address") or "",
        "gstin": str(farmer.get("gstin") or linked_user.get("gstin") or "").strip().upper(),
        "mitra_uid": farmer.get("mitra_uid") or linked_user.get("mapped_mitra_uid") or linked_user.get("mitra_uid") or "",
    }


def _ufc_buyer_snapshot(centre_uid, buyer_type, payload):
    buyer_type = str(buyer_type or "walk_in").strip().lower()
    if buyer_type not in UFC_BUYER_TYPES:
        raise ValueError("Choose a valid customer type.")
    if buyer_type == "registered_farmer":
        return _registered_farmer_snapshot(centre_uid, payload.get("farmer_id"))
    name = _clean(payload.get("buyer_name"), 160)
    if not name:
        name = "Walk-in Customer" if buyer_type == "walk_in" else UFC_BUYER_TYPES[buyer_type]
    gstin = str(payload.get("buyer_gstin") or "").strip().upper()
    if gstin and not _valid_gstin(gstin):
        raise ValueError("Buyer GSTIN must be a valid 15-character GSTIN or left blank.")
    state_name, state_code = _resolve_gst_state(payload.get("buyer_state") or "", payload.get("buyer_state_code") or "", gstin)
    return {
        "type": buyer_type,
        "key": _clean(payload.get("buyer_phone") or gstin or name, 180),
        "name": name,
        "phone": _clean(payload.get("buyer_phone"), 40),
        "address": _clean(payload.get("buyer_address"), 300),
        "state": state_name or _clean(payload.get("buyer_state"), 100),
        "state_code": state_code,
        "gstin": gstin,
        "mitra_uid": _clean(payload.get("mitra_uid"), 80),
    }


def _farmer_buyer_snapshot(profile, buyer_type, payload):
    buyer_type = str(buyer_type or "local_buyer").strip().lower()
    if buyer_type not in FARMER_BUYER_TYPES:
        raise ValueError("Choose a valid buyer type.")
    if buyer_type == "mapped_ufc":
        centre_uid = profile.get("centre_uid") or ""
        centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or mongo.db.ufc_centre_master.find_one({"centre_uid": centre_uid}) or {}
        if not centre_uid:
            raise ValueError("Your Farmer profile is not mapped to a UFC Centre.")
        return {
            "type": "mapped_ufc",
            "key": centre_uid,
            "centre_uid": centre_uid,
            "name": centre.get("name_of_enterprise") or centre.get("enterprise_name") or centre.get("centre_name") or centre.get("name") or centre_uid,
            "phone": centre.get("contact_no") or centre.get("phone") or "",
            "address": centre.get("address") or "",
            "state": centre.get("state") or "",
        }
    name = _clean(payload.get("buyer_name"), 160)
    if not name:
        raise ValueError("Enter the buyer name.")
    return {
        "type": buyer_type,
        "key": _clean(payload.get("buyer_phone") or name, 180),
        "name": name,
        "phone": _clean(payload.get("buyer_phone"), 40),
        "address": _clean(payload.get("buyer_address"), 300),
        "state": _clean(payload.get("buyer_state"), 100),
    }


def _line_tax_for_ufc(catalog_item, seller, buyer, quantity, rate, discount_percent):
    gross = quantity * rate
    discount_amount = (gross * discount_percent / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)
    taxable = max(gross - discount_amount, Decimal("0")).quantize(MONEY, rounding=ROUND_HALF_UP)
    mapping_product_id = _to_object_id(catalog_item.get("mapping_product_id"))
    hsn_code = ""
    taxability_code = "NON_GST"
    mapped_rate = Decimal("0")
    warning = ""
    if mapping_product_id:
        entity = _active_avpl_entity()
        if entity:
            try:
                mapping = get_product_accounting_mapping_for_posting(
                    entity["_id"], mapping_product_id,
                    transaction_date=business_today().isoformat(), operation="sales",
                )
                hsn = mapping.get("hsn") or {}
                hsn_code = hsn.get("hsn_code") or ""
                taxability_code = str(hsn.get("taxability_code") or "NON_GST").upper()
                if taxability_code == "TAXABLE":
                    mapped_rate = _decimal((mapping.get("effective_gst_rate") or {}).get("total_rate"))
            except Exception as exc:
                if catalog_item.get("source_type") == "input" and seller.get("gst_registered"):
                    raise RuntimeError(f"GST/HSN mapping is required before selling {catalog_item.get('product_name')} from POS: {exc}")
                warning = "Product GST mapping is not available."
    else:
        warning = "Farmer-produce stock is not yet mapped to Product Master tax classification. GST is not charged automatically on this line."

    gst_rate = mapped_rate if seller.get("gst_registered") and taxability_code == "TAXABLE" else Decimal("0")
    seller_state, seller_code = _resolve_gst_state(seller.get("state") or "", seller.get("state_code") or "", seller.get("gstin") or "")
    buyer_state, buyer_code = _resolve_gst_state(buyer.get("state") or "", buyer.get("state_code") or "", buyer.get("gstin") or "")
    if not buyer_code:
        buyer_state, buyer_code = seller_state, seller_code
    tax_amount = (taxable * gst_rate / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)
    cgst = sgst = igst = Decimal("0")
    if tax_amount > 0:
        if seller_code and buyer_code and seller_code == buyer_code:
            cgst = (tax_amount / Decimal("2")).quantize(MONEY, rounding=ROUND_HALF_UP)
            sgst = tax_amount - cgst
        else:
            igst = tax_amount
    return {
        "gross": gross.quantize(MONEY, rounding=ROUND_HALF_UP),
        "discount_amount": discount_amount,
        "taxable_value": taxable,
        "hsn_code": hsn_code,
        "taxability_code": taxability_code,
        "gst_rate": gst_rate,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "gst_amount": cgst + sgst + igst,
        "line_total": taxable + cgst + sgst + igst,
        "warning": warning,
        "place_of_supply_state": buyer_state or seller_state or "",
        "place_of_supply_state_code": buyer_code or seller_code or "",
    }


def _allocate_ufc_input(centre_uid, product_id, required, sale_id, line_no, actor, product_name):
    product_oid = _to_object_id(product_id)
    lots = list(mongo.db[UFC_INPUT_LOT_COLLECTION].find({
        "centre_uid": centre_uid,
        "source_product_id": product_oid,
        "status": {"$nin": ["cancelled", "expired"]},
        "available_quantity": {"$gt": 0},
    }))
    lots.sort(key=lambda row: (str(row.get("expiry_date") or "9999-12-31")[:10], row.get("created_at") or datetime.min, str(row.get("_id"))))
    total = sum((_input_saleable(lot) for lot in lots), Decimal("0"))
    if total + EPS < required:
        raise ValueError(f"Only {_qty(total)} is available for {product_name}.")
    remaining = required
    allocations = []
    try:
        for lot in lots:
            if remaining <= EPS:
                break
            saleable = _input_saleable(lot)
            if saleable <= EPS:
                continue
            take = min(saleable, remaining)
            result = mongo.db[UFC_INPUT_LOT_COLLECTION].update_one(
                {
                    "_id": lot["_id"], "centre_uid": centre_uid,
                    "$expr": {"$gte": [
                        {"$subtract": [
                            {"$ifNull": ["$available_quantity", 0]},
                            {"$add": [
                                {"$ifNull": ["$reserved_quantity", 0]},
                                {"$ifNull": ["$damaged_quantity", 0]},
                                {"$ifNull": ["$blocked_quantity", 0]},
                            ]},
                        ]}, float(take)
                    ]},
                },
                {"$inc": {"available_quantity": -float(take), "issued_quantity": float(take)}, "$set": {"updated_at": now_utc(), "last_pos_sale_id": sale_id}},
            )
            if result.modified_count != 1:
                raise RuntimeError(f"Stock for {product_name} changed in another session. Refresh POS and try again.")
            unit_cost = _input_unit_cost(lot)
            posting_key = f"POS:{sale_id}:{line_no}:{lot['_id']}"
            allocation = {
                "pool": "input", "lot_id": lot["_id"], "lot_id_str": str(lot["_id"]),
                "lot_number": lot.get("batch_number") or lot.get("lot_key") or "",
                "quantity": float(take), "unit_cost": float(unit_cost), "cogs": float(take * unit_cost),
                "sale_id": sale_id, "line_no": line_no, "product_name": product_name,
                "posting_key": posting_key, "movement_collection": UFC_INPUT_MOVEMENT_COLLECTION,
            }
            allocations.append(allocation)
            mongo.db[UFC_INPUT_MOVEMENT_COLLECTION].update_one(
                {"source_posting_key": posting_key},
                {"$setOnInsert": {
                    "source_posting_key": posting_key, "movement_uid": uuid4().hex,
                    "centre_uid": centre_uid, "source_document_type": "pos_sale", "source_document_id": sale_id,
                    "source_document_id_str": str(sale_id), "source_product_id": product_oid,
                    "source_product_id_str": str(product_oid), "product_name": product_name,
                    "movement_type": "pos_sale", "direction": "out", "quantity": float(take),
                    "quantity_display": _qty(take), "unit_code": lot.get("unit_code") or "Unit",
                    "warehouse_code": lot.get("warehouse_code") or f"{centre_uid}-MAIN",
                    "batch_number": lot.get("batch_number") or "", "barcode": lot.get("barcode") or "",
                    "movement_date": business_today().isoformat(), "reason": "Direct POS sale.",
                    "posted_by": actor.get("_id"), "posted_by_name": actor.get("resolved_name") or "",
                    "posted_at": now_utc(), "created_at": now_utc(),
                }}, upsert=True,
            )
            remaining -= take
        if remaining > EPS:
            raise RuntimeError(f"Stock for {product_name} changed while the POS sale was being saved. Refresh and try again.")
        return allocations
    except Exception:
        _rollback_allocations(allocations, remove_movements=True)
        raise

def _allocate_ufc_output(centre_uid, product_key, unit_code, required, sale_id, line_no, product_name, variety="", grade=""):
    lots = list(mongo.db[UFC_OUTPUT_LOT_COLLECTION].find({
        "buyer_type": "ufc",
        "$or": [{"buyer_key": centre_uid}, {"centre_uid": centre_uid}],
        "product_key": product_key,
        "unit_code": unit_code,
        "status": "active",
        "available_quantity": {"$gt": 0},
    }).sort([("received_at", ASCENDING), ("created_at", ASCENDING), ("_id", ASCENDING)]))
    wanted_variety = _clean(variety, 120).casefold()
    wanted_grade = _clean(grade, 80).casefold()
    lots = [lot for lot in lots if _clean(lot.get("variety"), 120).casefold() == wanted_variety and _clean(lot.get("grade"), 80).casefold() == wanted_grade]
    total = sum((max(_decimal(lot.get("available_quantity")), Decimal("0")) for lot in lots), Decimal("0"))
    if total + EPS < required:
        raise ValueError(f"Only {_qty(total)} is available for {product_name} from Farmer Produce Stock.")
    order_ids = [lot.get("farmer_marketplace_order_id") for lot in lots if lot.get("farmer_marketplace_order_id")]
    order_map = {row["_id"]: row for row in mongo.db[FARMER_MARKET_ORDER_COLLECTION].find({"_id": {"$in": order_ids}})} if order_ids else {}
    remaining = required
    allocations = []
    try:
        for lot in lots:
            if remaining <= EPS:
                break
            available = max(_decimal(lot.get("available_quantity")), Decimal("0"))
            take = min(available, remaining)
            result = mongo.db[UFC_OUTPUT_LOT_COLLECTION].update_one(
                {"_id": lot["_id"], "available_quantity": {"$gte": float(take)}, "status": "active"},
                {"$inc": {"available_quantity": -float(take)}, "$set": {"updated_at": now_utc(), "last_pos_sale_id": sale_id}},
            )
            if result.modified_count != 1:
                raise RuntimeError(f"Farmer Produce stock for {product_name} changed in another session. Refresh POS and try again.")
            order = order_map.get(lot.get("farmer_marketplace_order_id")) or {}
            unit_cost = _decimal(order.get("unit_price"))
            if unit_cost <= 0:
                base = _decimal(order.get("base_quantity")); total_amount = _decimal(order.get("total_amount"))
                unit_cost = total_amount / base if base > EPS else Decimal("0")
            posting_key = f"POS:{sale_id}:{line_no}:{lot['_id']}"
            allocation = {
                "pool": "output", "lot_id": lot["_id"], "lot_id_str": str(lot["_id"]),
                "lot_number": lot.get("lot_number") or "", "quantity": float(take),
                "unit_cost": float(unit_cost), "cogs": float(take * unit_cost),
                "source_farmer_user_id": lot.get("source_farmer_user_id"),
                "source_farmer_name": lot.get("source_farmer_name") or "Farmer",
                "farmer_marketplace_order_id": lot.get("farmer_marketplace_order_id"),
                "sale_id": sale_id, "line_no": line_no, "product_name": product_name,
                "posting_key": posting_key, "movement_collection": POS_MOVEMENT_COLLECTION,
            }
            allocations.append(allocation)
            mongo.db[POS_MOVEMENT_COLLECTION].update_one(
                {"posting_key": posting_key},
                {"$setOnInsert": {
                    "posting_key": posting_key, "seller_type": "ufc", "seller_key": centre_uid,
                    "stock_pool": "farmer_produce", "sale_id": sale_id, "sale_id_str": str(sale_id),
                    "lot_id": lot["_id"], "lot_id_str": str(lot["_id"]), "product_key": product_key,
                    "product_name": product_name, "unit_code": unit_code, "quantity": float(take),
                    "direction": "out", "movement_type": "pos_sale", "source_farmer_name": allocation["source_farmer_name"],
                    "created_at": now_utc(),
                }}, upsert=True,
            )
            remaining -= take
        if remaining > EPS:
            raise RuntimeError(f"Farmer Produce stock for {product_name} changed while the POS sale was being saved. Refresh and try again.")
        return allocations
    except Exception:
        _rollback_allocations(allocations, remove_movements=True)
        raise

def _allocate_farmer_stock(profile, product_key, required, sale_id, line_no, product_name, variety="", grade=""):
    lots = list(mongo.db[FARMER_LOT_COLLECTION].find({
        "farmer_user_id": profile["user_id"], "product_key": product_key,
        "status": "active", "available_quantity": {"$gt": 0},
    }).sort([("harvest_date", ASCENDING), ("created_at", ASCENDING), ("_id", ASCENDING)]))
    wanted_variety = _clean(variety, 120).casefold()
    wanted_grade = _clean(grade, 80).casefold()
    lots = [lot for lot in lots if _clean(lot.get("variety"), 120).casefold() == wanted_variety and _clean(lot.get("grade"), 80).casefold() == wanted_grade]
    total = sum((max(_decimal(lot.get("available_quantity")) - _decimal(lot.get("reserved_quantity")), Decimal("0")) for lot in lots), Decimal("0"))
    if total + EPS < required:
        raise ValueError(f"Only {_qty(total)} is available for {product_name}.")
    production_ids = [lot.get("production_entry_id") for lot in lots if lot.get("production_entry_id")]
    production_map = {row["_id"]: row for row in mongo.db[FARMER_PRODUCTION_COLLECTION].find({"_id": {"$in": production_ids}})} if production_ids else {}
    remaining = required
    allocations = []
    try:
        for lot in lots:
            if remaining <= EPS:
                break
            reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
            available = max(_decimal(lot.get("available_quantity")) - reserved, Decimal("0"))
            take = min(available, remaining)
            result = mongo.db[FARMER_LOT_COLLECTION].update_one(
                {"_id": lot["_id"], "farmer_user_id": profile["user_id"], "$expr": {"$gte": [{"$subtract": [{"$ifNull": ["$available_quantity", 0]}, {"$ifNull": ["$reserved_quantity", 0]}]}, float(take)]}},
                {"$inc": {"available_quantity": -float(take), "sold_quantity": float(take)}, "$set": {"updated_at": now_utc(), "last_pos_sale_id": sale_id}},
            )
            if result.modified_count != 1:
                raise RuntimeError(f"Produce stock for {product_name} changed in another session. Refresh POS and try again.")
            production = production_map.get(lot.get("production_entry_id")) or {}
            original = max(_decimal(lot.get("original_quantity")), Decimal("0"))
            batch_cost = max(_decimal(production.get("estimated_cost")), Decimal("0"))
            unit_cost = batch_cost / original if original > EPS else Decimal("0")
            allocation = {
                "pool": "farmer_output", "lot_id": lot["_id"], "lot_id_str": str(lot["_id"]),
                "lot_number": lot.get("lot_number") or "", "quantity": float(take),
                "unit_cost": float(unit_cost), "cogs": float(take * unit_cost),
                "sale_id": sale_id, "line_no": line_no, "product_name": product_name,
                "movement_collection": FARMER_MOVEMENT_COLLECTION,
            }
            allocations.append(allocation)
            movement = mongo.db[FARMER_MOVEMENT_COLLECTION].insert_one({
                "farmer_user_id": profile["user_id"], "farmer_user_id_str": profile["user_id_str"],
                "product_key": product_key, "product_name": product_name, "unit_code": lot.get("unit_code") or "KG",
                "lot_id": lot["_id"], "lot_number": lot.get("lot_number") or "", "movement_type": "pos_sale_out",
                "quantity": float(take), "direction": "out", "reference_type": "pos_sale", "reference_id": sale_id,
                "reference_number": str(sale_id), "note": "Farmer POS direct sale.", "created_at": now_utc(),
            })
            allocation["movement_id"] = movement.inserted_id
            remaining -= take
        if remaining > EPS:
            raise RuntimeError(f"Produce stock for {product_name} changed while the POS sale was being saved. Refresh and try again.")
        return allocations
    except Exception:
        _rollback_allocations(allocations, remove_movements=True)
        raise

def _rollback_allocations(allocations, *, remove_movements=False, reversal_reason=""):
    """Restore stock after a failed/voided POS transaction.

    Failed saves remove the provisional movement records. A user-approved void
    keeps the original sale movements and adds a distinct reversal movement so
    the stock ledger remains auditable.
    """
    for allocation in reversed(allocations or []):
        pool = allocation.get("pool")
        lot_id = _to_object_id(allocation.get("lot_id"))
        quantity = float(_decimal(allocation.get("quantity")))
        if not lot_id or quantity <= 0:
            continue
        if pool == "input":
            mongo.db[UFC_INPUT_LOT_COLLECTION].update_one(
                {"_id": lot_id},
                {"$inc": {"available_quantity": quantity, "issued_quantity": -quantity}, "$set": {"updated_at": now_utc()}},
            )
        elif pool == "output":
            mongo.db[UFC_OUTPUT_LOT_COLLECTION].update_one(
                {"_id": lot_id},
                {"$inc": {"available_quantity": quantity}, "$set": {"status": "active", "updated_at": now_utc()}},
            )
        elif pool == "farmer_output":
            mongo.db[FARMER_LOT_COLLECTION].update_one(
                {"_id": lot_id},
                {"$inc": {"available_quantity": quantity, "sold_quantity": -quantity}, "$set": {"status": "active", "updated_at": now_utc()}},
            )

        if remove_movements:
            movement_id = _to_object_id(allocation.get("movement_id"))
            if movement_id and allocation.get("movement_collection"):
                mongo.db[allocation["movement_collection"]].delete_one({"_id": movement_id})
            elif allocation.get("posting_key") and allocation.get("movement_collection"):
                key_field = "source_posting_key" if allocation.get("movement_collection") == UFC_INPUT_MOVEMENT_COLLECTION else "posting_key"
                mongo.db[allocation["movement_collection"]].delete_one({key_field: allocation.get("posting_key")})
        else:
            sale_id = _to_object_id(allocation.get("sale_id")) or allocation.get("sale_id")
            reversal_key = f"POS-VOID:{allocation.get('sale_id')}:{allocation.get('line_no')}:{allocation.get('lot_id')}"
            mongo.db[POS_MOVEMENT_COLLECTION].update_one(
                {"posting_key": reversal_key},
                {"$setOnInsert": {
                    "posting_key": reversal_key, "stock_pool": pool, "sale_id": sale_id,
                    "sale_id_str": str(allocation.get("sale_id") or ""), "lot_id": lot_id,
                    "lot_id_str": str(lot_id), "product_name": allocation.get("product_name") or "Product",
                    "quantity": quantity, "direction": "in", "movement_type": "pos_sale_void",
                    "reason": _clean(reversal_reason, 500) or "POS sale voided; stock restored.",
                    "created_at": now_utc(),
                }},
                upsert=True,
            )

def _ensure_receivable(sale, invoice):
    existing = mongo.db[POS_RECEIVABLE_COLLECTION].find_one({"invoice_id": invoice["_id"]})
    if existing:
        return existing
    total = _decimal(invoice.get("grand_total"))
    document = {
        "pos_sale_id": sale["_id"], "pos_sale_id_str": str(sale["_id"]),
        "invoice_id": invoice["_id"], "invoice_id_str": str(invoice["_id"]),
        "document_number": invoice.get("document_number") or "", "seller_type": sale.get("seller_type"),
        "seller_key": sale.get("seller_key"), "seller_name": sale.get("seller_name"),
        "buyer": sale.get("buyer") or {}, "total_amount": float(total), "amount_paid": 0.0,
        "outstanding_amount": float(total), "payment_status": "unpaid", "status": "open",
        "due_date": invoice.get("due_date") or "", "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[POS_RECEIVABLE_COLLECTION].insert_one(document); document["_id"] = result.inserted_id
        return document
    except DuplicateKeyError:
        return mongo.db[POS_RECEIVABLE_COLLECTION].find_one({"invoice_id": invoice["_id"]}) or document


def _build_ufc_invoice(sale, seller, buyer):
    existing = mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale["_id"]})
    if existing:
        return existing
    has_tax = any(_decimal(item.get("gst_amount")) > 0 for item in sale.get("items") or [])
    if seller.get("gst_registered") and has_tax:
        title, doc_type = "TAX INVOICE", "tax_invoice"
    elif seller.get("gst_registered"):
        title, doc_type = "BILL OF SUPPLY", "bill_of_supply"
    else:
        title, doc_type = "SALES RECEIPT", "sales_receipt"
    due = datetime.strptime(_date_iso(sale.get("sale_date")), "%Y-%m-%d").date()
    if sale.get("payment_term") == "credit":
        due += timedelta(days=max(int(sale.get("credit_days") or 0), 0))
    warnings = [item.get("document_warning") for item in sale.get("items") or [] if item.get("document_warning")]
    if seller.get("gst_warning"):
        warnings.append(seller.get("gst_warning"))
    invoice = {
        "document_number": _next_number(f"ufc_pos_invoice:{sale.get('seller_key')}", f"{sale.get('seller_key')}/POS/SI"),
        "document_title": title, "document_type": doc_type, "pos_sale_id": sale["_id"], "pos_sale_id_str": str(sale["_id"]),
        "sale_number": sale.get("sale_number") or "", "seller_type": "ufc", "seller_key": sale.get("seller_key"),
        "seller": seller, "buyer": buyer, "items": sale.get("items") or [], "subtotal": sale.get("subtotal") or 0,
        "discount_total": sale.get("discount_total") or 0, "taxable_value": sale.get("taxable_value") or 0,
        "cgst_amount": sale.get("cgst_amount") or 0, "sgst_amount": sale.get("sgst_amount") or 0,
        "igst_amount": sale.get("igst_amount") or 0, "gst_amount": sale.get("gst_amount") or 0,
        "grand_total": sale.get("grand_total") or 0, "payment_term": sale.get("payment_term") or "pay_now",
        "payment_term_label": PAYMENT_TERMS.get(sale.get("payment_term"), "Pay Now"), "due_date": due.isoformat(),
        "payment_status": "unpaid", "amount_paid": 0.0, "paid_amount": 0.0,
        "outstanding_amount": sale.get("grand_total") or 0, "payment_version": 0, "payment_ids": [],
        "status": "issued", "document_warning": " ".join(dict.fromkeys(warnings))[:1000],
        "issued_at": now_utc(), "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[POS_INVOICE_COLLECTION].insert_one(invoice); invoice["_id"] = result.inserted_id; return invoice
    except DuplicateKeyError:
        return mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale["_id"]}) or invoice


def _build_farmer_invoice(sale, profile, buyer):
    existing = mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale["_id"]})
    if existing:
        return existing
    due = datetime.strptime(_date_iso(sale.get("sale_date")), "%Y-%m-%d").date()
    if sale.get("payment_term") == "credit":
        due += timedelta(days=max(int(sale.get("credit_days") or 0), 0))
    seller = {
        "type": "farmer", "key": profile["user_id_str"], "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"], "name": profile["name"], "phone": profile["phone"],
        "state": profile.get("state") or "", "district": profile.get("district") or "",
        "block": profile.get("block") or "", "village": profile.get("village") or "", "address": profile.get("address") or "",
    }
    invoice = {
        "document_number": _next_number(f"farmer_pos_invoice:{profile['user_id_str']}", "FPOS-RCPT"),
        "document_title": "FARMER SALES RECEIPT", "document_type": "farmer_sales_receipt",
        "pos_sale_id": sale["_id"], "pos_sale_id_str": str(sale["_id"]), "sale_number": sale.get("sale_number") or "",
        "seller_type": "farmer", "seller_key": profile["user_id_str"], "seller_user_id": profile["user_id"],
        "seller_user_id_str": profile["user_id_str"], "seller": seller, "buyer": buyer, "items": sale.get("items") or [],
        "subtotal": sale.get("subtotal") or 0, "discount_total": sale.get("discount_total") or 0,
        "taxable_value": sale.get("taxable_value") or sale.get("grand_total") or 0, "gst_amount": 0.0,
        "grand_total": sale.get("grand_total") or 0, "payment_term": sale.get("payment_term") or "pay_now",
        "payment_term_label": PAYMENT_TERMS.get(sale.get("payment_term"), "Pay Now"), "due_date": due.isoformat(),
        "payment_status": "unpaid", "amount_paid": 0.0, "paid_amount": 0.0,
        "outstanding_amount": sale.get("grand_total") or 0, "payment_version": 0, "payment_ids": [],
        "status": "issued", "tax_note": "Farmer direct-produce POS receipt. GST is not charged in the current Farmer seller workflow.",
        "issued_at": now_utc(), "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[POS_INVOICE_COLLECTION].insert_one(invoice); invoice["_id"] = result.inserted_id; return invoice
    except DuplicateKeyError:
        return mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale["_id"]}) or invoice


def create_ufc_pos_sale(actor_user_id, centre_uid_hint, cart_json, *, buyer_type="walk_in", farmer_id="", buyer_name="", buyer_phone="", buyer_address="", buyer_state="", buyer_gstin="", payment_term="pay_now", credit_days=0, sale_date=None, note="", idempotency_key="", mitra_uid=""):
    _ensure_indexes()
    actor, master, centre_uid, centre_name, seller = _resolve_ufc(actor_user_id, centre_uid_hint)
    token = _clean(idempotency_key, 160) or f"UFC-POS-{uuid4().hex.upper()}"
    existing = mongo.db[POS_SALE_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        return _sale_result(existing, "This POS sale was already saved safely.", True)
    cart = _parse_cart(cart_json)
    catalog = {item["stock_key"]: item for item in (_input_catalog(centre_uid) + _output_catalog(centre_uid))}
    buyer = _ufc_buyer_snapshot(centre_uid, buyer_type, {
        "farmer_id": farmer_id, "buyer_name": buyer_name, "buyer_phone": buyer_phone,
        "buyer_address": buyer_address, "buyer_state": buyer_state, "buyer_gstin": buyer_gstin, "mitra_uid": mitra_uid,
    })
    term = str(payment_term or "pay_now").strip().lower()
    if term not in PAYMENT_TERMS:
        raise ValueError("Choose Pay Now or Credit / Pay Later.")
    try:
        days = max(min(int(credit_days or 0), 365), 0)
    except Exception:
        raise ValueError("Credit days must be a whole number.")
    if term != "credit":
        days = 0
    sale_day = _date_iso(sale_date)
    if datetime.strptime(sale_day, "%Y-%m-%d").date() > business_today():
        raise ValueError("POS sale date cannot be in the future.")

    sale_number = _next_number(f"ufc_pos_sale:{centre_uid}", f"{centre_uid}/POS")
    sale_doc = {
        "schema_version": 2, "sale_number": sale_number, "idempotency_key": token,
        "seller_type": "ufc", "seller_key": centre_uid, "seller_name": centre_name, "centre_uid": centre_uid,
        "seller": seller, "buyer": buyer, "buyer_type": buyer.get("type"), "sale_date": sale_day,
        # Compatibility fields keep Farmer finance/insurance eligibility and old
        # management views working while the new multi-item POS schema is adopted.
        "sale_type": "registered" if buyer.get("type") == "registered_farmer" else "unregistered",
        "farmer_id": buyer.get("farmer_master_id_str") if buyer.get("type") == "registered_farmer" else "",
        "farmer_name": buyer.get("name") or "", "farmer_phone": buyer.get("phone") or "",
        "sale_source": "unified_pos", "source_reference": f"POSV2-{token}",
        "accounting_status": "not_posted", "migration_status": "pos_v2",
        "payment_term": term, "payment_term_label": PAYMENT_TERMS[term], "credit_days": days,
        "status": "processing", "payment_status": "unpaid", "amount_paid": 0.0, "outstanding_amount": 0.0,
        "note": _clean(note, 1000), "mitra_uid": buyer.get("mitra_uid") or _clean(mitra_uid, 80),
        "created_by": actor["_id"], "created_by_name": actor.get("resolved_name") or "",
        "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[POS_SALE_COLLECTION].insert_one(sale_doc); sale_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[POS_SALE_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            return _sale_result(existing, "This POS sale was already saved safely.", True)
        raise RuntimeError("POS sale could not be started safely. Refresh and try again.")

    all_allocations = []
    items = []
    try:
        subtotal = discount_total = taxable_total = cgst_total = sgst_total = igst_total = gst_total = grand_total = cogs_total = Decimal("0")
        bonus_base_total = bonus_total = Decimal("0")
        for line_no, cart_item in enumerate(cart, start=1):
            catalog_item = catalog.get(cart_item["stock_key"])
            if not catalog_item:
                raise ValueError("One selected stock item is no longer available. Refresh POS and add it again.")
            qty_value = cart_item["quantity"]
            available = _decimal(catalog_item.get("available_quantity"))
            if qty_value > available + EPS:
                raise ValueError(f"Only {catalog_item.get('available_display')} {catalog_item.get('unit_code')} of {catalog_item.get('product_name')} is available.")
            tax = _line_tax_for_ufc(catalog_item, seller, buyer, qty_value, cart_item["unit_price"], cart_item["discount_percent"])
            if catalog_item.get("source_type") == "input":
                allocations = _allocate_ufc_input(centre_uid, catalog_item.get("product_id"), qty_value, sale_doc["_id"], line_no, actor, catalog_item.get("product_name"))
            else:
                allocations = _allocate_ufc_output(centre_uid, catalog_item.get("product_key"), catalog_item.get("unit_code"), qty_value, sale_doc["_id"], line_no, catalog_item.get("product_name"), catalog_item.get("variety") or "", catalog_item.get("grade") or "")
            all_allocations.extend(allocations)
            cogs = sum((_decimal(a.get("cogs")) for a in allocations), Decimal("0"))
            line_bonus_percentage = Decimal("0")
            line_bonus_amount = Decimal("0")
            if catalog_item.get("source_type") == "input" and (buyer.get("mitra_uid") or _clean(mitra_uid, 80)):
                line_bonus_percentage = _mitra_bonus_percentage(
                    buyer.get("mitra_uid") or _clean(mitra_uid, 80),
                    catalog_item.get("category") or "all",
                )
                line_bonus_amount = (tax["line_total"] * line_bonus_percentage / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)
                bonus_base_total += tax["line_total"]
                bonus_total += line_bonus_amount
            line = {
                "line_no": line_no, "stock_key": catalog_item["stock_key"], "source_type": catalog_item.get("source_type"),
                "source_label": catalog_item.get("source_label"), "product_id": _to_object_id(catalog_item.get("product_id")),
                "product_id_str": catalog_item.get("product_id") or "", "product_key": catalog_item.get("product_key") or "",
                "product_name": catalog_item.get("product_name") or "Product", "product_code": catalog_item.get("product_code") or "",
                "category": catalog_item.get("category") or "", "unit_code": catalog_item.get("unit_code") or "Unit",
                "quantity": float(qty_value), "quantity_display": _qty(qty_value), "unit_price": float(cart_item["unit_price"]),
                "unit_price_display": _money(cart_item["unit_price"]), "discount_percent": float(cart_item["discount_percent"]),
                "discount_amount": float(tax["discount_amount"]), "taxable_value": float(tax["taxable_value"]),
                "hsn_code": tax["hsn_code"], "taxability_code": tax["taxability_code"], "gst_rate": float(tax["gst_rate"]),
                "cgst_amount": float(tax["cgst"]), "sgst_amount": float(tax["sgst"]), "igst_amount": float(tax["igst"]),
                "gst_amount": float(tax["gst_amount"]), "line_total": float(tax["line_total"]),
                "document_warning": tax.get("warning") or "", "allocations": allocations, "cogs": float(cogs),
                "gross_margin": float(tax["line_total"] - cogs),
                "bonus_type": "avpl_product_sale" if catalog_item.get("source_type") == "input" else "",
                "bonus_percentage": float(line_bonus_percentage), "bonus_amount": float(line_bonus_amount),
            }
            items.append(line)
            subtotal += tax["gross"]; discount_total += tax["discount_amount"]; taxable_total += tax["taxable_value"]
            cgst_total += tax["cgst"]; sgst_total += tax["sgst"]; igst_total += tax["igst"]; gst_total += tax["gst_amount"]
            grand_total += tax["line_total"]; cogs_total += cogs

        patch = {
            "items": items, "subtotal": float(subtotal), "discount_total": float(discount_total),
            "taxable_value": float(taxable_total), "cgst_amount": float(cgst_total), "sgst_amount": float(sgst_total),
            "igst_amount": float(igst_total), "gst_amount": float(gst_total), "grand_total": float(grand_total),
            "total_amount": float(grand_total), "cogs": float(cogs_total), "gross_margin": float(grand_total - cogs_total),
            "product_name": items[0].get("product_name") if len(items) == 1 else f"{len(items)} POS items",
            "product_category": items[0].get("category") if len(items) == 1 else "Mixed",
            "quantity": items[0].get("quantity") if len(items) == 1 else 0.0,
            "unit_price": items[0].get("unit_price") if len(items) == 1 else 0.0,
            "bonus_type": "avpl_product_sale",
            "bonus_amount": float(bonus_total),
            "bonus_percentage": float((bonus_total * Decimal("100") / bonus_base_total) if bonus_base_total > EPS else Decimal("0")),
            "stock_allocations": all_allocations, "status": "completed", "outstanding_amount": float(grand_total), "updated_at": now_utc(),
        }
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"], "status": "processing"}, {"$set": patch})
        sale_doc = mongo.db[POS_SALE_COLLECTION].find_one({"_id": sale_doc["_id"]}) or {**sale_doc, **patch}
        invoice = _build_ufc_invoice(sale_doc, seller, buyer)
        receivable = _ensure_receivable(sale_doc, invoice)
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {
            "invoice_id": invoice["_id"], "invoice_id_str": str(invoice["_id"]), "document_number": invoice.get("document_number") or "",
            "invoice_no": invoice.get("document_number") or "",
            "receivable_id": receivable.get("_id"), "receivable_id_str": str(receivable.get("_id") or ""), "updated_at": now_utc(),
        }})
        sale_doc = mongo.db[POS_SALE_COLLECTION].find_one({"_id": sale_doc["_id"]}) or sale_doc
        return _sale_result(sale_doc, "POS sale completed. Stock and invoice were updated automatically.", False)
    except Exception as exc:
        _rollback_allocations(all_allocations, remove_movements=True)
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {"status": "failed", "failure_reason": _clean(exc, 800), "updated_at": now_utc()}})
        raise


def create_farmer_pos_sale(actor_user_id, cart_json, *, buyer_type="local_buyer", buyer_name="", buyer_phone="", buyer_address="", buyer_state="", payment_term="pay_now", credit_days=0, sale_date=None, note="", idempotency_key=""):
    _ensure_indexes()
    actor, master, profile = _resolve_farmer(actor_user_id)
    token = _clean(idempotency_key, 160) or f"FARMER-POS-{uuid4().hex.upper()}"
    existing = mongo.db[POS_SALE_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        return _sale_result(existing, "This Farmer POS sale was already saved safely.", True)
    cart = _parse_cart(cart_json)
    catalog = {item["stock_key"]: item for item in _farmer_catalog(profile)}
    buyer = _farmer_buyer_snapshot(profile, buyer_type, {"buyer_name": buyer_name, "buyer_phone": buyer_phone, "buyer_address": buyer_address, "buyer_state": buyer_state})
    term = str(payment_term or "pay_now").strip().lower()
    if term not in PAYMENT_TERMS:
        raise ValueError("Choose Pay Now or Credit / Pay Later.")
    try:
        days = max(min(int(credit_days or 0), 365), 0)
    except Exception:
        raise ValueError("Credit days must be a whole number.")
    if term != "credit": days = 0
    sale_day = _date_iso(sale_date)
    if datetime.strptime(sale_day, "%Y-%m-%d").date() > business_today():
        raise ValueError("Sale date cannot be in the future.")

    sale_number = _next_number(f"farmer_pos_sale:{profile['user_id_str']}", "FPOS")
    sale_doc = {
        "schema_version": 2, "sale_number": sale_number, "idempotency_key": token,
        "seller_type": "farmer", "seller_key": profile["user_id_str"], "seller_name": profile["name"],
        "seller_user_id": profile["user_id"], "seller_user_id_str": profile["user_id_str"], "farmer_master_id": profile["farmer_master_id"],
        "centre_uid": profile.get("centre_uid") or "", "buyer": buyer, "buyer_type": buyer.get("type"), "sale_date": sale_day,
        "payment_term": term, "payment_term_label": PAYMENT_TERMS[term], "credit_days": days,
        "status": "processing", "payment_status": "unpaid", "amount_paid": 0.0, "outstanding_amount": 0.0,
        "note": _clean(note, 1000), "created_by": actor["_id"], "created_by_name": actor.get("resolved_name") or "",
        "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[POS_SALE_COLLECTION].insert_one(sale_doc); sale_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[POS_SALE_COLLECTION].find_one({"idempotency_key": token})
        if existing: return _sale_result(existing, "This Farmer POS sale was already saved safely.", True)
        raise RuntimeError("Farmer POS sale could not be started safely. Refresh and try again.")

    all_allocations = []
    items = []
    try:
        subtotal = discount_total = grand_total = cogs_total = Decimal("0")
        for line_no, cart_item in enumerate(cart, start=1):
            catalog_item = catalog.get(cart_item["stock_key"])
            if not catalog_item:
                raise ValueError("One produce item is no longer available. Refresh POS and add it again.")
            qty_value = cart_item["quantity"]
            if qty_value > _decimal(catalog_item.get("available_quantity")) + EPS:
                raise ValueError(f"Only {catalog_item.get('available_display')} {catalog_item.get('unit_code')} of {catalog_item.get('product_name')} is available.")
            gross = qty_value * cart_item["unit_price"]
            discount_amount = (gross * cart_item["discount_percent"] / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)
            line_total = max(gross - discount_amount, Decimal("0")).quantize(MONEY, rounding=ROUND_HALF_UP)
            allocations = _allocate_farmer_stock(profile, catalog_item.get("product_key"), qty_value, sale_doc["_id"], line_no, catalog_item.get("product_name"), catalog_item.get("variety") or "", catalog_item.get("grade") or "")
            all_allocations.extend(allocations)
            cogs = sum((_decimal(a.get("cogs")) for a in allocations), Decimal("0"))
            items.append({
                "line_no": line_no, "stock_key": catalog_item["stock_key"], "source_type": "farmer_output", "source_label": "My Produce Stock",
                "product_key": catalog_item.get("product_key") or "", "product_name": catalog_item.get("product_name") or "Produce",
                "variety": catalog_item.get("variety") or "", "grade": catalog_item.get("grade") or "", "unit_code": catalog_item.get("unit_code") or "KG",
                "quantity": float(qty_value), "quantity_display": _qty(qty_value), "unit_price": float(cart_item["unit_price"]),
                "unit_price_display": _money(cart_item["unit_price"]), "discount_percent": float(cart_item["discount_percent"]),
                "discount_amount": float(discount_amount), "taxable_value": float(line_total), "gst_rate": 0.0, "gst_amount": 0.0,
                "line_total": float(line_total), "allocations": allocations, "cogs": float(cogs), "gross_margin": float(line_total - cogs),
            })
            subtotal += gross; discount_total += discount_amount; grand_total += line_total; cogs_total += cogs
        patch = {
            "items": items, "subtotal": float(subtotal), "discount_total": float(discount_total), "taxable_value": float(grand_total),
            "gst_amount": 0.0, "grand_total": float(grand_total), "total_amount": float(grand_total), "cogs": float(cogs_total),
            "gross_margin": float(grand_total - cogs_total), "stock_allocations": all_allocations, "status": "completed",
            "outstanding_amount": float(grand_total), "updated_at": now_utc(),
        }
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"], "status": "processing"}, {"$set": patch})
        sale_doc = mongo.db[POS_SALE_COLLECTION].find_one({"_id": sale_doc["_id"]}) or {**sale_doc, **patch}
        invoice = _build_farmer_invoice(sale_doc, profile, buyer)
        receivable = _ensure_receivable(sale_doc, invoice)
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {
            "invoice_id": invoice["_id"], "invoice_id_str": str(invoice["_id"]), "document_number": invoice.get("document_number") or "",
            "invoice_no": invoice.get("document_number") or "",
            "receivable_id": receivable.get("_id"), "receivable_id_str": str(receivable.get("_id") or ""), "updated_at": now_utc(),
        }})
        sale_doc = mongo.db[POS_SALE_COLLECTION].find_one({"_id": sale_doc["_id"]}) or sale_doc
        return _sale_result(sale_doc, "Farmer POS sale completed. Produce stock and receipt were updated automatically.", False)
    except Exception as exc:
        _rollback_allocations(all_allocations, remove_movements=True)
        mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {"status": "failed", "failure_reason": _clean(exc, 800), "updated_at": now_utc()}})
        raise


def _sale_result(sale, message, idempotent=False):
    invoice = mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale.get("_id")}) or {}
    return {"sale": serialize_sale(sale), "invoice": serialize_invoice(invoice), "message": message, "idempotent_replay": idempotent}


def serialize_sale(sale):
    if not sale:
        return {}
    row = dict(sale)
    row["id"] = str(row.get("_id") or "")
    row["invoice_id_str"] = str(row.get("invoice_id") or row.get("invoice_id_str") or "")
    row["grand_total_display"] = _money(row.get("grand_total") or row.get("total_amount"))
    row["subtotal_display"] = _money(row.get("subtotal") or row.get("grand_total") or row.get("total_amount"))
    row["discount_total_display"] = _money(row.get("discount_total"))
    row["gst_amount_display"] = _money(row.get("gst_amount"))
    row["amount_paid_display"] = _money(row.get("amount_paid"))
    row["outstanding_amount_display"] = _money(row.get("outstanding_amount") if row.get("outstanding_amount") is not None else row.get("grand_total"))
    row["cogs_display"] = _money(row.get("cogs"))
    row["gross_margin_display"] = _money(row.get("gross_margin"))
    row["payment_status_label"] = str(row.get("payment_status") or "unpaid").replace("_", " ").title()
    row["payment_term_label"] = PAYMENT_TERMS.get(row.get("payment_term"), str(row.get("payment_term") or "").replace("_", " ").title())
    row["buyer_name"] = (row.get("buyer") or {}).get("name") or row.get("farmer_name") or "Buyer"
    items = []
    for line in row.get("items") or []:
        item = dict(line)
        item["quantity_display"] = item.get("quantity_display") or _qty(item.get("quantity"))
        item["unit_price_display"] = item.get("unit_price_display") or _money(item.get("unit_price"))
        item["discount_amount_display"] = _money(item.get("discount_amount"))
        item["gst_amount_display"] = _money(item.get("gst_amount"))
        item["line_total_display"] = _money(item.get("line_total"))
        items.append(item)
    row["items"] = items
    return row


def serialize_invoice(invoice):
    if not invoice:
        return {}
    row = dict(invoice)
    row["id"] = str(row.get("_id") or "")
    row["pos_sale_id_str"] = str(row.get("pos_sale_id") or "")
    for field in ["subtotal", "discount_total", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount", "gst_amount", "grand_total", "amount_paid", "outstanding_amount"]:
        row[f"{field}_display"] = _money(row.get(field))
    row["payment_status_label"] = str(row.get("payment_status") or "unpaid").replace("_", " ").title()
    items = []
    for line in row.get("items") or []:
        item = dict(line)
        item["quantity_display"] = item.get("quantity_display") or _qty(item.get("quantity"))
        item["unit_price_display"] = item.get("unit_price_display") or _money(item.get("unit_price"))
        item["discount_amount_display"] = _money(item.get("discount_amount"))
        item["taxable_value_display"] = _money(item.get("taxable_value"))
        item["cgst_amount_display"] = _money(item.get("cgst_amount"))
        item["sgst_amount_display"] = _money(item.get("sgst_amount"))
        item["igst_amount_display"] = _money(item.get("igst_amount"))
        item["gst_amount_display"] = _money(item.get("gst_amount"))
        item["line_total_display"] = _money(item.get("line_total"))
        items.append(item)
    row["items"] = items
    return row


def get_pos_sale_context(actor_user_id, sale_id):
    _ensure_indexes()
    actor = _get_user(actor_user_id)
    oid = _to_object_id(sale_id)
    if not oid:
        raise ValueError("Invalid POS sale reference.")
    sale = mongo.db[POS_SALE_COLLECTION].find_one({"_id": oid}) or {}
    if not sale:
        raise ValueError("POS sale was not found.")
    seller_type = sale.get("seller_type")
    if seller_type == "ufc":
        if actor.get("resolved_role") == "ufc_admin":
            _, _, centre_uid, _, _ = _resolve_ufc(actor_user_id, sale.get("seller_key") or sale.get("centre_uid"))
            if str(sale.get("seller_key") or sale.get("centre_uid")) != centre_uid:
                raise PermissionError("This POS sale does not belong to your UFC Centre.")
        elif actor.get("resolved_role") not in {"super_admin", "avpl_admin", "accounts", "sales_unnatfarm"}:
            raise PermissionError("You cannot view this UFC POS sale.")
    elif seller_type == "farmer":
        if actor.get("resolved_role") != "farmer" or str(sale.get("seller_key") or sale.get("seller_user_id")) != str(actor.get("_id")):
            raise PermissionError("This Farmer POS sale does not belong to you.")
    else:
        # Legacy POS row: keep old invoice links readable for authorized users.
        if actor.get("resolved_role") not in {"ufc_admin", "super_admin", "avpl_admin", "accounts", "sales_unnatfarm"}:
            raise PermissionError("You cannot view this legacy POS sale.")
        legacy_centre = str(sale.get("centre_uid") or "")
        if actor.get("resolved_role") == "ufc_admin":
            _, _, centre_uid, _, _ = _resolve_ufc(actor_user_id, legacy_centre)
            if legacy_centre and legacy_centre != centre_uid:
                raise PermissionError("This POS sale belongs to another UFC Centre.")
    invoice = mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": oid}) or {}
    payments = []
    if invoice:
        for p in mongo.db.payments.find({"invoice_id": invoice.get("_id"), "source_type": {"$in": ["ufc_pos_invoice", "farmer_pos_invoice"]}, "status": {"$in": ["completed", "reversed"]}}).sort("created_at", DESCENDING):
            item = dict(p); item["id"] = str(item.get("_id") or ""); item["amount_display"] = _money(item.get("amount")); item["mode_label"] = str(item.get("payment_mode") or "").replace("_", " ").title(); item["status_label"] = str(item.get("status") or "").replace("_", " ").title(); payments.append(item)
    if not invoice and sale.get("invoice_no"):
        # Read-only bridge for legacy rows created before POS v2.
        invoice = {
            "document_number": sale.get("invoice_no"), "document_title": "LEGACY POS INVOICE", "document_type": "legacy",
            "seller_type": "ufc", "seller_key": sale.get("centre_uid") or "", "seller": {"legal_name": sale.get("centre_uid") or "UFC", "centre_uid": sale.get("centre_uid") or ""},
            "buyer": {"name": sale.get("farmer_name") or "Customer", "phone": sale.get("farmer_phone") or ""},
            "items": [{"product_name": sale.get("product_name") or "Product", "quantity": sale.get("quantity") or 0, "unit_code": sale.get("unit_code") or "Unit", "unit_price": sale.get("unit_price") or 0, "line_total": sale.get("total_amount") or 0, "gst_amount": 0}],
            "grand_total": sale.get("total_amount") or 0, "amount_paid": sale.get("amount_paid") or 0, "outstanding_amount": sale.get("outstanding_amount") or 0, "payment_status": sale.get("payment_status") or "not_recorded", "status": "legacy",
        }
    return {"sale": serialize_sale(sale), "invoice": serialize_invoice(invoice), "payments": payments, "payment_token": f"POS-PAY-{uuid4().hex.upper()}"}


def void_pos_sale(actor_user_id, sale_id, reason):
    _ensure_indexes()
    context = get_pos_sale_context(actor_user_id, sale_id)
    sale = mongo.db[POS_SALE_COLLECTION].find_one({"_id": _to_object_id(sale_id)}) or {}
    if sale.get("schema_version") != 2:
        raise ValueError("Legacy POS sales cannot be voided through the new POS. Use the existing accounting correction process.")
    if sale.get("status") == "voided":
        return {"message": "This POS sale is already voided."}
    if sale.get("status") != "completed":
        raise ValueError("Only a completed POS sale can be voided.")
    invoice = mongo.db[POS_INVOICE_COLLECTION].find_one({"pos_sale_id": sale["_id"]}) or {}
    paid = _decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount"))
    completed_payments = mongo.db.payments.count_documents({"invoice_id": invoice.get("_id"), "source_type": {"$in": ["ufc_pos_invoice", "farmer_pos_invoice"]}, "status": "completed"}) if invoice else 0
    if paid > MONEY / 2 or completed_payments:
        raise ValueError("Reverse the POS payment first. A paid sale cannot be voided while money is still settled against it.")
    clean_reason = _clean(reason, 800)
    if len(clean_reason) < 4:
        raise ValueError("Enter a clear reason for voiding the POS sale.")
    allocations = sale.get("stock_allocations") or []
    if not allocations:
        raise RuntimeError("Stock allocation history is missing. Do not void this sale automatically; ask an administrator to review it.")
    _rollback_allocations(allocations, remove_movements=False, reversal_reason=clean_reason)
    timestamp = now_utc()
    mongo.db[POS_SALE_COLLECTION].update_one({"_id": sale["_id"]}, {"$set": {"status": "voided", "void_reason": clean_reason, "voided_at": timestamp, "updated_at": timestamp}})
    if invoice:
        mongo.db[POS_INVOICE_COLLECTION].update_one({"_id": invoice["_id"]}, {"$set": {"status": "voided", "void_reason": clean_reason, "voided_at": timestamp, "updated_at": timestamp}})
        mongo.db[POS_RECEIVABLE_COLLECTION].update_one({"invoice_id": invoice["_id"]}, {"$set": {"status": "voided", "outstanding_amount": 0.0, "updated_at": timestamp}})
    return {"message": "POS sale voided and stock restored to the original stock pool(s)."}


def get_ufc_output_stock_overview(actor_user_id, centre_uid_hint=None, search=""):
    _actor, _master, centre_uid, centre_name, _seller = _resolve_ufc(actor_user_id, centre_uid_hint)
    rows = _output_catalog(centre_uid)
    text = _clean(search, 120).casefold()
    if text:
        rows = [r for r in rows if text in f"{r.get('product_name','')} {r.get('source_farmer_names','')}".casefold()]
    total_value = sum((_decimal(r.get("available_quantity")) * _decimal(r.get("unit_cost")) for r in rows), Decimal("0"))
    return {"rows": rows, "centre_uid": centre_uid, "centre_name": centre_name, "query": search or "", "summary": {"product_count": len(rows), "stock_value": _money(total_value)}}
