from datetime import date
from decimal import Decimal, InvalidOperation

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_gst_tax_service import get_effective_gst_tax_rate
from app.services.accounting_hsn_service import get_active_hsn_master_for_mapping
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.services.accounting_unit_service import get_active_unit_for_mapping
from app.utils.helpers import now_utc


MAPPING_COLLECTION = "accounting_product_mappings"
SOURCE_PRODUCT_COLLECTION = "products"
AVPL_ENTITY_CODE = "AVPL"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_CANCELLED = "cancelled"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending Approval",
    STATUS_RETURNED: "Returned for Correction",
    STATUS_ACTIVE: "Active",
    STATUS_INACTIVE: "Inactive",
    STATUS_CANCELLED: "Cancelled",
}

VIEW_PERMISSION = "accounting.product_mapping.view"
CREATE_PERMISSION = "accounting.product_mapping.create"
EDIT_PERMISSION = "accounting.product_mapping.edit"
SUBMIT_PERMISSION = "accounting.product_mapping.submit"
WITHDRAW_PERMISSION = "accounting.product_mapping.withdraw"
CANCEL_PERMISSION = "accounting.product_mapping.cancel"
APPROVE_PERMISSION = "accounting.product_mapping.approve"
RETURN_PERMISSION = "accounting.product_mapping.return"
DEACTIVATE_PERMISSION = "accounting.product_mapping.deactivate"
REACTIVATE_PERMISSION = "accounting.product_mapping.reactivate"

PURCHASE_GROUP_KEY = "purchase_accounts"
SALES_GROUP_KEY = "sales_accounts"
INVENTORY_GROUP_KEY = "stock_in_hand"

TRACKING_PROFILE_COLLECTION = "accounting_product_tracking_profiles"

PRODUCT_MASTER_ROLES = {"input", "output", "both"}

PRODUCT_READINESS_LABELS = {
    "disabled": "Disabled",
    "product_master_incomplete": "Product Master Incomplete",
    "accounting_unmapped": "Accounting Unmapped",
    "accounting_mapping_pending": "Accounting Mapping Pending",
    "tracking_configuration_pending": "Tracking Configuration Pending",
    "purchase_disabled": "Purchase Disabled",
    "avpl_only_ready": "AVPL-Only Ready",
    "sales_disabled": "Sales Disabled",
    "commercial_setup_pending": "Commercial Setup Pending",
    "waiting_for_stock": "Waiting for Stock",
    "ready": "Ready",
}


# ---------------------------------------------------------------------------
# Shared safety helpers
# ---------------------------------------------------------------------------


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
        raise RuntimeError(
            f"Could not inspect indexes for {collection.name}."
        ) from exc

    for existing_name, metadata in index_info.items():
        if existing_name == "_id_":
            continue

        existing_keys = _normalized_keys(metadata.get("key", []))
        same_name = existing_name == name
        same_keys = existing_keys == required_keys

        if not same_name and not same_keys:
            continue

        existing_unique = bool(metadata.get("unique", False))
        existing_partial = metadata.get("partialFilterExpression")

        if (
            same_keys
            and existing_unique == required_unique
            and existing_partial == required_partial
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


def ensure_product_mapping_indexes():
    collection = mongo.db[MAPPING_COLLECTION]

    _ensure_exact_index(
        collection,
        [("live_mapping_key", ASCENDING)],
        name="accounting_product_mapping_live_unique",
        unique=True,
        partialFilterExpression={"live_mapping_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("source_product_name", ASCENDING),
        ],
        name="accounting_product_mapping_entity_status_name_idx",
    )
    _ensure_exact_index(
        collection,
        [("status", ASCENDING), ("submitted_at", ASCENDING)],
        name="accounting_product_mapping_approval_queue_idx",
    )
    _ensure_exact_index(
        collection,
        [("source_product_id", ASCENDING), ("updated_at", DESCENDING)],
        name="accounting_product_mapping_source_updated_idx",
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("hsn_master_id", ASCENDING)],
        name="accounting_product_mapping_hsn_idx",
    )


def _clean_text(value, maximum=500):
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"Text cannot exceed {maximum} characters.")
    return text


def _bool_value(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _decimal_string(value):
    try:
        return format(Decimal(str(value or 0)), "f")
    except (InvalidOperation, TypeError, ValueError):
        return "0"


def _get_actor(actor_user_id, allowed_roles=None):
    actor_object_id = _to_object_id(actor_user_id)
    if not actor_object_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_object_id},
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

    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError("Inactive users cannot manage product Accounting mappings.")

    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError(
            "You are not authorized to perform this product Accounting mapping action."
        )

    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )
    return actor


def _assert_active_avpl_entity(entity_id=None):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    object_id = _to_object_id(entity_id) if entity_id else None
    if object_id:
        query["_id"] = object_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise ValueError("The active AVPL Accounting entity was not found.")
    return entity


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")

    if actor.get("resolved_role") != "super_admin":
        allowed_entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")

    if not has_accounting_permission(access, permission):
        raise PermissionError("You do not have permission to perform this action.")
    return access


def _workflow_event(
    action,
    actor,
    previous_status=None,
    new_status=None,
    reason="",
    note="",
    changed_fields=None,
):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": _clean_text(reason, 1000),
        "note": _clean_text(note, 1000),
        "changed_fields": sorted(set(changed_fields or [])),
        "at": now_utc(),
    }


def _record_audit(document, actor, action, previous_status=None, remarks="", changed_fields=None):
    timestamp = now_utc()
    audit = {
        "module": "accounting",
        "submodule": "product_mapping",
        "action": action,
        "accounting_entity_id": document["accounting_entity_id"],
        "accounting_entity_id_str": str(document["accounting_entity_id"]),
        "entity_type": "accounting_product_mapping",
        "entity_id": document["_id"],
        "entity_id_str": str(document["_id"]),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "previous_status": previous_status,
        "new_status": document.get("status"),
        "metadata": {
            "mapping_code": document.get("mapping_code"),
            "source_collection": document.get("source_collection"),
            "source_product_id": document.get("source_product_id_str"),
            "source_product_name": document.get("source_product_name"),
            "hsn_code": document.get("hsn_code"),
            "taxability_code": document.get("taxability_code"),
            "gst_rate_code": document.get("gst_rate_code"),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": _clean_text(remarks, 1500),
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
    except Exception as exc:
        mongo.db[MAPPING_COLLECTION].update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_error": str(exc)[:500],
                    "audit_recovery_required_at": timestamp,
                }
            },
        )


# ---------------------------------------------------------------------------
# Existing AVPL product adapter
# ---------------------------------------------------------------------------


def _source_product_query(include_inactive=False):
    query = {"is_deleted": {"$ne": True}}
    if not include_inactive:
        query.update({"is_active": {"$ne": False}, "status": {"$ne": "inactive"}})
    return query


