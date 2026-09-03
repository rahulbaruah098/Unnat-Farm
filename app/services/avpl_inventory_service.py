from __future__ import annotations
from app.utils.timezone import business_today, format_ist_datetime

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.extensions import mongo
from app.utils.helpers import now_utc


INVENTORY_LOT_COLLECTION = "avpl_inventory_lots"
STOCK_MOVEMENT_COLLECTION = "avpl_stock_movements"
GOODS_RECEIPT_COLLECTION = "avpl_goods_receipts"
STOCK_ADJUSTMENT_COLLECTION = "avpl_stock_adjustments"
MARKETPLACE_PUBLICATION_COLLECTION = "avpl_marketplace_publications"

ALLOWED_ROLES = {"super_admin", "avpl_admin", "accounts"}
APPROVER_ROLES = {"super_admin", "avpl_admin"}
MARKETPLACE_PUBLISHER_ROLES = {"super_admin", "avpl_admin"}

ADJUSTMENT_TYPES = {
    "increase": "Increase Stock",
    "decrease": "Reduce Stock",
    "damage": "Mark Damaged",
    "write_off_damaged": "Write Off Damaged",
    "write_off_expired": "Write Off Expired",
}

ADJUSTMENT_REASON_CODES = {
    "physical_count": "Physical Count Difference",
    "damage": "Damage / Quality Issue",
    "loss": "Loss / Shortage",
    "expiry": "Expiry",
    "data_correction": "Data Correction",
    "other": "Other",
}

MOVEMENT_LABELS = {
    "purchase_receipt": "Purchase Receipt",
    "purchase_return": "Purchase Return",
    "sale": "Sale / Dispatch",
    "sales_return": "Sales Return",
    "reservation": "Reservation",
    "reservation_release": "Reservation Release",
    "damage": "Damage",
    "expiry": "Expiry",
    "adjustment": "Adjustment",
    "opening_stock": "Opening Stock",
    "opening_stock_correction": "Opening Stock Correction",
    "opening_stock_void": "Opening Stock Void",
}


def _decimal(value, default="0"):
    try:
        parsed = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return parsed if parsed.is_finite() else Decimal(default)


def _qty(value):
    number = _decimal(value)
    text = f"{number.quantize(Decimal('0.0001')):f}"
    text = text.rstrip("0").rstrip(".")
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


def _date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _clean_text(value, maximum=1000):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _ensure_indexes():
    """Create Stage 3 indexes lazily without changing Stage 2 startup flow."""
    definitions = [
        (
            mongo.db[STOCK_ADJUSTMENT_COLLECTION],
            [("adjustment_number", ASCENDING)],
            {"name": "avpl_stock_adjustment_number_unique", "unique": True},
        ),
        (
            mongo.db[STOCK_ADJUSTMENT_COLLECTION],
            [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)],
            {"name": "avpl_stock_adjustment_entity_status_idx"},
        ),
        (
            mongo.db[MARKETPLACE_PUBLICATION_COLLECTION],
            [("accounting_entity_id", ASCENDING), ("source_product_id", ASCENDING)],
            {"name": "avpl_marketplace_product_unique", "unique": True},
        ),
        (
            mongo.db[MARKETPLACE_PUBLICATION_COLLECTION],
            [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
            {"name": "avpl_marketplace_status_idx"},
        ),
        (
            mongo.db[INVENTORY_LOT_COLLECTION],
            [("accounting_entity_id", ASCENDING), ("expiry_date", ASCENDING)],
            {"name": "avpl_inventory_expiry_idx"},
        ),
    ]
    for collection, keys, options in definitions:
        try:
            collection.create_index(keys, **options)
        except Exception:
            # Existing compatible indexes or restricted environments should not
            # make the operational inventory pages unavailable.
            pass


