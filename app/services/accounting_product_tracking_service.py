from datetime import date, datetime, timedelta
import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


PROFILE_COLLECTION = "accounting_product_tracking_profiles"
PRODUCT_MAPPING_COLLECTION = "accounting_product_mappings"
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

BARCODE_TYPES = {
    "none": {
        "label": "No barcode",
        "description": "Barcode scanning is not required for this product.",
    },
    "ean_13": {
        "label": "EAN-13",
        "description": "13-digit retail barcode with a valid GS1 check digit.",
    },
    "upc_a": {
        "label": "UPC-A",
        "description": "12-digit retail barcode with a valid check digit.",
    },
    "gtin_8": {
        "label": "GTIN-8",
        "description": "8-digit compact trade-item barcode.",
    },
    "gtin_14": {
        "label": "GTIN-14",
        "description": "14-digit trade-item or packaging-level identifier.",
    },
    "code_128": {
        "label": "Code 128",
        "description": "Flexible alphanumeric warehouse barcode.",
    },
    "internal": {
        "label": "Internal barcode",
        "description": "AVPL-controlled internal product barcode.",
    },
}

MOVEMENT_TYPES = {
    "receipt": "Purchase / Receipt",
    "issue": "Sale / Issue",
    "adjustment": "Stock Adjustment",
}

