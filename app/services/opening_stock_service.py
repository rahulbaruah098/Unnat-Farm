from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.services.accounting_product_mapping_service import assert_product_ready_for_accounting
from app.services.avpl_inventory_service import sync_legacy_product_quantity
from app.utils.helpers import now_utc
from app.utils.timezone import business_today, format_ist_datetime


CONTROL_COLLECTION = "inventory_migration_control"
CONTROL_ID = "opening_stock_v1"
ENTRY_COLLECTION = "inventory_opening_stock_entries"
EVENT_COLLECTION = "inventory_opening_stock_events"
AVPL_LOT_COLLECTION = "avpl_inventory_lots"
AVPL_MOVEMENT_COLLECTION = "avpl_stock_movements"
UFC_LOT_COLLECTION = "ufc_inventory_lots"
UFC_MOVEMENT_COLLECTION = "ufc_stock_movements"

VIEW_ROLES = {"super_admin", "avpl_admin", "accounts"}
CONTROL_ROLES = {"super_admin", "avpl_admin"}
AVPL_ENTRY_ROLES = {"super_admin", "avpl_admin"}
UFC_ROLE = "ufc_admin"


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


def _clean(value, limit=800):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _date_text(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _clean(value, 20)
    if not text:
        return ""
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError("Enter a valid date.") from exc


def _actor(user_id):
    actor_id = _oid(user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one({"_id": actor_id})
    if not actor:
        raise ValueError("Authenticated user was not found.")
    role = _clean(actor.get("role"), 50).lower()
    status = _clean(actor.get("status"), 30).lower()
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or status in {"inactive", "disabled", "deleted", "suspended"}
    ):
        raise PermissionError("Inactive users cannot manage opening stock.")
    actor["resolved_role"] = role
    actor["resolved_name"] = actor.get("name") or actor.get("full_name") or actor.get("username") or actor.get("phone") or role.replace("_", " ").title()
    return actor


def _active_avpl_entity(entity_id=None):
    query = {
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    if entity_id:
        oid = _oid(entity_id)
        if oid:
            query["_id"] = oid
    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise ValueError("The active AVPL Accounting entity is unavailable.")
    return entity


def _ufc_identity(actor, centre_uid_hint=None):
    if actor.get("resolved_role") != UFC_ROLE:
        raise PermissionError("Only UFC Admin can manage opening stock for a UFC Centre.")
    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor["_id"])})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor["_id"]})
        or {}
    )
    centre_uid = _clean(master.get("centre_uid") or actor.get("centre_uid") or actor.get("mapped_centre_uid") or centre_uid_hint, 80)
    if not centre_uid:
        raise ValueError("This UFC Admin is not linked to a valid Centre UID.")
    hint = _clean(centre_uid_hint, 80)
    if hint and hint != centre_uid:
        raise PermissionError("Your session Centre UID does not match your UFC profile. Please log in again.")
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or master
    centre_name = centre.get("name_of_enterprise") or centre.get("enterprise_name") or centre.get("centre_name") or centre.get("name") or centre_uid
    return centre_uid, centre_name


def _ensure_indexes():
    definitions = [
        (ENTRY_COLLECTION, [("opening_number", ASCENDING)], {"name": "opening_stock_number_unique", "unique": True}),
        (ENTRY_COLLECTION, [("entry_key", ASCENDING)], {"name": "opening_stock_entry_key_idx"}),
        (ENTRY_COLLECTION, [("active_key", ASCENDING)], {"name": "opening_stock_active_key_unique", "unique": True, "sparse": True}),
        (ENTRY_COLLECTION, [("idempotency_key", ASCENDING)], {"name": "opening_stock_idempotency_unique", "unique": True, "sparse": True}),
        (ENTRY_COLLECTION, [("scope", ASCENDING), ("centre_uid", ASCENDING), ("created_at", DESCENDING)], {"name": "opening_stock_scope_centre_idx"}),
        (EVENT_COLLECTION, [("opening_stock_entry_id", ASCENDING), ("created_at", DESCENDING)], {"name": "opening_stock_event_entry_idx"}),
        (EVENT_COLLECTION, [("event_key", ASCENDING)], {"name": "opening_stock_event_key_unique", "unique": True, "sparse": True}),
    ]
    for collection_name, keys, options in definitions:
        try:
            mongo.db[collection_name].create_index(keys, **options)
        except Exception:
            pass


def _record_event(document):
    """Best-effort secondary audit event. Core entry + movement remain authoritative."""
    try:
        event_key = document.get("event_key")
        if event_key:
            mongo.db[EVENT_COLLECTION].update_one(
                {"event_key": event_key}, {"$setOnInsert": document}, upsert=True
            )
        else:
            mongo.db[EVENT_COLLECTION].insert_one(document)
    except Exception:
        # Do not turn an already-posted stock transaction into an HTTP 500 merely
        # because the convenience event stream is temporarily unavailable. The
        # opening-stock entry, inventory lot/movement and application audit log
        # still retain the authoritative trail.
        pass


def _next_number(scope, centre_uid=""):
    year = business_today().year
    if scope == "ufc":
        safe = "".join(ch for ch in str(centre_uid or "UFC").upper() if ch.isalnum())[:14] or "UFC"
        counter_key = f"opening_stock:ufc:{safe}:{year}"
        prefix = f"{safe}-OPEN"
    else:
        counter_key = f"opening_stock:avpl:{year}"
        prefix = "AVPL-OPEN"
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": counter_key},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return f"{prefix}-{year}-{int((counter or {}).get('sequence') or 1):05d}"