def _get_actor(actor_user_id):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one(
        {"_id": actor_id},
        {
            "name": 1,
            "full_name": 1,
            "username": 1,
            "phone": 1,
            "role": 1,
            "active": 1,
            "is_active": 1,
            "status": 1,
        },
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")
    role = str(actor.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise PermissionError("You are not authorized to manage AVPL inventory.")
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError("Inactive users cannot manage AVPL inventory.")
    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )
    return actor


def _entity_id(accounting_entity_id):
    entity_id = _to_object_id(accounting_entity_id)
    if not entity_id:
        raise ValueError("Invalid AVPL Accounting entity.")
    return entity_id


def _is_expired(lot, on_date=None):
    expiry = _date_value(lot.get("expiry_date"))
    if not expiry:
        return False
    return expiry < (on_date or business_today())


def _lot_quantities(lot, on_date=None):
    physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
    reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
    damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
    blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
    expired = physical if _is_expired(lot, on_date=on_date) else Decimal("0")
    saleable = max(
        physical - reserved - damaged - blocked - expired,
        Decimal("0"),
    )
    return {
        "physical": physical,
        "reserved": reserved,
        "damaged": damaged,
        "blocked": blocked,
        "expired": expired,
        "saleable": saleable,
    }


def _purchase_cost_by_product(accounting_entity_id):
    """Derive WAC from posted GRNs plus auditable go-live opening stock.

    Opening stock is not a fake purchase/GRN, but its historical unit cost must
    participate in inventory valuation once the MIS becomes authoritative.
    """
    totals = defaultdict(lambda: {"qty": Decimal("0"), "cost": Decimal("0")})
    receipts = mongo.db[GOODS_RECEIPT_COLLECTION].find(
        {
            "accounting_entity_id": accounting_entity_id,
            "status": "posted",
            "stock_posted": True,
        },
        {"items": 1, "purchase_order_snapshot.items": 1},
    )

    for receipt in receipts:
        po_items = {
            int(item.get("line_no") or index): item
            for index, item in enumerate(
                (receipt.get("purchase_order_snapshot") or {}).get("items") or [],
                start=1,
            )
        }
        for index, item in enumerate(receipt.get("items") or [], start=1):
            product_id = _to_object_id(item.get("source_product_id"))
            if not product_id:
                continue
            quantity = _decimal(item.get("accepted_quantity"))
            if quantity <= 0:
                continue
            po_line_no = int(item.get("po_line_no") or index)
            po_item = po_items.get(po_line_no) or {}
            rate = _decimal(po_item.get("rate"))
            discount = _decimal(po_item.get("discount_percent"))
            net_rate = rate * (Decimal("1") - (discount / Decimal("100")))
            totals[str(product_id)]["qty"] += quantity
            totals[str(product_id)]["cost"] += quantity * net_rate

    # Go-live opening lots are a legitimate inventory origin, not a supplier
    # purchase. Include their recorded historical cost in WAC without creating
    # procurement, payable or invoice records.
    for lot in mongo.db[INVENTORY_LOT_COLLECTION].find(
        {
            "accounting_entity_id": accounting_entity_id,
            "status": {"$ne": "cancelled"},
            "source_type": "opening_stock",
            "opening_stock_entry_id": {"$exists": True},
        },
        {"source_product_id": 1, "opening_quantity": 1, "received_quantity": 1, "opening_unit_cost": 1, "purchase_cost_total": 1},
    ):
        product_id = _to_object_id(lot.get("source_product_id"))
        if not product_id:
            continue
        quantity = _decimal(lot.get("opening_quantity") if lot.get("opening_quantity") is not None else lot.get("received_quantity"))
        if quantity <= 0:
            continue
        unit_cost = _decimal(lot.get("opening_unit_cost"))
        cost_total = _decimal(lot.get("purchase_cost_total"))
        if unit_cost <= 0 and cost_total > 0:
            unit_cost = cost_total / quantity
        totals[str(product_id)]["qty"] += quantity
        totals[str(product_id)]["cost"] += quantity * unit_cost

    result = {}
    for product_id, values in totals.items():
        quantity = values["qty"]
        result[product_id] = values["cost"] / quantity if quantity > 0 else Decimal("0")
    return result


def synchronize_expired_lots(accounting_entity_id):
    """Block expired lots for sale and persist a one-time expiry movement.

    Expiry is a stock classification, not a physical stock deduction. The lot
    remains physically present but its saleable quantity becomes zero. A later
    approved write-off can physically remove it.
    """
    entity_id = _entity_id(accounting_entity_id)
    _ensure_indexes()
    today = business_today()
    today_iso = today.strftime("%Y-%m-%d")
    lots = list(
        mongo.db[INVENTORY_LOT_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "status": {"$ne": "cancelled"},
                "expiry_date": {"$type": "string", "$gt": "", "$lt": today_iso},
                "available_quantity": {"$gt": 0},
            }
        )
    )
    for lot in lots:
        expiry_text = str(lot.get("expiry_date") or "")[:10]
        physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        if physical <= 0:
            continue
        timestamp = now_utc()
        mongo.db[INVENTORY_LOT_COLLECTION].update_one(
            {"_id": lot["_id"], "status": {"$ne": "cancelled"}},
            {
                "$set": {
                    "status": "expired",
                    "expired_blocked_at": lot.get("expired_blocked_at") or timestamp,
                    "updated_at": timestamp,
                }
            },
        )
        source_key = f"EXPIRY:{lot['_id']}:{expiry_text}"
        mongo.db[STOCK_MOVEMENT_COLLECTION].update_one(
            {"source_posting_key": source_key},
            {
                "$setOnInsert": {
                    "source_posting_key": source_key,
                    "movement_uid": uuid4().hex,
                    "accounting_entity_id": entity_id,
                    "accounting_entity_id_str": str(entity_id),
                    "source_document_type": "inventory_lot",
                    "source_document_id": lot["_id"],
                    "source_document_id_str": str(lot["_id"]),
                    "source_document_number": lot.get("batch_number") or "Expiry",
                    "source_product_id": lot.get("source_product_id"),
                    "source_product_id_str": str(lot.get("source_product_id") or ""),
                    "product_code": lot.get("product_code") or "",
                    "product_name": lot.get("product_name") or "",
                    "movement_type": "expiry",
                    "direction": "block",
                    "quantity": float(physical),
                    "quantity_display": _qty(physical),
                    "unit_code": lot.get("unit_code") or "",
                    "warehouse_code": lot.get("warehouse_code") or "",
                    "warehouse_name": lot.get("warehouse_name") or "",
                    "warehouse_bin": lot.get("warehouse_bin") or "",
                    "batch_number": lot.get("batch_number") or "",
                    "manufacturing_date": lot.get("manufacturing_date") or "",
                    "expiry_date": lot.get("expiry_date") or "",
                    "movement_date": expiry_text,
                    "reason": "Lot reached its expiry date and was automatically blocked from sale.",
                    "posted_by": None,
                    "posted_by_name": "System",
                    "posted_at": timestamp,
                    "created_at": timestamp,
                }
            },
            upsert=True,
        )
    return len(lots)


def get_product_inventory_snapshot_map(accounting_entity_id, product_ids=None):
    entity_id = _entity_id(accounting_entity_id)
    synchronize_expired_lots(entity_id)
    query = {"accounting_entity_id": entity_id, "status": {"$ne": "cancelled"}}
    normalized_ids = []
    if product_ids is not None:
        normalized_ids = [pid for pid in (_to_object_id(value) for value in product_ids) if pid]
        if not normalized_ids:
            return {}
        query["source_product_id"] = {"$in": normalized_ids}

    grouped = defaultdict(
        lambda: {
            "physical": Decimal("0"),
            "reserved": Decimal("0"),
            "damaged": Decimal("0"),
            "expired": Decimal("0"),
            "blocked": Decimal("0"),
            "saleable": Decimal("0"),
            "lot_count": 0,
            "warehouses": set(),
        }
    )
    today = business_today()
    for lot in mongo.db[INVENTORY_LOT_COLLECTION].find(query):
        product_id = str(lot.get("source_product_id") or "")
        if not product_id:
            continue
        quantities = _lot_quantities(lot, on_date=today)
        row = grouped[product_id]
        for key in ("physical", "reserved", "damaged", "expired", "blocked", "saleable"):
            row[key] += quantities[key]
        row["lot_count"] += 1
        warehouse = str(lot.get("warehouse_code") or "AVPL-MAIN").strip().upper()
        if warehouse:
            row["warehouses"].add(warehouse)

    result = {}
    for product_id, row in grouped.items():
        result[product_id] = {
            "physical_quantity": _qty(row["physical"]),
            "reserved_quantity": _qty(row["reserved"]),
            "saleable_quantity": _qty(row["saleable"]),
            "damaged_quantity": _qty(row["damaged"]),
            "expired_quantity": _qty(row["expired"]),
            "blocked_quantity": _qty(row["blocked"]),
            "lot_count": row["lot_count"],
            "warehouse_count": len(row["warehouses"]),
            "warehouses": sorted(row["warehouses"]),
            "has_stock": row["physical"] > 0,
            "has_saleable_stock": row["saleable"] > 0,
        }
    return result