VIEW_PERMISSION = "accounting.product_tracking.view"
CREATE_PERMISSION = "accounting.product_tracking.create"
EDIT_PERMISSION = "accounting.product_tracking.edit"
SUBMIT_PERMISSION = "accounting.product_tracking.submit"
WITHDRAW_PERMISSION = "accounting.product_tracking.withdraw"
CANCEL_PERMISSION = "accounting.product_tracking.cancel"
APPROVE_PERMISSION = "accounting.product_tracking.approve"
RETURN_PERMISSION = "accounting.product_tracking.return"
DEACTIVATE_PERMISSION = "accounting.product_tracking.deactivate"
REACTIVATE_PERMISSION = "accounting.product_tracking.reactivate"
VALIDATE_PERMISSION = "accounting.product_tracking.validate"


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

        if (
            same_keys
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


def ensure_product_tracking_indexes():
    collection = mongo.db[PROFILE_COLLECTION]

    _ensure_exact_index(
        collection,
        [("live_profile_key", ASCENDING)],
        name="accounting_product_tracking_live_unique",
        unique=True,
        partialFilterExpression={"live_profile_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [("barcode_unique_key", ASCENDING)],
        name="accounting_product_tracking_barcode_unique",
        unique=True,
        partialFilterExpression={"barcode_unique_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("source_product_name", ASCENDING),
        ],
        name="accounting_product_tracking_entity_status_name_idx",
    )
    _ensure_exact_index(
        collection,
        [("status", ASCENDING), ("submitted_at", ASCENDING)],
        name="accounting_product_tracking_approval_queue_idx",
    )
    _ensure_exact_index(
        collection,
        [("product_mapping_id", ASCENDING), ("updated_at", DESCENDING)],
        name="accounting_product_tracking_mapping_updated_idx",
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


def _int_value(value, *, field_name, minimum=0, maximum=100000, default=0):
    if value in (None, ""):
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum} and {maximum}."
        )
    return parsed


def _parse_date(value, *, field_name, required=False):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field_name} is required.")
        return None

    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must use YYYY-MM-DD format.") from exc


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
        raise PermissionError(
            "Inactive users cannot manage product tracking controls."
        )

    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError(
            "You are not authorized to perform this product tracking action."
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
        raise PermissionError(
            access.get("message") or "Accounting access is disabled."
        )

    if actor.get("resolved_role") != "super_admin":
        allowed_entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError(
                "You do not have access to this Accounting entity."
            )

    if not has_accounting_permission(access, permission):
        raise PermissionError("You do not have permission to perform this action.")
    return access


def _expected_version(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "The tracking profile version is invalid. Refresh and try again."
        ) from exc


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


def _record_audit(
    document,
    actor,
    action,
    previous_status=None,
    remarks="",
    changed_fields=None,
):
    timestamp = now_utc()
    audit = {
        "module": "accounting",
        "submodule": "product_tracking",
        "action": action,
        "accounting_entity_id": document["accounting_entity_id"],
        "accounting_entity_id_str": str(document["accounting_entity_id"]),
        "entity_type": "accounting_product_tracking_profile",
        "entity_id": document["_id"],
        "entity_id_str": str(document["_id"]),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "previous_status": previous_status,
        "new_status": document.get("status"),
        "metadata": {
            "profile_code": document.get("profile_code"),
            "mapping_code": document.get("mapping_code"),
            "source_product_id": document.get("source_product_id_str"),
            "source_product_name": document.get("source_product_name"),
            "barcode_type": document.get("barcode_type"),
            "batch_tracking_enabled": document.get("batch_tracking_enabled"),
            "expiry_tracking_enabled": document.get("expiry_tracking_enabled"),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": _clean_text(remarks, 1500),
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
    except Exception as exc:
        mongo.db[PROFILE_COLLECTION].update_one(
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
# Barcode and product-mapping validation
# ---------------------------------------------------------------------------


def _gs1_check_digit_is_valid(value):
    if not value.isdigit() or len(value) < 2:
        return False
    digits = [int(character) for character in value]
    supplied = digits.pop()
    total = 0
    for index, digit in enumerate(reversed(digits), start=1):
        total += digit * (3 if index % 2 == 1 else 1)
    expected = (10 - (total % 10)) % 10
    return supplied == expected


def _normalize_barcode(barcode_type, value):
    barcode_type = str(barcode_type or "none").strip().lower()
    if barcode_type not in BARCODE_TYPES:
        raise ValueError("Select a supported barcode type.")

    barcode = _clean_text(value, 64)
    if barcode_type == "none":
        if barcode:
            raise ValueError(
                "Select a barcode type before entering a barcode value."
            )
        return ""

    if not barcode:
        raise ValueError("Barcode value is required for the selected barcode type.")

    compact = re.sub(r"\s+", "", barcode).upper()
    numeric_lengths = {
        "ean_13": 13,
        "upc_a": 12,
        "gtin_8": 8,
        "gtin_14": 14,
    }
    if barcode_type in numeric_lengths:
        required_length = numeric_lengths[barcode_type]
        if not compact.isdigit() or len(compact) != required_length:
            raise ValueError(
                f"{BARCODE_TYPES[barcode_type]['label']} must contain exactly "
                f"{required_length} digits."
            )
        if not _gs1_check_digit_is_valid(compact):
            raise ValueError(
                f"{BARCODE_TYPES[barcode_type]['label']} has an invalid check digit."
            )
        return compact

    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._/\-]{2,63}", compact):
        raise ValueError(
            "Alphanumeric barcodes must be 3–64 characters and may use letters, "
            "numbers, period, underscore, slash or hyphen."
        )
    return compact


def _get_active_product_mapping(mapping_id, entity_id=None):
    object_id = _to_object_id(mapping_id)
    if not object_id:
        raise ValueError("Invalid product Accounting mapping.")

    query = {
        "_id": object_id,
        "status": "active",
        "is_active": True,
        "is_accounting_eligible": True,
        "is_deleted": {"$ne": True},
    }
    entity_object_id = _to_object_id(entity_id) if entity_id else None
    if entity_object_id:
        query["accounting_entity_id"] = entity_object_id

    mapping = mongo.db[PRODUCT_MAPPING_COLLECTION].find_one(query)
    if not mapping:
        raise ValueError(
            "The selected product must have an active approved Accounting mapping."
        )
    return mapping


def _profile_payload(entity_id, form, *, existing=None):
    mapping_id = form.get("product_mapping_id") or (
        existing.get("product_mapping_id_str") if existing else None
    )
    mapping = _get_active_product_mapping(mapping_id, entity_id)

    barcode_type = str(form.get("barcode_type") or "none").strip().lower()
    primary_barcode = _normalize_barcode(
        barcode_type,
        form.get("primary_barcode"),
    )
    barcode_required = _bool_value(
        form.get("barcode_required_on_transaction"), default=False
    )
    if barcode_type == "none":
        barcode_required = False

    batch_tracking_enabled = _bool_value(
        form.get("batch_tracking_enabled"), default=False
    )
    batch_number_required = _bool_value(
        form.get("batch_number_required"), default=False
    )
    manufacturing_date_required = _bool_value(
        form.get("manufacturing_date_required"), default=False
    )
    expiry_tracking_enabled = _bool_value(
        form.get("expiry_tracking_enabled"), default=False
    )
    expiry_date_required = _bool_value(
        form.get("expiry_date_required"), default=False
    )

    if expiry_tracking_enabled:
        batch_tracking_enabled = True
    if not batch_tracking_enabled:
        batch_number_required = False
        manufacturing_date_required = False
    if not expiry_tracking_enabled:
        expiry_date_required = False

    shelf_life_days = _int_value(
        form.get("shelf_life_days"),
        field_name="Shelf life days",
        minimum=0,
        maximum=3650,
        default=0,
    )
    minimum_remaining_shelf_life_days = _int_value(
        form.get("minimum_remaining_shelf_life_days"),
        field_name="Minimum remaining shelf life",
        minimum=0,
        maximum=3650,
        default=0,
    )

    if not expiry_tracking_enabled and (
        shelf_life_days or minimum_remaining_shelf_life_days
    ):
        raise ValueError(
            "Enable expiry tracking before setting shelf-life controls."
        )
    if (
        shelf_life_days
        and minimum_remaining_shelf_life_days > shelf_life_days
    ):
        raise ValueError(
            "Minimum remaining shelf life cannot exceed the configured shelf life."
        )
    if manufacturing_date_required and not batch_tracking_enabled:
        raise ValueError(
            "Manufacturing date control requires batch tracking."
        )
    if expiry_date_required and not expiry_tracking_enabled:
        raise ValueError(
            "Expiry date requirement needs expiry tracking to be enabled."
        )
    if (batch_tracking_enabled or expiry_tracking_enabled) and (
        mapping.get("inventory_tracking_enabled") is False
    ):
        raise ValueError(
            "Batch or expiry controls require inventory tracking on the product Accounting mapping."
        )

    if not any(
        [
            bool(primary_barcode),
            barcode_required,
            batch_tracking_enabled,
            expiry_tracking_enabled,
        ]
    ):
        raise ValueError(
            "Select at least one barcode, batch or expiry control. Products that need no control do not require a tracking profile."
        )

    return {
        "product_mapping_id": mapping["_id"],
        "product_mapping_id_str": str(mapping["_id"]),
        "mapping_code": mapping.get("mapping_code") or "",
        "source_product_id": mapping.get("source_product_id"),
        "source_product_id_str": mapping.get("source_product_id_str")
        or str(mapping.get("source_product_id") or ""),
        "source_product_name": mapping.get("source_product_name") or "",
        "source_product_category": mapping.get("source_product_category") or "",
        "base_unit_id": mapping.get("base_unit_id"),
        "base_unit_id_str": mapping.get("base_unit_id_str")
        or str(mapping.get("base_unit_id") or ""),
        "base_unit_code": mapping.get("base_unit_code") or "",
        "base_unit_name": mapping.get("base_unit_name") or "",
        "inventory_tracking_enabled": mapping.get("inventory_tracking_enabled")
        is not False,
        "barcode_type": barcode_type,
        "barcode_type_label": BARCODE_TYPES[barcode_type]["label"],
        "primary_barcode": primary_barcode,
        "primary_barcode_normalized": primary_barcode,
        "barcode_required_on_transaction": barcode_required,
        "batch_tracking_enabled": batch_tracking_enabled,
        "batch_number_required": batch_number_required,
        "manufacturing_date_required": manufacturing_date_required,
        "expiry_tracking_enabled": expiry_tracking_enabled,
        "expiry_date_required": expiry_date_required,
        "shelf_life_days": shelf_life_days,
        "minimum_remaining_shelf_life_days": minimum_remaining_shelf_life_days,
        "block_expired_stock": True,
        "tracking_note": _clean_text(form.get("tracking_note"), 1500),
    }


def _changed_fields(existing, payload):
    return [key for key, value in payload.items() if existing.get(key) != value]


def _get_profile(profile_id):
    object_id = _to_object_id(profile_id)
    if not object_id:
        raise ValueError("Invalid product tracking profile.")
    document = mongo.db[PROFILE_COLLECTION].find_one(
        {"_id": object_id, "is_deleted": {"$ne": True}}
    )
    if not document:
        raise ValueError("Product tracking profile was not found.")
    return document


def _assert_maker(document, actor):
    if str(document.get("created_by") or "") != str(actor.get("_id") or ""):
        raise PermissionError(
            "Only the original maker can change this tracking profile draft."
        )


def _barcode_unique_key(entity_id, primary_barcode, status):
    if not primary_barcode or status == STATUS_CANCELLED:
        return None
    return f"{entity_id}:{primary_barcode}"


# ---------------------------------------------------------------------------
# Serialization and dashboard overview
# ---------------------------------------------------------------------------


def serialize_product_tracking_profile(document):
    return {
        "id": str(document.get("_id") or ""),
        "profile_code": document.get("profile_code") or "",
        "mapping_code": document.get("mapping_code") or "",
        "product_mapping_id": document.get("product_mapping_id_str")
        or str(document.get("product_mapping_id") or ""),
        "source_product_id": document.get("source_product_id_str")
        or str(document.get("source_product_id") or ""),
        "source_product_name": document.get("source_product_name") or "",
        "source_product_category": document.get("source_product_category") or "",
        "base_unit_code": document.get("base_unit_code") or "",
        "base_unit_name": document.get("base_unit_name") or "",
        "barcode_type": document.get("barcode_type") or "none",
        "barcode_type_label": document.get("barcode_type_label")
        or BARCODE_TYPES.get(document.get("barcode_type") or "none", {}).get(
            "label", "No barcode"
        ),
        "primary_barcode": document.get("primary_barcode") or "",
        "barcode_required_on_transaction": document.get(
            "barcode_required_on_transaction"
        )
        is True,
        "batch_tracking_enabled": document.get("batch_tracking_enabled") is True,
        "batch_number_required": document.get("batch_number_required") is True,
        "manufacturing_date_required": document.get(
            "manufacturing_date_required"
        )
        is True,
        "expiry_tracking_enabled": document.get("expiry_tracking_enabled") is True,
        "expiry_date_required": document.get("expiry_date_required") is True,
        "shelf_life_days": int(document.get("shelf_life_days") or 0),
        "minimum_remaining_shelf_life_days": int(
            document.get("minimum_remaining_shelf_life_days") or 0
        ),
        "block_expired_stock": document.get("block_expired_stock") is not False,
        "tracking_note": document.get("tracking_note") or "",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(
            document.get("status"), document.get("status") or "Draft"
        ),
        "is_active": document.get("is_active") is True,
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


def get_product_tracking_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    if not entity_id:
        return {
            "eligible_mappings": [],
            "all_active_mappings": [],
            "barcode_types": [
                {"code": code, **details}
                for code, details in BARCODE_TYPES.items()
            ],
            "movement_types": [
                {"code": code, "label": label}
                for code, label in MOVEMENT_TYPES.items()
            ],
            "status_labels": dict(STATUS_LABELS),
        }

    ensure_product_tracking_indexes()
    existing_profiles = list(
        mongo.db[PROFILE_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "status": {"$ne": STATUS_CANCELLED},
                "is_deleted": {"$ne": True},
            },
            {"product_mapping_id": 1},
        )
    )
    profiled_mapping_ids = {
        str(row.get("product_mapping_id") or "") for row in existing_profiles
    }

    mappings = list(
        mongo.db[PRODUCT_MAPPING_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "status": "active",
                "is_active": True,
                "is_accounting_eligible": True,
                "is_deleted": {"$ne": True},
            }
        ).sort([("source_product_name", ASCENDING), ("mapping_code", ASCENDING)])
    )
    serialized = [
        {
            "id": str(row["_id"]),
            "mapping_code": row.get("mapping_code") or "",
            "source_product_name": row.get("source_product_name") or "",
            "source_product_category": row.get("source_product_category") or "",
            "base_unit_code": row.get("base_unit_code") or "",
            "base_unit_name": row.get("base_unit_name") or "",
            "inventory_tracking_enabled": row.get("inventory_tracking_enabled")
            is not False,
        }
        for row in mappings
    ]

    return {
        "eligible_mappings": [
            row for row in serialized if row["id"] not in profiled_mapping_ids
        ],
        "all_active_mappings": serialized,
        "barcode_types": [
            {"code": code, **details}
            for code, details in BARCODE_TYPES.items()
        ],
        "movement_types": [
            {"code": code, "label": label}
            for code, label in MOVEMENT_TYPES.items()
        ],
        "status_labels": dict(STATUS_LABELS),
    }


def get_product_tracking_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_product_tracking_indexes()

    rows = [
        serialize_product_tracking_profile(row)
        for row in mongo.db[PROFILE_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "is_deleted": {"$ne": True},
            }
        )
        .sort([("updated_at", DESCENDING), ("source_product_name", ASCENDING)])
    ]
    options = get_product_tracking_option_catalog(entity["_id"])

    by_status = {
        status: [row for row in rows if row["status"] == status]
        for status in STATUS_LABELS
    }
    active_rows = by_status[STATUS_ACTIVE]

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("legal_name")
        or entity.get("entity_name")
        or "AVPL",
        "rows": rows,
        "working_rows": by_status[STATUS_DRAFT] + by_status[STATUS_RETURNED],
        "pending_rows": by_status[STATUS_PENDING_APPROVAL],
        "active_rows": active_rows,
        "inactive_rows": by_status[STATUS_INACTIVE],
        "cancelled_rows": by_status[STATUS_CANCELLED],
        "counts": {
            status: len(by_status[status]) for status in STATUS_LABELS
        },
        "total_profile_count": len(rows),
        "active_profile_count": len(active_rows),
        "barcode_profile_count": len(
            [row for row in active_rows if row["primary_barcode"]]
        ),
        "batch_profile_count": len(
            [row for row in active_rows if row["batch_tracking_enabled"]]
        ),
        "expiry_profile_count": len(
            [row for row in active_rows if row["expiry_tracking_enabled"]]
        ),
        "unconfigured_mapping_count": len(options["eligible_mappings"]),
        "audit_recovery_count": len(
            [row for row in rows if row["audit_sync_required"]]
        ),
        "options": options,
        "prerequisites": {
            "has_accounting_ready_products": bool(options["all_active_mappings"]),
            "has_unconfigured_products": bool(options["eligible_mappings"]),
            "is_ready": bool(options["all_active_mappings"]),
        },
    }


