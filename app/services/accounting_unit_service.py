from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


AVPL_ENTITY_CODE = "AVPL"
UNIT_COLLECTION = "accounting_units"
CONVERSION_COLLECTION = "accounting_unit_conversions"
LOCK_COLLECTION = "accounting_master_locks"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_CANCELLED = "cancelled"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending approval",
    STATUS_RETURNED: "Returned for correction",
    STATUS_ACTIVE: "Active",
    STATUS_INACTIVE: "Inactive",
    STATUS_CANCELLED: "Cancelled",
}

VIEW_PERMISSION = "accounting.unit.view"
BOOTSTRAP_PERMISSION = "accounting.unit.bootstrap"
CREATE_PERMISSION = "accounting.unit.create"
EDIT_PERMISSION = "accounting.unit.edit"
SUBMIT_PERMISSION = "accounting.unit.submit"
WITHDRAW_PERMISSION = "accounting.unit.withdraw"
CANCEL_PERMISSION = "accounting.unit.cancel"
APPROVE_PERMISSION = "accounting.unit.approve"
RETURN_PERMISSION = "accounting.unit.return"
DEACTIVATE_PERMISSION = "accounting.unit.deactivate"
REACTIVATE_PERMISSION = "accounting.unit.reactivate"

UNIT_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_\-]{1,19}$")
UQC_PATTERN = re.compile(r"^[A-Z]{3}$")

DIMENSION_LABELS = {
    "count": "Count",
    "packaging": "Packaging",
    "weight": "Weight",
    "volume": "Volume",
    "length": "Length",
    "area": "Area",
    "other": "Other",
}

# Official e-Invoice UQC master snapshot used as protected system units.
# Runtime users may create business-specific units, but every custom unit must
# map to one of these official UQC codes for future invoice/export compatibility.
STANDARD_UNIT_DEFINITIONS = (
    ("BAG", "Bags", "bag", "packaging", False, 0),
    ("BAL", "Bale", "bale", "packaging", False, 0),
    ("BDL", "Bundles", "bundle", "packaging", False, 0),
    ("BKL", "Buckles", "buckle", "packaging", False, 0),
    ("BOU", "Billion of Units", "billion units", "count", True, 3),
    ("BOX", "Box", "box", "packaging", False, 0),
    ("BTL", "Bottles", "bottle", "packaging", False, 0),
    ("BUN", "Bunches", "bunch", "packaging", False, 0),
    ("CAN", "Cans", "can", "packaging", False, 0),
    ("CBM", "Cubic Meters", "m³", "volume", True, 3),
    ("CCM", "Cubic Centimeters", "cm³", "volume", True, 3),
    ("CMS", "Centimeters", "cm", "length", True, 3),
    ("CTN", "Cartons", "carton", "packaging", False, 0),
    ("DOZ", "Dozens", "dozen", "count", True, 3),
    ("DRM", "Drums", "drum", "packaging", False, 0),
    ("GGK", "Great Gross", "great gross", "count", True, 3),
    ("GMS", "Grammes", "g", "weight", True, 3),
    ("GRS", "Gross", "gross", "count", True, 3),
    ("GYD", "Gross Yards", "gross yd", "length", True, 3),
    ("KGS", "Kilograms", "kg", "weight", True, 3),
    ("KLR", "Kilolitre", "kL", "volume", True, 3),
    ("KME", "Kilometre", "km", "length", True, 3),
    ("LTR", "Litres", "L", "volume", True, 3),
    ("MLT", "Millilitre", "mL", "volume", True, 3),
    ("MTR", "Meters", "m", "length", True, 3),
    ("MTS", "Metric Ton", "MT", "weight", True, 3),
    ("NOS", "Numbers", "nos", "count", False, 0),
    ("OTH", "Others", "other", "other", True, 3),
    ("PAC", "Packs", "pack", "packaging", False, 0),
    ("PCS", "Pieces", "pcs", "count", False, 0),
    ("PRS", "Pairs", "pair", "count", False, 0),
    ("QTL", "Quintal", "qtl", "weight", True, 3),
    ("ROL", "Rolls", "roll", "packaging", False, 0),
    ("SET", "Sets", "set", "count", False, 0),
    ("SQF", "Square Feet", "sq ft", "area", True, 3),
    ("SQM", "Square Meters", "m²", "area", True, 3),
    ("SQY", "Square Yards", "sq yd", "area", True, 3),
    ("TBS", "Tablets", "tablet", "count", False, 0),
    ("TGM", "Ten Gross", "ten gross", "count", True, 3),
    ("THD", "Thousands", "thousand", "count", True, 3),
    ("TON", "Tonnes", "tonne", "weight", True, 3),
    ("TUB", "Tubes", "tube", "packaging", False, 0),
    ("UGS", "US Gallons", "US gal", "volume", True, 3),
    ("UNT", "Units", "unit", "count", False, 0),
    ("YDS", "Yards", "yd", "length", True, 3),
)


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in keys]


def _ensure_exact_index(collection, keys, name, **options):
    required_keys = _normalized_keys(keys)
    required_unique = bool(options.get("unique", False))
    required_partial = options.get("partialFilterExpression")
    try:
        index_info = collection.index_information()
    except Exception as exc:
        raise RuntimeError(f"Could not inspect indexes for {collection.name}.") from exc

    for existing_name, metadata in index_info.items():
        if existing_name == "_id_":
            continue
        existing_keys = _normalized_keys(metadata.get("key", []))
        if existing_name != name and existing_keys != required_keys:
            continue
        if (
            existing_keys == required_keys
            and bool(metadata.get("unique", False)) == required_unique
            and metadata.get("partialFilterExpression") == required_partial
        ):
            return existing_name
        raise RuntimeError(
            f"Conflicting index detected on {collection.name}: {existing_name}. "
            "No index was dropped automatically."
        )

    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure as exc:
        raise RuntimeError(
            f"Could not create Accounting index {name} on {collection.name}."
        ) from exc