def get_marketplace_publication_map(accounting_entity_id, product_ids=None):
    entity_id = _entity_id(accounting_entity_id)
    query = {"accounting_entity_id": entity_id}
    if product_ids is not None:
        normalized_ids = [pid for pid in (_to_object_id(value) for value in product_ids) if pid]
        if not normalized_ids:
            return {}
        query["source_product_id"] = {"$in": normalized_ids}
    return {
        str(row.get("source_product_id")): row
        for row in mongo.db[MARKETPLACE_PUBLICATION_COLLECTION].find(query)
    }


def get_current_stock_overview(accounting_entity_id, query_text="", warehouse_code=""):
    entity_id = _entity_id(accounting_entity_id)
    synchronize_expired_lots(entity_id)

    lot_query = {"accounting_entity_id": entity_id, "status": {"$ne": "cancelled"}}
    warehouse_filter = str(warehouse_code or "").strip().upper()
    if warehouse_filter:
        lot_query["warehouse_code"] = warehouse_filter

    lots = list(
        mongo.db[INVENTORY_LOT_COLLECTION]
        .find(lot_query)
        .sort([("product_name", 1), ("warehouse_code", 1), ("expiry_date", 1)])
    )

    product_ids = list(
        {
            product_id
            for product_id in (_to_object_id(row.get("source_product_id")) for row in lots)
            if product_id
        }
    )
    products = (
        {
            str(row["_id"]): row
            for row in mongo.db.products.find(
                {"_id": {"$in": product_ids}},
                {
                    "name": 1,
                    "product_code": 1,
                    "category": 1,
                    "product_role": 1,
                    "type": 1,
                    "base_unit_code": 1,
                    "base_unit_name": 1,
                    "reorder_level": 1,
                    "minimum_stock_level": 1,
                    "available_quantity": 1,
                    "is_active": 1,
                    "status": 1,
                },
            )
        }
        if product_ids
        else {}
    )

    mapping_rows = (
        list(
            mongo.db.accounting_product_mappings.find(
                {
                    "source_product_id": {"$in": product_ids},
                    "is_deleted": {"$ne": True},
                    "status": {"$ne": "cancelled"},
                },
                {"source_product_id": 1, "base_unit_code": 1, "base_unit_name": 1},
            )
        )
        if product_ids
        else []
    )
    mappings = {str(row.get("source_product_id")): row for row in mapping_rows}
    publications = get_marketplace_publication_map(entity_id, product_ids)
    costs = _purchase_cost_by_product(entity_id)

    today = business_today()
    grouped = {}
    warehouse_codes = set()

    for lot in lots:
        product_id = str(lot.get("source_product_id") or "")
        warehouse = str(lot.get("warehouse_code") or "AVPL-MAIN").strip().upper()
        warehouse_codes.add(warehouse)
        key = (product_id, warehouse)
        product = products.get(product_id) or {}
        mapping = mappings.get(product_id) or {}
        quantities = _lot_quantities(lot, on_date=today)
        is_expired = quantities["expired"] > 0

        if key not in grouped:
            role = product.get("product_role") or product.get("type") or ""
            unit_code = (
                lot.get("unit_code")
                or mapping.get("base_unit_code")
                or product.get("base_unit_code")
                or mapping.get("base_unit_name")
                or product.get("base_unit_name")
                or "Unit"
            )
            publication = publications.get(product_id) or {}
            grouped[key] = {
                "product_id": product_id,
                "product_name": product.get("name") or lot.get("product_name") or "Product",
                "sku": product.get("product_code") or lot.get("product_code") or "-",
                "category": product.get("category") or "-",
                "product_role": str(role).replace("_", " ").title() if role else "-",
                "unit": unit_code,
                "warehouse_code": warehouse,
                "warehouse_name": lot.get("warehouse_name") or warehouse,
                "physical": Decimal("0"),
                "reserved": Decimal("0"),
                "damaged": Decimal("0"),
                "expired": Decimal("0"),
                "blocked": Decimal("0"),
                "batch_count": 0,
                "expired_lot_count": 0,
                "reorder_level": _decimal(
                    product.get("reorder_level")
                    if product.get("reorder_level") not in (None, "")
                    else product.get("minimum_stock_level")
                ),
                "legacy_quantity": _decimal(product.get("available_quantity")),
                "weighted_average_cost": costs.get(product_id, Decimal("0")),
                "marketplace_published": publication.get("status") == "published",
            }

        row = grouped[key]
        for key_name in ("physical", "reserved", "damaged", "expired", "blocked"):
            row[key_name] += quantities[key_name]
        row["batch_count"] += 1
        if is_expired:
            row["expired_lot_count"] += 1

    rows = []
    query = str(query_text or "").strip().lower()
    total_value = Decimal("0")
    low_stock_count = 0

    for row in grouped.values():
        saleable = max(
            row["physical"] - row["reserved"] - row["damaged"] - row["expired"] - row["blocked"],
            Decimal("0"),
        )
        stock_value = row["physical"] * row["weighted_average_cost"]
        reorder_level = row["reorder_level"]
        is_low_stock = reorder_level > 0 and saleable <= reorder_level
        searchable = " ".join(
            str(row.get(field) or "")
            for field in (
                "product_name",
                "sku",
                "category",
                "product_role",
                "unit",
                "warehouse_code",
                "warehouse_name",
            )
        ).lower()
        if query and query not in searchable:
            continue
        if is_low_stock:
            low_stock_count += 1
        total_value += stock_value
        rows.append(
            {
                **{k: v for k, v in row.items() if not isinstance(v, Decimal)},
                "total_quantity": _qty(row["physical"]),
                "reserved_quantity": _qty(row["reserved"]),
                "saleable_quantity": _qty(saleable),
                "damaged_quantity": _qty(row["damaged"]),
                "expired_quantity": _qty(row["expired"]),
                "reorder_level": _qty(reorder_level),
                "weighted_average_cost": _money(row["weighted_average_cost"]),
                "stock_value": _money(stock_value),
                "is_low_stock": is_low_stock,
            }
        )

    rows.sort(key=lambda row: (row["product_name"].lower(), row["warehouse_code"]))
    return {
        "rows": rows,
        "query": query_text or "",
        "selected_warehouse": warehouse_filter,
        "warehouses": sorted(code for code in warehouse_codes if code),
        "summary": {
            "product_rows": len(rows),
            "warehouse_count": len(warehouse_codes),
            "low_stock_count": low_stock_count,
            "expired_lot_count": sum(row.get("expired_lot_count", 0) for row in rows),
            "stock_value": _money(total_value),
        },
        "cost_basis_note": (
            "Average cost uses posted GRN quantities/approved PO net rates plus "
            "verified go-live opening-stock unit costs; GST is excluded from stock cost."
        ),
    }