def get_opening_stock_mode():
    row = mongo.db[CONTROL_COLLECTION].find_one({"_id": CONTROL_ID}) or {}
    return {
        "enabled": row.get("enabled") is True,
        "status": "open" if row.get("enabled") is True else "closed",
        "opened_at": row.get("opened_at"),
        "opened_by_name": row.get("opened_by_name") or "",
        "open_reason": row.get("open_reason") or "",
        "closed_at": row.get("closed_at"),
        "closed_by_name": row.get("closed_by_name") or "",
        "close_reason": row.get("close_reason") or "",
        "reopen_count": int(row.get("reopen_count") or 0),
        "updated_at": row.get("updated_at"),
    }


def set_opening_stock_mode(actor_user_id, enabled, reason):
    actor = _actor(actor_user_id)
    if actor["resolved_role"] not in CONTROL_ROLES:
        raise PermissionError("Only AVPL Admin or Super Admin can open or close Opening Stock Mode.")
    reason = _clean(reason, 1000)
    if len(reason) < 5:
        raise ValueError("Enter a clear reason for changing Opening Stock Mode.")
    current = mongo.db[CONTROL_COLLECTION].find_one({"_id": CONTROL_ID}) or {}
    enabled = bool(enabled)
    if current.get("enabled") is enabled:
        return {"mode": get_opening_stock_mode(), "message": f"Opening Stock Mode is already {'open' if enabled else 'closed'}."}
    if not enabled:
        unresolved = mongo.db[ENTRY_COLLECTION].count_documents(
            {"status": {"$in": ["posting", "posting_failed"]}}
        )
        if unresolved:
            raise RuntimeError(
                f"Cannot close Opening Stock Mode while {unresolved} opening-stock posting(s) need recovery. Retry those entries first."
            )
    timestamp = now_utc()
    update = {
        "enabled": enabled,
        "updated_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_name": actor["resolved_name"],
    }
    if enabled:
        update.update({"opened_at": timestamp, "opened_by": actor["_id"], "opened_by_name": actor["resolved_name"], "open_reason": reason})
        if current:
            update["reopen_count"] = int(current.get("reopen_count") or 0) + 1
    else:
        update.update({"closed_at": timestamp, "closed_by": actor["_id"], "closed_by_name": actor["resolved_name"], "close_reason": reason})
    # Do not target reopen_count from both $set and $setOnInsert in the same
    # MongoDB update. MongoDB treats that as a conflicting update path.
    # For a brand-new control record, initialise the migration metadata directly
    # in $set; for an existing closed record, preserve the increment calculated
    # above when the mode is reopened.
    if not current:
        update["created_at"] = timestamp
        update.setdefault("reopen_count", 0)

    mongo.db[CONTROL_COLLECTION].update_one(
        {"_id": CONTROL_ID},
        {"$set": update},
        upsert=True,
    )
    _record_event({
        "event_type": "mode_opened" if enabled else "mode_closed",
        "scope": "control",
        "reason": reason,
        "actor_user_id": actor["_id"],
        "actor_name": actor["resolved_name"],
        "actor_role": actor["resolved_role"],
        "created_at": timestamp,
    })
    return {"mode": get_opening_stock_mode(), "message": f"Opening Stock Mode {'opened' if enabled else 'closed'} successfully."}


def _assert_mode_open():
    if not get_opening_stock_mode()["enabled"]:
        raise PermissionError("Opening Stock Mode is closed. AVPL Admin or Super Admin must reopen it before migration entries can change.")


def _product(product_id, entity_id=None):
    product_oid = _oid(product_id)
    if not product_oid:
        raise ValueError("Select a valid product.")
    product = mongo.db.products.find_one({"_id": product_oid, "is_deleted": {"$ne": True}, "is_active": {"$ne": False}, "status": {"$nin": ["disabled", "deleted", "inactive"]}})
    if not product:
        raise ValueError("The selected Product Master is not active.")
    if entity_id:
        # Opening stock must join the same accounting-ready Product Master used by future transactions.
        assert_product_ready_for_accounting(entity_id, product_oid)
    unit = product.get("base_unit_code") or product.get("base_unit_name") or product.get("unit") or "Unit"
    return product, unit


def _product_catalog(entity_id):
    rows = list(
        mongo.db.products.find(
            {
                "is_deleted": {"$ne": True},
                "is_active": {"$ne": False},
                "status": {"$nin": ["disabled", "deleted", "inactive"]},
            }
        ).sort("name", ASCENDING)
    )
    product_ids = [row["_id"] for row in rows]
    mapping_rows = list(
        mongo.db.accounting_product_mappings.find(
            {
                "accounting_entity_id": entity_id,
                "source_product_id": {"$in": product_ids or [ObjectId()]},
                "status": "active",
                "is_active": True,
                "is_accounting_eligible": True,
                "is_deleted": {"$ne": True},
            },
            {"source_product_id": 1, "base_unit_code": 1, "base_unit_name": 1},
        )
    ) if product_ids else []
    mapping_by_product = {str(row.get("source_product_id")): row for row in mapping_rows}
    catalog = []
    for product in rows:
        mapping = mapping_by_product.get(str(product["_id"])) or {}
        ready = bool(mapping)
        catalog.append({
            "id": str(product["_id"]),
            "name": product.get("name") or "Product",
            "code": product.get("product_code") or "-",
            "category": product.get("category") or "-",
            "role": product.get("product_role") or product.get("type") or "",
            "unit_code": mapping.get("base_unit_code") or product.get("base_unit_code") or mapping.get("base_unit_name") or product.get("base_unit_name") or "Unit",
            "accounting_ready": ready,
            "readiness_error": "" if ready else "Complete and activate the Product Accounting mapping.",
        })
    return catalog


