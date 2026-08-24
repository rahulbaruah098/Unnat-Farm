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


PRODUCTION_COLLECTION = "farmer_production_entries"
LOT_COLLECTION = "farmer_produce_lots"
MOVEMENT_COLLECTION = "farmer_produce_movements"
SALE_COLLECTION = "farmer_external_sales"
INVOICE_COLLECTION = "farmer_external_sales_invoices"
RECEIVABLE_COLLECTION = "farmer_external_receivables"
EXPENSE_COLLECTION = "farmer_production_expenses"
EXTERNAL_PURCHASE_COLLECTION = "farmer_external_purchases"
EXTERNAL_PURCHASE_INVOICE_COLLECTION = "farmer_external_purchase_invoices"
EXTERNAL_PAYABLE_COLLECTION = "farmer_external_payables"
AUDIT_COLLECTION = "farmer_production_audit"
MARKETPLACE_SALE_COLLECTION = "farmer_marketplace_sales"
MARKETPLACE_PURCHASE_COLLECTION = "farmer_marketplace_purchase_entries"

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.001")

UNIT_CHOICES = {
    "KG": "Kilogram (KG)",
    "QUINTAL": "Quintal",
    "TON": "Ton",
    "LITRE": "Litre",
    "PIECE": "Piece",
    "DOZEN": "Dozen",
    "TRAY": "Tray",
    "BAG": "Bag",
    "BUNDLE": "Bundle",
}

BUYER_TYPES = {
    "trader": "Trader / Wholesaler",
    "local_buyer": "Local Buyer",
    "mapped_ufc": "My UFC Centre",
    "other": "Other Buyer",
}

EXPENSE_CATEGORIES = {
    "labour": "Labour",
    "transport": "Transport",
    "feed": "Feed / Fodder",
    "seed": "Seed / Planting Material",
    "fertilizer": "Fertilizer / Manure",
    "medicine": "Medicine / Treatment",
    "utilities": "Water / Electricity",
    "packaging": "Packaging",
    "other": "Other",
}

PAYMENT_TERM_LABELS = {
    "pay_now": "Pay Now",
    "credit": "Credit / Pay Later",
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


def _clean(value, maximum=300):
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


def _ensure_indexes():
    definitions = [
        (PRODUCTION_COLLECTION, [("production_number", ASCENDING)], {"unique": True, "name": "farmer_production_number_unique"}),
        (PRODUCTION_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "farmer_production_idempotency_unique"}),
        (PRODUCTION_COLLECTION, [("farmer_user_id", ASCENDING), ("harvest_date", DESCENDING)], {"name": "farmer_production_owner_date_idx"}),
        (LOT_COLLECTION, [("lot_number", ASCENDING)], {"unique": True, "name": "farmer_produce_lot_number_unique"}),
        (LOT_COLLECTION, [("farmer_user_id", ASCENDING), ("product_key", ASCENDING), ("harvest_date", ASCENDING)], {"name": "farmer_produce_stock_idx"}),
        (MOVEMENT_COLLECTION, [("farmer_user_id", ASCENDING), ("created_at", DESCENDING)], {"name": "farmer_produce_movement_idx"}),
        (SALE_COLLECTION, [("sale_number", ASCENDING)], {"unique": True, "name": "farmer_external_sale_number_unique"}),
        (SALE_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "farmer_external_sale_idempotency_unique"}),
        (SALE_COLLECTION, [("farmer_user_id", ASCENDING), ("sale_date", DESCENDING)], {"name": "farmer_external_sale_owner_date_idx"}),
        (INVOICE_COLLECTION, [("document_number", ASCENDING)], {"unique": True, "name": "farmer_external_invoice_number_unique"}),
        (INVOICE_COLLECTION, [("farmer_external_sale_id", ASCENDING)], {"unique": True, "name": "farmer_external_invoice_sale_unique"}),
        (RECEIVABLE_COLLECTION, [("farmer_external_sale_id", ASCENDING)], {"unique": True, "name": "farmer_external_receivable_sale_unique"}),
        (EXPENSE_COLLECTION, [("farmer_user_id", ASCENDING), ("expense_date", DESCENDING)], {"name": "farmer_expense_owner_date_idx"}),
        (EXPENSE_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "farmer_expense_idempotency_unique"}),
        (EXTERNAL_PURCHASE_COLLECTION, [("purchase_number", ASCENDING)], {"unique": True, "name": "farmer_external_purchase_number_unique"}),
        (EXTERNAL_PURCHASE_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "farmer_external_purchase_idempotency_unique"}),
        (EXTERNAL_PURCHASE_COLLECTION, [("farmer_user_id", ASCENDING), ("purchase_date", DESCENDING)], {"name": "farmer_external_purchase_owner_date_idx"}),
        (EXTERNAL_PURCHASE_INVOICE_COLLECTION, [("document_number", ASCENDING)], {"unique": True, "name": "farmer_external_purchase_invoice_number_unique"}),
        (EXTERNAL_PURCHASE_INVOICE_COLLECTION, [("farmer_external_purchase_id", ASCENDING)], {"unique": True, "name": "farmer_external_purchase_invoice_purchase_unique"}),
        (EXTERNAL_PAYABLE_COLLECTION, [("farmer_external_purchase_id", ASCENDING)], {"unique": True, "name": "farmer_external_payable_purchase_unique"}),
        (AUDIT_COLLECTION, [("farmer_user_id", ASCENDING), ("created_at", DESCENDING)], {"name": "farmer_production_audit_idx"}),
    ]
    for collection_name, keys, options in definitions:
        try:
            mongo.db[collection_name].create_index(keys, **options)
        except Exception:
            pass


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


def _get_farmer(actor_user_id):
    oid = _to_object_id(actor_user_id)
    if not oid:
        raise ValueError("Please login again.")
    user = mongo.db.users.find_one({"_id": oid}) or {}
    if not user:
        raise ValueError("Farmer account was not found.")
    if str(user.get("role") or "").strip().lower() != "farmer":
        raise PermissionError("Only Farmers can use Produce & Sell.")
    if user.get("active", True) is False or user.get("is_active", True) is False or str(user.get("status") or "").lower() == "inactive":
        raise PermissionError("Inactive Farmer accounts cannot record production or sales.")

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(oid)})
        or mongo.db.farmer_master.find_one({"linked_user_id": oid})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )
    if not farmer:
        raise ValueError("Complete the Farmer profile before recording production.")

    return {
        "user": user,
        "master": farmer,
        "user_id": oid,
        "user_id_str": str(oid),
        "farmer_master_id": farmer.get("_id"),
        "name": farmer.get("name") or user.get("name") or "Farmer",
        "phone": farmer.get("contact_no") or user.get("phone") or "",
        "centre_uid": farmer.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid") or "",
        "mitra_uid": farmer.get("mitra_uid") or user.get("mapped_mitra_uid") or user.get("mitra_uid") or "",
        "state": farmer.get("state") or user.get("state") or "",
        "district": farmer.get("district") or user.get("district") or "",
        "block": farmer.get("block") or user.get("block") or "",
        "village": farmer.get("village") or user.get("village") or "",
    }


def _product_key(product_name, unit_code):
    product = re.sub(r"[^a-z0-9]+", "-", str(product_name or "").lower()).strip("-")
    unit = re.sub(r"[^a-z0-9]+", "-", str(unit_code or "").lower()).strip("-")
    return f"{product}:{unit}"


def _unit_code(value):
    code = str(value or "KG").strip().upper().replace(" ", "_")
    if code not in UNIT_CHOICES:
        raise ValueError("Choose a valid unit.")
    return code