def _get_source_product(product_id, include_inactive=False):
    object_id = _to_object_id(product_id)
    if not object_id:
        raise ValueError("Invalid AVPL product.")

    product = mongo.db[SOURCE_PRODUCT_COLLECTION].find_one(
        {"_id": object_id, **_source_product_query(include_inactive=include_inactive)}
    )
    if not product:
        raise ValueError("The selected AVPL product was not found or is unavailable.")
    return product


def _source_product_snapshot(product):
    return {
        "source_product_name": _clean_text(
            product.get("name") or product.get("product_name") or "Unnamed product",
            240,
        ),
        "source_product_category": _clean_text(product.get("category"), 160),
        "source_product_type": _clean_text(product.get("type"), 160),
        "source_product_price": _decimal_string(
            product.get("price") or product.get("unit_price") or 0
        ),
        "source_available_quantity": _decimal_string(
            product.get("available_quantity") or product.get("quantity") or 0
        ),
        "source_image_name": _clean_text(
            product.get("image_name")
            or product.get("filename")
            or product.get("file_name"),
            500,
        ),
        "source_product_status": str(product.get("status") or "active"),
        "source_product_is_active": product.get("is_active", True) is not False,
        "source_product_updated_at": product.get("updated_at"),
    }


def _serialize_source_product(product, mapping=None):
    snapshot = _source_product_snapshot(product)
    return {
        "id": str(product.get("_id") or ""),
        "name": snapshot["source_product_name"],
        "category": snapshot["source_product_category"],
        "product_type": snapshot["source_product_type"],
        "price": snapshot["source_product_price"],
        "available_quantity": snapshot["source_available_quantity"],
        "status": snapshot["source_product_status"],
        "is_active": snapshot["source_product_is_active"],
        "mapping_id": str(mapping.get("_id") or "") if mapping else "",
        "mapping_status": mapping.get("status") if mapping else "unmapped",
        "mapping_status_display": (
            STATUS_LABELS.get(mapping.get("status"), mapping.get("status"))
            if mapping
            else "Unmapped"
        ),
    }


# ---------------------------------------------------------------------------
# Reference master validation
# ---------------------------------------------------------------------------


def _active_account_groups(entity_id):
    rows = mongo.db.account_groups.find(
        {
            "accounting_entity_id": entity_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
        },
        {"_id": 1, "system_key": 1, "name": 1},
    )
    return {str(row["_id"]): row for row in rows}


def _eligible_ledgers(entity_id):
    groups = _active_account_groups(entity_id)
    by_role = {"purchase": [], "sales": [], "inventory": []}
    expected_group = {
        "purchase": PURCHASE_GROUP_KEY,
        "sales": SALES_GROUP_KEY,
        "inventory": INVENTORY_GROUP_KEY,
    }

    rows = mongo.db.ledgers.find(
        {
            "accounting_entity_id": entity_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
            "is_party_ledger": {"$ne": True},
        }
    ).sort("name", ASCENDING)

    for row in rows:
        group = groups.get(str(row.get("account_group_id") or ""))
        group_key = (
            row.get("account_group_system_key")
            or (group or {}).get("system_key")
            or ""
        )
        for role, system_key in expected_group.items():
            if group_key == system_key:
                by_role[role].append(
                    {
                        "id": str(row.get("_id") or ""),
                        "ledger_code": row.get("ledger_code") or "",
                        "name": row.get("name") or "",
                        "system_key": row.get("system_key") or "",
                        "account_group_name": (group or {}).get("name") or "",
                    }
                )
    return by_role


def _validate_ledger(entity_id, ledger_id, role):
    role_to_group = {
        "purchase": PURCHASE_GROUP_KEY,
        "sales": SALES_GROUP_KEY,
        "inventory": INVENTORY_GROUP_KEY,
    }
    object_id = _to_object_id(ledger_id)
    if not object_id or role not in role_to_group:
        raise ValueError(f"Invalid {role} ledger.")

    ledger = mongo.db.ledgers.find_one(
        {
            "_id": object_id,
            "accounting_entity_id": entity_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
            "is_party_ledger": {"$ne": True},
        }
    )
    if not ledger:
        raise ValueError(f"The selected {role} ledger is not active.")

    group_key = ledger.get("account_group_system_key") or ""
    if not group_key and ledger.get("account_group_id"):
        group = mongo.db.account_groups.find_one(
            {"_id": ledger["account_group_id"], "accounting_entity_id": entity_id}
        )
        group_key = (group or {}).get("system_key") or ""

    if group_key != role_to_group[role]:
        raise ValueError(
            f"The selected {role} ledger is not mapped to the required Accounting group."
        )
    return ledger