def _validate_common(quantity, unit_cost, warehouse_code, warehouse_name, batch_number, manufacturing_date, expiry_date, reference, note):
    qty = _decimal(quantity)
    if qty <= 0:
        raise ValueError("Opening quantity must be greater than zero.")
    cost = _decimal(unit_cost)
    if cost < 0:
        raise ValueError("Opening unit cost cannot be negative.")
    wh_code = _clean(warehouse_code, 50).upper() or "MAIN"
    wh_name = _clean(warehouse_name, 160) or wh_code
    batch = _clean(batch_number, 120).upper()
    mfg = _date_text(manufacturing_date)
    expiry = _date_text(expiry_date)
    if mfg and expiry and expiry < mfg:
        raise ValueError("Expiry date cannot be earlier than manufacturing date.")
    ref = _clean(reference, 220)
    if len(ref) < 2:
        raise ValueError("Enter an opening-stock reference, such as physical count sheet or previous-system reference.")
    return qty, cost, wh_code, wh_name, batch, mfg, expiry, ref, _clean(note, 1200)


def _entry_key(scope, owner_key, product_id, warehouse_code, batch_number, expiry_date):
    batch_key = batch_number or "NO-BATCH"
    expiry_key = expiry_date or "NO-EXPIRY"
    return f"{scope}:{owner_key}:{product_id}:{warehouse_code}:{batch_key}:{expiry_key}".upper()