# ---------------------------------------------------------------------------
# Maker-checker workflow
# ---------------------------------------------------------------------------


def create_product_tracking_profile(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_product_tracking_indexes()

    payload = _profile_payload(entity["_id"], form)
    live_profile_key = f"{entity['_id']}:{payload['product_mapping_id']}"
    if mongo.db[PROFILE_COLLECTION].find_one({"live_profile_key": live_profile_key}):
        raise ValueError(
            "This product already has a live tracking profile. Open the existing profile instead."
        )

    timestamp = now_utc()
    profile_code = f"TRK-{payload['mapping_code'].replace('APM-', '')}"
    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "profile_code": profile_code,
        "live_profile_key": live_profile_key,
        "barcode_unique_key": _barcode_unique_key(
            entity["_id"], payload["primary_barcode"], STATUS_DRAFT
        ),
        **payload,
        "status": STATUS_DRAFT,
        "is_active": False,
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
                "tracking_profile_draft_created",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                note=payload.get("tracking_note"),
            )
        ],
    }

    try:
        result = mongo.db[PROFILE_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(
            "The product already has a live profile or the barcode is already assigned to another product."
        ) from exc

    document["_id"] = result.inserted_id
    _record_audit(
        document,
        actor,
        "create_product_tracking_profile",
        remarks=payload.get("tracking_note"),
    )
    return {
        "profile": serialize_product_tracking_profile(document),
        "message": f"Product tracking profile {profile_code} created as Draft.",
    }


def update_product_tracking_profile(
    profile_id,
    actor_user_id,
    expected_version,
    form,
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    document = _get_profile(profile_id)
    entity = _assert_active_avpl_entity(document.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    _assert_maker(document, actor)

    if document.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only Draft or Returned tracking profiles can be edited.")

    current_version = int(document.get("version") or 1)
    if _expected_version(expected_version) != current_version:
        raise RuntimeError(
            "This tracking profile changed in another session. Refresh and try again."
        )

    payload = _profile_payload(entity["_id"], form, existing=document)
    if str(payload["product_mapping_id"]) != str(document.get("product_mapping_id")):
        raise ValueError(
            "The product Accounting mapping cannot be changed after profile creation."
        )

    changed_fields = _changed_fields(document, payload)
    timestamp = now_utc()
    event = _workflow_event(
        "tracking_profile_draft_updated",
        actor,
        previous_status=document.get("status"),
        new_status=STATUS_DRAFT,
        note=payload.get("tracking_note"),
        changed_fields=changed_fields,
    )

    try:
        result = mongo.db[PROFILE_COLLECTION].update_one(
            {"_id": document["_id"], "version": current_version},
            {
                "$set": {
                    **payload,
                    "barcode_unique_key": _barcode_unique_key(
                        entity["_id"], payload["primary_barcode"], STATUS_DRAFT
                    ),
                    "status": STATUS_DRAFT,
                    "is_active": False,
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
    except DuplicateKeyError as exc:
        raise ValueError(
            "The barcode is already assigned to another live product tracking profile."
        ) from exc

    if result.modified_count != 1:
        raise RuntimeError(
            "This tracking profile changed in another session. Refresh and try again."
        )

    updated = _get_profile(document["_id"])
    _record_audit(
        updated,
        actor,
        "update_product_tracking_profile",
        previous_status=document.get("status"),
        remarks=payload.get("tracking_note"),
        changed_fields=changed_fields,
    )
    return {
        "profile": serialize_product_tracking_profile(updated),
        "message": "Product tracking profile draft updated.",
    }


def _transition(
    profile_id,
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
    document = _get_profile(profile_id)
    entity = _assert_active_avpl_entity(document.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)

    current_status = document.get("status") or STATUS_DRAFT
    if current_status not in set(source_statuses):
        raise ValueError(
            "This action is not available while the tracking profile is "
            f"{STATUS_LABELS.get(current_status, current_status)}."
        )

    current_version = int(document.get("version") or 1)
    if _expected_version(expected_version) != current_version:
        raise RuntimeError(
            "This tracking profile changed in another session. Refresh and try again."
        )

    if action in {"submit", "withdraw", "cancel"}:
        _assert_maker(document, actor)

    if action in {"approve", "return"} and str(document.get("created_by")) == str(
        actor["_id"]
    ):
        raise PermissionError(
            "The maker cannot approve or return their own product tracking profile."
        )

    clean_reason = _clean_text(reason, 1000)
    clean_note = _clean_text(note, 1000)
    if action in {"withdraw", "cancel", "return", "deactivate", "reactivate"} and not clean_reason:
        raise ValueError("A reason is required for this action.")

    if action in {"submit", "approve", "reactivate"}:
        current_form = {
            "product_mapping_id": document.get("product_mapping_id_str"),
            "barcode_type": document.get("barcode_type") or "none",
            "primary_barcode": document.get("primary_barcode") or "",
            "barcode_required_on_transaction": document.get(
                "barcode_required_on_transaction"
            ),
            "batch_tracking_enabled": document.get("batch_tracking_enabled"),
            "batch_number_required": document.get("batch_number_required"),
            "manufacturing_date_required": document.get(
                "manufacturing_date_required"
            ),
            "expiry_tracking_enabled": document.get("expiry_tracking_enabled"),
            "expiry_date_required": document.get("expiry_date_required"),
            "shelf_life_days": document.get("shelf_life_days"),
            "minimum_remaining_shelf_life_days": document.get(
                "minimum_remaining_shelf_life_days"
            ),
            "tracking_note": document.get("tracking_note") or "",
        }
        _profile_payload(entity["_id"], current_form, existing=document)

    timestamp = now_utc()
    set_fields = {
        "status": target_status,
        "is_active": target_status == STATUS_ACTIVE,
        "barcode_unique_key": _barcode_unique_key(
            entity["_id"],
            document.get("primary_barcode") or "",
            target_status,
        ),
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
                "live_profile_key": None,
                "barcode_unique_key": None,
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
        f"tracking_profile_{action}",
        actor,
        previous_status=current_status,
        new_status=target_status,
        reason=clean_reason,
        note=clean_note,
    )

    try:
        result = mongo.db[PROFILE_COLLECTION].update_one(
            {"_id": document["_id"], "version": current_version},
            {
                "$set": set_fields,
                "$push": {"change_history": event},
            },
        )
    except DuplicateKeyError as exc:
        raise ValueError(
            "The barcode is already assigned to another live product tracking profile."
        ) from exc

    if result.modified_count != 1:
        raise RuntimeError(
            "This tracking profile changed in another session. Refresh and try again."
        )

    updated = _get_profile(document["_id"])
    _record_audit(
        updated,
        actor,
        f"{action}_product_tracking_profile",
        previous_status=current_status,
        remarks=clean_reason or clean_note,
    )
    return {
        "profile": serialize_product_tracking_profile(updated),
        "message": (
            f"Product tracking profile {STATUS_LABELS.get(target_status, target_status)}."
        ),
    }


def submit_product_tracking_profile(profile_id, actor_user_id, expected_version, note=""):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="submit",
        permission=SUBMIT_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses=EDITABLE_STATUSES,
        target_status=STATUS_PENDING_APPROVAL,
        note=note,
    )


def withdraw_product_tracking_profile(profile_id, actor_user_id, expected_version, reason):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="withdraw",
        permission=WITHDRAW_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_DRAFT,
        reason=reason,
    )


def cancel_product_tracking_profile(profile_id, actor_user_id, expected_version, reason):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="cancel",
        permission=CANCEL_PERMISSION,
        allowed_roles={"accounts"},
        source_statuses=EDITABLE_STATUSES,
        target_status=STATUS_CANCELLED,
        reason=reason,
    )


def approve_product_tracking_profile(profile_id, actor_user_id, expected_version, note=""):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="approve",
        permission=APPROVE_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_ACTIVE,
        note=note,
    )


def return_product_tracking_profile(profile_id, actor_user_id, expected_version, reason):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="return",
        permission=RETURN_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_PENDING_APPROVAL},
        target_status=STATUS_RETURNED,
        reason=reason,
    )


def deactivate_product_tracking_profile(profile_id, actor_user_id, expected_version, reason):
    return _transition(
        profile_id,
        actor_user_id,
        expected_version,
        action="deactivate",
        permission=DEACTIVATE_PERMISSION,
        allowed_roles={"avpl_admin", "super_admin"},
        source_statuses={STATUS_ACTIVE},
        target_status=STATUS_INACTIVE,
        reason=reason,
    )


def reactivate_product_tracking_profile(profile_id, actor_user_id, expected_version, reason):
    return _transition(
        profile_id,
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
# Posting-ready validation contract
# ---------------------------------------------------------------------------


def get_product_tracking_profile_for_posting(
    accounting_entity_id,
    *,
    product_mapping_id=None,
    source_product_id=None,
    required=False,
):
    entity = _assert_active_avpl_entity(accounting_entity_id)
    query = {
        "accounting_entity_id": entity["_id"],
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": {"$ne": True},
    }

    if product_mapping_id:
        mapping_object_id = _to_object_id(product_mapping_id)
        if not mapping_object_id:
            raise ValueError("Invalid product Accounting mapping.")
        query["product_mapping_id"] = mapping_object_id
    elif source_product_id:
        source_object_id = _to_object_id(source_product_id)
        if not source_object_id:
            raise ValueError("Invalid source product.")
        query["source_product_id"] = source_object_id
    else:
        raise ValueError(
            "Product mapping or source product is required to resolve tracking controls."
        )

    document = mongo.db[PROFILE_COLLECTION].find_one(query)
    if not document:
        if required:
            raise ValueError(
                "An active approved tracking profile is required for this product."
            )
        return None
    return document


def validate_product_tracking_for_posting(
    profile,
    *,
    transaction_date,
    movement_type,
    scanned_barcode="",
    batch_number="",
    manufacturing_date=None,
    expiry_date=None,
):
    if not profile or profile.get("status") != STATUS_ACTIVE or profile.get("is_active") is not True:
        raise ValueError("An active approved product tracking profile is required.")

    movement_type = str(movement_type or "").strip().lower()
    if movement_type not in MOVEMENT_TYPES:
        raise ValueError("Select Purchase / Receipt, Sale / Issue or Stock Adjustment.")

    transaction_day = _parse_date(
        transaction_date,
        field_name="Transaction date",
        required=True,
    )
    normalized_batch = _clean_text(batch_number, 80).upper()
    if normalized_batch and not re.fullmatch(r"[A-Z0-9][A-Z0-9._/\-]{0,79}", normalized_batch):
        raise ValueError(
            "Batch number may contain letters, numbers, period, underscore, slash or hyphen."
        )

    configured_barcode = profile.get("primary_barcode_normalized") or ""
    scanned = re.sub(r"\s+", "", str(scanned_barcode or "")).upper()
    if profile.get("barcode_required_on_transaction") and not scanned:
        raise ValueError("Barcode scan is required for this product.")
    if scanned and configured_barcode and scanned != configured_barcode:
        raise ValueError("Scanned barcode does not match the approved product barcode.")

    if profile.get("batch_number_required") and not normalized_batch:
        raise ValueError("Batch number is required for this product.")
    if not profile.get("batch_tracking_enabled") and normalized_batch:
        raise ValueError("This product is not configured for batch tracking.")

    manufacturing_day = _parse_date(
        manufacturing_date,
        field_name="Manufacturing date",
        required=profile.get("manufacturing_date_required") is True,
    )
    expiry_day = _parse_date(
        expiry_date,
        field_name="Expiry date",
        required=profile.get("expiry_date_required") is True,
    )

    if not profile.get("expiry_tracking_enabled") and expiry_day:
        raise ValueError("This product is not configured for expiry tracking.")
    if manufacturing_day and manufacturing_day > transaction_day:
        raise ValueError("Manufacturing date cannot be after the transaction date.")
    if manufacturing_day and expiry_day and expiry_day < manufacturing_day:
        raise ValueError("Expiry date cannot be before the manufacturing date.")

    shelf_life_days = int(profile.get("shelf_life_days") or 0)
    calculated_expiry = None
    if shelf_life_days and manufacturing_day:
        calculated_expiry = manufacturing_day + timedelta(days=shelf_life_days)
        if expiry_day and expiry_day != calculated_expiry:
            raise ValueError(
                "Expiry date does not match the approved shelf-life policy. "
                f"Expected {calculated_expiry.isoformat()}."
            )
        if not expiry_day:
            expiry_day = calculated_expiry

    if expiry_day and profile.get("block_expired_stock") is not False:
        if expiry_day < transaction_day:
            raise ValueError("Expired stock cannot be received, issued or adjusted.")

    remaining_days = None
    minimum_remaining_days = int(
        profile.get("minimum_remaining_shelf_life_days") or 0
    )
    if expiry_day:
        remaining_days = (expiry_day - transaction_day).days
        if movement_type == "receipt" and remaining_days < minimum_remaining_days:
            raise ValueError(
                "The received batch does not meet the minimum remaining shelf-life requirement."
            )

    controls_applied = []
    if configured_barcode:
        controls_applied.append("Barcode")
    if profile.get("batch_tracking_enabled"):
        controls_applied.append("Batch")
    if profile.get("expiry_tracking_enabled"):
        controls_applied.append("Expiry")

    return {
        "is_valid": True,
        "message": "Tracking controls passed. The lot metadata is ready for future posting.",
        "movement_type": movement_type,
        "movement_type_label": MOVEMENT_TYPES[movement_type],
        "transaction_date": transaction_day.isoformat(),
        "profile_code": profile.get("profile_code") or "",
        "mapping_code": profile.get("mapping_code") or "",
        "source_product_name": profile.get("source_product_name") or "",
        "base_unit_code": profile.get("base_unit_code") or "",
        "scanned_barcode": scanned,
        "configured_barcode": configured_barcode,
        "batch_number": normalized_batch,
        "manufacturing_date": (
            manufacturing_day.isoformat() if manufacturing_day else ""
        ),
        "expiry_date": expiry_day.isoformat() if expiry_day else "",
        "calculated_expiry": (
            calculated_expiry.isoformat() if calculated_expiry else ""
        ),
        "remaining_shelf_life_days": remaining_days,
        "minimum_remaining_shelf_life_days": minimum_remaining_days,
        "controls_applied": controls_applied,
    }


def preview_product_tracking_validation(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VALIDATE_PERMISSION)

    profile_id = _to_object_id(form.get("profile_id"))
    if not profile_id:
        raise ValueError("Select an active product tracking profile.")

    profile = mongo.db[PROFILE_COLLECTION].find_one(
        {
            "_id": profile_id,
            "accounting_entity_id": entity["_id"],
            "status": STATUS_ACTIVE,
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    )
    if not profile:
        raise ValueError("The selected active product tracking profile was not found.")

    return validate_product_tracking_for_posting(
        profile,
        transaction_date=form.get("transaction_date"),
        movement_type=form.get("movement_type"),
        scanned_barcode=form.get("scanned_barcode"),
        batch_number=form.get("batch_number"),
        manufacturing_date=form.get("manufacturing_date"),
        expiry_date=form.get("expiry_date"),
    )