def _active_conversion_exists(entity_id, alternate_unit_id, base_unit_id):
    direct = mongo.db.accounting_unit_conversions.find_one(
        {
            "accounting_entity_id": entity_id,
            "from_unit_id": alternate_unit_id,
            "to_unit_id": base_unit_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    )
    if direct:
        return True

    reverse = mongo.db.accounting_unit_conversions.find_one(
        {
            "accounting_entity_id": entity_id,
            "from_unit_id": base_unit_id,
            "to_unit_id": alternate_unit_id,
            "status": "active",
            "is_active": True,
            "is_bidirectional": True,
            "is_deleted": {"$ne": True},
        }
    )
    return bool(reverse)


def _validate_alternate_units(entity_id, base_unit, raw_values):
    values = raw_values or []
    if isinstance(values, str):
        values = [values]

    alternate_units = []
    seen = set()
    for value in values:
        object_id = _to_object_id(value)
        if not object_id:
            continue
        if str(object_id) == str(base_unit["_id"]):
            raise ValueError("The base unit cannot also be an alternate unit.")
        if str(object_id) in seen:
            continue
        seen.add(str(object_id))

        unit = get_active_unit_for_mapping(entity_id, object_id)
        if not _active_conversion_exists(entity_id, unit["_id"], base_unit["_id"]):
            raise ValueError(
                f"No approved direct conversion exists between {unit.get('name') or unit.get('unit_code')} "
                f"and the selected base unit."
            )
        alternate_units.append(unit)
    return alternate_units


def _mapping_payload(entity_id, form):
    product = _get_source_product(form.get("source_product_id"))
    hsn = get_active_hsn_master_for_mapping(entity_id, form.get("hsn_master_id"))
    base_unit = get_active_unit_for_mapping(entity_id, form.get("base_unit_id"))

    getlist = getattr(form, "getlist", None)
    alternate_values = (
        getlist("alternate_unit_ids")
        if callable(getlist)
        else form.get("alternate_unit_ids") or []
    )
    alternate_units = _validate_alternate_units(entity_id, base_unit, alternate_values)

    purchase_ledger = _validate_ledger(
        entity_id, form.get("purchase_ledger_id"), "purchase"
    )
    sales_ledger = _validate_ledger(
        entity_id, form.get("sales_ledger_id"), "sales"
    )
    inventory_ledger = _validate_ledger(
        entity_id, form.get("inventory_ledger_id"), "inventory"
    )

    taxability_code = str(hsn.get("taxability_code") or "").strip().upper()
    gst_rate_code = str(hsn.get("gst_rate_code") or "").strip().upper()
    if taxability_code == "TAXABLE" and not gst_rate_code:
        raise ValueError("The selected taxable HSN does not have an approved GST rate code.")
    if taxability_code != "TAXABLE":
        gst_rate_code = ""

    snapshot = _source_product_snapshot(product)
    return {
        "source_collection": SOURCE_PRODUCT_COLLECTION,
        "source_product_id": product["_id"],
        "source_product_id_str": str(product["_id"]),
        **snapshot,
        "hsn_master_id": hsn["_id"],
        "hsn_master_id_str": str(hsn["_id"]),
        "hsn_code": hsn.get("hsn_code") or "",
        "hsn_description": hsn.get("description") or "",
        "taxability_code": taxability_code,
        "taxability_name": hsn.get("taxability_name") or taxability_code,
        "gst_rate_code": gst_rate_code,
        "base_unit_id": base_unit["_id"],
        "base_unit_id_str": str(base_unit["_id"]),
        "base_unit_code": base_unit.get("unit_code") or "",
        "base_unit_name": base_unit.get("name") or "",
        "base_unit_allows_fractional": base_unit.get("allows_fractional") is True,
        "base_unit_decimal_places": int(base_unit.get("decimal_places") or 0),
        "alternate_unit_ids": [row["_id"] for row in alternate_units],
        "alternate_unit_id_strs": [str(row["_id"]) for row in alternate_units],
        "alternate_units": [
            {
                "id": str(row["_id"]),
                "unit_code": row.get("unit_code") or "",
                "name": row.get("name") or "",
            }
            for row in alternate_units
        ],
        "purchase_ledger_id": purchase_ledger["_id"],
        "purchase_ledger_id_str": str(purchase_ledger["_id"]),
        "purchase_ledger_code": purchase_ledger.get("ledger_code") or "",
        "purchase_ledger_name": purchase_ledger.get("name") or "",
        "sales_ledger_id": sales_ledger["_id"],
        "sales_ledger_id_str": str(sales_ledger["_id"]),
        "sales_ledger_code": sales_ledger.get("ledger_code") or "",
        "sales_ledger_name": sales_ledger.get("name") or "",
        "inventory_ledger_id": inventory_ledger["_id"],
        "inventory_ledger_id_str": str(inventory_ledger["_id"]),
        "inventory_ledger_code": inventory_ledger.get("ledger_code") or "",
        "inventory_ledger_name": inventory_ledger.get("name") or "",
        "inventory_tracking_enabled": _bool_value(
            form.get("inventory_tracking_enabled"), default=True
        ),
        "purchase_enabled": _bool_value(form.get("purchase_enabled"), default=True),
        "sales_enabled": _bool_value(form.get("sales_enabled"), default=True),
        "mapping_note": _clean_text(form.get("mapping_note"), 1500),
    }


def _changed_fields(existing, payload):
    fields = []
    for key, value in payload.items():
        if key in {"source_product_updated_at"}:
            continue
        if existing.get(key) != value:
            fields.append(key)
    return fields


def _get_mapping(mapping_id):
    object_id = _to_object_id(mapping_id)
    if not object_id:
        raise ValueError("Invalid product Accounting mapping.")
    row = mongo.db[MAPPING_COLLECTION].find_one(
        {"_id": object_id, "is_deleted": {"$ne": True}}
    )
    if not row:
        raise ValueError("Product Accounting mapping was not found.")
    return row


def _assert_maker(document, actor):
    if str(document.get("created_by") or "") != str(actor.get("_id") or ""):
        raise PermissionError("Only the original maker can change this mapping draft.")


def _expected_version(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("The mapping version is invalid. Refresh and try again.") from exc


# ---------------------------------------------------------------------------
# Serialization and dashboard data
# ---------------------------------------------------------------------------


def serialize_product_mapping(document):
    alternate_units = document.get("alternate_units") or []
    return {
        "id": str(document.get("_id") or ""),
        "mapping_code": document.get("mapping_code") or "",
        "source_product_id": document.get("source_product_id_str") or str(document.get("source_product_id") or ""),
        "source_product_name": document.get("source_product_name") or "",
        "source_product_category": document.get("source_product_category") or "",
        "source_product_type": document.get("source_product_type") or "",
        "source_product_price": document.get("source_product_price") or "0",
        "source_available_quantity": document.get("source_available_quantity") or "0",
        "source_product_status": document.get("source_product_status") or "",
        "source_product_is_active": document.get("source_product_is_active") is not False,
        "hsn_master_id": document.get("hsn_master_id_str") or str(document.get("hsn_master_id") or ""),
        "hsn_code": document.get("hsn_code") or "",
        "hsn_description": document.get("hsn_description") or "",
        "taxability_code": document.get("taxability_code") or "",
        "taxability_name": document.get("taxability_name") or "",
        "gst_rate_code": document.get("gst_rate_code") or "",
        "base_unit_id": document.get("base_unit_id_str") or str(document.get("base_unit_id") or ""),
        "base_unit_code": document.get("base_unit_code") or "",
        "base_unit_name": document.get("base_unit_name") or "",
        "base_unit_allows_fractional": document.get("base_unit_allows_fractional") is True,
        "base_unit_decimal_places": int(document.get("base_unit_decimal_places") or 0),
        "alternate_unit_ids": document.get("alternate_unit_id_strs") or [],
        "alternate_units": alternate_units,
        "alternate_unit_summary": ", ".join(
            row.get("unit_code") or row.get("name") or ""
            for row in alternate_units
            if row.get("unit_code") or row.get("name")
        ),
        "purchase_ledger_id": document.get("purchase_ledger_id_str") or str(document.get("purchase_ledger_id") or ""),
        "purchase_ledger_code": document.get("purchase_ledger_code") or "",
        "purchase_ledger_name": document.get("purchase_ledger_name") or "",
        "sales_ledger_id": document.get("sales_ledger_id_str") or str(document.get("sales_ledger_id") or ""),
        "sales_ledger_code": document.get("sales_ledger_code") or "",
        "sales_ledger_name": document.get("sales_ledger_name") or "",
        "inventory_ledger_id": document.get("inventory_ledger_id_str") or str(document.get("inventory_ledger_id") or ""),
        "inventory_ledger_code": document.get("inventory_ledger_code") or "",
        "inventory_ledger_name": document.get("inventory_ledger_name") or "",
        "inventory_tracking_enabled": document.get("inventory_tracking_enabled") is not False,
        "purchase_enabled": document.get("purchase_enabled") is not False,
        "sales_enabled": document.get("sales_enabled") is not False,
        "mapping_note": document.get("mapping_note") or "",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(document.get("status"), document.get("status") or "Draft"),
        "is_active": document.get("is_active") is True,
        "is_accounting_eligible": document.get("is_accounting_eligible") is True,
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": document.get("created_by_name") or "",
        "submitted_by_name": document.get("submitted_by_name") or "",
        "approved_by_name": document.get("approved_by_name") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "return_reason": document.get("return_reason") or "",
        "deactivation_reason": document.get("deactivation_reason") or "",
        "audit_sync_required": document.get("audit_sync_required") is True,
        "change_history": document.get("change_history") or [],
        "updated_at": document.get("updated_at"),
    }


def get_product_mapping_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    if not entity_id:
        return {
            "products": [],
            "hsn_masters": [],
            "units": [],
            "purchase_ledgers": [],
            "sales_ledgers": [],
            "inventory_ledgers": [],
            "status_labels": dict(STATUS_LABELS),
        }

    ensure_product_mapping_indexes()
    mapping_rows = list(
        mongo.db[MAPPING_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "is_deleted": {"$ne": True},
                "status": {"$ne": STATUS_CANCELLED},
            }
        )
    )
    mapping_by_product = {
        str(row.get("source_product_id") or ""): row for row in mapping_rows
    }

    products = [
        _serialize_source_product(row, mapping_by_product.get(str(row["_id"])))
        for row in mongo.db[SOURCE_PRODUCT_COLLECTION]
        .find(_source_product_query(include_inactive=False))
        .sort([("name", ASCENDING), ("_id", ASCENDING)])
    ]

    hsn_rows = mongo.db.hsn_masters.find(
        {
            "accounting_entity_id": entity_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    ).sort("hsn_code", ASCENDING)
    hsn_masters = [
        {
            "id": str(row["_id"]),
            "hsn_code": row.get("hsn_code") or "",
            "description": row.get("description") or "",
            "taxability_code": row.get("taxability_code") or "",
            "taxability_name": row.get("taxability_name") or row.get("taxability_code") or "",
            "gst_rate_code": row.get("gst_rate_code") or "",
        }
        for row in hsn_rows
    ]

    unit_rows = mongo.db.accounting_units.find(
        {
            "accounting_entity_id": entity_id,
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    ).sort([("is_system", DESCENDING), ("name", ASCENDING)])
    units = [
        {
            "id": str(row["_id"]),
            "unit_code": row.get("unit_code") or "",
            "name": row.get("name") or "",
            "dimension": row.get("dimension") or "other",
            "allows_fractional": row.get("allows_fractional") is True,
            "decimal_places": int(row.get("decimal_places") or 0),
        }
        for row in unit_rows
    ]

    eligible = _eligible_ledgers(entity_id)
    return {
        "products": products,
        "unmapped_products": [row for row in products if row["mapping_status"] == "unmapped"],
        "hsn_masters": hsn_masters,
        "units": units,
        "purchase_ledgers": eligible["purchase"],
        "sales_ledgers": eligible["sales"],
        "inventory_ledgers": eligible["inventory"],
        "status_labels": dict(STATUS_LABELS),
    }


def get_product_mapping_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_product_mapping_indexes()

    rows = list(
        mongo.db[MAPPING_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_deleted": {"$ne": True},
            }
        ).sort([("status", ASCENDING), ("source_product_name", ASCENDING)])
    )

    # Refresh only read-model source status fields; operational product records
    # themselves are never mutated by the Accounting mapping module.
    serialized = []
    for row in rows:
        source = mongo.db[SOURCE_PRODUCT_COLLECTION].find_one(
            {"_id": row.get("source_product_id")}
        )
        if source:
            current_snapshot = _source_product_snapshot(source)
            row = {**row, **current_snapshot}
        else:
            row = {
                **row,
                "source_product_is_active": False,
                "source_product_status": "missing",
            }
        serialized.append(serialize_product_mapping(row))

    counts = {status: 0 for status in STATUS_LABELS}
    for row in rows:
        status = row.get("status") or STATUS_DRAFT
        counts[status] = counts.get(status, 0) + 1

    active_product_count = mongo.db[SOURCE_PRODUCT_COLLECTION].count_documents(
        _source_product_query(include_inactive=False)
    )
    active_mapped_product_ids = {
        str(row.get("source_product_id") or "")
        for row in rows
        if row.get("status") == STATUS_ACTIVE
        and row.get("is_active") is True
        and row.get("is_accounting_eligible") is True
    }

    options = get_product_mapping_option_catalog(entity["_id"])
    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "rows": serialized,
        "working_rows": [row for row in serialized if row["status"] in EDITABLE_STATUSES],
        "pending_rows": [row for row in serialized if row["status"] == STATUS_PENDING_APPROVAL],
        "active_rows": [row for row in serialized if row["status"] == STATUS_ACTIVE],
        "inactive_rows": [row for row in serialized if row["status"] == STATUS_INACTIVE],
        "cancelled_rows": [row for row in serialized if row["status"] == STATUS_CANCELLED],
        "counts": counts,
        "total_mapping_count": len(rows),
        "active_operational_product_count": active_product_count,
        "active_mapped_product_count": len(active_mapped_product_ids),
        "unmapped_active_product_count": max(
            active_product_count - len(active_mapped_product_ids), 0
        ),
        "audit_recovery_count": sum(
            1 for row in rows if row.get("audit_sync_required") is True
        ),
        "options": options,
        "prerequisites": {
            "has_active_hsn": bool(options["hsn_masters"]),
            "has_active_units": bool(options["units"]),
            "has_purchase_ledger": bool(options["purchase_ledgers"]),
            "has_sales_ledger": bool(options["sales_ledgers"]),
            "has_inventory_ledger": bool(options["inventory_ledgers"]),
            "is_ready": all(
                [
                    bool(options["hsn_masters"]),
                    bool(options["units"]),
                    bool(options["purchase_ledgers"]),
                    bool(options["sales_ledgers"]),
                    bool(options["inventory_ledgers"]),
                ]
            ),
        },
    }


# ---------------------------------------------------------------------------
# Maker-checker workflow
# ---------------------------------------------------------------------------


def create_product_mapping(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_product_mapping_indexes()

    payload = _mapping_payload(entity["_id"], form)
    live_mapping_key = f"{entity['_id']}:{payload['source_product_id']}"
    existing = mongo.db[MAPPING_COLLECTION].find_one(
        {"live_mapping_key": live_mapping_key}
    )
    if existing:
        raise ValueError(
            "This AVPL product already has a live Accounting mapping. Open the existing mapping instead."
        )

    timestamp = now_utc()
    mapping_code = f"APM-{str(payload['source_product_id'])[-8:].upper()}"
    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "mapping_code": mapping_code,
        "live_mapping_key": live_mapping_key,
        **payload,
        "status": STATUS_DRAFT,
        "is_active": False,
        "is_accounting_eligible": False,
        "is_deleted": False,
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [
            _workflow_event(
                "mapping_draft_created",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                note=payload.get("mapping_note"),
            )
        ],
    }

    try:
        result = mongo.db[MAPPING_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(
            "This AVPL product already has a live Accounting mapping."
        ) from exc

    document["_id"] = result.inserted_id
    _record_audit(document, actor, "create_product_mapping", remarks=payload.get("mapping_note"))
    return {
        "mapping": serialize_product_mapping(document),
        "message": f"Product Accounting mapping {mapping_code} created as Draft.",
    }


def update_product_mapping(mapping_id, actor_user_id, expected_version, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    document = _get_mapping(mapping_id)
    entity = _assert_active_avpl_entity(document.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    _assert_maker(document, actor)

    if document.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only Draft or Returned mappings can be edited.")

    current_version = int(document.get("version") or 1)
    if _expected_version(expected_version) != current_version:
        raise RuntimeError("This mapping changed in another session. Refresh and try again.")

    payload = _mapping_payload(entity["_id"], form)
    if str(payload["source_product_id"]) != str(document.get("source_product_id")):
        raise ValueError("The AVPL source product cannot be changed after mapping creation.")

    changed_fields = _changed_fields(document, payload)
    timestamp = now_utc()
    event = _workflow_event(
        "mapping_draft_updated",
        actor,
        previous_status=document.get("status"),
        new_status=STATUS_DRAFT,
        note=payload.get("mapping_note"),
        changed_fields=changed_fields,
    )

    result = mongo.db[MAPPING_COLLECTION].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                **payload,
                "status": STATUS_DRAFT,
                "is_active": False,
                "is_accounting_eligible": False,
                "return_reason": "",
                "returned_by": None,
                "returned_at": None,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"change_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError("This mapping changed in another session. Refresh and try again.")

    updated = _get_mapping(document["_id"])
    _record_audit(
        updated,
        actor,
        "update_product_mapping",
        previous_status=document.get("status"),
        remarks=payload.get("mapping_note"),
        changed_fields=changed_fields,
    )
    return {
        "mapping": serialize_product_mapping(updated),
        "message": "Product Accounting mapping draft updated.",
    }


def _transition(
    mapping_id,
    actor_user_id,
    expected_version,
    *,
    action,
    permission,
    allowed_roles,
    source_statuses,
    target_status,
    reason="",
    note="",
):
    actor = _get_actor(actor_user_id, allowed_roles=allowed_roles)
    document = _get_mapping(mapping_id)
    entity = _assert_active_avpl_entity(document.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)

    current_status = document.get("status") or STATUS_DRAFT
    if current_status not in set(source_statuses):
        raise ValueError(
            f"This action is not available while the mapping is {STATUS_LABELS.get(current_status, current_status)}."
        )

    current_version = int(document.get("version") or 1)
    if _expected_version(expected_version) != current_version:
        raise RuntimeError("This mapping changed in another session. Refresh and try again.")

    if action in {"submit", "withdraw", "cancel"}:
        _assert_maker(document, actor)

    if action in {"approve", "return"} and str(document.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot approve or return their own product mapping.")

    clean_reason = _clean_text(reason, 1000)
    clean_note = _clean_text(note, 1000)
    if action in {"withdraw", "cancel", "return", "deactivate", "reactivate"} and not clean_reason:
        raise ValueError("A reason is required for this action.")

    # Revalidate every master reference immediately before submission,
    # approval, and reactivation so stale/deactivated masters cannot leak into
    # future posting services.
    if action in {"submit", "approve", "reactivate"}:
        current_form = {
            "source_product_id": document.get("source_product_id_str"),
            "hsn_master_id": document.get("hsn_master_id_str"),
            "base_unit_id": document.get("base_unit_id_str"),
            "alternate_unit_ids": document.get("alternate_unit_id_strs") or [],
            "purchase_ledger_id": document.get("purchase_ledger_id_str"),
            "sales_ledger_id": document.get("sales_ledger_id_str"),
            "inventory_ledger_id": document.get("inventory_ledger_id_str"),
            "inventory_tracking_enabled": document.get("inventory_tracking_enabled", True),
            "purchase_enabled": document.get("purchase_enabled", True),
            "sales_enabled": document.get("sales_enabled", True),
            "mapping_note": document.get("mapping_note") or "",
        }
        _mapping_payload(entity["_id"], current_form)

    timestamp = now_utc()
    set_fields = {
        "status": target_status,
        "is_active": target_status == STATUS_ACTIVE,
        "is_accounting_eligible": target_status == STATUS_ACTIVE,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_at": timestamp,
        "version": current_version + 1,
    }

    if action == "submit":
        set_fields.update(
            {
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_by_name": actor.get("resolved_name") or "",
                "submitted_at": timestamp,
                "submission_note": clean_note,
                "return_reason": "",
            }
        )
    elif action == "withdraw":
        set_fields.update(
            {
                "withdrawn_by": actor["_id"],
                "withdrawn_at": timestamp,
                "withdraw_reason": clean_reason,
            }
        )
    elif action == "cancel":
        set_fields.update(
            {
                "cancelled_by": actor["_id"],
                "cancelled_at": timestamp,
                "cancel_reason": clean_reason,
                "live_mapping_key": None,
            }
        )
    elif action == "approve":
        set_fields.update(
            {
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_by_name": actor.get("resolved_name") or "",
                "approved_at": timestamp,
                "approval_note": clean_note,
                "return_reason": "",
            }
        )
    elif action == "return":
        set_fields.update(
            {
                "returned_by": actor["_id"],
                "returned_by_str": str(actor["_id"]),
                "returned_by_name": actor.get("resolved_name") or "",
                "returned_at": timestamp,
                "return_reason": clean_reason,
            }
        )
    elif action == "deactivate":
        set_fields.update(
            {
                "deactivated_by": actor["_id"],
                "deactivated_at": timestamp,
                "deactivation_reason": clean_reason,
            }
        )
    elif action == "reactivate":
        set_fields.update(
            {
                "reactivated_by": actor["_id"],
                "reactivated_at": timestamp,
                "reactivation_reason": clean_reason,
                "deactivation_reason": "",
            }
        )

    event = _workflow_event(
        f"mapping_{action}",
        actor,
        previous_status=current_status,
        new_status=target_status,
        reason=clean_reason,
        note=clean_note,
    )
    result = mongo.db[MAPPING_COLLECTION].update_one(
        {"_id": document["_id"], "version": current_version},
        {"$set": set_fields, "$push": {"change_history": event}},
    )
    if result.modified_count != 1:
        raise RuntimeError("This mapping changed in another session. Refresh and try again.")

    updated = _get_mapping(document["_id"])
    _record_audit(
        updated,
        actor,
        f"{action}_product_mapping",
        previous_status=current_status,
        remarks=clean_reason or clean_note,
    )
    return {
        "mapping": serialize_product_mapping(updated),
        "message": f"Product Accounting mapping {action} completed successfully.",
    }


def submit_product_mapping(mapping_id, actor_user_id, expected_version, submission_note=""):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="submit",
        permission=SUBMIT_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses=EDITABLE_STATUSES,
        target_status=STATUS_PENDING_APPROVAL,
        note=submission_note,
    )


def withdraw_product_mapping(mapping_id, actor_user_id, expected_version, reason):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="withdraw",
        permission=WITHDRAW_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_DRAFT,
        reason=reason,
    )


def cancel_product_mapping(mapping_id, actor_user_id, expected_version, reason):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="cancel",
        permission=CANCEL_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses=EDITABLE_STATUSES,
        target_status=STATUS_CANCELLED,
        reason=reason,
    )


def approve_product_mapping(mapping_id, actor_user_id, expected_version, approval_note=""):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="approve",
        permission=APPROVE_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_ACTIVE,
        note=approval_note,
    )


def return_product_mapping(mapping_id, actor_user_id, expected_version, return_reason):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="return",
        permission=RETURN_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_RETURNED,
        reason=return_reason,
    )