def _product_choices(profile):
    farmer = profile.get("master") or {}
    choices = []
    for item in farmer.get("activities") or []:
        text = _clean(item, 120)
        if text:
            choices.append("Chicken" if text.lower() == "poultry" else text)
            choices.append(text)
    for item in farmer.get("agri_sub_categories") or []:
        text = _clean(item, 120)
        if text:
            choices.append(text)
    for row in mongo.db[PRODUCTION_COLLECTION].find({"farmer_user_id": profile["user_id"]}, {"product_name": 1}).sort("created_at", DESCENDING).limit(50):
        text = _clean(row.get("product_name"), 120)
        if text:
            choices.append(text)
    clean = []
    seen = set()
    for item in choices:
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            clean.append(item)
    return clean


def _audit(profile, action, entity_type, entity_id=None, note=""):
    mongo.db[AUDIT_COLLECTION].insert_one({
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_name": profile["name"],
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_id_str": str(entity_id or ""),
        "note": _clean(note, 1000),
        "created_at": now_utc(),
    })


def record_production(actor_user_id, product_name, quantity, unit_code, *, harvest_date=None, variety="", grade="", estimated_cost=0, notes="", idempotency_key=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    product_name = _clean(product_name, 120)
    if len(product_name) < 2:
        raise ValueError("Enter the produce name.")
    quantity_value = _decimal(quantity)
    if quantity_value <= 0:
        raise ValueError("Produced quantity must be greater than zero.")
    if quantity_value > Decimal("100000000"):
        raise ValueError("Produced quantity is unusually large. Please check it once.")
    unit = _unit_code(unit_code)
    harvest = _date_iso(harvest_date)
    if datetime.strptime(harvest, "%Y-%m-%d").date() > business_today():
        raise ValueError("Harvest / production date cannot be in the future.")
    cost = max(_decimal(estimated_cost), Decimal("0"))
    token = _clean(idempotency_key, 120) or f"PROD-{uuid4().hex.upper()}"

    existing = mongo.db[PRODUCTION_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        if str(existing.get("farmer_user_id") or "") != profile["user_id_str"]:
            raise RuntimeError("This production request token is already in use.")
        return {"production": serialize_production(existing), "message": "This production entry was already saved.", "idempotent_replay": True}

    timestamp = now_utc()
    production_number = _next_number("farmer_production", "PROD")
    lot_number = _next_number("farmer_produce_lot", "HARV")
    product_key = _product_key(product_name, unit)
    document = {
        "production_number": production_number,
        "idempotency_key": token,
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_master_id": profile["farmer_master_id"],
        "farmer_name": profile["name"],
        "centre_uid": profile["centre_uid"],
        "mitra_uid": profile["mitra_uid"],
        "product_name": product_name,
        "product_key": product_key,
        "variety": _clean(variety, 120),
        "grade": _clean(grade, 80),
        "quantity_produced": float(quantity_value),
        "unit_code": unit,
        "harvest_date": harvest,
        "estimated_cost": float(cost),
        "notes": _clean(notes, 1000),
        "lot_number": lot_number,
        "status": "recorded",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[PRODUCTION_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[PRODUCTION_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            return {"production": serialize_production(existing), "message": "This production entry was already saved.", "idempotent_replay": True}
        raise RuntimeError("Production entry could not be saved safely. Refresh and try again.")

    lot = {
        "lot_number": lot_number,
        "production_entry_id": document["_id"],
        "production_entry_id_str": str(document["_id"]),
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_name": profile["name"],
        "centre_uid": profile["centre_uid"],
        "product_name": product_name,
        "product_key": product_key,
        "variety": document["variety"],
        "grade": document["grade"],
        "unit_code": unit,
        "harvest_date": harvest,
        "original_quantity": float(quantity_value),
        "available_quantity": float(quantity_value),
        "sold_quantity": 0.0,
        "waste_quantity": 0.0,
        "reserved_quantity": 0.0,
        "status": "active",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        lot_result = mongo.db[LOT_COLLECTION].insert_one(lot)
        lot["_id"] = lot_result.inserted_id
    except Exception as exc:
        mongo.db[PRODUCTION_COLLECTION].update_one({"_id": document["_id"]}, {"$set": {"status": "stock_creation_failed", "stock_creation_error": _clean(exc, 500), "updated_at": now_utc()}})
        raise RuntimeError("Production was saved but produce stock could not be created. Do not add it again; contact the administrator to repair this entry.")

    mongo.db[MOVEMENT_COLLECTION].insert_one({
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "product_name": product_name,
        "product_key": product_key,
        "unit_code": unit,
        "lot_id": lot["_id"],
        "lot_number": lot_number,
        "movement_type": "production_in",
        "quantity": float(quantity_value),
        "direction": "in",
        "reference_type": "production",
        "reference_id": document["_id"],
        "reference_number": production_number,
        "note": "Produced / harvested stock added.",
        "created_at": timestamp,
    })
    _audit(profile, "record_production", "production", document["_id"], f"{_qty(quantity_value)} {unit} {product_name} added to produce stock.")
    return {"production": serialize_production(document), "message": f"{_qty(quantity_value)} {unit} of {product_name} added to My Produce Stock.", "idempotent_replay": False}


def record_expense(actor_user_id, category, amount, *, expense_date=None, note="", idempotency_key=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    category = str(category or "other").strip().lower()
    if category not in EXPENSE_CATEGORIES:
        category = "other"
    value = _decimal(amount)
    if value <= 0:
        raise ValueError("Expense amount must be greater than zero.")
    token = _clean(idempotency_key, 120) or f"EXP-{uuid4().hex.upper()}"
    existing = mongo.db[EXPENSE_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        return {"expense": existing, "message": "This expense was already saved.", "idempotent_replay": True}
    document = {
        "expense_number": _next_number("farmer_production_expense", "FEXP"),
        "idempotency_key": token,
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_name": profile["name"],
        "category": category,
        "category_label": EXPENSE_CATEGORIES[category],
        "amount": float(value),
        "expense_date": _date_iso(expense_date),
        "note": _clean(note, 500),
        "status": "recorded",
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    try:
        result = mongo.db[EXPENSE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[EXPENSE_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            return {"expense": existing, "message": "This expense was already saved.", "idempotent_replay": True}
        raise RuntimeError("Expense could not be saved safely. Refresh and try again.")
    _audit(profile, "record_expense", "expense", document["_id"], f"₹{_money(value)} · {EXPENSE_CATEGORIES[category]}")
    return {"expense": document, "message": "Expense saved.", "idempotent_replay": False}


def _stock_groups(profile, search=""):
    query = {"farmer_user_id": profile["user_id"], "status": {"$ne": "cancelled"}}
    if search:
        query["$or"] = [
            {"product_name": {"$regex": re.escape(search), "$options": "i"}},
            {"variety": {"$regex": re.escape(search), "$options": "i"}},
            {"lot_number": {"$regex": re.escape(search), "$options": "i"}},
        ]
    groups = {}
    for lot in mongo.db[LOT_COLLECTION].find(query).sort([("harvest_date", ASCENDING), ("created_at", ASCENDING)]):
        key = lot.get("product_key") or _product_key(lot.get("product_name"), lot.get("unit_code"))
        row = groups.setdefault(key, {
            "product_key": key,
            "product_name": lot.get("product_name") or "Produce",
            "unit_code": lot.get("unit_code") or "KG",
            "available": Decimal("0"),
            "produced": Decimal("0"),
            "sold": Decimal("0"),
            "waste": Decimal("0"),
            "reserved": Decimal("0"),
            "lot_count": 0,
            "oldest_harvest": lot.get("harvest_date") or "",
            "latest_harvest": lot.get("harvest_date") or "",
        })
        row["available"] += max(_decimal(lot.get("available_quantity")), Decimal("0"))
        row["produced"] += max(_decimal(lot.get("original_quantity")), Decimal("0"))
        row["sold"] += max(_decimal(lot.get("sold_quantity")), Decimal("0"))
        row["waste"] += max(_decimal(lot.get("waste_quantity")), Decimal("0"))
        row["reserved"] += max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
        row["lot_count"] += 1
        harvest = str(lot.get("harvest_date") or "")
        if harvest:
            if not row["oldest_harvest"] or harvest < row["oldest_harvest"]:
                row["oldest_harvest"] = harvest
            if not row["latest_harvest"] or harvest > row["latest_harvest"]:
                row["latest_harvest"] = harvest
    rows = []
    for row in groups.values():
        row["available_quantity"] = float(row["available"])
        row["reserved_quantity"] = float(row["reserved"])
        row["saleable_quantity"] = float(max(row["available"] - row["reserved"], Decimal("0")))
        row["available_display"] = _qty(row["available"])
        row["reserved_display"] = _qty(row["reserved"])
        row["saleable_display"] = _qty(max(row["available"] - row["reserved"], Decimal("0")))
        row["produced_display"] = _qty(row["produced"])
        row["sold_display"] = _qty(row["sold"])
        row["waste_display"] = _qty(row["waste"])
        row["has_stock"] = max(row["available"] - row["reserved"], Decimal("0")) > Decimal("0.0004")
        for key in ["available", "produced", "sold", "waste", "reserved"]:
            row.pop(key, None)
        rows.append(row)
    rows.sort(key=lambda item: (not item.get("has_stock"), str(item.get("product_name") or "").lower()))
    return rows


def _farmer_purchase_total(profile):
    query = {"$or": [{"farmer_user_id": profile["user_id"]}, {"farmer_user_id_str": profile["user_id_str"]}]}
    total = Decimal("0")
    for row in mongo.db.farmer_purchase_entries.find(query, {"total_amount": 1, "grand_total": 1, "invoice_total": 1}):
        total += max(_decimal(row.get("grand_total") or row.get("invoice_total") or row.get("total_amount")), Decimal("0"))
    for row in mongo.db[EXTERNAL_PURCHASE_COLLECTION].find({"farmer_user_id": profile["user_id"], "status": {"$ne": "voided"}}, {"total_amount": 1}):
        total += max(_decimal(row.get("total_amount")), Decimal("0"))
    for row in mongo.db[MARKETPLACE_PURCHASE_COLLECTION].find({"buyer_type": "farmer", "buyer_key": profile["user_id_str"], "status": {"$ne": "voided"}}, {"total_amount": 1}):
        total += max(_decimal(row.get("total_amount")), Decimal("0"))
    return total


def _expense_total(profile):
    total = Decimal("0")
    for row in mongo.db[EXPENSE_COLLECTION].find({"farmer_user_id": profile["user_id"], "status": {"$ne": "voided"}}, {"amount": 1}):
        total += max(_decimal(row.get("amount")), Decimal("0"))
    return total


def _sales_totals(profile):
    sale_value = Decimal("0")
    paid = Decimal("0")
    for row in mongo.db[SALE_COLLECTION].find({"farmer_user_id": profile["user_id"], "status": "completed"}, {"grand_total": 1, "amount_paid": 1, "paid_amount": 1}):
        sale_value += max(_decimal(row.get("grand_total")), Decimal("0"))
        paid += max(_decimal(row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount")), Decimal("0"))
    for row in mongo.db[MARKETPLACE_SALE_COLLECTION].find({"seller_farmer_user_id": profile["user_id"], "status": "completed"}, {"total_amount": 1, "amount_paid": 1}):
        sale_value += max(_decimal(row.get("total_amount")), Decimal("0"))
        paid += max(_decimal(row.get("amount_paid")), Decimal("0"))
    return sale_value, paid


def get_production_overview(actor_user_id, search=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    q = _clean(search, 120)
    production_query = {"farmer_user_id": profile["user_id"]}
    if q:
        production_query["$or"] = [
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"variety": {"$regex": re.escape(q), "$options": "i"}},
            {"production_number": {"$regex": re.escape(q), "$options": "i"}},
        ]
    productions = [serialize_production(row) for row in mongo.db[PRODUCTION_COLLECTION].find(production_query).sort("created_at", DESCENDING).limit(60)]
    production_count = mongo.db[PRODUCTION_COLLECTION].count_documents({"farmer_user_id": profile["user_id"]})
    stock_rows = _stock_groups(profile, q)
    expenses = []
    for row in mongo.db[EXPENSE_COLLECTION].find({"farmer_user_id": profile["user_id"], "status": {"$ne": "voided"}}).sort("expense_date", DESCENDING).limit(20):
        item = dict(row)
        item["id"] = str(item.get("_id") or "")
        item["amount_display"] = _money(item.get("amount"))
        expenses.append(item)
    input_cost = _farmer_purchase_total(profile)
    other_expenses = _expense_total(profile)
    sales_value, paid = _sales_totals(profile)
    estimated_balance = sales_value - input_cost - other_expenses
    return {
        "farmer": profile,
        "product_choices": _product_choices(profile),
        "unit_choices": UNIT_CHOICES,
        "expense_categories": EXPENSE_CATEGORIES,
        "productions": productions,
        "stock_rows": stock_rows,
        "expenses": expenses,
        "query": q,
        "today": business_today().isoformat(),
        "production_token": f"PROD-{uuid4().hex.upper()}",
        "expense_token": f"EXP-{uuid4().hex.upper()}",
        "summary": {
            "production_batches": production_count,
            "products_in_stock": sum(1 for row in stock_rows if row.get("has_stock")),
            "sales_value": _money(sales_value),
            "cash_received": _money(paid),
            "input_cost": _money(input_cost),
            "other_expenses": _money(other_expenses),
            "estimated_balance": _money(estimated_balance),
            "estimated_balance_raw": float(estimated_balance),
        },
    }


def get_stock_overview(actor_user_id, search=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    q = _clean(search, 120)
    rows = _stock_groups(profile, q)
    movements = []
    movement_query = {"farmer_user_id": profile["user_id"]}
    if q:
        movement_query["product_name"] = {"$regex": re.escape(q), "$options": "i"}
    for row in mongo.db[MOVEMENT_COLLECTION].find(movement_query).sort("created_at", DESCENDING).limit(50):
        item = dict(row)
        item["id"] = str(item.get("_id") or "")
        item["quantity_display"] = _qty(item.get("quantity"))
        item["movement_label"] = {
            "production_in": "Production Added",
            "sale_out": "Sold",
            "waste_out": "Loss / Wastage",
            "sale_void_in": "Sale Cancelled · Stock Restored",
            "marketplace_sale_out": "Marketplace Order Dispatched",
        }.get(item.get("movement_type"), str(item.get("movement_type") or "Movement").replace("_", " ").title())
        movements.append(item)
    return {
        "farmer": profile,
        "rows": rows,
        "movements": movements,
        "query": q,
        "loss_token": f"LOSS-{uuid4().hex.upper()}",
    }


def _allocate_stock(profile, product_key, quantity_value, *, movement_type, reference_id, reference_number, note=""):
    required = _decimal(quantity_value)
    if required <= 0:
        raise ValueError("Quantity must be greater than zero.")
    lots = list(mongo.db[LOT_COLLECTION].find({
        "farmer_user_id": profile["user_id"],
        "product_key": product_key,
        "status": "active",
        "available_quantity": {"$gt": 0},
    }).sort([("harvest_date", ASCENDING), ("created_at", ASCENDING), ("_id", ASCENDING)]))
    total = sum((max(_decimal(lot.get("available_quantity")) - _decimal(lot.get("reserved_quantity")), Decimal("0")) for lot in lots), Decimal("0"))
    if total + Decimal("0.0004") < required:
        raise ValueError(f"Only {_qty(total)} is currently available. Reduce the quantity and try again.")

    remaining = required
    applied = []
    try:
        for lot in lots:
            if remaining <= Decimal("0.0004"):
                break
            reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
            available = max(_decimal(lot.get("available_quantity")) - reserved, Decimal("0"))
            take = min(available, remaining)
            result = mongo.db[LOT_COLLECTION].update_one(
                {"_id": lot["_id"], "available_quantity": {"$gte": float(reserved + take)}, "status": "active"},
                {
                    "$inc": {
                        "available_quantity": -float(take),
                        "sold_quantity" if movement_type == "sale_out" else "waste_quantity": float(take),
                    },
                    "$set": {"updated_at": now_utc()},
                },
            )
            if result.modified_count != 1:
                raise RuntimeError("Produce stock changed in another session. Refresh and try again.")
            applied.append({"lot_id": lot["_id"], "lot_number": lot.get("lot_number") or "", "quantity": float(take)})
            remaining -= take
        if remaining > Decimal("0.0004"):
            raise RuntimeError("Produce stock changed while this transaction was being saved. Refresh and try again.")
    except Exception:
        for allocation in applied:
            increment_field = "sold_quantity" if movement_type == "sale_out" else "waste_quantity"
            mongo.db[LOT_COLLECTION].update_one(
                {"_id": allocation["lot_id"]},
                {"$inc": {"available_quantity": allocation["quantity"], increment_field: -allocation["quantity"]}, "$set": {"updated_at": now_utc()}},
            )
        raise

    movement_ids = []
    try:
        for allocation in applied:
            movement = mongo.db[MOVEMENT_COLLECTION].insert_one({
                "farmer_user_id": profile["user_id"],
                "farmer_user_id_str": profile["user_id_str"],
                "product_key": product_key,
                "product_name": next((lot.get("product_name") for lot in lots if lot.get("_id") == allocation["lot_id"]), "Produce"),
                "unit_code": next((lot.get("unit_code") for lot in lots if lot.get("_id") == allocation["lot_id"]), "KG"),
                "lot_id": allocation["lot_id"],
                "lot_number": allocation["lot_number"],
                "movement_type": movement_type,
                "quantity": allocation["quantity"],
                "direction": "out",
                "reference_type": "sale" if movement_type == "sale_out" else "stock_loss",
                "reference_id": reference_id,
                "reference_number": reference_number,
                "note": _clean(note, 500),
                "created_at": now_utc(),
            })
            movement_ids.append(movement.inserted_id)
    except Exception:
        increment_field = "sold_quantity" if movement_type == "sale_out" else "waste_quantity"
        for allocation in applied:
            mongo.db[LOT_COLLECTION].update_one(
                {"_id": allocation["lot_id"]},
                {"$inc": {"available_quantity": allocation["quantity"], increment_field: -allocation["quantity"]}, "$set": {"updated_at": now_utc()}},
            )
        if movement_ids:
            mongo.db[MOVEMENT_COLLECTION].delete_many({"_id": {"$in": movement_ids}})
        raise RuntimeError("Produce stock movement could not be saved safely. No stock change was kept.")
    return applied


def record_stock_loss(actor_user_id, product_key, quantity, *, reason="wastage", note="", idempotency_key=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    product_key = _clean(product_key, 250)
    if not product_key:
        raise ValueError("Choose a produce item.")
    quantity_value = _decimal(quantity)
    if quantity_value <= 0:
        raise ValueError("Loss quantity must be greater than zero.")
    token = _clean(idempotency_key, 120) or f"LOSS-{uuid4().hex.upper()}"
    existing = mongo.db[MOVEMENT_COLLECTION].find_one({"idempotency_key": token, "movement_type": "waste_summary"})
    if existing:
        return {"message": "This stock loss was already recorded.", "idempotent_replay": True}
    sample = mongo.db[LOT_COLLECTION].find_one({"farmer_user_id": profile["user_id"], "product_key": product_key})
    if not sample:
        raise ValueError("Produce stock was not found.")
    reference_number = _next_number("farmer_stock_loss", "LOSS")
    summary = {
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "product_key": product_key,
        "product_name": sample.get("product_name") or "Produce",
        "unit_code": sample.get("unit_code") or "KG",
        "movement_type": "waste_summary",
        "idempotency_key": token,
        "quantity": float(quantity_value),
        "direction": "out",
        "reference_type": "stock_loss",
        "reference_number": reference_number,
        "reason": _clean(reason, 80),
        "note": _clean(note, 500),
        "created_at": now_utc(),
    }
    result = mongo.db[MOVEMENT_COLLECTION].insert_one(summary)
    try:
        _allocate_stock(profile, product_key, quantity_value, movement_type="waste_out", reference_id=result.inserted_id, reference_number=reference_number, note=f"{_clean(reason,80)} · {_clean(note,300)}")
    except Exception:
        mongo.db[MOVEMENT_COLLECTION].delete_one({"_id": result.inserted_id})
        raise
    _audit(profile, "record_stock_loss", "stock_loss", result.inserted_id, f"{_qty(quantity_value)} {sample.get('unit_code') or ''} {sample.get('product_name') or ''} recorded as {_clean(reason,80)}.")
    return {"message": "Produce stock updated.", "idempotent_replay": False}


def _resolve_mapped_ufc(profile):
    centre_uid = profile.get("centre_uid") or ""
    if not centre_uid:
        return {}
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or {}
    return {
        "name": centre.get("business_name") or centre.get("centre_name") or centre.get("trade_name") or centre_uid,
        "phone": centre.get("contact_number") or centre.get("phone") or "",
        "address": ", ".join([x for x in [centre.get("village"), centre.get("district"), centre.get("state")] if x]),
        "centre_uid": centre_uid,
    }


def create_external_sale(actor_user_id, product_key, quantity, unit_price, *, buyer_type="local_buyer", buyer_name="", buyer_phone="", buyer_address="", sale_date=None, payment_term="pay_now", credit_days=0, note="", idempotency_key=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    product_key = _clean(product_key, 250)
    sample = mongo.db[LOT_COLLECTION].find_one({"farmer_user_id": profile["user_id"], "product_key": product_key})
    if not sample:
        raise ValueError("Choose produce from My Produce Stock.")
    quantity_value = _decimal(quantity)
    rate = _decimal(unit_price)
    if quantity_value <= 0:
        raise ValueError("Sale quantity must be greater than zero.")
    if rate <= 0:
        raise ValueError("Selling price must be greater than zero.")
    buyer_type = str(buyer_type or "local_buyer").strip().lower()
    if buyer_type not in BUYER_TYPES:
        raise ValueError("Choose a valid buyer type.")
    buyer_name = _clean(buyer_name, 160)
    buyer_phone = re.sub(r"\D", "", str(buyer_phone or ""))[:15]
    buyer_address = _clean(buyer_address, 350)
    mapped_ufc = {}
    if buyer_type == "mapped_ufc":
        mapped_ufc = _resolve_mapped_ufc(profile)
        buyer_name = mapped_ufc.get("name") or buyer_name or profile.get("centre_uid") or "Mapped UFC"
        buyer_phone = mapped_ufc.get("phone") or buyer_phone
        buyer_address = mapped_ufc.get("address") or buyer_address
    if len(buyer_name) < 2:
        raise ValueError("Enter the buyer name.")
    term = str(payment_term or "pay_now").strip().lower()
    if term not in PAYMENT_TERM_LABELS:
        term = "pay_now"
    try:
        days = int(credit_days or 0)
    except Exception:
        days = 0
    days = min(max(days, 0), 365)
    sale_day = _date_iso(sale_date)
    if datetime.strptime(sale_day, "%Y-%m-%d").date() > business_today():
        raise ValueError("Sale date cannot be in the future.")
    token = _clean(idempotency_key, 120) or f"FSALE-{uuid4().hex.upper()}"

    existing = mongo.db[SALE_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        if str(existing.get("farmer_user_id") or "") != profile["user_id_str"]:
            raise RuntimeError("This sale request token is already in use.")
        if existing.get("status") == "failed":
            raise RuntimeError("A previous attempt with this sale token failed safely. Refresh the page and record the sale again after checking stock.")
        return _sale_result(existing, message="This sale was already saved.", idempotent=True)

    total = (quantity_value * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    sale_number = _next_number("farmer_external_sale", "FSALE")
    timestamp = now_utc()
    sale_doc = {
        "sale_number": sale_number,
        "idempotency_key": token,
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_master_id": profile["farmer_master_id"],
        "farmer_name": profile["name"],
        "farmer_phone": profile["phone"],
        "centre_uid": profile["centre_uid"],
        "product_key": product_key,
        "product_name": sample.get("product_name") or "Produce",
        "variety": sample.get("variety") or "",
        "unit_code": sample.get("unit_code") or "KG",
        "quantity": float(quantity_value),
        "unit_price": float(rate),
        "grand_total": float(total),
        "buyer_type": buyer_type,
        "buyer_type_label": BUYER_TYPES[buyer_type],
        "buyer_name": buyer_name,
        "buyer_phone": buyer_phone,
        "buyer_address": buyer_address,
        "buyer_centre_uid": mapped_ufc.get("centre_uid") or "",
        "sale_date": sale_day,
        "payment_term": term,
        "payment_term_label": PAYMENT_TERM_LABELS[term],
        "credit_days": days if term == "credit" else 0,
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(total),
        "accounting_status": "not_posted",
        "status": "processing",
        "note": _clean(note, 1000),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[SALE_COLLECTION].insert_one(sale_doc)
        sale_doc["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[SALE_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            if existing.get("status") == "failed":
                raise RuntimeError("A previous attempt with this sale token failed safely. Refresh the page and record the sale again after checking stock.")
            return _sale_result(existing, message="This sale was already saved.", idempotent=True)
        raise RuntimeError("Sale could not be saved safely. Refresh and try again.")

    allocations = []
    try:
        allocations = _allocate_stock(profile, product_key, quantity_value, movement_type="sale_out", reference_id=sale_doc["_id"], reference_number=sale_number, note=f"Sold to {buyer_name}.")
        invoice = _ensure_external_invoice(profile, sale_doc)
        receivable = _ensure_external_receivable(profile, sale_doc, invoice)
        mongo.db[SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {
            "status": "completed",
            "stock_allocations": allocations,
            "invoice_id": invoice["_id"],
            "invoice_id_str": str(invoice["_id"]),
            "document_number": invoice.get("document_number") or "",
            "receivable_id": receivable.get("_id"),
            "receivable_id_str": str(receivable.get("_id") or ""),
            "updated_at": now_utc(),
        }})
    except Exception as exc:
        if allocations:
            for allocation in allocations:
                mongo.db[LOT_COLLECTION].update_one(
                    {"_id": allocation["lot_id"]},
                    {"$inc": {"available_quantity": allocation["quantity"], "sold_quantity": -allocation["quantity"]}, "$set": {"updated_at": now_utc()}},
                )
            mongo.db[MOVEMENT_COLLECTION].delete_many({"reference_type": "sale", "reference_id": sale_doc["_id"], "movement_type": "sale_out"})
        mongo.db[INVOICE_COLLECTION].update_many(
            {"farmer_external_sale_id": sale_doc["_id"]},
            {"$set": {"status": "voided", "payment_status": "voided", "outstanding_amount": 0.0, "failure_reason": _clean(exc, 500), "updated_at": now_utc()}},
        )
        mongo.db[RECEIVABLE_COLLECTION].update_many(
            {"farmer_external_sale_id": sale_doc["_id"]},
            {"$set": {"status": "voided", "payment_status": "voided", "outstanding_amount": 0.0, "failure_reason": _clean(exc, 500), "updated_at": now_utc()}},
        )
        mongo.db[SALE_COLLECTION].update_one({"_id": sale_doc["_id"]}, {"$set": {"status": "failed", "failure_reason": _clean(exc, 500), "updated_at": now_utc()}})
        raise RuntimeError(f"Sale could not be completed safely: {_clean(exc, 300)}")

    sale = mongo.db[SALE_COLLECTION].find_one({"_id": sale_doc["_id"]}) or sale_doc
    _audit(profile, "record_sale", "external_sale", sale_doc["_id"], f"{_qty(quantity_value)} {sale_doc['unit_code']} {sale_doc['product_name']} sold to {buyer_name} for ₹{_money(total)}.")
    return _sale_result(sale, message="Sale recorded. Produce stock, sales receipt and outstanding balance were updated automatically.", idempotent=False)


def _ensure_external_invoice(profile, sale):
    existing = mongo.db[INVOICE_COLLECTION].find_one({"farmer_external_sale_id": sale["_id"]})
    if existing:
        return existing
    total = _decimal(sale.get("grand_total"))
    due_date = sale.get("sale_date") or business_today().isoformat()
    if sale.get("payment_term") == "credit":
        base = datetime.strptime(_date_iso(sale.get("sale_date")), "%Y-%m-%d").date()
        due_date = (base + timedelta(days=max(int(sale.get("credit_days") or 0), 0))).isoformat()
    document = {
        "document_number": _next_number("farmer_external_invoice", "FRCPT"),
        "document_type": "sales_receipt",
        "document_title": "Farmer Sales Receipt",
        "farmer_external_sale_id": sale["_id"],
        "farmer_external_sale_id_str": str(sale["_id"]),
        "sale_number": sale.get("sale_number") or "",
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "seller": {
            "farmer_user_id": profile["user_id"],
            "farmer_user_id_str": profile["user_id_str"],
            "name": profile["name"],
            "phone": profile["phone"],
            "centre_uid": profile["centre_uid"],
            "village": profile["village"],
            "district": profile["district"],
            "state": profile["state"],
        },
        "buyer": {
            "type": sale.get("buyer_type") or "local_buyer",
            "type_label": sale.get("buyer_type_label") or "Buyer",
            "name": sale.get("buyer_name") or "Buyer",
            "phone": sale.get("buyer_phone") or "",
            "address": sale.get("buyer_address") or "",
            "centre_uid": sale.get("buyer_centre_uid") or "",
        },
        "product_name": sale.get("product_name") or "Produce",
        "variety": sale.get("variety") or "",
        "quantity": float(_decimal(sale.get("quantity"))),
        "unit_code": sale.get("unit_code") or "KG",
        "unit_price": float(_decimal(sale.get("unit_price"))),
        "taxable_value": float(total),
        "gst_rate": 0.0,
        "gst_amount": 0.0,
        "grand_total": float(total),
        "tax_note": "Farmer output sale recorded as a non-GST sales receipt. GST is not charged unless a future verified Farmer GST workflow explicitly enables tax invoicing.",
        "payment_term": sale.get("payment_term") or "pay_now",
        "payment_term_label": sale.get("payment_term_label") or "Pay Now",
        "due_date": due_date,
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "paid_amount": 0.0,
        "outstanding_amount": float(total),
        "payment_version": 0,
        "payment_ids": [],
        "status": "issued",
        "issued_at": now_utc(),
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    try:
        result = mongo.db[INVOICE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except DuplicateKeyError:
        existing = mongo.db[INVOICE_COLLECTION].find_one({"farmer_external_sale_id": sale["_id"]})
        if existing:
            return existing
        raise


def _ensure_external_receivable(profile, sale, invoice):
    existing = mongo.db[RECEIVABLE_COLLECTION].find_one({"farmer_external_sale_id": sale["_id"]})
    if existing:
        return existing
    total = _decimal(invoice.get("grand_total"))
    document = {
        "farmer_external_sale_id": sale["_id"],
        "farmer_external_sale_id_str": str(sale["_id"]),
        "invoice_id": invoice["_id"],
        "invoice_id_str": str(invoice["_id"]),
        "document_number": invoice.get("document_number") or "",
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_name": profile["name"],
        "buyer_name": sale.get("buyer_name") or "Buyer",
        "total_amount": float(total),
        "amount_paid": 0.0,
        "outstanding_amount": float(total),
        "payment_status": "unpaid",
        "status": "open",
        "due_date": invoice.get("due_date") or "",
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    try:
        result = mongo.db[RECEIVABLE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except DuplicateKeyError:
        return mongo.db[RECEIVABLE_COLLECTION].find_one({"farmer_external_sale_id": sale["_id"]}) or document


def _sale_result(sale, message="", idempotent=False):
    invoice = {}
    if sale and sale.get("invoice_id"):
        invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": sale.get("invoice_id")}) or {}
    elif sale:
        invoice = mongo.db[INVOICE_COLLECTION].find_one({"farmer_external_sale_id": sale.get("_id")}) or {}
    return {
        "sale": serialize_sale(sale),
        "invoice": serialize_external_invoice(invoice),
        "message": message,
        "idempotent_replay": idempotent,
    }


def create_external_purchase(actor_user_id, seller_name, product_name, quantity, unit_code, total_amount, *, purchase_date=None, bill_number="", payment_term="pay_now", credit_days=0, note="", idempotency_key=""):
    """Record an input/material purchase made outside UnnatFarm.

    This is deliberately a financial/manual record only. It does not pretend
    to create Product Master stock because outside purchases may be seed,
    labour material, feed, medicine, packaging, etc. Internal UFC purchases
    remain automatic through Stage 7.
    """
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    seller_name = _clean(seller_name, 160)
    product_name = _clean(product_name, 160)
    if len(seller_name) < 2:
        raise ValueError("Enter the shop / seller name.")
    if len(product_name) < 2:
        raise ValueError("Enter what you purchased.")
    qty_value = _decimal(quantity)
    if qty_value <= 0:
        raise ValueError("Quantity must be greater than zero.")
    unit = _unit_code(unit_code)
    total = _decimal(total_amount)
    if total <= 0:
        raise ValueError("Total purchase amount must be greater than zero.")
    purchase_day = _date_iso(purchase_date)
    if datetime.strptime(purchase_day, "%Y-%m-%d").date() > business_today():
        raise ValueError("Purchase date cannot be in the future.")
    term = str(payment_term or "pay_now").strip().lower()
    if term not in PAYMENT_TERM_LABELS:
        term = "pay_now"
    try:
        days = int(credit_days or 0)
    except Exception:
        days = 0
    days = min(max(days, 0), 365)
    token = _clean(idempotency_key, 120) or f"FPUR-{uuid4().hex.upper()}"
    existing = mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"idempotency_key": token})
    if existing:
        if str(existing.get("farmer_user_id") or "") != profile["user_id_str"]:
            raise RuntimeError("This outside-purchase request token is already in use.")
        invoice = mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].find_one({"farmer_external_purchase_id": existing.get("_id")}) or _ensure_external_purchase_invoice(profile, existing)
        payable = mongo.db[EXTERNAL_PAYABLE_COLLECTION].find_one({"farmer_external_purchase_id": existing.get("_id")}) or _ensure_external_payable(profile, existing, invoice)
        mongo.db[EXTERNAL_PURCHASE_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": {"invoice_id": invoice.get("_id"), "invoice_id_str": str(invoice.get("_id") or ""), "document_number": invoice.get("document_number") or "", "payable_id": payable.get("_id"), "payable_id_str": str(payable.get("_id") or ""), "updated_at": now_utc()}})
        existing = mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"_id": existing["_id"]}) or existing
        return {"purchase": serialize_external_purchase(existing), "invoice": serialize_external_purchase_invoice(invoice), "message": "This outside purchase was already saved.", "idempotent_replay": True}

    timestamp = now_utc()
    document = {
        "purchase_number": _next_number("farmer_external_purchase", "FPUR"),
        "idempotency_key": token,
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "farmer_master_id": profile["farmer_master_id"],
        "farmer_name": profile["name"],
        "centre_uid": profile["centre_uid"],
        "seller_name": seller_name,
        "product_name": product_name,
        "quantity": float(qty_value),
        "unit_code": unit,
        "total_amount": float(total),
        "bill_number": _clean(bill_number, 120),
        "purchase_date": purchase_day,
        "payment_term": term,
        "payment_term_label": PAYMENT_TERM_LABELS[term],
        "credit_days": days if term == "credit" else 0,
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(total),
        "note": _clean(note, 1000),
        "status": "recorded",
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        result = mongo.db[EXTERNAL_PURCHASE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"idempotency_key": token})
        if existing:
            invoice = mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].find_one({"farmer_external_purchase_id": existing.get("_id")}) or _ensure_external_purchase_invoice(profile, existing)
            payable = mongo.db[EXTERNAL_PAYABLE_COLLECTION].find_one({"farmer_external_purchase_id": existing.get("_id")}) or _ensure_external_payable(profile, existing, invoice)
            mongo.db[EXTERNAL_PURCHASE_COLLECTION].update_one({"_id": existing["_id"]}, {"$set": {"invoice_id": invoice.get("_id"), "invoice_id_str": str(invoice.get("_id") or ""), "document_number": invoice.get("document_number") or "", "payable_id": payable.get("_id"), "payable_id_str": str(payable.get("_id") or ""), "updated_at": now_utc()}})
            existing = mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"_id": existing["_id"]}) or existing
            return {"purchase": serialize_external_purchase(existing), "invoice": serialize_external_purchase_invoice(invoice), "message": "This outside purchase was already saved.", "idempotent_replay": True}
        raise RuntimeError("Outside purchase could not be saved safely. Refresh and try again.")

    invoice = _ensure_external_purchase_invoice(profile, document)
    payable = _ensure_external_payable(profile, document, invoice)
    mongo.db[EXTERNAL_PURCHASE_COLLECTION].update_one({"_id": document["_id"]}, {"$set": {
        "invoice_id": invoice["_id"], "invoice_id_str": str(invoice["_id"]),
        "document_number": invoice.get("document_number") or "",
        "payable_id": payable.get("_id"), "payable_id_str": str(payable.get("_id") or ""),
        "updated_at": now_utc(),
    }})
    document = mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"_id": document["_id"]}) or document
    _audit(profile, "record_external_purchase", "external_purchase", document["_id"], f"₹{_money(total)} outside purchase from {seller_name}: {product_name}.")
    return {"purchase": serialize_external_purchase(document), "invoice": serialize_external_purchase_invoice(invoice), "message": "Outside purchase saved. It is included in My Purchases and the simple farm-cost view.", "idempotent_replay": False}


def _ensure_external_purchase_invoice(profile, purchase):
    existing = mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].find_one({"farmer_external_purchase_id": purchase["_id"]})
    if existing:
        return existing
    total = _decimal(purchase.get("total_amount"))
    base = datetime.strptime(_date_iso(purchase.get("purchase_date")), "%Y-%m-%d").date()
    due_date = base.isoformat()
    if purchase.get("payment_term") == "credit":
        due_date = (base + timedelta(days=max(int(purchase.get("credit_days") or 0), 0))).isoformat()
    document = {
        "document_number": _next_number("farmer_external_purchase_invoice", "FBILL"),
        "document_type": "outside_purchase_record",
        "document_title": "Outside Purchase Record",
        "farmer_external_purchase_id": purchase["_id"],
        "farmer_external_purchase_id_str": str(purchase["_id"]),
        "purchase_number": purchase.get("purchase_number") or "",
        "farmer_user_id": profile["user_id"],
        "farmer_user_id_str": profile["user_id_str"],
        "seller": {"name": purchase.get("seller_name") or "Seller", "bill_number": purchase.get("bill_number") or ""},
        "buyer": {"farmer_user_id": profile["user_id"], "farmer_user_id_str": profile["user_id_str"], "name": profile["name"], "phone": profile["phone"]},
        "product_name": purchase.get("product_name") or "Input / Material",
        "quantity": float(_decimal(purchase.get("quantity"))),
        "unit_code": purchase.get("unit_code") or "KG",
        "grand_total": float(total),
        "total_amount": float(total),
        "payment_term": purchase.get("payment_term") or "pay_now",
        "payment_term_label": purchase.get("payment_term_label") or "Pay Now",
        "due_date": due_date,
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "paid_amount": 0.0,
        "outstanding_amount": float(total),
        "payment_version": 0,
        "payment_ids": [],
        "status": "issued",
        "source_note": "Manual record of a purchase made outside UnnatFarm. Keep the seller's original bill/tax invoice separately when applicable.",
        "issued_at": now_utc(), "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except DuplicateKeyError:
        return mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].find_one({"farmer_external_purchase_id": purchase["_id"]}) or document


def _ensure_external_payable(profile, purchase, invoice):
    existing = mongo.db[EXTERNAL_PAYABLE_COLLECTION].find_one({"farmer_external_purchase_id": purchase["_id"]})
    if existing:
        return existing
    total = _decimal(invoice.get("grand_total"))
    document = {
        "farmer_external_purchase_id": purchase["_id"], "farmer_external_purchase_id_str": str(purchase["_id"]),
        "invoice_id": invoice["_id"], "invoice_id_str": str(invoice["_id"]),
        "farmer_user_id": profile["user_id"], "farmer_user_id_str": profile["user_id_str"],
        "seller_name": purchase.get("seller_name") or "Seller",
        "total_amount": float(total), "amount_paid": 0.0, "outstanding_amount": float(total),
        "payment_status": "unpaid", "status": "open", "due_date": invoice.get("due_date") or "",
        "created_at": now_utc(), "updated_at": now_utc(),
    }
    try:
        result = mongo.db[EXTERNAL_PAYABLE_COLLECTION].insert_one(document); document["_id"] = result.inserted_id; return document
    except DuplicateKeyError:
        return mongo.db[EXTERNAL_PAYABLE_COLLECTION].find_one({"farmer_external_purchase_id": purchase["_id"]}) or document


def serialize_external_purchase(row):
    if not row:
        return {}
    item = dict(row); item["id"] = str(item.get("_id") or ""); item["invoice_id_str"] = str(item.get("invoice_id") or item.get("invoice_id_str") or "")
    item["quantity_display"] = _qty(item.get("quantity")); item["total_amount_display"] = _money(item.get("total_amount")); item["amount_paid_display"] = _money(item.get("amount_paid")); item["outstanding_amount_display"] = _money(item.get("outstanding_amount"))
    item["payment_status_label"] = str(item.get("payment_status") or "unpaid").replace("_", " ").title()
    return item


def serialize_external_purchase_invoice(row):
    if not row:
        return {}
    item = dict(row); item["id"] = str(item.get("_id") or ""); item["grand_total_display"] = _money(item.get("grand_total")); item["paid_display"] = _money(item.get("amount_paid")); item["outstanding_display"] = _money(item.get("outstanding_amount")); item["payment_status_label"] = str(item.get("payment_status") or "unpaid").replace("_", " ").title(); return item


def get_external_purchase_form_context(actor_user_id):
    profile = _get_farmer(actor_user_id)
    return {"farmer": profile, "unit_choices": UNIT_CHOICES, "today": business_today().isoformat(), "purchase_token": f"FPUR-{uuid4().hex.upper()}", "payment_token": f"FPAY-{uuid4().hex.upper()}"}


def get_external_purchase_rows(actor_user_id, search=""):
    profile = _get_farmer(actor_user_id); q = _clean(search, 120)
    query = {"farmer_user_id": profile["user_id"], "status": {"$ne": "voided"}}
    if q:
        query["$or"] = [{"purchase_number": {"$regex": re.escape(q), "$options": "i"}}, {"product_name": {"$regex": re.escape(q), "$options": "i"}}, {"seller_name": {"$regex": re.escape(q), "$options": "i"}}, {"bill_number": {"$regex": re.escape(q), "$options": "i"}}]
    rows=[]; total=Decimal("0")
    for raw in mongo.db[EXTERNAL_PURCHASE_COLLECTION].find(query).sort("purchase_date", DESCENDING).limit(100):
        p=serialize_external_purchase(raw); amount=_decimal(raw.get("total_amount")); total+=amount
        rows.append({
            **raw, "id": p["id"], "purchase_number": raw.get("purchase_number") or "Outside Purchase", "order_number": raw.get("bill_number") or "Outside Purchase",
            "seller_name": raw.get("seller_name") or "Outside Seller", "product_name": raw.get("product_name") or "Input / Material", "quantity_display": p["quantity_display"], "unit_code": raw.get("unit_code") or "",
            "unit_price_display": "—", "taxable_value_display": _money(amount), "gst_amount_display": "0.00", "total_amount_display": _money(amount),
            "amount_paid_display": p["amount_paid_display"], "outstanding_amount_display": p["outstanding_amount_display"], "payment_status": raw.get("payment_status") or "unpaid", "payment_status_label": p["payment_status_label"],
            "invoice_id_str": p["invoice_id_str"], "source_type": "farmer_external", "purchase_date": raw.get("purchase_date") or raw.get("created_at"),
        })
    return {"rows": rows, "total": total, "total_display": _money(total)}


def get_external_purchase_print_context(actor_user_id, invoice_id):
    profile=_get_farmer(actor_user_id); oid=_to_object_id(invoice_id)
    if not oid: raise ValueError("Invalid outside purchase reference.")
    invoice=mongo.db[EXTERNAL_PURCHASE_INVOICE_COLLECTION].find_one({"_id":oid,"farmer_user_id":profile["user_id"]})
    if not invoice: raise ValueError("Outside purchase record was not found.")
    purchase=mongo.db[EXTERNAL_PURCHASE_COLLECTION].find_one({"_id":invoice.get("farmer_external_purchase_id")}) or {}
    return {"farmer": profile, "invoice": serialize_external_purchase_invoice(invoice), "purchase": serialize_external_purchase(purchase)}


def serialize_production(row):
    if not row:
        return None
    item = dict(row)
    item["id"] = str(item.get("_id") or "")
    item["quantity_display"] = _qty(item.get("quantity_produced"))
    item["estimated_cost_display"] = _money(item.get("estimated_cost"))
    return item


def serialize_external_invoice(row):
    if not row:
        return {}
    item = dict(row)
    item["id"] = str(item.get("_id") or "")
    item["grand_total_display"] = _money(item.get("grand_total"))
    item["paid_display"] = _money(item.get("amount_paid") if item.get("amount_paid") is not None else item.get("paid_amount"))
    item["outstanding_display"] = _money(item.get("outstanding_amount"))
    item["payment_status_label"] = str(item.get("payment_status") or "unpaid").replace("_", " ").title()
    return item


def serialize_sale(row):
    if not row:
        return None
    item = dict(row)
    item["id"] = str(item.get("_id") or "")
    item["invoice_id_str"] = str(item.get("invoice_id") or item.get("invoice_id_str") or "")
    item["quantity_display"] = _qty(item.get("quantity"))
    item["unit_price_display"] = _money(item.get("unit_price"))
    item["grand_total_display"] = _money(item.get("grand_total"))
    item["paid_display"] = _money(item.get("amount_paid") if item.get("amount_paid") is not None else item.get("paid_amount"))
    item["outstanding_display"] = _money(item.get("outstanding_amount"))
    item["status_label"] = str(item.get("status") or "").replace("_", " ").title()
    item["payment_status_label"] = str(item.get("payment_status") or "unpaid").replace("_", " ").title()
    return item


def get_sales_overview(actor_user_id, search=""):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    q = _clean(search, 120)
    query = {"farmer_user_id": profile["user_id"], "status": {"$ne": "failed"}}
    if q:
        query["$or"] = [
            {"sale_number": {"$regex": re.escape(q), "$options": "i"}},
            {"product_name": {"$regex": re.escape(q), "$options": "i"}},
            {"buyer_name": {"$regex": re.escape(q), "$options": "i"}},
        ]
    rows = [serialize_sale(row) for row in mongo.db[SALE_COLLECTION].find(query).sort("created_at", DESCENDING).limit(100)]
    total = sum((_decimal(row.get("grand_total")) for row in rows if row.get("status") == "completed"), Decimal("0"))
    paid = sum((_decimal(row.get("amount_paid")) for row in rows if row.get("status") == "completed"), Decimal("0"))
    outstanding = sum((_decimal(row.get("outstanding_amount")) for row in rows if row.get("status") == "completed"), Decimal("0"))
    return {
        "farmer": profile,
        "rows": rows,
        "query": q,
        "summary": {
            "sale_count": sum(1 for row in rows if row.get("status") == "completed"),
            "sale_value": _money(total),
            "received": _money(paid),
            "outstanding": _money(outstanding),
        },
    }


def get_sale_form_context(actor_user_id):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    stock_rows = [row for row in _stock_groups(profile) if row.get("has_stock")]
    return {
        "farmer": profile,
        "stock_rows": stock_rows,
        "buyer_types": BUYER_TYPES,
        "payment_terms": PAYMENT_TERM_LABELS,
        "mapped_ufc": _resolve_mapped_ufc(profile),
        "today": business_today().isoformat(),
        "sale_token": f"FSALE-{uuid4().hex.upper()}",
        "payment_token": f"FPAY-{uuid4().hex.upper()}",
    }


def get_sale_detail(actor_user_id, sale_id):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    oid = _to_object_id(sale_id)
    if not oid:
        raise ValueError("Invalid sale reference.")
    sale = mongo.db[SALE_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]})
    if not sale:
        raise ValueError("Sale was not found.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"farmer_external_sale_id": oid}) or {}
    payments = []
    for row in mongo.db.payments.find({"source_type": "farmer_external_invoice", "invoice_id": invoice.get("_id"), "status": {"$in": ["completed", "reversed"]}}).sort("created_at", DESCENDING):
        item = dict(row)
        item["id"] = str(item.get("_id") or "")
        item["amount_display"] = _money(item.get("amount"))
        item["mode_label"] = str(item.get("payment_mode") or "").replace("_", " ").title()
        item["status_label"] = str(item.get("status") or "").replace("_", " ").title()
        payments.append(item)
    return {
        "farmer": profile,
        "sale": serialize_sale(sale),
        "invoice": serialize_external_invoice(invoice),
        "payments": payments,
        "payment_token": f"FPAY-{uuid4().hex.upper()}",
    }


def get_invoice_print_context(actor_user_id, invoice_id):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    oid = _to_object_id(invoice_id)
    if not oid:
        raise ValueError("Invalid receipt reference.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]})
    if not invoice:
        raise ValueError("Sales receipt was not found.")
    sale = mongo.db[SALE_COLLECTION].find_one({"_id": invoice.get("farmer_external_sale_id")}) or {}
    return {"farmer": profile, "invoice": serialize_external_invoice(invoice), "sale": serialize_sale(sale)}


def void_external_sale(actor_user_id, sale_id, reason):
    _ensure_indexes()
    profile = _get_farmer(actor_user_id)
    oid = _to_object_id(sale_id)
    if not oid:
        raise ValueError("Invalid sale reference.")
    sale = mongo.db[SALE_COLLECTION].find_one({"_id": oid, "farmer_user_id": profile["user_id"]})
    if not sale:
        raise ValueError("Sale was not found.")
    if sale.get("status") == "voided":
        return {"message": "This sale is already cancelled."}
    if sale.get("status") != "completed":
        raise ValueError("Only a completed sale can be cancelled.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"farmer_external_sale_id": oid}) or {}
    paid = _decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount"))
    completed_payments = mongo.db.payments.count_documents({"source_type": "farmer_external_invoice", "invoice_id": invoice.get("_id"), "status": "completed"}) if invoice else 0
    if paid > Decimal("0.004") or completed_payments:
        raise ValueError("Reverse the payment first. A paid sale cannot be cancelled while money is still settled against it.")
    reason = _clean(reason, 500)
    if len(reason) < 4:
        raise ValueError("Enter a clear reason for cancelling this sale.")

    allocations = sale.get("stock_allocations") or []
    if not allocations:
        raise RuntimeError("Stock allocation history is missing. Do not cancel this sale automatically; ask an administrator to review it.")
    for allocation in allocations:
        lot_id = _to_object_id(allocation.get("lot_id"))
        quantity_value = _decimal(allocation.get("quantity"))
        if not lot_id or quantity_value <= 0:
            continue
        mongo.db[LOT_COLLECTION].update_one({"_id": lot_id, "farmer_user_id": profile["user_id"]}, {"$inc": {"available_quantity": float(quantity_value), "sold_quantity": -float(quantity_value)}, "$set": {"status": "active", "updated_at": now_utc()}})
        lot = mongo.db[LOT_COLLECTION].find_one({"_id": lot_id}) or {}
        mongo.db[MOVEMENT_COLLECTION].insert_one({
            "farmer_user_id": profile["user_id"],
            "farmer_user_id_str": profile["user_id_str"],
            "product_key": sale.get("product_key") or "",
            "product_name": sale.get("product_name") or "Produce",
            "unit_code": sale.get("unit_code") or "KG",
            "lot_id": lot_id,
            "lot_number": lot.get("lot_number") or allocation.get("lot_number") or "",
            "movement_type": "sale_void_in",
            "quantity": float(quantity_value),
            "direction": "in",
            "reference_type": "sale_void",
            "reference_id": sale["_id"],
            "reference_number": sale.get("sale_number") or "",
            "note": reason,
            "created_at": now_utc(),
        })
    timestamp = now_utc()
    mongo.db[SALE_COLLECTION].update_one({"_id": sale["_id"]}, {"$set": {"status": "voided", "void_reason": reason, "voided_at": timestamp, "updated_at": timestamp}})
    if invoice:
        mongo.db[INVOICE_COLLECTION].update_one({"_id": invoice["_id"]}, {"$set": {"status": "voided", "void_reason": reason, "voided_at": timestamp, "outstanding_amount": 0.0, "payment_status": "voided", "updated_at": timestamp}})
    mongo.db[RECEIVABLE_COLLECTION].update_one({"farmer_external_sale_id": sale["_id"]}, {"$set": {"status": "voided", "outstanding_amount": 0.0, "payment_status": "voided", "void_reason": reason, "updated_at": timestamp}})
    _audit(profile, "void_sale", "external_sale", sale["_id"], reason)
    return {"message": "Sale cancelled and the produce quantity was restored to stock."}