def ensure_unit_indexes():
    units = mongo.db[UNIT_COLLECTION]
    conversions = mongo.db[CONVERSION_COLLECTION]
    _ensure_exact_index(
        units,
        [("live_unit_key", ASCENDING)],
        name="accounting_unit_live_key_unique",
        unique=True,
        partialFilterExpression={"live_unit_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        units,
        [("accounting_entity_id", ASCENDING), ("system_key", ASCENDING)],
        name="accounting_unit_system_key_unique",
        unique=True,
        partialFilterExpression={"system_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        units,
        [("accounting_entity_id", ASCENDING), ("uqc_code", ASCENDING), ("is_system", ASCENDING)],
        name="accounting_unit_entity_uqc_system_idx",
    )
    _ensure_exact_index(
        units,
        [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("name", ASCENDING)],
        name="accounting_unit_entity_status_name_idx",
    )
    _ensure_exact_index(
        conversions,
        [("live_conversion_key", ASCENDING)],
        name="accounting_unit_conversion_live_key_unique",
        unique=True,
        partialFilterExpression={"live_conversion_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        conversions,
        [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("updated_at", DESCENDING)],
        name="accounting_unit_conversion_entity_status_idx",
    )
    _ensure_exact_index(
        conversions,
        [("from_unit_id", ASCENDING), ("to_unit_id", ASCENDING)],
        name="accounting_unit_conversion_pair_idx",
    )
    lock_collection = mongo.db[LOCK_COLLECTION]
    _ensure_exact_index(
        lock_collection,
        [("lock_key", ASCENDING)],
        name="accounting_master_lock_key_unique",
        unique=True,
    )
    _ensure_exact_index(
        lock_collection,
        [("expires_at", ASCENDING)],
        name="accounting_master_lock_expiry_ttl",
        expireAfterSeconds=0,
    )


def _clean_text(value, label, maximum=160, required=True):
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _clean_multiline(value, label, maximum=1000, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _sanitize_unit_code(value):
    code = re.sub(r"[^A-Z0-9_\-]", "_", str(value or "").strip().upper())
    code = re.sub(r"_+", "_", code).strip("_-")
    if not UNIT_CODE_PATTERN.fullmatch(code):
        raise ValueError(
            "Unit code must start with a letter and use 2 to 20 uppercase letters, numbers, underscores or hyphens."
        )
    return code


def _decimal(value, label, minimum=None, maximum=None):
    try:
        result = Decimal(str(value or "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc
    if minimum is not None and result < Decimal(str(minimum)):
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and result > Decimal(str(maximum)):
        raise ValueError(f"{label} cannot exceed {maximum}.")
    return result


def _decimal_string(value):
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    if not isinstance(value, Decimal):
        value = Decimal(str(value or 0))
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _get_actor(actor_user_id, allowed_roles=None):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one(
        {"_id": actor_id},
        {"name": 1, "full_name": 1, "username": 1, "phone": 1, "role": 1, "active": 1, "is_active": 1, "status": 1},
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")
    if actor.get("active", True) is False or actor.get("is_active", True) is False or actor.get("status") == "inactive":
        raise PermissionError("Inactive users cannot perform Accounting actions.")
    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError("You are not authorized to perform this unit-master action.")
    actor["resolved_role"] = role
    actor["resolved_name"] = actor.get("name") or actor.get("full_name") or actor.get("username") or actor.get("phone") or role.replace("_", " ").title()
    return actor


def _assert_active_avpl_entity(entity_id=None):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    if entity_id:
        object_id = _to_object_id(entity_id)
        if not object_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = object_id
    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is not available.")
    return entity


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")
    if actor.get("resolved_role") != "super_admin":
        entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")
    if not has_accounting_permission(access, permission):
        raise PermissionError("Your Accounting access mapping does not allow this unit-master action.")
    return access


@contextmanager
def _master_lock(lock_key, seconds=30):
    token = str(uuid4())
    timestamp = now_utc()
    expires_at = timestamp + timedelta(seconds=seconds)
    try:
        mongo.db[LOCK_COLLECTION].insert_one(
            {"lock_key": lock_key, "lock_token": token, "created_at": timestamp, "expires_at": expires_at}
        )
    except DuplicateKeyError:
        current = mongo.db[LOCK_COLLECTION].find_one({"lock_key": lock_key})
        if current and current.get("expires_at") and current["expires_at"] <= timestamp:
            result = mongo.db[LOCK_COLLECTION].update_one(
                {"_id": current["_id"], "expires_at": current["expires_at"]},
                {"$set": {"lock_token": token, "created_at": timestamp, "expires_at": expires_at}},
            )
            if result.modified_count != 1:
                raise RuntimeError("Another unit-master update is in progress. Please retry shortly.")
        else:
            raise RuntimeError("Another unit-master update is in progress. Please retry shortly.")
    try:
        yield
    finally:
        mongo.db[LOCK_COLLECTION].delete_one({"lock_key": lock_key, "lock_token": token})


def _change_event(action, actor, previous_status=None, new_status=None, remarks="", changed_fields=None):
    return {
        "event_id": str(uuid4()),
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "remarks": remarks or "",
        "changed_fields": sorted(set(changed_fields or [])),
        "created_at": now_utc(),
    }


def _record_audit(document, actor, action, resource_type, previous_status=None, remarks="", changed_fields=None):
    timestamp = now_utc()
    collection_name = UNIT_COLLECTION if resource_type == "unit" else CONVERSION_COLLECTION
    resource_code = document.get("unit_code") if resource_type == "unit" else document.get("conversion_code")
    audit = {
        "event_id": str(uuid4()),
        "accounting_entity_id": document.get("accounting_entity_id"),
        "entity_code": document.get("entity_code") or AVPL_ENTITY_CODE,
        "module": "unit_master",
        "resource_type": resource_type,
        "resource_id": document.get("_id"),
        "resource_id_str": str(document.get("_id") or ""),
        "resource_code": resource_code or "",
        "action": action,
        "previous_status": previous_status,
        "new_status": document.get("status"),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "remarks": remarks or "",
        "changed_fields": sorted(set(changed_fields or [])),
        "created_at": timestamp,
    }
    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
        mongo.db[collection_name].update_one(
            {"_id": document["_id"]},
            {"$set": {"audit_sync_required": False, "last_audit_event_id": audit["event_id"], "last_audited_at": timestamp}, "$unset": {"audit_sync_error": ""}},
        )
    except Exception as exc:
        mongo.db[collection_name].update_one(
            {"_id": document["_id"]},
            {"$set": {"audit_sync_required": True, "audit_sync_error": str(exc)[:500], "audit_recovery_required_at": timestamp}},
        )


def _serialize_unit(document):
    return {
        "id": str(document.get("_id") or ""),
        "unit_code": document.get("unit_code") or "",
        "name": document.get("name") or "",
        "symbol": document.get("symbol") or "",
        "uqc_code": document.get("uqc_code") or "",
        "dimension": document.get("dimension") or "other",
        "dimension_display": DIMENSION_LABELS.get(document.get("dimension"), str(document.get("dimension") or "Other").title()),
        "allows_fractional": document.get("allows_fractional") is True,
        "decimal_places": int(document.get("decimal_places") or 0),
        "description": document.get("description") or "",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(document.get("status"), document.get("status") or "Draft"),
        "is_system": document.get("is_system") is True,
        "is_protected": document.get("is_protected") is True,
        "is_active": document.get("is_active") is True,
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": document.get("created_by_name") or "",
        "return_reason": document.get("return_reason") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "deactivation_reason": document.get("deactivation_reason") or "",
        "audit_sync_required": document.get("audit_sync_required") is True,
        "change_history": document.get("change_history") or [],
        "updated_at": document.get("updated_at"),
    }


def _serialize_conversion(document, unit_map=None):
    unit_map = unit_map or {}
    from_id = str(document.get("from_unit_id") or "")
    to_id = str(document.get("to_unit_id") or "")
    factor = document.get("factor")
    return {
        "id": str(document.get("_id") or ""),
        "conversion_code": document.get("conversion_code") or "",
        "from_unit_id": from_id,
        "to_unit_id": to_id,
        "from_unit": unit_map.get(from_id, {}).get("name") or document.get("from_unit_name") or "",
        "from_unit_code": unit_map.get(from_id, {}).get("unit_code") or document.get("from_unit_code") or "",
        "to_unit": unit_map.get(to_id, {}).get("name") or document.get("to_unit_name") or "",
        "to_unit_code": unit_map.get(to_id, {}).get("unit_code") or document.get("to_unit_code") or "",
        "factor": _decimal_string(factor),
        "is_bidirectional": document.get("is_bidirectional") is True,
        "description": document.get("description") or "",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(document.get("status"), document.get("status") or "Draft"),
        "is_active": document.get("is_active") is True,
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": document.get("created_by_name") or "",
        "return_reason": document.get("return_reason") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "audit_sync_required": document.get("audit_sync_required") is True,
        "change_history": document.get("change_history") or [],
        "updated_at": document.get("updated_at"),
    }


def _canonical_standard_unit(entity, definition, actor, existing=None):
    code, name, symbol, dimension, allows_fractional, decimal_places = definition
    timestamp = now_utc()
    return {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "system_key": f"uqc_{code.lower()}",
        "unit_code": code,
        "name": name,
        "symbol": symbol,
        "uqc_code": code,
        "dimension": dimension,
        "allows_fractional": bool(allows_fractional),
        "decimal_places": int(decimal_places),
        "description": "Protected GST/e-Invoice Unit Quantity Code master.",
        "live_unit_key": f"{entity['_id']}:{code}",
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_system": True,
        "is_protected": True,
        "name_locked": True,
        "deletion_locked": True,
        "is_deleted": False,
        "version": int((existing or {}).get("version") or 0) + (1 if existing else 1),
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "audit_sync_required": False,
    }


def seed_standard_units(accounting_entity_id, actor_user_id):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)

    # Protected UQC foundation setup is an explicit platform authority
    # for Super Admin and AVPL Admin. Do not depend on an older stored
    # accounting_user_access permission mapping for this bootstrap action.
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )

    if not access.get("enabled"):
        raise PermissionError(
            access.get("message") or "Accounting access is disabled."
        )

    # AVPL Admin must still be scoped to this Accounting entity.
    if actor.get("resolved_role") == "avpl_admin":
        entity_ids = {
            str(value)
            for value in access.get("entity_ids") or []
        }
        if str(entity["_id"]) not in entity_ids:
            raise PermissionError(
                "You do not have access to this Accounting entity."
            )

    ensure_unit_indexes()

    created = repaired = unchanged = 0
    with _master_lock(f"unit-foundation:{entity['_id']}"):
        for definition in STANDARD_UNIT_DEFINITIONS:
            code = definition[0]
            existing = mongo.db[UNIT_COLLECTION].find_one(
                {
                    "accounting_entity_id": entity["_id"],
                    "$or": [
                        {"system_key": f"uqc_{code.lower()}"},
                        {"unit_code": code, "is_system": True},
                        {"uqc_code": code, "is_protected": True},
                    ],
                }
            )
            canonical = _canonical_standard_unit(entity, definition, actor, existing=existing)
            if not existing:
                canonical.update({
                    "created_by": actor["_id"],
                    "created_by_str": str(actor["_id"]),
                    "created_by_name": actor.get("resolved_name") or "",
                    "created_at": now_utc(),
                    "change_history": [_change_event("seed_standard_unit", actor, new_status=STATUS_ACTIVE)],
                })
                mongo.db[UNIT_COLLECTION].insert_one(canonical)
                created += 1
                continue
            fields = [
                "system_key", "unit_code", "name", "symbol", "uqc_code", "dimension", "allows_fractional",
                "decimal_places", "live_unit_key", "status", "is_active", "is_system",
                "is_protected", "name_locked", "deletion_locked", "is_deleted",
            ]
            changed = [field for field in fields if existing.get(field) != canonical.get(field)]
            if changed:
                mongo.db[UNIT_COLLECTION].update_one(
                    {"_id": existing["_id"]},
                    {"$set": canonical, "$push": {"change_history": _change_event("repair_standard_unit", actor, previous_status=existing.get("status"), new_status=STATUS_ACTIVE, changed_fields=changed)}},
                )
                repaired += 1
            else:
                unchanged += 1

    summary = {"created": created, "repaired": repaired, "unchanged": unchanged}
    try:
        mongo.db.accounting_audit_logs.insert_one({
            "event_id": str(uuid4()), "accounting_entity_id": entity["_id"],
            "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
            "module": "unit_master", "resource_type": "unit_foundation",
            "resource_id": entity["_id"], "resource_id_str": str(entity["_id"]),
            "resource_code": "UQC_FOUNDATION", "action": "synchronize_standard_units",
            "actor_user_id": actor["_id"], "actor_user_id_str": str(actor["_id"]),
            "actor_name": actor.get("resolved_name") or "", "actor_role": actor.get("resolved_role") or "",
            "summary": summary, "created_at": now_utc(),
        })
    except Exception:
        mongo.db[UNIT_COLLECTION].update_many(
            {"accounting_entity_id": entity["_id"], "is_system": True},
            {"$set": {"audit_sync_required": True, "audit_recovery_required_at": now_utc()}},
        )
    return {
        **summary,
        "message": f"Standard UQC units synchronized: {created} created, {repaired} repaired, {unchanged} already correct.",
    }


def _unit_payload(form):
    unit_code = _sanitize_unit_code(form.get("unit_code"))
    name = _clean_text(form.get("name"), "Unit name", maximum=100)
    symbol = _clean_text(form.get("symbol"), "Unit symbol", maximum=30)
    uqc_code = str(form.get("uqc_code") or "").strip().upper()
    if not UQC_PATTERN.fullmatch(uqc_code):
        raise ValueError("Select a valid three-character GST UQC code.")
    dimension = str(form.get("dimension") or "").strip().lower()
    if dimension not in DIMENSION_LABELS:
        raise ValueError("Select a valid unit dimension.")
    try:
        decimal_places = int(form.get("decimal_places") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Decimal places must be a whole number.") from exc
    if decimal_places < 0 or decimal_places > 6:
        raise ValueError("Decimal places must be between 0 and 6.")
    allows_fractional = str(form.get("allows_fractional") or "").lower() in {"1", "true", "yes", "on"}
    if not allows_fractional:
        decimal_places = 0
    return {
        "unit_code": unit_code,
        "name": name,
        "symbol": symbol,
        "uqc_code": uqc_code,
        "dimension": dimension,
        "allows_fractional": allows_fractional,
        "decimal_places": decimal_places,
        "description": _clean_multiline(form.get("description"), "Description", maximum=600),
    }


def _validate_uqc_reference(entity_id, uqc_code, dimension):
    row = mongo.db[UNIT_COLLECTION].find_one({
        "accounting_entity_id": entity_id,
        "unit_code": uqc_code,
        "is_system": True,
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": False,
    })
    if not row:
        raise ValueError("The selected GST UQC unit is not active. Synchronize standard units first.")
    # The GST UQC is the outward reporting code, while the custom unit
    # dimension controls inventory conversion. A business package such as a
    # 25 kg bag may therefore report as BAG while converting within weight.
    return row


def create_custom_unit(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_unit_indexes()
    payload = _unit_payload(form)
    _validate_uqc_reference(entity["_id"], payload["uqc_code"], payload["dimension"])
    live_key = f"{entity['_id']}:{payload['unit_code']}"
    timestamp = now_utc()
    document = {
        "accounting_entity_id": entity["_id"], "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE, **payload,
        "live_unit_key": live_key, "status": STATUS_DRAFT, "is_active": False,
        "is_system": False, "is_protected": False, "name_locked": False,
        "deletion_locked": False, "is_deleted": False, "version": 1,
        "created_by": actor["_id"], "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "", "created_at": timestamp,
        "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "", "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [_change_event("create_custom_unit", actor, new_status=STATUS_DRAFT, changed_fields=list(payload))],
    }
    try:
        result = mongo.db[UNIT_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError("A live unit with this code already exists for AVPL.") from exc
    document["_id"] = result.inserted_id
    _record_audit(document, actor, "create_custom_unit", "unit", changed_fields=list(payload))
    return {"unit": _serialize_unit(document), "message": "Custom unit draft created."}


def _get_unit(unit_id):
    object_id = _to_object_id(unit_id)
    if not object_id:
        raise ValueError("Invalid unit master.")
    row = mongo.db[UNIT_COLLECTION].find_one({"_id": object_id, "is_deleted": {"$ne": True}})
    if not row:
        raise ValueError("Unit master was not found.")
    return row


def _assert_custom_unit(row):
    if row.get("is_system") or row.get("is_protected"):
        raise PermissionError("Protected standard UQC units cannot be edited through custom-unit actions.")


def update_custom_unit(unit_id, actor_user_id, expected_version, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_unit(unit_id)
    _assert_custom_unit(current)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned custom units can be edited.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can edit this unit.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid unit version.") from exc
    payload = _unit_payload(form)
    _validate_uqc_reference(entity["_id"], payload["uqc_code"], payload["dimension"])
    new_live_key = f"{entity['_id']}:{payload['unit_code']}"
    changed = [key for key, value in payload.items() if current.get(key) != value]
    if current.get("live_unit_key") != new_live_key:
        changed.append("unit_code")
    timestamp = now_utc()
    try:
        result = mongo.db[UNIT_COLLECTION].update_one(
            {"_id": current["_id"], "version": expected_version, "status": {"$in": list(EDITABLE_STATUSES)}},
            {"$set": {**payload, "live_unit_key": new_live_key, "version": expected_version + 1, "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]), "updated_by_name": actor.get("resolved_name") or "", "updated_at": timestamp}, "$push": {"change_history": _change_event("update_custom_unit", actor, previous_status=current.get("status"), new_status=current.get("status"), changed_fields=changed)}},
        )
    except DuplicateKeyError as exc:
        raise ValueError("A live unit with this code already exists for AVPL.") from exc
    if result.matched_count != 1:
        raise RuntimeError("This unit changed. Refresh and try again.")
    updated = _get_unit(unit_id)
    _record_audit(updated, actor, "update_custom_unit", "unit", previous_status=current.get("status"), changed_fields=changed)
    return {"unit": _serialize_unit(updated), "message": "Custom unit draft updated."}


def _transition_unit(unit_id, actor_user_id, expected_version, action, permission, allowed_roles, source_statuses, target_status, reason="", note=""):
    actor = _get_actor(actor_user_id, allowed_roles=allowed_roles)
    current = _get_unit(unit_id)
    _assert_custom_unit(current)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)
    if current.get("status") not in set(source_statuses):
        raise ValueError(f"This unit cannot be moved from {STATUS_LABELS.get(current.get('status'), current.get('status'))}.")
    if action in {"submit_custom_unit", "withdraw_custom_unit", "cancel_custom_unit"} and str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can perform this action.")
    if action in {"approve_custom_unit", "return_custom_unit"} and str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot approve or return their own unit master.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid unit version.") from exc
    reason = _clean_multiline(reason, "Reason", maximum=1000, required=action in {"withdraw_custom_unit", "cancel_custom_unit", "return_custom_unit", "deactivate_custom_unit", "reactivate_custom_unit"})
    note = _clean_multiline(note, "Note", maximum=1000)
    updates = {
        "status": target_status,
        "is_active": target_status == STATUS_ACTIVE,
        "version": expected_version + 1,
        "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "", "updated_at": now_utc(),
    }
    if target_status == STATUS_CANCELLED:
        updates.pop("is_active", None)
    if action == "submit_custom_unit":
        _validate_uqc_reference(entity["_id"], current.get("uqc_code"), current.get("dimension"))
        updates.update({"submitted_by": actor["_id"], "submitted_by_name": actor.get("resolved_name") or "", "submitted_at": now_utc(), "submission_note": note, "return_reason": ""})
    elif action == "withdraw_custom_unit":
        updates.update({"withdraw_reason": reason, "withdrawn_at": now_utc()})
    elif action == "cancel_custom_unit":
        updates.update({"cancel_reason": reason, "cancelled_at": now_utc()})
    elif action == "approve_custom_unit":
        updates.update({"approved_by": actor["_id"], "approved_by_name": actor.get("resolved_name") or "", "approved_at": now_utc(), "approval_note": note, "return_reason": ""})
    elif action == "return_custom_unit":
        updates.update({"return_reason": reason, "returned_at": now_utc()})
    elif action == "deactivate_custom_unit":
        if mongo.db[CONVERSION_COLLECTION].count_documents({"accounting_entity_id": entity["_id"], "status": STATUS_ACTIVE, "is_active": True, "$or": [{"from_unit_id": current["_id"]}, {"to_unit_id": current["_id"]}]}) > 0:
            raise ValueError("Deactivate active unit conversions using this unit first.")
        if mongo.db.product_accounting_mappings.count_documents({"accounting_entity_id": entity["_id"], "status": "active", "$or": [{"base_unit_id": current["_id"]}, {"alternate_units.unit_id": current["_id"]}]}) > 0:
            raise ValueError("This unit is used by active product Accounting mappings and cannot be deactivated.")
        updates.update({"deactivation_reason": reason, "deactivated_at": now_utc()})
    elif action == "reactivate_custom_unit":
        _validate_uqc_reference(entity["_id"], current.get("uqc_code"), current.get("dimension"))
        updates.update({"reactivation_reason": reason, "reactivated_at": now_utc()})
    update_doc = {"$set": updates, "$push": {"change_history": _change_event(action, actor, previous_status=current.get("status"), new_status=target_status, remarks=reason or note)}}
    if target_status == STATUS_CANCELLED:
        update_doc["$unset"] = {"live_unit_key": ""}
    result = mongo.db[UNIT_COLLECTION].update_one({"_id": current["_id"], "version": expected_version, "status": {"$in": list(source_statuses)}}, update_doc)
    if result.matched_count != 1:
        raise RuntimeError("This unit changed. Refresh and try again.")
    updated = _get_unit(unit_id)
    _record_audit(updated, actor, action, "unit", previous_status=current.get("status"), remarks=reason or note)
    return {"unit": _serialize_unit(updated), "message": f"Custom unit {STATUS_LABELS[target_status].lower()}."}


def submit_custom_unit(unit_id, actor_user_id, expected_version, submission_note=""):
    return _transition_unit(unit_id, actor_user_id, expected_version, "submit_custom_unit", SUBMIT_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_PENDING_APPROVAL, note=submission_note)


def withdraw_custom_unit(unit_id, actor_user_id, expected_version, reason):
    return _transition_unit(unit_id, actor_user_id, expected_version, "withdraw_custom_unit", WITHDRAW_PERMISSION, {"accounts"}, {STATUS_PENDING_APPROVAL}, STATUS_DRAFT, reason=reason)


def cancel_custom_unit(unit_id, actor_user_id, expected_version, reason):
    return _transition_unit(unit_id, actor_user_id, expected_version, "cancel_custom_unit", CANCEL_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_CANCELLED, reason=reason)


def approve_custom_unit(unit_id, actor_user_id, expected_version, approval_note=""):
    return _transition_unit(unit_id, actor_user_id, expected_version, "approve_custom_unit", APPROVE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_ACTIVE, note=approval_note)


def return_custom_unit(unit_id, actor_user_id, expected_version, return_reason):
    return _transition_unit(unit_id, actor_user_id, expected_version, "return_custom_unit", RETURN_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_RETURNED, reason=return_reason)


def deactivate_custom_unit(unit_id, actor_user_id, expected_version, reason):
    return _transition_unit(unit_id, actor_user_id, expected_version, "deactivate_custom_unit", DEACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_ACTIVE}, STATUS_INACTIVE, reason=reason)


def reactivate_custom_unit(unit_id, actor_user_id, expected_version, reason):
    return _transition_unit(unit_id, actor_user_id, expected_version, "reactivate_custom_unit", REACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_INACTIVE}, STATUS_ACTIVE, reason=reason)


def _conversion_payload(entity_id, form):
    from_unit = _get_unit(form.get("from_unit_id"))
    to_unit = _get_unit(form.get("to_unit_id"))
    if from_unit.get("accounting_entity_id") != entity_id or to_unit.get("accounting_entity_id") != entity_id:
        raise PermissionError("Both units must belong to the AVPL Accounting entity.")
    if from_unit["_id"] == to_unit["_id"]:
        raise ValueError("Source and target units must be different.")
    if not (
        from_unit.get("status") == STATUS_ACTIVE
        and from_unit.get("is_active") is True
        and to_unit.get("status") == STATUS_ACTIVE
        and to_unit.get("is_active") is True
    ):
        raise ValueError("Only active units can be used in a conversion draft.")
    if from_unit.get("dimension") != to_unit.get("dimension"):
        raise ValueError("Unit conversions are allowed only within the same dimension.")
    factor = _decimal(form.get("factor"), "Conversion factor", minimum="0.000000001", maximum="1000000000")
    pair = sorted([str(from_unit["_id"]), str(to_unit["_id"])])
    return {
        "from_unit_id": from_unit["_id"], "from_unit_code": from_unit.get("unit_code") or "", "from_unit_name": from_unit.get("name") or "",
        "to_unit_id": to_unit["_id"], "to_unit_code": to_unit.get("unit_code") or "", "to_unit_name": to_unit.get("name") or "",
        "factor": Decimal128(factor),
        "is_bidirectional": str(form.get("is_bidirectional") or "").lower() in {"1", "true", "yes", "on"},
        "dimension": from_unit.get("dimension") or "other",
        "description": _clean_multiline(form.get("description"), "Description", maximum=600),
        "conversion_code": f"{from_unit.get('unit_code')}_TO_{to_unit.get('unit_code')}",
        "live_conversion_key": f"{entity_id}:{pair[0]}:{pair[1]}",
    }


def create_unit_conversion(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_unit_indexes()
    payload = _conversion_payload(entity["_id"], form)
    timestamp = now_utc()
    document = {
        "accounting_entity_id": entity["_id"], "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE, **payload,
        "status": STATUS_DRAFT, "is_active": False, "is_deleted": False, "version": 1,
        "created_by": actor["_id"], "created_by_str": str(actor["_id"]), "created_by_name": actor.get("resolved_name") or "", "created_at": timestamp,
        "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]), "updated_by_name": actor.get("resolved_name") or "", "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [_change_event("create_unit_conversion", actor, new_status=STATUS_DRAFT, changed_fields=list(payload))],
    }
    try:
        result = mongo.db[CONVERSION_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError("A live conversion for this unit pair already exists.") from exc
    document["_id"] = result.inserted_id
    _record_audit(document, actor, "create_unit_conversion", "unit_conversion", changed_fields=list(payload))
    return {"conversion": _serialize_conversion(document), "message": "Alternate-unit conversion draft created."}


def _get_conversion(conversion_id):
    object_id = _to_object_id(conversion_id)
    if not object_id:
        raise ValueError("Invalid unit conversion.")
    row = mongo.db[CONVERSION_COLLECTION].find_one({"_id": object_id, "is_deleted": {"$ne": True}})
    if not row:
        raise ValueError("Unit conversion was not found.")
    return row


def update_unit_conversion(conversion_id, actor_user_id, expected_version, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_conversion(conversion_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned conversions can be edited.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can edit this conversion.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid conversion version.") from exc
    payload = _conversion_payload(entity["_id"], form)
    changed = [key for key, value in payload.items() if current.get(key) != value]
    try:
        result = mongo.db[CONVERSION_COLLECTION].update_one(
            {"_id": current["_id"], "version": expected_version, "status": {"$in": list(EDITABLE_STATUSES)}},
            {"$set": {**payload, "version": expected_version + 1, "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]), "updated_by_name": actor.get("resolved_name") or "", "updated_at": now_utc()}, "$push": {"change_history": _change_event("update_unit_conversion", actor, previous_status=current.get("status"), new_status=current.get("status"), changed_fields=changed)}},
        )
    except DuplicateKeyError as exc:
        raise ValueError("A live conversion for this unit pair already exists.") from exc
    if result.matched_count != 1:
        raise RuntimeError("This conversion changed. Refresh and try again.")
    updated = _get_conversion(conversion_id)
    _record_audit(updated, actor, "update_unit_conversion", "unit_conversion", previous_status=current.get("status"), changed_fields=changed)
    return {"conversion": _serialize_conversion(updated), "message": "Unit conversion draft updated."}


def _transition_conversion(conversion_id, actor_user_id, expected_version, action, permission, allowed_roles, source_statuses, target_status, reason="", note=""):
    actor = _get_actor(actor_user_id, allowed_roles=allowed_roles)
    current = _get_conversion(conversion_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)
    if current.get("status") not in set(source_statuses):
        raise ValueError("This unit conversion is not in a valid state for the requested action.")
    if action in {"submit_unit_conversion", "withdraw_unit_conversion", "cancel_unit_conversion"} and str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can perform this action.")
    if action in {"approve_unit_conversion", "return_unit_conversion"} and str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot approve or return their own conversion.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid conversion version.") from exc
    reason = _clean_multiline(reason, "Reason", maximum=1000, required=action in {"withdraw_unit_conversion", "cancel_unit_conversion", "return_unit_conversion", "deactivate_unit_conversion", "reactivate_unit_conversion"})
    note = _clean_multiline(note, "Note", maximum=1000)
    updates = {
        "status": target_status, "is_active": target_status == STATUS_ACTIVE,
        "version": expected_version + 1, "updated_by": actor["_id"], "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "", "updated_at": now_utc(),
    }
    if action == "approve_unit_conversion":
        from_unit = _get_unit(current.get("from_unit_id")); to_unit = _get_unit(current.get("to_unit_id"))
        if not (from_unit.get("status") == STATUS_ACTIVE and to_unit.get("status") == STATUS_ACTIVE):
            raise ValueError("Both units must be active before approving their conversion.")
        updates.update({"approved_by": actor["_id"], "approved_by_name": actor.get("resolved_name") or "", "approved_at": now_utc(), "approval_note": note, "return_reason": ""})
    elif action == "submit_unit_conversion":
        updates.update({"submitted_by": actor["_id"], "submitted_by_name": actor.get("resolved_name") or "", "submitted_at": now_utc(), "submission_note": note, "return_reason": ""})
    elif action == "return_unit_conversion":
        updates.update({"return_reason": reason, "returned_at": now_utc()})
    elif action == "withdraw_unit_conversion":
        updates.update({"withdraw_reason": reason, "withdrawn_at": now_utc()})
    elif action == "cancel_unit_conversion":
        updates.update({"cancel_reason": reason, "cancelled_at": now_utc()})
    elif action == "deactivate_unit_conversion":
        updates.update({"deactivation_reason": reason, "deactivated_at": now_utc()})
    elif action == "reactivate_unit_conversion":
        from_unit = _get_unit(current.get("from_unit_id")); to_unit = _get_unit(current.get("to_unit_id"))
        if not (from_unit.get("status") == STATUS_ACTIVE and to_unit.get("status") == STATUS_ACTIVE):
            raise ValueError("Both units must be active before reactivating their conversion.")
        updates.update({"reactivation_reason": reason, "reactivated_at": now_utc()})
    update_doc = {"$set": updates, "$push": {"change_history": _change_event(action, actor, previous_status=current.get("status"), new_status=target_status, remarks=reason or note)}}
    if target_status == STATUS_CANCELLED:
        update_doc["$unset"] = {"live_conversion_key": ""}
    result = mongo.db[CONVERSION_COLLECTION].update_one({"_id": current["_id"], "version": expected_version, "status": {"$in": list(source_statuses)}}, update_doc)
    if result.matched_count != 1:
        raise RuntimeError("This conversion changed. Refresh and try again.")
    updated = _get_conversion(conversion_id)
    _record_audit(updated, actor, action, "unit_conversion", previous_status=current.get("status"), remarks=reason or note)
    return {"conversion": _serialize_conversion(updated), "message": f"Unit conversion {STATUS_LABELS[target_status].lower()}."}


def submit_unit_conversion(conversion_id, actor_user_id, expected_version, submission_note=""):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "submit_unit_conversion", SUBMIT_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_PENDING_APPROVAL, note=submission_note)


def withdraw_unit_conversion(conversion_id, actor_user_id, expected_version, reason):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "withdraw_unit_conversion", WITHDRAW_PERMISSION, {"accounts"}, {STATUS_PENDING_APPROVAL}, STATUS_DRAFT, reason=reason)


def cancel_unit_conversion(conversion_id, actor_user_id, expected_version, reason):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "cancel_unit_conversion", CANCEL_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_CANCELLED, reason=reason)


def approve_unit_conversion(conversion_id, actor_user_id, expected_version, approval_note=""):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "approve_unit_conversion", APPROVE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_ACTIVE, note=approval_note)


def return_unit_conversion(conversion_id, actor_user_id, expected_version, return_reason):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "return_unit_conversion", RETURN_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_RETURNED, reason=return_reason)


def deactivate_unit_conversion(conversion_id, actor_user_id, expected_version, reason):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "deactivate_unit_conversion", DEACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_ACTIVE}, STATUS_INACTIVE, reason=reason)


def reactivate_unit_conversion(conversion_id, actor_user_id, expected_version, reason):
    return _transition_conversion(conversion_id, actor_user_id, expected_version, "reactivate_unit_conversion", REACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_INACTIVE}, STATUS_ACTIVE, reason=reason)


def get_unit_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    units = []
    if entity_id:
        rows = mongo.db[UNIT_COLLECTION].find({"accounting_entity_id": entity_id, "status": STATUS_ACTIVE, "is_active": True, "is_deleted": False}).sort([("is_system", DESCENDING), ("name", ASCENDING)])
        units = [_serialize_unit(row) for row in rows]
    return {
        "dimension_labels": dict(DIMENSION_LABELS),
        "status_labels": dict(STATUS_LABELS),
        "active_units": units,
        "standard_uqc_units": [row for row in units if row.get("is_system")],
    }


def get_unit_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_unit_indexes()
    unit_rows = list(mongo.db[UNIT_COLLECTION].find({"accounting_entity_id": entity["_id"], "is_deleted": {"$ne": True}}).sort([("is_system", DESCENDING), ("name", ASCENDING)]))
    serialized_units = [_serialize_unit(row) for row in unit_rows]
    unit_map = {row["id"]: row for row in serialized_units}
    conversion_rows = list(mongo.db[CONVERSION_COLLECTION].find({"accounting_entity_id": entity["_id"], "is_deleted": {"$ne": True}}).sort([("status", ASCENDING), ("conversion_code", ASCENDING)]))
    serialized_conversions = [_serialize_conversion(row, unit_map) for row in conversion_rows]
    definition_map = {definition[0]: definition for definition in STANDARD_UNIT_DEFINITIONS}
    system_by_key = {row.get("system_key"): row for row in unit_rows if row.get("system_key")}
    missing = [code for code in definition_map if f"uqc_{code.lower()}" not in system_by_key]
    drifted = []
    for code, definition in definition_map.items():
        row = system_by_key.get(f"uqc_{code.lower()}")
        if not row:
            continue
        _, name, symbol, dimension, allows_fractional, decimal_places = definition
        expected = {"name": name, "symbol": symbol, "uqc_code": code, "dimension": dimension, "allows_fractional": allows_fractional, "decimal_places": decimal_places, "status": STATUS_ACTIVE, "is_active": True, "is_system": True, "is_protected": True, "is_deleted": False}
        changed = [field for field, value in expected.items() if row.get(field) != value]
        if changed:
            drifted.append({"unit_code": code, "changed_fields": changed})
    unit_counts = {status: 0 for status in STATUS_LABELS}
    conversion_counts = {status: 0 for status in STATUS_LABELS}
    for row in unit_rows:
        unit_counts[row.get("status") or STATUS_DRAFT] = unit_counts.get(row.get("status") or STATUS_DRAFT, 0) + 1
    for row in conversion_rows:
        conversion_counts[row.get("status") or STATUS_DRAFT] = conversion_counts.get(row.get("status") or STATUS_DRAFT, 0) + 1
    return {
        "entity_id": str(entity["_id"]), "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "foundation": {
            "required_count": len(STANDARD_UNIT_DEFINITIONS),
            "present_count": sum(
                1
                for code in definition_map
                if f"uqc_{code.lower()}" in system_by_key
            ),
            "missing": missing,
            "drifted": drifted,
            "is_complete": not missing and not drifted,
        },
        "units": serialized_units,
        "standard_units": [row for row in serialized_units if row["is_system"]],
        "custom_units": [row for row in serialized_units if not row["is_system"]],
        "custom_working": [row for row in serialized_units if not row["is_system"] and row["status"] in EDITABLE_STATUSES],
        "custom_pending": [row for row in serialized_units if not row["is_system"] and row["status"] == STATUS_PENDING_APPROVAL],
        "custom_active": [row for row in serialized_units if not row["is_system"] and row["status"] == STATUS_ACTIVE],
        "custom_inactive": [row for row in serialized_units if not row["is_system"] and row["status"] == STATUS_INACTIVE],
        "unit_counts": unit_counts,
        "conversions": serialized_conversions,
        "conversion_working": [row for row in serialized_conversions if row["status"] in EDITABLE_STATUSES],
        "conversion_pending": [row for row in serialized_conversions if row["status"] == STATUS_PENDING_APPROVAL],
        "conversion_active": [row for row in serialized_conversions if row["status"] == STATUS_ACTIVE],
        "conversion_inactive": [row for row in serialized_conversions if row["status"] == STATUS_INACTIVE],
        "conversion_counts": conversion_counts,
        "audit_recovery_count": sum(1 for row in unit_rows + conversion_rows if row.get("audit_sync_required") is True),
        "options": get_unit_option_catalog(entity["_id"]),
    }


def get_active_unit_for_mapping(accounting_entity_id, unit_id):
    entity_id = _to_object_id(accounting_entity_id)
    object_id = _to_object_id(unit_id)
    if not entity_id or not object_id:
        raise ValueError("Invalid Accounting entity or unit.")
    row = mongo.db[UNIT_COLLECTION].find_one({"_id": object_id, "accounting_entity_id": entity_id, "status": STATUS_ACTIVE, "is_active": True, "is_deleted": False})
    if not row:
        raise ValueError("The selected unit is not active for product Accounting mapping.")
    return row


def resolve_unit_conversion(accounting_entity_id, from_unit_id, to_unit_id, quantity):
    entity_id = _to_object_id(accounting_entity_id)
    from_id = _to_object_id(from_unit_id)
    to_id = _to_object_id(to_unit_id)
    if not entity_id or not from_id or not to_id:
        raise ValueError("Invalid unit conversion request.")
    amount = _decimal(quantity, "Quantity", minimum="0")
    row = mongo.db[CONVERSION_COLLECTION].find_one({"accounting_entity_id": entity_id, "from_unit_id": from_id, "to_unit_id": to_id, "status": STATUS_ACTIVE, "is_active": True, "is_deleted": False})
    if row:
        return amount * row["factor"].to_decimal()
    reverse = mongo.db[CONVERSION_COLLECTION].find_one({"accounting_entity_id": entity_id, "from_unit_id": to_id, "to_unit_id": from_id, "status": STATUS_ACTIVE, "is_active": True, "is_bidirectional": True, "is_deleted": False})
    if reverse:
        factor = reverse["factor"].to_decimal()
        if factor == 0:
            raise RuntimeError("Stored unit conversion factor is invalid.")
        return amount / factor
    raise ValueError("No approved direct unit conversion exists for the selected pair.")