def deactivate_product_mapping(mapping_id, actor_user_id, expected_version, reason):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="deactivate",
        permission=DEACTIVATE_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_ACTIVE},
        target_status=STATUS_INACTIVE,
        reason=reason,
    )


def reactivate_product_mapping(mapping_id, actor_user_id, expected_version, reason):
    return _transition(
        mapping_id,
        actor_user_id,
        expected_version,
        action="reactivate",
        permission=REACTIVATE_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_INACTIVE},
        target_status=STATUS_ACTIVE,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Future purchase/sales/stock posting contract
# ---------------------------------------------------------------------------


def get_product_accounting_mapping_for_posting(
    accounting_entity_id,
    source_product_id,
    transaction_date=None,
    operation=None,
):
    entity = _assert_active_avpl_entity(accounting_entity_id)
    product = _get_source_product(source_product_id)

    row = mongo.db[MAPPING_COLLECTION].find_one(
        {
            "accounting_entity_id": entity["_id"],
            "source_product_id": product["_id"],
            "status": STATUS_ACTIVE,
            "is_active": True,
            "is_accounting_eligible": True,
            "is_deleted": {"$ne": True},
        }
    )
    if not row:
        raise ValueError(
            "This product does not have an approved active Accounting mapping."
        )

    operation_name = str(operation or "").strip().lower()
    if operation_name == "purchase" and row.get("purchase_enabled") is False:
        raise ValueError("Purchase Accounting is disabled for this product mapping.")
    if operation_name == "sales" and row.get("sales_enabled") is False:
        raise ValueError("Sales Accounting is disabled for this product mapping.")

    hsn = get_active_hsn_master_for_mapping(entity["_id"], row.get("hsn_master_id"))
    base_unit = get_active_unit_for_mapping(entity["_id"], row.get("base_unit_id"))
    purchase_ledger = _validate_ledger(entity["_id"], row.get("purchase_ledger_id"), "purchase")
    sales_ledger = _validate_ledger(entity["_id"], row.get("sales_ledger_id"), "sales")
    inventory_ledger = _validate_ledger(entity["_id"], row.get("inventory_ledger_id"), "inventory")

    taxability_code = str(hsn.get("taxability_code") or "").strip().upper()
    effective_rate = None
    if taxability_code == "TAXABLE":
        effective_rate = get_effective_gst_tax_rate(
            entity["_id"],
            rate_code=hsn.get("gst_rate_code"),
            transaction_date=transaction_date or date.today(),
        )
        if not effective_rate:
            raise ValueError(
                "No approved effective GST rate is available for this product and transaction date."
            )

    return {
        "mapping": serialize_product_mapping(row),
        "source_product": _serialize_source_product(product, row),
        "hsn": {
            "id": str(hsn["_id"]),
            "hsn_code": hsn.get("hsn_code") or "",
            "description": hsn.get("description") or "",
            "taxability_code": taxability_code,
            "taxability_name": hsn.get("taxability_name") or taxability_code,
            "gst_rate_code": hsn.get("gst_rate_code") or "",
        },
        "effective_gst_rate": effective_rate,
        "base_unit": {
            "id": str(base_unit["_id"]),
            "unit_code": base_unit.get("unit_code") or "",
            "name": base_unit.get("name") or "",
            "allows_fractional": base_unit.get("allows_fractional") is True,
            "decimal_places": int(base_unit.get("decimal_places") or 0),
        },
        "ledgers": {
            "purchase": purchase_ledger,
            "sales": sales_ledger,
            "inventory": inventory_ledger,
        },
    }