def _post_lot_and_movement(entry, actor):
    scope = entry["scope"]
    lot_collection = AVPL_LOT_COLLECTION if scope == "avpl" else UFC_LOT_COLLECTION
    movement_collection = AVPL_MOVEMENT_COLLECTION if scope == "avpl" else UFC_MOVEMENT_COLLECTION
    timestamp = now_utc()
    lot_key = f"OPEN:{entry['_id']}"
    source_key = f"OPEN:{entry['_id']}"
    quantity = _decimal(entry.get("opening_quantity"))
    cost = _decimal(entry.get("unit_cost"))
    lot = {
        "lot_key": lot_key,
        "source_type": "opening_stock",
        "opening_stock_entry_id": entry["_id"],
        "opening_stock_entry_id_str": str(entry["_id"]),
        "source_product_id": entry["source_product_id"],
        "source_product_id_str": str(entry["source_product_id"]),
        "product_code": entry.get("product_code") or "",
        "product_name": entry.get("product_name") or "",
        "category": entry.get("category") or "",
        "product_role": entry.get("product_role") or "",
        "unit_code": entry.get("unit_code") or "Unit",
        "warehouse_code": entry.get("warehouse_code") or "MAIN",
        "warehouse_name": entry.get("warehouse_name") or entry.get("warehouse_code") or "Main Stock",
        "warehouse_bin": entry.get("warehouse_bin") or "",
        "batch_number": entry.get("batch_number") or "",
        "manufacturing_date": entry.get("manufacturing_date") or "",
        "expiry_date": entry.get("expiry_date") or "",
        "received_quantity": float(quantity),
        "opening_quantity": float(quantity),
        "available_quantity": float(quantity),
        "reserved_quantity": 0.0,
        "issued_quantity": 0.0,
        "damaged_quantity": 0.0,
        "blocked_quantity": 0.0,
        "opening_unit_cost": float(cost),
        # Compatibility cost fields are populated because existing UFC sale/
        # reporting services read one or more of these names. They all represent
        # the same verified historical opening cost; no supplier purchase is made.
        "unit_cost": float(cost),
        "purchase_unit_cost": float(cost),
        "purchase_cost_total": float(quantity * cost),
        "last_purchase_price": float(cost),
        "status": "available",
        "applied_stock_keys": [source_key],
        "applied_receipt_keys": [source_key],
        "created_by": actor["_id"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if scope == "avpl":
        lot.update({"accounting_entity_id": entry["accounting_entity_id"], "accounting_entity_id_str": str(entry["accounting_entity_id"])})
    else:
        lot.update({"centre_uid": entry["centre_uid"], "centre_name": entry.get("centre_name") or entry["centre_uid"]})
    existing = mongo.db[lot_collection].find_one({"lot_key": lot_key})
    if not existing:
        mongo.db[lot_collection].insert_one(lot)
        existing = mongo.db[lot_collection].find_one({"lot_key": lot_key})

    movement = {
        "source_posting_key": source_key,
        "movement_uid": uuid4().hex,
        "source_document_type": "opening_stock",
        "source_document_id": entry["_id"],
        "source_document_id_str": str(entry["_id"]),
        "source_document_number": entry.get("opening_number") or "Opening Stock",
        "source_product_id": entry["source_product_id"],
        "source_product_id_str": str(entry["source_product_id"]),
        "product_code": entry.get("product_code") or "",
        "product_name": entry.get("product_name") or "",
        "movement_type": "opening_stock",
        "direction": "in",
        "quantity": float(quantity),
        "quantity_display": _qty(quantity),
        "unit_code": entry.get("unit_code") or "Unit",
        "warehouse_code": entry.get("warehouse_code") or "MAIN",
        "warehouse_name": entry.get("warehouse_name") or "",
        "warehouse_bin": entry.get("warehouse_bin") or "",
        "batch_number": entry.get("batch_number") or "",
        "manufacturing_date": entry.get("manufacturing_date") or "",
        "expiry_date": entry.get("expiry_date") or "",
        "movement_date": entry.get("opening_date") or business_today().isoformat(),
        "reason": f"Opening balance migrated at go-live. Reference: {entry.get('reference') or '-'}",
        "posted_by": actor["_id"],
        "posted_by_name": actor["resolved_name"],
        "posted_at": timestamp,
        "created_at": timestamp,
    }
    if scope == "avpl":
        movement.update({"accounting_entity_id": entry["accounting_entity_id"], "accounting_entity_id_str": str(entry["accounting_entity_id"])})
    else:
        movement.update({"centre_uid": entry["centre_uid"], "centre_name": entry.get("centre_name") or entry["centre_uid"]})
    mongo.db[movement_collection].update_one({"source_posting_key": source_key}, {"$setOnInsert": movement}, upsert=True)
    return existing


def _resume_entry_posting(entry, actor):
    if entry.get("status") == "active":
        return entry
    if entry.get("status") not in {"posting", "posting_failed"}:
        return entry
    lot = _post_lot_and_movement(entry, actor)
    timestamp = now_utc()
    mongo.db[ENTRY_COLLECTION].update_one(
        {"_id": entry["_id"]},
        {"$set": {
            "status": "active",
            "inventory_lot_id": lot["_id"],
            "inventory_lot_id_str": str(lot["_id"]),
            "posted_at": entry.get("posted_at") or timestamp,
            "updated_at": timestamp,
        }},
    )
    if entry.get("scope") == "avpl":
        sync_legacy_product_quantity(entry.get("accounting_entity_id"), entry.get("source_product_id"))
    return mongo.db[ENTRY_COLLECTION].find_one({"_id": entry["_id"]})


def _create_entry(*, scope, actor, entity=None, centre_uid="", centre_name="", product_id, quantity, unit_cost, warehouse_code, warehouse_name, warehouse_bin="", batch_number="", manufacturing_date="", expiry_date="", opening_date="", reference="", note="", proof_filename="", proof_document_id=None, idempotency_key=""):
    _ensure_indexes()
    _assert_mode_open()
    if scope == "avpl" and actor["resolved_role"] not in AVPL_ENTRY_ROLES:
        raise PermissionError("Only AVPL Admin or Super Admin can enter AVPL opening stock.")
    if scope == "ufc" and actor["resolved_role"] != UFC_ROLE:
        raise PermissionError("Only UFC Admin can enter opening stock for its own Centre.")
    entity_id = entity["_id"] if entity else _active_avpl_entity()["_id"]
    product, unit_code = _product(product_id, entity_id)
    qty, cost, wh_code, wh_name, batch, mfg, expiry, ref, clean_note = _validate_common(quantity, unit_cost, warehouse_code, warehouse_name, batch_number, manufacturing_date, expiry_date, reference, note)
    open_date = _date_text(opening_date) or business_today().isoformat()
    if open_date > business_today().isoformat():
        raise ValueError("Opening date cannot be in the future.")
    owner_key = str(entity_id) if scope == "avpl" else centre_uid
    key = _entry_key(scope, owner_key, product["_id"], wh_code, batch, expiry)
    token = _clean(idempotency_key, 160)
    if token:
        existing_token = mongo.db[ENTRY_COLLECTION].find_one({"idempotency_key": token})
        if existing_token:
            recovered = _resume_entry_posting(existing_token, actor)
            return {"entry": recovered, "message": "This opening-stock entry was already saved."}
    existing = mongo.db[ENTRY_COLLECTION].find_one({"active_key": key})
    if existing:
        if existing.get("status") in {"posting", "posting_failed"}:
            recovered = _resume_entry_posting(existing, actor)
            return {"entry": recovered, "message": f"{recovered.get('opening_number')} recovered and posted successfully."}
        raise ValueError(f"Opening stock already exists for this product/batch/warehouse ({existing.get('opening_number')}). Use Correct instead of adding it again.")
    timestamp = now_utc()
    entry_id = ObjectId()
    document = {
        "_id": entry_id,
        "opening_number": _next_number(scope, centre_uid),
        "entry_key": key,
        "active_key": key,
        "idempotency_key": token or f"OPEN-{uuid4().hex}",
        "scope": scope,
        "source_product_id": product["_id"],
        "source_product_id_str": str(product["_id"]),
        "product_code": product.get("product_code") or "",
        "product_name": product.get("name") or "Product",
        "category": product.get("category") or "",
        "product_role": product.get("product_role") or product.get("type") or "",
        "unit_code": unit_code,
        "opening_quantity": float(qty),
        "unit_cost": float(cost),
        "total_cost": float(qty * cost),
        "warehouse_code": wh_code,
        "warehouse_name": wh_name,
        "warehouse_bin": _clean(warehouse_bin, 80).upper(),
        "batch_number": batch,
        "manufacturing_date": mfg,
        "expiry_date": expiry,
        "opening_date": open_date,
        "reference": ref,
        "note": clean_note,
        "proof_filename": _clean(proof_filename, 255),
        "proof_document_id": _oid(proof_document_id),
        "status": "posting",
        "revision_count": 0,
        "created_by": actor["_id"],
        "created_by_name": actor["resolved_name"],
        "created_by_role": actor["resolved_role"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if scope == "avpl":
        document.update({"accounting_entity_id": entity_id, "accounting_entity_id_str": str(entity_id), "owner_name": entity.get("legal_name") or entity.get("name") or "AVPL"})
    else:
        document.update({"centre_uid": centre_uid, "centre_name": centre_name, "owner_name": centre_name})
    try:
        mongo.db[ENTRY_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        concurrent = mongo.db[ENTRY_COLLECTION].find_one({
            "$or": [
                {"active_key": key},
                {"idempotency_key": document["idempotency_key"]},
            ]
        })
        if concurrent:
            recovered = _resume_entry_posting(concurrent, actor)
            return {
                "entry": recovered,
                "message": f"{recovered.get('opening_number')} was already created and has been recovered safely.",
            }
        raise RuntimeError("Opening stock was submitted concurrently. Refresh and try again.") from exc
    try:
        lot = _post_lot_and_movement(document, actor)
        mongo.db[ENTRY_COLLECTION].update_one({"_id": entry_id}, {"$set": {"status": "active", "inventory_lot_id": lot["_id"], "inventory_lot_id_str": str(lot["_id"]), "posted_at": timestamp, "updated_at": timestamp}})
        if scope == "avpl":
            sync_legacy_product_quantity(entity_id, product["_id"])
    except Exception:
        mongo.db[ENTRY_COLLECTION].update_one({"_id": entry_id}, {"$set": {"status": "posting_failed", "updated_at": now_utc()}})
        raise
    saved = mongo.db[ENTRY_COLLECTION].find_one({"_id": entry_id})
    _record_event({"opening_stock_entry_id": entry_id, "opening_number": saved.get("opening_number"), "event_type": "created", "scope": scope, "old_quantity": 0.0, "new_quantity": float(qty), "old_unit_cost": 0.0, "new_unit_cost": float(cost), "reason": ref, "actor_user_id": actor["_id"], "actor_name": actor["resolved_name"], "actor_role": actor["resolved_role"], "created_at": timestamp})
    return {"entry": saved, "message": f"{saved.get('opening_number')} posted to opening stock."}


def create_avpl_opening_stock(accounting_entity_id, actor_user_id, **kwargs):
    actor = _actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    return _create_entry(scope="avpl", actor=actor, entity=entity, **kwargs)


def create_ufc_opening_stock(actor_user_id, centre_uid_hint=None, **kwargs):
    actor = _actor(actor_user_id)
    centre_uid, centre_name = _ufc_identity(actor, centre_uid_hint)
    return _create_entry(scope="ufc", actor=actor, centre_uid=centre_uid, centre_name=centre_name, warehouse_code=f"{centre_uid.upper()}-MAIN", warehouse_name=f"{centre_name} Main Stock", **kwargs)


def _entry_for_change(entry_id):
    oid = _oid(entry_id)
    if not oid:
        raise ValueError("Invalid opening-stock reference.")
    entry = mongo.db[ENTRY_COLLECTION].find_one({"_id": oid})
    if not entry:
        raise ValueError("Opening-stock entry was not found.")
    if entry.get("status") != "active":
        raise ValueError("Only active opening-stock entries can be corrected or voided.")
    return entry


def _authorize_entry_change(actor, entry, centre_uid_hint=None):
    if actor["resolved_role"] in CONTROL_ROLES:
        return
    if actor["resolved_role"] == UFC_ROLE and entry.get("scope") == "ufc":
        centre_uid, _ = _ufc_identity(actor, centre_uid_hint)
        if centre_uid == entry.get("centre_uid"):
            return
    raise PermissionError("You are not authorized to change this opening-stock entry.")


def correct_opening_stock_entry(actor_user_id, entry_id, *, new_quantity, new_unit_cost, reason, centre_uid_hint=None):
    _assert_mode_open()
    actor = _actor(actor_user_id)
    entry = _entry_for_change(entry_id)
    _authorize_entry_change(actor, entry, centre_uid_hint)
    reason = _clean(reason, 1000)
    if len(reason) < 5:
        raise ValueError("Enter a clear correction reason.")
    qty = _decimal(new_quantity)
    cost = _decimal(new_unit_cost)
    if qty <= 0:
        raise ValueError("Corrected opening quantity must be greater than zero. Use Void for an unused entry that should be removed.")
    if cost < 0:
        raise ValueError("Corrected unit cost cannot be negative.")

    lot_collection = AVPL_LOT_COLLECTION if entry["scope"] == "avpl" else UFC_LOT_COLLECTION
    movement_collection = AVPL_MOVEMENT_COLLECTION if entry["scope"] == "avpl" else UFC_MOVEMENT_COLLECTION
    lot = mongo.db[lot_collection].find_one({"_id": entry.get("inventory_lot_id")})
    if not lot or lot.get("status") == "cancelled":
        raise RuntimeError("The inventory lot linked to this opening stock no longer exists.")

    old_qty = _decimal(entry.get("opening_quantity"))
    old_cost = _decimal(entry.get("unit_cost"))
    delta = qty - old_qty
    revision_no = int(entry.get("revision_count") or 0) + 1
    correction_key = f"OPEN-CORR:{entry['_id']}:{revision_no}"
    applied_keys = {str(value) for value in (lot.get("applied_stock_keys") or [])}
    already_applied = correction_key in applied_keys
    timestamp = now_utc()

    if not already_applied:
        current_physical = max(_decimal(lot.get("available_quantity")), Decimal("0"))
        reserved = max(_decimal(lot.get("reserved_quantity")), Decimal("0"))
        damaged = max(_decimal(lot.get("damaged_quantity")), Decimal("0"))
        blocked = max(_decimal(lot.get("blocked_quantity")), Decimal("0"))
        saleable_now = max(current_physical - reserved - damaged - blocked, Decimal("0"))
        if delta < 0 and abs(delta) > saleable_now:
            raise ValueError(
                f"Opening quantity cannot be reduced by {_qty(abs(delta))} {entry.get('unit_code') or 'units'} "
                f"because only {_qty(saleable_now)} is currently free. Existing reservations/issues must remain valid."
            )
        new_physical = current_physical + delta
        lot_update = {
            "opening_quantity": float(qty),
            "received_quantity": float(qty),
            "available_quantity": float(new_physical),
            "opening_unit_cost": float(cost),
            "unit_cost": float(cost),
            "purchase_unit_cost": float(cost),
            "purchase_cost_total": float(qty * cost),
            "last_purchase_price": float(cost),
            "updated_at": timestamp,
            "last_opening_correction_at": timestamp,
        }
        result = mongo.db[lot_collection].update_one(
            {
                "_id": lot["_id"],
                "available_quantity": lot.get("available_quantity"),
                "applied_stock_keys": {"$ne": correction_key},
            },
            {"$set": lot_update, "$addToSet": {"applied_stock_keys": correction_key}},
        )
        if result.matched_count != 1:
            refreshed = mongo.db[lot_collection].find_one({"_id": lot["_id"]}) or {}
            if correction_key not in {str(value) for value in (refreshed.get("applied_stock_keys") or [])}:
                raise RuntimeError("Stock changed while correcting the opening balance. Refresh and try again.")

    direction = "in" if delta > 0 else ("out" if delta < 0 else "none")
    movement = {
        "source_posting_key": correction_key,
        "movement_uid": uuid4().hex,
        "source_document_type": "opening_stock_correction",
        "source_document_id": entry["_id"],
        "source_document_id_str": str(entry["_id"]),
        "source_document_number": entry.get("opening_number") or "Opening Stock",
        "source_product_id": entry["source_product_id"],
        "source_product_id_str": str(entry["source_product_id"]),
        "product_code": entry.get("product_code") or "",
        "product_name": entry.get("product_name") or "",
        "movement_type": "opening_stock_correction",
        "direction": direction,
        "quantity": float(abs(delta)),
        "quantity_display": _qty(abs(delta)),
        "unit_code": entry.get("unit_code") or "Unit",
        "warehouse_code": entry.get("warehouse_code") or "",
        "warehouse_name": entry.get("warehouse_name") or "",
        "batch_number": entry.get("batch_number") or "",
        "movement_date": business_today().isoformat(),
        "reason": reason,
        "posted_by": actor["_id"],
        "posted_by_name": actor["resolved_name"],
        "posted_at": timestamp,
        "created_at": timestamp,
    }
    if entry["scope"] == "avpl":
        movement.update({
            "accounting_entity_id": entry["accounting_entity_id"],
            "accounting_entity_id_str": str(entry["accounting_entity_id"]),
        })
    else:
        movement.update({
            "centre_uid": entry["centre_uid"],
            "centre_name": entry.get("centre_name") or entry["centre_uid"],
        })
    mongo.db[movement_collection].update_one(
        {"source_posting_key": correction_key}, {"$setOnInsert": movement}, upsert=True
    )

    # Conditional revision update makes recovery idempotent if a previous request
    # changed the lot but failed before finishing this entry update.
    entry_result = mongo.db[ENTRY_COLLECTION].update_one(
        {"_id": entry["_id"], "revision_count": int(entry.get("revision_count") or 0), "status": "active"},
        {
            "$set": {
                "opening_quantity": float(qty),
                "unit_cost": float(cost),
                "total_cost": float(qty * cost),
                "last_correction_reason": reason,
                "last_corrected_at": timestamp,
                "last_corrected_by": actor["_id"],
                "last_corrected_by_name": actor["resolved_name"],
                "updated_at": timestamp,
            },
            "$inc": {"revision_count": 1},
        },
    )
    if entry_result.matched_count != 1:
        refreshed_entry = mongo.db[ENTRY_COLLECTION].find_one({"_id": entry["_id"]}) or {}
        if int(refreshed_entry.get("revision_count") or 0) < revision_no:
            raise RuntimeError("Opening stock changed while saving the correction. Refresh and verify the latest value.")

    _record_event({
        "event_key": correction_key,
        "opening_stock_entry_id": entry["_id"],
        "opening_number": entry.get("opening_number"),
        "event_type": "corrected",
        "scope": entry["scope"],
        "old_quantity": float(old_qty),
        "new_quantity": float(qty),
        "old_unit_cost": float(old_cost),
        "new_unit_cost": float(cost),
        "quantity_delta": float(delta),
        "reason": reason,
        "actor_user_id": actor["_id"],
        "actor_name": actor["resolved_name"],
        "actor_role": actor["resolved_role"],
        "created_at": timestamp,
    })
    if entry["scope"] == "avpl":
        sync_legacy_product_quantity(entry["accounting_entity_id"], entry["source_product_id"])
    return {
        "entry": mongo.db[ENTRY_COLLECTION].find_one({"_id": entry["_id"]}),
        "message": "Opening stock corrected. The previous value remains in audit history.",
    }


def void_opening_stock_entry(actor_user_id, entry_id, *, reason, centre_uid_hint=None):
    _assert_mode_open()
    actor = _actor(actor_user_id)
    entry = _entry_for_change(entry_id)
    _authorize_entry_change(actor, entry, centre_uid_hint)
    reason = _clean(reason, 1000)
    if len(reason) < 5:
        raise ValueError("Enter a clear reason for voiding this opening entry.")

    lot_collection = AVPL_LOT_COLLECTION if entry["scope"] == "avpl" else UFC_LOT_COLLECTION
    movement_collection = AVPL_MOVEMENT_COLLECTION if entry["scope"] == "avpl" else UFC_MOVEMENT_COLLECTION
    lot = mongo.db[lot_collection].find_one({"_id": entry.get("inventory_lot_id")})
    if not lot:
        raise RuntimeError("The linked opening inventory lot no longer exists.")

    opening_qty = _decimal(entry.get("opening_quantity"))
    void_key = f"OPEN-VOID:{entry['_id']}"
    applied_keys = {str(value) for value in (lot.get("applied_stock_keys") or [])}
    already_applied = lot.get("status") == "cancelled" and void_key in applied_keys
    timestamp = now_utc()

    if not already_applied:
        if lot.get("status") == "cancelled":
            raise RuntimeError("The linked opening inventory lot is already cancelled without a matching opening-stock void record.")
        current = _decimal(lot.get("available_quantity"))
        reserved = _decimal(lot.get("reserved_quantity"))
        issued = _decimal(lot.get("issued_quantity"))
        damaged = _decimal(lot.get("damaged_quantity"))
        blocked = _decimal(lot.get("blocked_quantity"))
        # Voiding a source after business activity would rewrite history; use a correction/adjustment instead.
        if abs(current - opening_qty) > Decimal("0.0001") or any(
            abs(x) > Decimal("0.0001") for x in (reserved, issued, damaged, blocked)
        ):
            raise ValueError(
                "This opening lot already has downstream stock activity/classification and cannot be voided. "
                "Correct the opening quantity while mode is open, or use the controlled adjustment workflow."
            )
        result = mongo.db[lot_collection].update_one(
            {"_id": lot["_id"], "status": {"$ne": "cancelled"}, "applied_stock_keys": {"$ne": void_key}},
            {
                "$set": {
                    "status": "cancelled",
                    "available_quantity": 0.0,
                    "received_quantity": 0.0,
                    "opening_quantity": 0.0,
                    "purchase_cost_total": 0.0,
                    "voided_at": timestamp,
                    "void_reason": reason,
                    "updated_at": timestamp,
                },
                "$addToSet": {"applied_stock_keys": void_key},
            },
        )
        if result.matched_count != 1:
            refreshed = mongo.db[lot_collection].find_one({"_id": lot["_id"]}) or {}
            refreshed_keys = {str(value) for value in (refreshed.get("applied_stock_keys") or [])}
            if not (refreshed.get("status") == "cancelled" and void_key in refreshed_keys):
                raise RuntimeError("Stock changed while voiding the opening balance. Refresh and try again.")

    movement = {
        "source_posting_key": void_key,
        "movement_uid": uuid4().hex,
        "source_document_type": "opening_stock_void",
        "source_document_id": entry["_id"],
        "source_document_id_str": str(entry["_id"]),
        "source_document_number": entry.get("opening_number") or "Opening Stock",
        "source_product_id": entry["source_product_id"],
        "source_product_id_str": str(entry["source_product_id"]),
        "product_code": entry.get("product_code") or "",
        "product_name": entry.get("product_name") or "",
        "movement_type": "opening_stock_void",
        "direction": "out",
        "quantity": float(opening_qty),
        "quantity_display": _qty(opening_qty),
        "unit_code": entry.get("unit_code") or "Unit",
        "warehouse_code": entry.get("warehouse_code") or "",
        "warehouse_name": entry.get("warehouse_name") or "",
        "batch_number": entry.get("batch_number") or "",
        "movement_date": business_today().isoformat(),
        "reason": reason,
        "posted_by": actor["_id"],
        "posted_by_name": actor["resolved_name"],
        "posted_at": timestamp,
        "created_at": timestamp,
    }
    if entry["scope"] == "avpl":
        movement.update({
            "accounting_entity_id": entry["accounting_entity_id"],
            "accounting_entity_id_str": str(entry["accounting_entity_id"]),
        })
    else:
        movement.update({
            "centre_uid": entry["centre_uid"],
            "centre_name": entry.get("centre_name") or entry["centre_uid"],
        })
    mongo.db[movement_collection].update_one(
        {"source_posting_key": void_key}, {"$setOnInsert": movement}, upsert=True
    )
    mongo.db[ENTRY_COLLECTION].update_one(
        {"_id": entry["_id"], "status": "active"},
        {
            "$set": {
                "status": "voided",
                "void_reason": reason,
                "voided_at": timestamp,
                "voided_by": actor["_id"],
                "voided_by_name": actor["resolved_name"],
                "updated_at": timestamp,
            },
            "$unset": {"active_key": ""},
        },
    )
    _record_event({
        "event_key": void_key,
        "opening_stock_entry_id": entry["_id"],
        "opening_number": entry.get("opening_number"),
        "event_type": "voided",
        "scope": entry["scope"],
        "old_quantity": float(opening_qty),
        "new_quantity": 0.0,
        "reason": reason,
        "actor_user_id": actor["_id"],
        "actor_name": actor["resolved_name"],
        "actor_role": actor["resolved_role"],
        "created_at": timestamp,
    })
    if entry["scope"] == "avpl":
        sync_legacy_product_quantity(entry["accounting_entity_id"], entry["source_product_id"])
    return {
        "entry": mongo.db[ENTRY_COLLECTION].find_one({"_id": entry["_id"]}),
        "message": "Opening-stock entry voided. Audit history was preserved.",
    }


def _serialize_entry(row, actor_role="", own_centre="", mode_enabled=None):
    created = row.get("created_at")
    scope = row.get("scope") or "avpl"
    if mode_enabled is None:
        mode_enabled = get_opening_stock_mode()["enabled"]
    can_change = bool(mode_enabled) and row.get("status") == "active" and (
        actor_role in CONTROL_ROLES or (actor_role == UFC_ROLE and scope == "ufc" and row.get("centre_uid") == own_centre)
    )
    return {
        **row,
        "id": str(row["_id"]),
        "scope_label": "AVPL" if scope == "avpl" else (row.get("centre_uid") or "UFC"),
        "quantity_display": _qty(row.get("opening_quantity")),
        "unit_cost_display": _money(row.get("unit_cost")),
        "total_cost_display": _money(row.get("total_cost")),
        "created_display": format_ist_datetime(created, "%d %b %Y %I:%M %p", "-") if isinstance(created, datetime) else "-",
        "status_label": str(row.get("status") or "active").replace("_", " ").title(),
        "can_change": can_change,
    }


def get_management_opening_stock_overview(accounting_entity_id, actor_user_id, *, search="", scope="all"):
    _ensure_indexes()
    actor = _actor(actor_user_id)
    if actor["resolved_role"] not in VIEW_ROLES:
        raise PermissionError("You are not authorized to view opening-stock migration.")
    entity = _active_avpl_entity(accounting_entity_id)
    query = {}
    selected_scope = _clean(scope, 20).lower()
    if selected_scope in {"avpl", "ufc"}:
        query["scope"] = selected_scope
    text = _clean(search, 120)
    if text:
        escaped = re.escape(text)
        query["$or"] = [
            {"opening_number": {"$regex": escaped, "$options": "i"}}, {"product_name": {"$regex": escaped, "$options": "i"}},
            {"product_code": {"$regex": escaped, "$options": "i"}}, {"centre_uid": {"$regex": escaped, "$options": "i"}},
            {"batch_number": {"$regex": escaped, "$options": "i"}}, {"reference": {"$regex": escaped, "$options": "i"}},
        ]
    mode = get_opening_stock_mode()
    rows = [_serialize_entry(row, actor["resolved_role"], mode_enabled=mode["enabled"]) for row in mongo.db[ENTRY_COLLECTION].find(query).sort("created_at", DESCENDING).limit(500)]
    active = [r for r in rows if r.get("status") == "active"]
    return {
        "mode": mode, "rows": rows, "products": _product_catalog(entity["_id"]), "query": search or "", "selected_scope": selected_scope or "all",
        "actor_role": actor["resolved_role"], "can_toggle": actor["resolved_role"] in CONTROL_ROLES, "can_create_avpl": actor["resolved_role"] in AVPL_ENTRY_ROLES,
        "summary": {"active_entries": len(active), "avpl_entries": sum(1 for r in active if r.get("scope") == "avpl"), "ufc_entries": sum(1 for r in active if r.get("scope") == "ufc"), "opening_value": _money(sum((_decimal(r.get("total_cost")) for r in active), Decimal("0")))},
    }


def get_ufc_opening_stock_overview(actor_user_id, centre_uid_hint=None, *, search=""):
    _ensure_indexes()
    actor = _actor(actor_user_id)
    centre_uid, centre_name = _ufc_identity(actor, centre_uid_hint)
    entity = _active_avpl_entity()
    query = {"scope": "ufc", "centre_uid": centre_uid}
    text = _clean(search, 120)
    if text:
        escaped = re.escape(text)
        query["$or"] = [{"opening_number": {"$regex": escaped, "$options": "i"}}, {"product_name": {"$regex": escaped, "$options": "i"}}, {"product_code": {"$regex": escaped, "$options": "i"}}, {"batch_number": {"$regex": escaped, "$options": "i"}}, {"reference": {"$regex": escaped, "$options": "i"}}]
    mode = get_opening_stock_mode()
    rows = [_serialize_entry(row, actor["resolved_role"], centre_uid, mode_enabled=mode["enabled"]) for row in mongo.db[ENTRY_COLLECTION].find(query).sort("created_at", DESCENDING).limit(300)]
    active = [r for r in rows if r.get("status") == "active"]
    return {
        "mode": mode, "rows": rows, "products": _product_catalog(entity["_id"]), "query": search or "", "centre_uid": centre_uid, "centre_name": centre_name,
        "summary": {"active_entries": len(active), "product_count": len({str(r.get("source_product_id")) for r in active}), "opening_value": _money(sum((_decimal(r.get("total_cost")) for r in active), Decimal("0")))},
    }