def get_batch_expiry_overview(
    accounting_entity_id,
    query_text="",
    warehouse_code="",
    status_filter="all",
    expiring_days=30,
):
    entity_id = _entity_id(accounting_entity_id)
    synchronize_expired_lots(entity_id)
    warehouse_filter = str(warehouse_code or "").strip().upper()
    query = {"accounting_entity_id": entity_id, "status": {"$ne": "cancelled"}}
    if warehouse_filter:
        query["warehouse_code"] = warehouse_filter

    lots = list(
        mongo.db[INVENTORY_LOT_COLLECTION]
        .find(query)
        .sort([("expiry_date", 1), ("product_name", 1), ("batch_number", 1)])
    )
    product_ids = [pid for pid in {_to_object_id(row.get("source_product_id")) for row in lots} if pid]
    products = (
        {
            str(row["_id"]): row
            for row in mongo.db.products.find(
                {"_id": {"$in": product_ids}},
                {"name": 1, "product_code": 1, "category": 1, "product_role": 1, "type": 1},
            )
        }
        if product_ids
        else {}
    )

    today = business_today()
    rows = []
    search = str(query_text or "").strip().lower()
    warehouse_codes = set()
    summary = {"total_lots": 0, "expired": 0, "expiring_soon": 0, "healthy": 0, "untracked": 0}

    for lot in lots:
        product = products.get(str(lot.get("source_product_id"))) or {}
        warehouse = str(lot.get("warehouse_code") or "AVPL-MAIN").strip().upper()
        warehouse_codes.add(warehouse)
        expiry = _date_value(lot.get("expiry_date"))
        mfg = _date_value(lot.get("manufacturing_date"))
        days_remaining = (expiry - today).days if expiry else None
        if expiry and expiry < today:
            expiry_status = "expired"
            status_label = "Expired"
        elif expiry and days_remaining is not None and days_remaining <= int(expiring_days):
            expiry_status = "expiring_soon"
            status_label = "Expiring Soon"
        elif expiry:
            expiry_status = "healthy"
            status_label = "Healthy"
        else:
            expiry_status = "untracked"
            status_label = "No Expiry"

        quantities = _lot_quantities(lot, on_date=today)
        searchable = " ".join(
            str(value or "")
            for value in (
                product.get("name") or lot.get("product_name"),
                product.get("product_code") or lot.get("product_code"),
                product.get("category"),
                lot.get("batch_number"),
                warehouse,
                lot.get("warehouse_bin"),
                lot.get("barcode"),
            )
        ).lower()
        if search and search not in searchable:
            continue
        if status_filter and status_filter != "all" and expiry_status != status_filter:
            continue

        summary["total_lots"] += 1
        summary[expiry_status] += 1
        rows.append(
            {
                "lot_id": str(lot["_id"]),
                "product_id": str(lot.get("source_product_id") or ""),
                "product_name": product.get("name") or lot.get("product_name") or "Product",
                "sku": product.get("product_code") or lot.get("product_code") or "-",
                "category": product.get("category") or "-",
                "warehouse_code": warehouse,
                "warehouse_name": lot.get("warehouse_name") or warehouse,
                "warehouse_bin": lot.get("warehouse_bin") or "-",
                "batch_number": lot.get("batch_number") or "Not tracked",
                "barcode": lot.get("barcode") or "-",
                "manufacturing_date": mfg.strftime("%d %b %Y") if mfg else "-",
                "expiry_date": expiry.strftime("%d %b %Y") if expiry else "-",
                "expiry_date_raw": expiry.strftime("%Y-%m-%d") if expiry else "",
                "days_remaining": days_remaining,
                "expiry_status": expiry_status,
                "status_label": status_label,
                "physical_quantity": _qty(quantities["physical"]),
                "reserved_quantity": _qty(quantities["reserved"]),
                "damaged_quantity": _qty(quantities["damaged"]),
                "saleable_quantity": _qty(quantities["saleable"]),
                "unit_code": lot.get("unit_code") or "Unit",
                "first_receipt_number": lot.get("first_receipt_number") or "-",
            }
        )

    return {
        "rows": rows,
        "summary": summary,
        "query": query_text or "",
        "selected_warehouse": warehouse_filter,
        "selected_status": status_filter or "all",
        "warehouses": sorted(warehouse_codes),
        "expiring_days": int(expiring_days),
    }


def _movement_effect(row):
    qty_display = row.get("quantity_display") or _qty(row.get("quantity"))
    direction = str(row.get("direction") or "").lower()
    if direction == "in":
        return f"+{qty_display}"
    if direction == "out":
        return f"-{qty_display}"
    if direction == "reserve":
        return f"Reserve {qty_display}"
    if direction == "release":
        return f"Release {qty_display}"
    if direction == "block":
        return f"Block {qty_display}"
    if direction == "reclass":
        return f"Move {qty_display}"
    return qty_display