def assert_product_ready_for_accounting(
    accounting_entity_id,
    source_product_id,
    transaction_date=None,
    operation=None,
):
    return get_product_accounting_mapping_for_posting(
        accounting_entity_id,
        source_product_id,
        transaction_date=transaction_date,
        operation=operation,
    )



def _readiness_decimal(value):
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _product_master_missing_fields(product):
    missing = []
    if not str(product.get("name") or "").strip():
        missing.append("Product name")
    if not str(product.get("product_code") or "").strip():
        missing.append("Product code")
    if not str(product.get("category") or "").strip():
        missing.append("Category")

    product_role = str(
        product.get("product_role") or product.get("type") or ""
    ).strip().lower()
    if product_role not in PRODUCT_MASTER_ROLES:
        missing.append("Product role")
    if not str(product.get("metadata_source") or "").strip():
        missing.append("Metadata source")
    if not product.get("base_unit_id"):
        missing.append("Base unit")
    if _readiness_decimal(product.get("pack_size")) <= 0:
        missing.append("Pack size")
    return missing


def get_product_readiness_snapshot(
    accounting_entity_id,
    source_product_id,
    *,
    product_document=None,
    mapping_document=None,
    transaction_date=None,
):
    entity = _assert_active_avpl_entity(accounting_entity_id)
    product = product_document or _get_source_product(
        source_product_id,
        include_inactive=True,
    )

    product_status = str(product.get("status") or "active").strip().lower()
    product_is_active = (
        product.get("is_active", True) is not False
        and product_status not in {"inactive", "disabled", "deleted"}
        and product.get("is_deleted") is not True
    )

    master_missing_fields = _product_master_missing_fields(product)
    product_master_ready = not master_missing_fields

    mapping = mapping_document
    if mapping is None:
        mapping = mongo.db[MAPPING_COLLECTION].find_one(
            {
                "accounting_entity_id": entity["_id"],
                "source_product_id": product["_id"],
                "is_deleted": {"$ne": True},
                "status": {"$ne": STATUS_CANCELLED},
            },
            sort=[("updated_at", DESCENDING)],
        )

    mapping_status = str(mapping.get("status") or STATUS_DRAFT) if mapping else "unmapped"
    accounting_ready = False
    accounting_error = ""
    if product_is_active and mapping:
        try:
            get_product_accounting_mapping_for_posting(
                entity["_id"],
                product["_id"],
                transaction_date=transaction_date or date.today(),
            )
            accounting_ready = True
        except Exception as exc:
            accounting_error = str(exc)
    elif not mapping:
        accounting_error = "Create and activate the Product Accounting mapping."
    elif mapping_status != STATUS_ACTIVE:
        accounting_error = (
            "The Product Accounting mapping is "
            f"{STATUS_LABELS.get(mapping_status, mapping_status)}."
        )

    purchase_enabled = bool(
        mapping and mapping_status == STATUS_ACTIVE and mapping.get("purchase_enabled") is not False
    )
    sales_enabled = bool(
        mapping and mapping_status == STATUS_ACTIVE and mapping.get("sales_enabled") is not False
    )

    tracking_profile = mongo.db[TRACKING_PROFILE_COLLECTION].find_one(
        {
            "accounting_entity_id": entity["_id"],
            "source_product_id": product["_id"],
            "is_deleted": {"$ne": True},
            "status": {"$ne": STATUS_CANCELLED},
        },
        sort=[("updated_at", DESCENDING)],
    )

    tracking_ready = True
    tracking_status = "optional_not_configured"
    tracking_issues = []
    if tracking_profile:
        tracking_status = str(tracking_profile.get("status") or STATUS_DRAFT)
        if tracking_status != STATUS_ACTIVE or tracking_profile.get("is_active") is not True:
            tracking_ready = False
            tracking_issues.append("Complete and activate the product tracking profile.")
        else:
            if tracking_profile.get("barcode_required_on_transaction") and not (
                tracking_profile.get("primary_barcode") or product.get("barcode")
            ):
                tracking_ready = False
                tracking_issues.append("A barcode is required by the active tracking profile.")
            if tracking_profile.get("batch_number_required") and not tracking_profile.get("batch_tracking_enabled"):
                tracking_ready = False
                tracking_issues.append("Batch-number tracking is required but not enabled.")
            if tracking_profile.get("expiry_date_required") and not tracking_profile.get("expiry_tracking_enabled"):
                tracking_ready = False
                tracking_issues.append("Expiry-date tracking is required but not enabled.")

    selected_centres = product.get("available_centres") or []
    if isinstance(selected_centres, str):
        selected_centres = [selected_centres] if selected_centres.strip() else []

    selling_price = _readiness_decimal(product.get("price"))
    available_quantity = _readiness_decimal(
        product.get("available_quantity")
        or product.get("quantity")
        or product.get("stock_quantity")
        or product.get("stock")
        or 0
    )

    commercial_configured = bool(
        product.get("commercial_setup_status") == "configured" or selected_centres
    )
    commercial_issues = []
    if not commercial_configured:
        commercial_issues.append("Complete the Commercial Setup.")
    if selling_price <= 0:
        commercial_issues.append("Set a selling price greater than zero.")
    if not selected_centres:
        commercial_issues.append("Select at least one Centre.")

    commercial_ready = commercial_configured and selling_price > 0 and bool(selected_centres)
    stock_ready = available_quantity > 0
    unnatfarm_eligible = product.get("unnatfarm_eligible", True) is not False

    purchase_ready = bool(
        product_is_active
        and product_master_ready
        and accounting_ready
        and purchase_enabled
        and tracking_ready
    )
    listing_ready = bool(
        product_is_active
        and product_master_ready
        and accounting_ready
        and tracking_ready
        and unnatfarm_eligible
        and sales_enabled
        and commercial_ready
    )
    sale_ready = bool(listing_ready and stock_ready)

    issues = []
    if not product_is_active:
        issues.append("Enable the product.")
    if master_missing_fields:
        issues.append("Complete: " + ", ".join(master_missing_fields) + ".")
    if accounting_error:
        issues.append(accounting_error)
    issues.extend(tracking_issues)
    if unnatfarm_eligible and sales_enabled:
        issues.extend(commercial_issues)
    if listing_ready and not stock_ready:
        issues.append("Receive or add stock before selling the product.")

    if not product_is_active:
        status = "disabled"
    elif not product_master_ready:
        status = "product_master_incomplete"
    elif not mapping:
        status = "accounting_unmapped"
    elif not accounting_ready:
        status = "accounting_mapping_pending"
    elif not tracking_ready:
        status = "tracking_configuration_pending"
    elif not purchase_enabled:
        status = "purchase_disabled"
    elif not unnatfarm_eligible:
        status = "avpl_only_ready"
    elif not sales_enabled:
        status = "sales_disabled"
    elif not commercial_ready:
        status = "commercial_setup_pending"
    elif not stock_ready:
        status = "waiting_for_stock"
    else:
        status = "ready"

    tone_by_status = {
        "ready": "active",
        "avpl_only_ready": "info",
        "waiting_for_stock": "info",
        "commercial_setup_pending": "pending",
        "accounting_mapping_pending": "pending",
        "tracking_configuration_pending": "pending",
        "purchase_disabled": "pending",
        "sales_disabled": "pending",
        "product_master_incomplete": "error",
        "accounting_unmapped": "error",
        "disabled": "disabled",
    }

    return {
        "status": status,
        "label": PRODUCT_READINESS_LABELS.get(status, status.replace("_", " ").title()),
        "tone": tone_by_status.get(status, "pending"),
        "product_master_ready": product_master_ready,
        "master_missing_fields": master_missing_fields,
        "accounting_ready": accounting_ready,
        "mapping_status": mapping_status,
        "purchase_enabled": purchase_enabled,
        "sales_enabled": sales_enabled,
        "tracking_ready": tracking_ready,
        "tracking_status": tracking_status,
        "commercial_ready": commercial_ready,
        "stock_ready": stock_ready,
        "unnatfarm_eligible": unnatfarm_eligible,
        "purchase_ready": purchase_ready,
        "listing_ready": listing_ready,
        "sale_ready": sale_ready,
        "selling_price": format(selling_price, "f"),
        "available_quantity": format(available_quantity, "f"),
        "issues": issues,
        "primary_issue": issues[0] if issues else "",
    }


def assert_product_ready_for_avpl_purchase(
    accounting_entity_id,
    source_product_id,
    *,
    transaction_date=None,
):
    readiness = get_product_readiness_snapshot(
        accounting_entity_id,
        source_product_id,
        transaction_date=transaction_date,
    )
    if not readiness.get("purchase_ready"):
        raise ValueError(
            readiness.get("primary_issue")
            or "This product is not ready for an AVPL purchase."
        )
    accounting_context = get_product_accounting_mapping_for_posting(
        accounting_entity_id,
        source_product_id,
        transaction_date=transaction_date,
        operation="purchase",
    )
    return {"readiness": readiness, "accounting": accounting_context}


def assert_product_ready_for_avpl_sale(
    accounting_entity_id,
    source_product_id,
    *,
    transaction_date=None,
):
    readiness = get_product_readiness_snapshot(
        accounting_entity_id,
        source_product_id,
        transaction_date=transaction_date,
    )
    if not readiness.get("sale_ready"):
        raise ValueError(
            readiness.get("primary_issue")
            or "This product is not ready for an AVPL sale."
        )
    accounting_context = get_product_accounting_mapping_for_posting(
        accounting_entity_id,
        source_product_id,
        transaction_date=transaction_date,
        operation="sales",
    )
    return {"readiness": readiness, "accounting": accounting_context}