def get_stock_movement_overview(
    accounting_entity_id,
    query_text="",
    movement_type="",
    page=1,
    per_page=50,
):
    entity_id = _entity_id(accounting_entity_id)
    synchronize_expired_lots(entity_id)
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 50), 10), 100)
    query = {"accounting_entity_id": entity_id}
    movement_filter = str(movement_type or "").strip().lower()
    if movement_filter:
        query["movement_type"] = movement_filter
    search = str(query_text or "").strip()
    if search:
        escaped = re.escape(search)
        query["$or"] = [
            {"product_name": {"$regex": escaped, "$options": "i"}},
            {"product_code": {"$regex": escaped, "$options": "i"}},
            {"source_document_number": {"$regex": escaped, "$options": "i"}},
            {"batch_number": {"$regex": escaped, "$options": "i"}},
            {"warehouse_code": {"$regex": escaped, "$options": "i"}},
        ]

    collection = mongo.db[STOCK_MOVEMENT_COLLECTION]
    total = collection.count_documents(query)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    cursor = (
        collection.find(query)
        .sort([("posted_at", DESCENDING), ("created_at", DESCENDING)])
        .skip((page - 1) * per_page)
        .limit(per_page)
    )
    rows = []
    for row in cursor:
        movement_type_code = str(row.get("movement_type") or "adjustment")
        posted_at = row.get("posted_at") or row.get("created_at")
        movement_day = _date_value(row.get("movement_date"))
        rows.append(
            {
                "id": str(row["_id"]),
                "date": (
                    format_ist_datetime(posted_at, "%d %b %Y %I:%M %p", "-")
                    if isinstance(posted_at, datetime)
                    else movement_day.strftime("%d %b %Y") if movement_day else "-"
                ),
                "movement_type": movement_type_code,
                "movement_label": MOVEMENT_LABELS.get(
                    movement_type_code,
                    movement_type_code.replace("_", " ").title(),
                ),
                "direction": row.get("direction") or "",
                "quantity_effect": _movement_effect(row),
                "quantity": row.get("quantity_display") or _qty(row.get("quantity")),
                "unit_code": row.get("unit_code") or "Unit",
                "product_name": row.get("product_name") or "Product",
                "product_code": row.get("product_code") or "-",
                "warehouse_code": row.get("warehouse_code") or "-",
                "warehouse_bin": row.get("warehouse_bin") or "-",
                "batch_number": row.get("batch_number") or "-",
                "source_document_number": row.get("source_document_number") or "-",
                "source_document_type": str(row.get("source_document_type") or "").replace("_", " ").title(),
                "posted_by_name": row.get("posted_by_name") or "System",
                "reason": row.get("reason") or "",
            }
        )

    return {
        "rows": rows,
        "query": query_text or "",
        "selected_type": movement_filter,
        "movement_types": [
            {"code": code, "label": label} for code, label in MOVEMENT_LABELS.items()
        ],
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def _next_adjustment_number():
    year = business_today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_stock_adjustment:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"AVPL-ADJ-{year}-{sequence:05d}"


def _sync_legacy_product_quantity(entity_id, product_id):
    """Keep old screens numerically compatible; never use this as source of truth."""
    physical = Decimal("0")
    for lot in mongo.db[INVENTORY_LOT_COLLECTION].find(
        {
            "accounting_entity_id": entity_id,
            "source_product_id": product_id,
            "status": {"$ne": "cancelled"},
        },
        {"available_quantity": 1},
    ):
        physical += max(_decimal(lot.get("available_quantity")), Decimal("0"))
    mongo.db.products.update_one(
        {"_id": product_id},
        {
            "$set": {
                "available_quantity": float(physical),
                "legacy_stock_mirror_updated_at": now_utc(),
                "legacy_stock_mirror_source": "stage3_inventory",
            }
        },
    )


def sync_legacy_product_quantity(accounting_entity_id, product_id):
    """Public compatibility wrapper for transaction-owned inventory updates."""
    entity_id = _entity_id(accounting_entity_id)
    source_product_id = _to_object_id(product_id)
    if not source_product_id:
        raise ValueError("Invalid product reference while synchronizing inventory.")
    return _sync_legacy_product_quantity(entity_id, source_product_id)


def create_stock_adjustment(
    accounting_entity_id,
    actor_user_id,
    *,
    lot_id,
    adjustment_type,
    quantity,
    reason_code,
    reason,
    proof_filename,
    proof_document_id=None,
    adjustment_id=None,
):
    _ensure_indexes()
    entity_id = _entity_id(accounting_entity_id)
    actor = _get_actor(actor_user_id)
    lot_object_id = _to_object_id(lot_id)
    if not lot_object_id:
        raise ValueError("Select a valid stock batch / lot.")
    lot = mongo.db[INVENTORY_LOT_COLLECTION].find_one(
        {
            "_id": lot_object_id,
            "accounting_entity_id": entity_id,
            "status": {"$ne": "cancelled"},
        }
    )
    if not lot:
        raise ValueError("The selected stock batch was not found.")

    adjustment_type = str(adjustment_type or "").strip().lower()
    if adjustment_type not in ADJUSTMENT_TYPES:
        raise ValueError("Select a valid adjustment type.")
    qty = _decimal(quantity)
    if qty <= 0:
        raise ValueError("Adjustment quantity must be greater than zero.")
    reason_code = str(reason_code or "other").strip().lower()
    if reason_code not in ADJUSTMENT_REASON_CODES:
        reason_code = "other"
    reason = _clean_text(reason, 1000)
    if len(reason) < 5:
        raise ValueError("Enter a clear reason for this stock adjustment.")
    if not proof_filename:
        raise ValueError("Supporting proof is required for stock adjustments.")

    quantities = _lot_quantities(lot)
    expired = _is_expired(lot)
    if adjustment_type == "increase" and expired:
        raise ValueError("Expired stock cannot be increased. Use an active batch.")
    if adjustment_type in {"decrease", "damage"} and qty > quantities["saleable"]:
        raise ValueError(
            f"Only {_qty(quantities['saleable'])} {lot.get('unit_code') or 'units'} are saleable in this batch."
        )
    if adjustment_type == "write_off_damaged" and qty > quantities["damaged"]:
        raise ValueError(
            f"Only {_qty(quantities['damaged'])} {lot.get('unit_code') or 'units'} are currently marked damaged."
        )
    if adjustment_type == "write_off_expired":
        if not expired:
            raise ValueError("This batch is not expired.")
        if qty > quantities["physical"]:
            raise ValueError("Write-off quantity cannot exceed physical expired stock.")

    adjustment_id = _to_object_id(adjustment_id) or ObjectId()
    timestamp = now_utc()
    document = {
        "_id": adjustment_id,
        "adjustment_number": _next_adjustment_number(),
        "accounting_entity_id": entity_id,
        "accounting_entity_id_str": str(entity_id),
        "inventory_lot_id": lot["_id"],
        "inventory_lot_id_str": str(lot["_id"]),
        "source_product_id": lot.get("source_product_id"),
        "source_product_id_str": str(lot.get("source_product_id") or ""),
        "product_code": lot.get("product_code") or "",
        "product_name": lot.get("product_name") or "Product",
        "unit_code": lot.get("unit_code") or "",
        "warehouse_code": lot.get("warehouse_code") or "",
        "warehouse_name": lot.get("warehouse_name") or "",
        "warehouse_bin": lot.get("warehouse_bin") or "",
        "batch_number": lot.get("batch_number") or "",
        "expiry_date": lot.get("expiry_date") or "",
        "adjustment_type": adjustment_type,
        "adjustment_type_label": ADJUSTMENT_TYPES[adjustment_type],
        "quantity": float(qty),
        "quantity_display": _qty(qty),
        "reason_code": reason_code,
        "reason_label": ADJUSTMENT_REASON_CODES[reason_code],
        "reason": reason,
        "proof_filename": proof_filename,
        "proof_document_id": _to_object_id(proof_document_id),
        "status": "submitted",
        "requested_by": actor["_id"],
        "requested_by_name": actor["resolved_name"],
        "requested_by_role": actor["resolved_role"],
        "submitted_at": timestamp,
        "created_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }
    mongo.db[STOCK_ADJUSTMENT_COLLECTION].insert_one(document)
    return {"adjustment": document, "message": "Stock adjustment submitted for approval."}


def _apply_adjustment_to_lot(adjustment):
    lot_id = adjustment.get("inventory_lot_id")
    entity_id = adjustment.get("accounting_entity_id")
    adjustment_key = f"ADJ:{adjustment['_id']}"
    qty = _decimal(adjustment.get("quantity"))
    adjustment_type = adjustment.get("adjustment_type")

    for _attempt in range(6):
        lot = mongo.db[INVENTORY_LOT_COLLECTION].find_one(
            {"_id": lot_id, "accounting_entity_id": entity_id, "status": {"$ne": "cancelled"}}
        )
        if not lot:
            raise RuntimeError("The stock batch no longer exists.")
        if adjustment_key in (lot.get("applied_adjustment_keys") or []):
            return lot

        quantities = _lot_quantities(lot)
        expired = _is_expired(lot)
        if adjustment_type == "increase" and expired:
            raise ValueError("This batch expired while waiting for approval. Use a current batch.")
        if adjustment_type in {"decrease", "damage"} and qty > quantities["saleable"]:
            raise ValueError(
                f"Saleable stock changed. Only {_qty(quantities['saleable'])} {lot.get('unit_code') or 'units'} remain."
            )
        if adjustment_type == "write_off_damaged" and qty > quantities["damaged"]:
            raise ValueError("Damaged stock changed and is now below the requested write-off quantity.")
        if adjustment_type == "write_off_expired":
            if not expired:
                raise ValueError("The selected batch is no longer classified as expired.")
            if qty > quantities["physical"]:
                raise ValueError("Physical expired stock is below the requested write-off quantity.")

        increments = {}
        if adjustment_type == "increase":
            increments = {"available_quantity": float(qty), "adjusted_in_quantity": float(qty)}
        elif adjustment_type == "decrease":
            increments = {"available_quantity": -float(qty), "adjusted_out_quantity": float(qty)}
        elif adjustment_type == "damage":
            increments = {"damaged_quantity": float(qty)}
        elif adjustment_type == "write_off_damaged":
            increments = {
                "available_quantity": -float(qty),
                "damaged_quantity": -float(qty),
                "adjusted_out_quantity": float(qty),
            }
        elif adjustment_type == "write_off_expired":
            # Expiry and damage can overlap on the same physical units. If expired
            # stock is physically written off, clear any damaged classification for
            # the units leaving inventory so sub-quantities never exceed physical.
            damaged_to_clear = min(qty, quantities["damaged"])
            increments = {
                "available_quantity": -float(qty),
                "damaged_quantity": -float(damaged_to_clear),
                "expired_writeoff_quantity": float(qty),
            }

        timestamp = now_utc()
        update = {
            "$addToSet": {"applied_adjustment_keys": adjustment_key},
            "$set": {"updated_at": timestamp, "last_adjustment_number": adjustment.get("adjustment_number")},
        }
        if increments:
            update["$inc"] = increments
        result = mongo.db[INVENTORY_LOT_COLLECTION].update_one(
            {
                "_id": lot_id,
                "accounting_entity_id": entity_id,
                "applied_adjustment_keys": {"$ne": adjustment_key},
                "updated_at": lot.get("updated_at"),
            },
            update,
        )
        if result.matched_count == 1:
            refreshed = mongo.db[INVENTORY_LOT_COLLECTION].find_one({"_id": lot_id})
            if refreshed:
                physical = max(_decimal(refreshed.get("available_quantity")), Decimal("0"))
                status = "depleted" if physical <= 0 else "expired" if _is_expired(refreshed) else "available"
                mongo.db[INVENTORY_LOT_COLLECTION].update_one(
                    {"_id": lot_id}, {"$set": {"status": status, "updated_at": now_utc()}}
                )
                return mongo.db[INVENTORY_LOT_COLLECTION].find_one({"_id": lot_id})
        # Concurrent change: re-read and validate again.
    raise RuntimeError("Stock changed repeatedly while approving this adjustment. Please retry.")


def _record_adjustment_movement(adjustment, actor):
    adjustment_type = adjustment.get("adjustment_type")
    movement_type = "adjustment"
    direction = "in" if adjustment_type == "increase" else "out"
    if adjustment_type == "damage":
        movement_type = "damage"
        direction = "reclass"
    elif adjustment_type == "write_off_damaged":
        movement_type = "damage"
        direction = "out"
    elif adjustment_type == "write_off_expired":
        movement_type = "expiry"
        direction = "out"
    source_key = f"ADJ:{adjustment['_id']}"
    timestamp = now_utc()
    mongo.db[STOCK_MOVEMENT_COLLECTION].update_one(
        {"source_posting_key": source_key},
        {
            "$setOnInsert": {
                "source_posting_key": source_key,
                "movement_uid": uuid4().hex,
                "accounting_entity_id": adjustment.get("accounting_entity_id"),
                "accounting_entity_id_str": adjustment.get("accounting_entity_id_str") or "",
                "source_document_type": "stock_adjustment",
                "source_document_id": adjustment["_id"],
                "source_document_id_str": str(adjustment["_id"]),
                "source_document_number": adjustment.get("adjustment_number") or "Adjustment",
                "source_product_id": adjustment.get("source_product_id"),
                "source_product_id_str": adjustment.get("source_product_id_str") or "",
                "product_code": adjustment.get("product_code") or "",
                "product_name": adjustment.get("product_name") or "Product",
                "movement_type": movement_type,
                "direction": direction,
                "quantity": adjustment.get("quantity") or 0,
                "quantity_display": adjustment.get("quantity_display") or "0",
                "unit_code": adjustment.get("unit_code") or "",
                "warehouse_code": adjustment.get("warehouse_code") or "",
                "warehouse_name": adjustment.get("warehouse_name") or "",
                "warehouse_bin": adjustment.get("warehouse_bin") or "",
                "batch_number": adjustment.get("batch_number") or "",
                "expiry_date": adjustment.get("expiry_date") or "",
                "movement_date": business_today().strftime("%Y-%m-%d"),
                "reason": adjustment.get("reason") or adjustment.get("reason_label") or "Stock adjustment",
                "posted_by": actor["_id"],
                "posted_by_name": actor["resolved_name"],
                "posted_at": timestamp,
                "created_at": timestamp,
            }
        },
        upsert=True,
    )


def approve_stock_adjustment(accounting_entity_id, actor_user_id, adjustment_id):
    entity_id = _entity_id(accounting_entity_id)
    actor = _get_actor(actor_user_id)
    if actor["resolved_role"] not in APPROVER_ROLES:
        raise PermissionError("Only an AVPL Admin or Super Admin can approve stock adjustments.")
    adjustment_object_id = _to_object_id(adjustment_id)
    if not adjustment_object_id:
        raise ValueError("Invalid stock adjustment reference.")
    adjustment = mongo.db[STOCK_ADJUSTMENT_COLLECTION].find_one(
        {"_id": adjustment_object_id, "accounting_entity_id": entity_id}
    )
    if not adjustment:
        raise ValueError("Stock adjustment not found.")
    if adjustment.get("status") == "approved":
        return {"adjustment": adjustment, "message": "Stock adjustment is already approved."}
    if adjustment.get("status") != "submitted":
        raise ValueError("Only submitted stock adjustments can be approved.")
    if adjustment.get("requested_by") == actor["_id"]:
        raise PermissionError("The person who submitted an adjustment cannot approve the same adjustment.")

    _apply_adjustment_to_lot(adjustment)
    _record_adjustment_movement(adjustment, actor)
    _sync_legacy_product_quantity(entity_id, adjustment.get("source_product_id"))
    timestamp = now_utc()
    mongo.db[STOCK_ADJUSTMENT_COLLECTION].update_one(
        {"_id": adjustment["_id"], "status": "submitted"},
        {
            "$set": {
                "status": "approved",
                "approved_by": actor["_id"],
                "approved_by_name": actor["resolved_name"],
                "approved_at": timestamp,
                "updated_at": timestamp,
            },
            "$inc": {"version": 1},
        },
    )
    updated = mongo.db[STOCK_ADJUSTMENT_COLLECTION].find_one({"_id": adjustment["_id"]})
    return {"adjustment": updated, "message": "Stock adjustment approved and inventory updated."}


def reject_stock_adjustment(accounting_entity_id, actor_user_id, adjustment_id, reason):
    entity_id = _entity_id(accounting_entity_id)
    actor = _get_actor(actor_user_id)
    if actor["resolved_role"] not in APPROVER_ROLES:
        raise PermissionError("Only an AVPL Admin or Super Admin can reject stock adjustments.")
    adjustment_object_id = _to_object_id(adjustment_id)
    if not adjustment_object_id:
        raise ValueError("Invalid stock adjustment reference.")
    reason = _clean_text(reason, 700)
    if len(reason) < 3:
        raise ValueError("Enter a rejection reason.")
    adjustment = mongo.db[STOCK_ADJUSTMENT_COLLECTION].find_one(
        {"_id": adjustment_object_id, "accounting_entity_id": entity_id}
    )
    if not adjustment:
        raise ValueError("Stock adjustment not found.")
    if adjustment.get("status") != "submitted":
        raise ValueError("Only submitted stock adjustments can be rejected.")
    timestamp = now_utc()
    mongo.db[STOCK_ADJUSTMENT_COLLECTION].update_one(
        {"_id": adjustment["_id"], "status": "submitted"},
        {
            "$set": {
                "status": "rejected",
                "rejected_by": actor["_id"],
                "rejected_by_name": actor["resolved_name"],
                "rejection_reason": reason,
                "rejected_at": timestamp,
                "updated_at": timestamp,
            },
            "$inc": {"version": 1},
        },
    )
    updated = mongo.db[STOCK_ADJUSTMENT_COLLECTION].find_one({"_id": adjustment["_id"]})
    return {"adjustment": updated, "message": "Stock adjustment rejected. No stock was changed."}


def get_stock_adjustment_overview(accounting_entity_id, actor_user_id, status_filter="all"):
    entity_id = _entity_id(accounting_entity_id)
    actor = _get_actor(actor_user_id)
    synchronize_expired_lots(entity_id)
    _ensure_indexes()
    query = {"accounting_entity_id": entity_id}
    if status_filter and status_filter != "all":
        query["status"] = status_filter
    adjustments = list(
        mongo.db[STOCK_ADJUSTMENT_COLLECTION].find(query).sort("created_at", DESCENDING).limit(200)
    )
    rows = []
    for row in adjustments:
        created = row.get("created_at")
        can_approve = (
            row.get("status") == "submitted"
            and actor["resolved_role"] in APPROVER_ROLES
            and row.get("requested_by") != actor["_id"]
        )
        rows.append(
            {
                **row,
                "id": str(row["_id"]),
                "created_display": format_ist_datetime(created, "%d %b %Y %I:%M %p", "-") if isinstance(created, datetime) else "-",
                "can_approve": can_approve,
                "status_label": str(row.get("status") or "submitted").replace("_", " ").title(),
            }
        )

    lot_options = []
    for lot in mongo.db[INVENTORY_LOT_COLLECTION].find(
        {"accounting_entity_id": entity_id, "status": {"$ne": "cancelled"}, "available_quantity": {"$gt": 0}}
    ).sort([("product_name", 1), ("warehouse_code", 1), ("expiry_date", 1)]).limit(500):
        quantities = _lot_quantities(lot)
        expiry = _date_value(lot.get("expiry_date"))
        lot_options.append(
            {
                "id": str(lot["_id"]),
                "product_name": lot.get("product_name") or "Product",
                "product_code": lot.get("product_code") or "-",
                "warehouse_code": lot.get("warehouse_code") or "AVPL-MAIN",
                "batch_number": lot.get("batch_number") or "No batch",
                "expiry_date": expiry.strftime("%d %b %Y") if expiry else "No expiry",
                "unit_code": lot.get("unit_code") or "Unit",
                "physical_quantity": _qty(quantities["physical"]),
                "saleable_quantity": _qty(quantities["saleable"]),
                "damaged_quantity": _qty(quantities["damaged"]),
                "expired": _is_expired(lot),
            }
        )

    counts = {
        status: mongo.db[STOCK_ADJUSTMENT_COLLECTION].count_documents(
            {"accounting_entity_id": entity_id, "status": status}
        )
        for status in ("submitted", "approved", "rejected")
    }
    return {
        "rows": rows,
        "lots": lot_options,
        "adjustment_types": ADJUSTMENT_TYPES,
        "reason_codes": ADJUSTMENT_REASON_CODES,
        "selected_status": status_filter or "all",
        "counts": counts,
        "actor_role": actor["resolved_role"],
    }


def publish_products_to_ufc(
    accounting_entity_id,
    actor_user_id,
    product_ids,
    *,
    publish=True,
):
    """Publish/unpublish selected Product Masters to all active UFC Centres.

    Publication changes visibility only. It never changes stock quantities or
    creates a stock movement.
    """
    _ensure_indexes()
    entity_id = _entity_id(accounting_entity_id)
    actor = _get_actor(actor_user_id)
    if publish and actor["resolved_role"] not in MARKETPLACE_PUBLISHER_ROLES:
        raise PermissionError("Only an AVPL Admin or Super Admin can publish products to the UFC Marketplace.")
    normalized = []
    for value in product_ids or []:
        oid = _to_object_id(value)
        if oid and oid not in normalized:
            normalized.append(oid)
    if not normalized:
        raise ValueError("Select at least one product.")
    if len(normalized) > 200:
        raise ValueError("Publish at most 200 products at a time.")

    products = {
        row["_id"]: row
        for row in mongo.db.products.find(
            {"_id": {"$in": normalized}, "is_deleted": {"$ne": True}}
        )
    }
    mappings = {
        row.get("source_product_id"): row
        for row in mongo.db.accounting_product_mappings.find(
            {
                "accounting_entity_id": entity_id,
                "source_product_id": {"$in": normalized},
                "is_deleted": {"$ne": True},
                "status": "active",
            }
        )
    }
    timestamp = now_utc()
    changed = 0
    skipped = []
    for product_id in normalized:
        product = products.get(product_id)
        if not product:
            skipped.append({"product_id": str(product_id), "reason": "Product not found."})
            continue
        product_name = product.get("name") or product.get("product_code") or "Product"
        if publish:
            if product.get("is_active", True) is False or product.get("status") in {"disabled", "deleted"}:
                skipped.append({"product_id": str(product_id), "product_name": product_name, "reason": "Product is disabled."})
                continue
            if product.get("unnatfarm_eligible", True) is False:
                skipped.append({"product_id": str(product_id), "product_name": product_name, "reason": "Not marked UnnatFarm eligible."})
                continue
            mapping = mappings.get(product_id)
            if not mapping:
                skipped.append({"product_id": str(product_id), "product_name": product_name, "reason": "Active Accounting mapping is required."})
                continue
            if mapping.get("sales_enabled") is False:
                skipped.append({"product_id": str(product_id), "product_name": product_name, "reason": "Sales is disabled in Accounting mapping."})
                continue

        status = "published" if publish else "unpublished"
        update = {
            "accounting_entity_id": entity_id,
            "accounting_entity_id_str": str(entity_id),
            "source_product_id": product_id,
            "source_product_id_str": str(product_id),
            "product_code": product.get("product_code") or "",
            "product_name": product_name,
            "scope": "all_active_ufc",
            "status": status,
            "updated_by": actor["_id"],
            "updated_by_name": actor["resolved_name"],
            "updated_at": timestamp,
        }
        if publish:
            update.update({"published_by": actor["_id"], "published_by_name": actor["resolved_name"], "published_at": timestamp})
        else:
            update.update({"unpublished_by": actor["_id"], "unpublished_by_name": actor["resolved_name"], "unpublished_at": timestamp})
        result = mongo.db[MARKETPLACE_PUBLICATION_COLLECTION].update_one(
            {"accounting_entity_id": entity_id, "source_product_id": product_id},
            {"$set": update, "$setOnInsert": {"created_at": timestamp}},
            upsert=True,
        )
        if result.matched_count or result.upserted_id:
            changed += 1

    return {
        "changed": changed,
        "skipped": skipped,
        "action": "published" if publish else "unpublished",
        "message": f"{changed} product(s) {'published to' if publish else 'removed from'} the UFC Marketplace.",
    }