def upsert_product_mapping_request_from_product_master(
    accounting_entity_id,
    actor_user_id,
    source_product_id,
    form,
):
    """Create or refresh a product Accounting mapping from the AVPL product form.

    An AVPL Admin is the authorised product-master approver, so mappings saved
    from this form are activated immediately. An Accounts user may prepare the
    same mapping, but it remains Draft for AVPL Admin review. This keeps the
    product-entry flow fast without allowing an Accounts maker to approve their
    own Accounting master.
    """
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "avpl_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    ensure_product_mapping_indexes()

    values = dict(form or {})
    values["source_product_id"] = str(source_product_id)
    payload = _mapping_payload(entity["_id"], values)
    live_mapping_key = f"{entity['_id']}:{payload['source_product_id']}"
    existing = mongo.db[MAPPING_COLLECTION].find_one({"live_mapping_key": live_mapping_key})
    timestamp = now_utc()

    auto_approve = actor.get("resolved_role") == "avpl_admin"
    target_status = STATUS_ACTIVE if auto_approve else STATUS_DRAFT
    active_state = bool(auto_approve)

    # Pending mappings must still complete their existing workflow. Inactive
    # mappings must be reactivated through the controlled Accounting action.
    if existing and existing.get("status") in {STATUS_PENDING_APPROVAL, STATUS_INACTIVE}:
        raise ValueError(
            "The product already has a pending or inactive Accounting mapping. "
            "Complete that action from Accounting Product Mapping first."
        )

    # An Accounts user cannot overwrite an already approved mapping.
    if existing and existing.get("status") == STATUS_ACTIVE and not auto_approve:
        raise PermissionError(
            "Only AVPL Admin can update and automatically approve an active product Accounting mapping."
        )

    approval_fields = {}
    if auto_approve:
        approval_fields = {
            "approved_by": actor["_id"],
            "approved_by_str": str(actor["_id"]),
            "approved_by_name": actor.get("resolved_name") or "",
            "approved_at": timestamp,
            "approval_note": payload.get("mapping_note") or "Auto-approved from AVPL Product Master.",
            "return_reason": "",
        }

    if existing:
        current_version = int(existing.get("version") or 1)
        changed_fields = _changed_fields(existing, payload)
        event_name = (
            "mapping_auto_approved_from_product_master"
            if auto_approve
            else "mapping_request_refreshed_from_product_master"
        )
        event = _workflow_event(
            event_name,
            actor,
            previous_status=existing.get("status"),
            new_status=target_status,
            note=payload.get("mapping_note"),
            changed_fields=changed_fields,
        )
        result = mongo.db[MAPPING_COLLECTION].update_one(
            {"_id": existing["_id"], "version": current_version},
            {
                "$set": {
                    **payload,
                    "status": target_status,
                    "is_active": active_state,
                    "is_accounting_eligible": active_state,
                    "workflow_origin": "product_master",
                    "requested_by": actor["_id"],
                    "requested_by_str": str(actor["_id"]),
                    "requested_by_name": actor.get("resolved_name") or "",
                    "requested_by_role": actor.get("resolved_role") or "",
                    "requested_at": timestamp,
                    **approval_fields,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_at": timestamp,
                    "version": current_version + 1,
                },
                "$push": {"change_history": event},
            },
        )
        if result.modified_count != 1:
            raise RuntimeError(
                "This product Accounting mapping changed in another session. Refresh and try again."
            )
        updated = _get_mapping(existing["_id"])
        _record_audit(
            updated,
            actor,
            "auto_approve_product_mapping" if auto_approve else "refresh_product_mapping_request",
            previous_status=existing.get("status"),
            remarks=payload.get("mapping_note"),
            changed_fields=changed_fields,
        )
        return {
            "mapping": serialize_product_mapping(updated),
            "message": (
                "Product Accounting mapping updated and activated automatically."
                if auto_approve
                else "Product Accounting mapping request refreshed as Draft for AVPL Admin approval."
            ),
        }

    mapping_code = f"APM-{str(payload['source_product_id'])[-8:].upper()}"
    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "mapping_code": mapping_code,
        "live_mapping_key": live_mapping_key,
        **payload,
        "status": target_status,
        "is_active": active_state,
        "is_accounting_eligible": active_state,
        "is_deleted": False,
        "workflow_origin": "product_master",
        "requested_by": actor["_id"],
        "requested_by_str": str(actor["_id"]),
        "requested_by_name": actor.get("resolved_name") or "",
        "requested_by_role": actor.get("resolved_role") or "",
        "requested_at": timestamp,
        **approval_fields,
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [
            _workflow_event(
                "mapping_auto_approved_from_product_master"
                if auto_approve
                else "mapping_request_created_from_product_master",
                actor,
                previous_status=None,
                new_status=target_status,
                note=payload.get("mapping_note"),
            )
        ],
    }
    try:
        result = mongo.db[MAPPING_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(
            "This AVPL product already has a live Accounting mapping."
        ) from exc

    document["_id"] = result.inserted_id
    _record_audit(
        document,
        actor,
        "auto_approve_product_mapping" if auto_approve else "create_product_mapping_request",
        remarks=payload.get("mapping_note"),
    )
    return {
        "mapping": serialize_product_mapping(document),
        "message": (
            f"Product Accounting mapping {mapping_code} created and activated automatically."
            if auto_approve
            else f"Product Accounting mapping {mapping_code} created as Draft for AVPL Admin approval."
        ),
    }

