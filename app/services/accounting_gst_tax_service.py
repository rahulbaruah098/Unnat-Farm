from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
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
from app.services.workflow_policy_service import workflow_is_streamlined


AVPL_ENTITY_CODE = "AVPL"

GST_COMPONENT_COLLECTION = "gst_tax_components"
GST_TAXABILITY_COLLECTION = "gst_taxability_masters"
GST_RATE_COLLECTION = "gst_tax_rates"
GST_LOCK_COLLECTION = "accounting_master_locks"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_ACTIVE = "active"
STATUS_RETIRED = "retired"
STATUS_CANCELLED = "cancelled"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
WORKING_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_APPROVAL,
    STATUS_RETURNED,
}

STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending approval",
    STATUS_RETURNED: "Returned for correction",
    STATUS_ACTIVE: "Active",
    STATUS_RETIRED: "Retired",
    STATUS_CANCELLED: "Cancelled",
}

VIEW_PERMISSION = "accounting.gst_tax.view"
BOOTSTRAP_PERMISSION = "accounting.gst_tax.bootstrap"
CREATE_PERMISSION = "accounting.gst_tax.create"
EDIT_PERMISSION = "accounting.gst_tax.edit"
SUBMIT_PERMISSION = "accounting.gst_tax.submit"
WITHDRAW_PERMISSION = "accounting.gst_tax.withdraw"
CANCEL_PERMISSION = "accounting.gst_tax.cancel"
APPROVE_PERMISSION = "accounting.gst_tax.approve"
RETURN_PERMISSION = "accounting.gst_tax.return"
RETIRE_PERMISSION = "accounting.gst_tax.retire"

RATE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_\-]{2,39}$")

GST_COMPONENT_DEFINITIONS = (
    {
        "system_key": "cgst",
        "component_code": "CGST",
        "name": "Central Goods and Services Tax",
        "short_name": "CGST",
        "jurisdiction": "central",
        "transaction_scope": "intra_state",
        "is_input_component": True,
        "is_output_component": True,
        "sort_order": 10,
        "description": (
            "Central GST component used together with SGST for intra-state supplies."
        ),
    },
    {
        "system_key": "sgst",
        "component_code": "SGST",
        "name": "State Goods and Services Tax",
        "short_name": "SGST",
        "jurisdiction": "state",
        "transaction_scope": "intra_state",
        "is_input_component": True,
        "is_output_component": True,
        "sort_order": 20,
        "description": (
            "State GST component used together with CGST for intra-state supplies."
        ),
    },
    {
        "system_key": "igst",
        "component_code": "IGST",
        "name": "Integrated Goods and Services Tax",
        "short_name": "IGST",
        "jurisdiction": "integrated",
        "transaction_scope": "inter_state",
        "is_input_component": True,
        "is_output_component": True,
        "sort_order": 30,
        "description": "Integrated GST component used for inter-state supplies.",
    },
)

GST_TAXABILITY_DEFINITIONS = (
    {
        "system_key": "taxable",
        "taxability_code": "TAXABLE",
        "name": "Taxable",
        "requires_tax_rate": True,
        "posts_tax_components": True,
        "allows_input_tax_credit": True,
        "sort_order": 10,
        "description": (
            "Supply attracts GST at an approved effective-dated tax rate."
        ),
    },
    {
        "system_key": "exempt",
        "taxability_code": "EXEMPT",
        "name": "Exempt",
        "requires_tax_rate": False,
        "posts_tax_components": False,
        "allows_input_tax_credit": False,
        "sort_order": 20,
        "description": (
            "Supply is exempt under the applicable notification or provision."
        ),
    },
    {
        "system_key": "nil_rated",
        "taxability_code": "NIL_RATED",
        "name": "Nil Rated",
        "requires_tax_rate": False,
        "posts_tax_components": False,
        "allows_input_tax_credit": False,
        "sort_order": 30,
        "description": "Supply is taxable in nature but carries a nil GST rate.",
    },
    {
        "system_key": "non_gst",
        "taxability_code": "NON_GST",
        "name": "Non-GST",
        "requires_tax_rate": False,
        "posts_tax_components": False,
        "allows_input_tax_credit": False,
        "sort_order": 40,
        "description": "Supply falls outside the GST levy.",
    },
)


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
    required_expire_after = options.get("expireAfterSeconds")

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
        existing_expire_after = metadata.get("expireAfterSeconds")

        if (
            same_keys
            and existing_unique == required_unique
            and existing_partial == required_partial
            and existing_expire_after == required_expire_after
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


def ensure_gst_tax_indexes():
    """Install Stage 4 Batch 1 indexes without dropping old indexes."""
    component_collection = mongo.db[GST_COMPONENT_COLLECTION]
    taxability_collection = mongo.db[GST_TAXABILITY_COLLECTION]
    rate_collection = mongo.db[GST_RATE_COLLECTION]
    lock_collection = mongo.db[GST_LOCK_COLLECTION]

    _ensure_exact_index(
        component_collection,
        [("accounting_entity_id", ASCENDING), ("system_key", ASCENDING)],
        name="gst_component_entity_system_key_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        component_collection,
        [("accounting_entity_id", ASCENDING), ("component_code", ASCENDING)],
        name="gst_component_entity_code_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        component_collection,
        [("accounting_entity_id", ASCENDING), ("sort_order", ASCENDING)],
        name="gst_component_entity_sort_idx",
    )

    _ensure_exact_index(
        taxability_collection,
        [("accounting_entity_id", ASCENDING), ("system_key", ASCENDING)],
        name="gst_taxability_entity_system_key_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        taxability_collection,
        [("accounting_entity_id", ASCENDING), ("taxability_code", ASCENDING)],
        name="gst_taxability_entity_code_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        taxability_collection,
        [("accounting_entity_id", ASCENDING), ("sort_order", ASCENDING)],
        name="gst_taxability_entity_sort_idx",
    )

    _ensure_exact_index(
        rate_collection,
        [("working_scope_key", ASCENDING)],
        name="gst_rate_working_scope_unique",
        unique=True,
        partialFilterExpression={"working_scope_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        rate_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("rate_code", ASCENDING),
            ("effective_from", ASCENDING),
        ],
        name="gst_rate_entity_code_effective_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        rate_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("effective_from", DESCENDING),
        ],
        name="gst_rate_entity_status_effective_idx",
    )
    _ensure_exact_index(
        rate_collection,
        [("status", ASCENDING), ("submitted_at", ASCENDING)],
        name="gst_rate_approval_queue_idx",
    )
    _ensure_exact_index(
        rate_collection,
        [("created_by", ASCENDING), ("updated_at", DESCENDING)],
        name="gst_rate_maker_updated_idx",
    )
    _ensure_exact_index(
        rate_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("total_rate", ASCENDING),
            ("status", ASCENDING),
        ],
        name="gst_rate_entity_percentage_status_idx",
    )

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


def _clean_single_line(value, label, maximum=180, required=True):
    cleaned = " ".join(str(value or "").strip().split())
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _clean_multiline(value, label, maximum=1200, required=False):
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    cleaned = "\n".join(line for line in lines if line).strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _parse_date(value, label, required=True):
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc
    return datetime.combine(parsed, time.min)


def _date_display(value):
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return ""


def _date_input(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return ""


def _parse_rate(value):
    text = str(value or "").strip()
    try:
        parsed = Decimal(text).quantize(Decimal("0.001"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("GST rate must be a valid percentage.") from exc
    if parsed <= Decimal("0") or parsed > Decimal("100"):
        raise ValueError("Taxable GST rate must be greater than 0 and not exceed 100%.")
    return parsed


def _decimal_value(value):
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _decimal_string(value, places="0.001"):
    return format(_decimal_value(value).quantize(Decimal(places)), "f").rstrip("0").rstrip(".") or "0"


def _sanitize_rate_code(value):
    code = re.sub(r"[^A-Z0-9_\-]", "_", str(value or "").strip().upper())
    code = re.sub(r"_+", "_", code).strip("_-")
    if not RATE_CODE_PATTERN.fullmatch(code):
        raise ValueError(
            "Rate code must start with a letter and use 3 to 40 uppercase letters, numbers, underscores or hyphens."
        )
    return code


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
        raise PermissionError("Inactive users cannot perform Accounting actions.")

    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError("You are not authorized to perform this GST master action.")

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
        raise PermissionError(
            "Your Accounting access mapping does not allow this GST master action."
        )
    return access


@contextmanager
def _rate_scope_lock(entity_id, rate_code, seconds=30):
    token = str(uuid4())
    timestamp = now_utc()
    expires_at = timestamp + timedelta(seconds=seconds)
    lock_key = f"gst-rate:{entity_id}:{rate_code}"

    try:
        mongo.db[GST_LOCK_COLLECTION].insert_one(
            {
                "lock_key": lock_key,
                "lock_token": token,
                "created_at": timestamp,
                "expires_at": expires_at,
            }
        )
    except DuplicateKeyError:
        current = mongo.db[GST_LOCK_COLLECTION].find_one({"lock_key": lock_key})
        if current and current.get("expires_at") and current["expires_at"] <= timestamp:
            result = mongo.db[GST_LOCK_COLLECTION].update_one(
                {
                    "_id": current["_id"],
                    "expires_at": current["expires_at"],
                },
                {
                    "$set": {
                        "lock_token": token,
                        "created_at": timestamp,
                        "expires_at": expires_at,
                    }
                },
            )
            if result.modified_count != 1:
                raise RuntimeError(
                    "Another GST rate update is in progress. Please retry shortly."
                )
        else:
            raise RuntimeError(
                "Another GST rate update is in progress. Please retry shortly."
            )

    try:
        yield
    finally:
        mongo.db[GST_LOCK_COLLECTION].delete_one(
            {"lock_key": lock_key, "lock_token": token}
        )


# ---------------------------------------------------------------------------
# Audit and serialization
# ---------------------------------------------------------------------------


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


def _record_audit(document, actor, action, previous_status=None, remarks="", changed_fields=None):
    timestamp = now_utc()
    audit = {
        "event_id": str(uuid4()),
        "accounting_entity_id": document.get("accounting_entity_id"),
        "entity_code": document.get("entity_code") or AVPL_ENTITY_CODE,
        "module": "gst_tax_master",
        "resource_type": "gst_tax_rate",
        "resource_id": document.get("_id"),
        "resource_id_str": str(document.get("_id") or ""),
        "resource_code": document.get("rate_code") or "",
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
        mongo.db[GST_RATE_COLLECTION].update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "audit_sync_required": False,
                    "last_audit_event_id": audit["event_id"],
                    "last_audited_at": timestamp,
                },
                "$unset": {"audit_sync_error": ""},
            },
        )
    except Exception as exc:
        mongo.db[GST_RATE_COLLECTION].update_one(
            {"_id": document["_id"]},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_error": str(exc)[:500],
                    "audit_recovery_required_at": timestamp,
                }
            },
        )


def _record_foundation_audit(entity, actor, action, summary):
    try:
        mongo.db.accounting_audit_logs.insert_one(
            {
                "event_id": str(uuid4()),
                "accounting_entity_id": entity["_id"],
                "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
                "module": "gst_tax_master",
                "resource_type": "gst_tax_foundation",
                "resource_id": entity["_id"],
                "resource_id_str": str(entity["_id"]),
                "resource_code": "GST_FOUNDATION",
                "action": action,
                "actor_user_id": actor["_id"],
                "actor_user_id_str": str(actor["_id"]),
                "actor_name": actor.get("resolved_name") or "",
                "actor_role": actor.get("resolved_role") or actor.get("role") or "",
                "summary": summary,
                "created_at": now_utc(),
            }
        )
    except Exception:
        # Foundation documents carry their own audit-recovery marker below.
        for collection_name in (
            GST_COMPONENT_COLLECTION,
            GST_TAXABILITY_COLLECTION,
        ):
            mongo.db[collection_name].update_many(
                {"accounting_entity_id": entity["_id"]},
                {
                    "$set": {
                        "audit_sync_required": True,
                        "audit_recovery_required_at": now_utc(),
                    }
                },
            )


def _component_definition_map():
    return {item["system_key"]: item for item in GST_COMPONENT_DEFINITIONS}


def _taxability_definition_map():
    return {item["system_key"]: item for item in GST_TAXABILITY_DEFINITIONS}


def serialize_gst_component(document):
    return {
        "id": str(document.get("_id") or ""),
        "system_key": document.get("system_key") or "",
        "component_code": document.get("component_code") or "",
        "name": document.get("name") or "",
        "short_name": document.get("short_name") or "",
        "jurisdiction": document.get("jurisdiction") or "",
        "transaction_scope": document.get("transaction_scope") or "",
        "transaction_scope_display": str(document.get("transaction_scope") or "").replace("_", " ").title(),
        "description": document.get("description") or "",
        "is_active": document.get("is_active") is True,
        "is_protected": document.get("is_protected") is True,
        "audit_sync_required": document.get("audit_sync_required") is True,
    }


def serialize_gst_taxability(document):
    return {
        "id": str(document.get("_id") or ""),
        "system_key": document.get("system_key") or "",
        "taxability_code": document.get("taxability_code") or "",
        "name": document.get("name") or "",
        "description": document.get("description") or "",
        "requires_tax_rate": document.get("requires_tax_rate") is True,
        "posts_tax_components": document.get("posts_tax_components") is True,
        "allows_input_tax_credit": document.get("allows_input_tax_credit") is True,
        "is_active": document.get("is_active") is True,
        "is_protected": document.get("is_protected") is True,
        "audit_sync_required": document.get("audit_sync_required") is True,
    }


def serialize_gst_tax_rate(document):
    total_rate = _decimal_value(document.get("total_rate"))
    half_rate = (total_rate / Decimal("2")).quantize(Decimal("0.001"))
    return {
        "id": str(document.get("_id") or ""),
        "rate_master_id": document.get("rate_master_id") or "",
        "rate_code": document.get("rate_code") or "",
        "name": document.get("name") or "",
        "description": document.get("description") or "",
        "taxability_code": document.get("taxability_code") or "TAXABLE",
        "total_rate": _decimal_string(total_rate),
        "total_rate_display": f"{_decimal_string(total_rate)}%",
        "cgst_rate": _decimal_string(document.get("cgst_rate", half_rate)),
        "sgst_rate": _decimal_string(document.get("sgst_rate", half_rate)),
        "igst_rate": _decimal_string(document.get("igst_rate", total_rate)),
        "effective_from": _date_input(document.get("effective_from")),
        "effective_from_display": _date_display(document.get("effective_from")),
        "effective_to": _date_input(document.get("effective_to")),
        "effective_to_display": _date_display(document.get("effective_to")) or "Open-ended",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(document.get("status"), document.get("status") or "Draft"),
        "is_active": document.get("is_active") is True,
        "is_current_today": _is_date_current(document, datetime.combine(date.today(), time.min)),
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": document.get("created_by_name") or "",
        "submitted_by_name": document.get("submitted_by_name") or "",
        "approved_by_name": document.get("approved_by_name") or "",
        "return_reason": document.get("return_reason") or "",
        "withdraw_reason": document.get("withdraw_reason") or "",
        "cancel_reason": document.get("cancel_reason") or "",
        "retirement_reason": document.get("retirement_reason") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "supersedes_rate_id": str(document.get("supersedes_rate_id") or ""),
        "supersedes_rate_code": document.get("supersedes_rate_code") or "",
        "superseded_by_rate_id": str(document.get("superseded_by_rate_id") or ""),
        "audit_sync_required": document.get("audit_sync_required") is True,
        "updated_at": document.get("updated_at"),
        "change_history": document.get("change_history") or [],
    }


# ---------------------------------------------------------------------------
# Protected GST foundation
# ---------------------------------------------------------------------------


def _canonical_foundation_document(entity, definition, kind, actor, existing=None):
    timestamp = now_utc()
    common = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "system_key": definition["system_key"],
        "name": definition["name"],
        "description": definition["description"],
        "sort_order": definition["sort_order"],
        "is_system": True,
        "is_protected": True,
        "name_locked": True,
        "deletion_locked": True,
        "is_active": True,
        "is_deleted": False,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "audit_sync_required": False,
    }
    if kind == "component":
        common.update(
            {
                "component_code": definition["component_code"],
                "short_name": definition["short_name"],
                "jurisdiction": definition["jurisdiction"],
                "transaction_scope": definition["transaction_scope"],
                "is_input_component": definition["is_input_component"],
                "is_output_component": definition["is_output_component"],
            }
        )
    else:
        common.update(
            {
                "taxability_code": definition["taxability_code"],
                "requires_tax_rate": definition["requires_tax_rate"],
                "posts_tax_components": definition["posts_tax_components"],
                "allows_input_tax_credit": definition["allows_input_tax_credit"],
            }
        )

    if not existing:
        common.update(
            {
                "version": 1,
                "created_by": actor["_id"],
                "created_by_str": str(actor["_id"]),
                "created_by_name": actor.get("resolved_name") or "",
                "created_at": timestamp,
                "change_history": [
                    _change_event(
                        f"create_protected_gst_{kind}",
                        actor,
                        new_status="active",
                    )
                ],
            }
        )
    return common


def _foundation_drift(existing, canonical, kind):
    fields = [
        "name",
        "description",
        "sort_order",
        "is_system",
        "is_protected",
        "name_locked",
        "deletion_locked",
        "is_active",
        "is_deleted",
    ]
    fields.extend(
        [
            "component_code",
            "short_name",
            "jurisdiction",
            "transaction_scope",
            "is_input_component",
            "is_output_component",
        ]
        if kind == "component"
        else [
            "taxability_code",
            "requires_tax_rate",
            "posts_tax_components",
            "allows_input_tax_credit",
        ]
    )
    return [field for field in fields if existing.get(field) != canonical.get(field)]


def seed_gst_tax_foundation(actor_user_id, accounting_entity_id=None):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], BOOTSTRAP_PERMISSION)
    ensure_gst_tax_indexes()

    created = 0
    repaired = 0
    unchanged = 0
    repair_details = []

    for collection_name, definitions, kind in (
        (GST_COMPONENT_COLLECTION, GST_COMPONENT_DEFINITIONS, "component"),
        (GST_TAXABILITY_COLLECTION, GST_TAXABILITY_DEFINITIONS, "taxability"),
    ):
        collection = mongo.db[collection_name]
        for definition in definitions:
            existing = collection.find_one(
                {
                    "accounting_entity_id": entity["_id"],
                    "system_key": definition["system_key"],
                }
            )
            canonical = _canonical_foundation_document(
                entity, definition, kind, actor, existing=existing
            )
            if not existing:
                try:
                    collection.insert_one(canonical)
                except DuplicateKeyError as exc:
                    raise RuntimeError(
                        f"A conflicting GST {kind} record already uses {definition['system_key']}. "
                        "No custom record was overwritten."
                    ) from exc
                created += 1
                continue

            changed_fields = _foundation_drift(existing, canonical, kind)
            if not changed_fields:
                unchanged += 1
                continue

            next_version = int(existing.get("version") or 1) + 1
            canonical["version"] = next_version
            result = collection.update_one(
                {"_id": existing["_id"], "version": existing.get("version", 1)},
                {
                    "$set": canonical,
                    "$push": {
                        "change_history": _change_event(
                            f"repair_protected_gst_{kind}",
                            actor,
                            previous_status="drifted",
                            new_status="active",
                            changed_fields=changed_fields,
                            remarks="Canonical protected GST master restored.",
                        )
                    },
                },
            )
            if result.matched_count != 1:
                raise RuntimeError(
                    f"The protected GST {kind} changed during repair. Refresh and retry."
                )
            repaired += 1
            repair_details.append(
                {
                    "kind": kind,
                    "system_key": definition["system_key"],
                    "changed_fields": changed_fields,
                }
            )

    summary = {
        "created": created,
        "repaired": repaired,
        "unchanged": unchanged,
        "repair_details": repair_details,
    }
    _record_foundation_audit(entity, actor, "synchronize_gst_foundation", summary)
    return {
        **summary,
        "message": (
            f"GST master foundation verified: {created} created, "
            f"{repaired} repaired and {unchanged} already correct."
        ),
    }


# ---------------------------------------------------------------------------
# GST rate payload and overlap controls
# ---------------------------------------------------------------------------


def _get_rate(rate_id):
    object_id = _to_object_id(rate_id)
    if not object_id:
        raise ValueError("Invalid GST tax rate.")
    document = mongo.db[GST_RATE_COLLECTION].find_one(
        {"_id": object_id, "is_deleted": {"$ne": True}}
    )
    if not document:
        raise ValueError("GST tax rate was not found.")
    return document


def _is_date_current(document, transaction_date):
    if document.get("status") != STATUS_ACTIVE or document.get("is_active") is not True:
        return False
    effective_from = document.get("effective_from")
    effective_to = document.get("effective_to")
    if not effective_from or transaction_date < effective_from:
        return False
    return not effective_to or transaction_date <= effective_to


def _assert_foundation_ready(entity_id):
    component_count = mongo.db[GST_COMPONENT_COLLECTION].count_documents(
        {
            "accounting_entity_id": entity_id,
            "is_system": True,
            "is_protected": True,
            "is_active": True,
            "is_deleted": False,
        }
    )
    taxability_count = mongo.db[GST_TAXABILITY_COLLECTION].count_documents(
        {
            "accounting_entity_id": entity_id,
            "is_system": True,
            "is_protected": True,
            "is_active": True,
            "is_deleted": False,
        }
    )
    if component_count != len(GST_COMPONENT_DEFINITIONS) or taxability_count != len(GST_TAXABILITY_DEFINITIONS):
        raise RuntimeError(
            "Initialize and verify the protected GST component and taxability masters first."
        )


def _working_scope_key(entity_id, rate_code):
    return f"{entity_id}:{rate_code}"


def _validate_rate_payload(raw_payload, entity, existing=None):
    name = _clean_single_line(raw_payload.get("name"), "Rate name", maximum=140)
    rate_code = _sanitize_rate_code(raw_payload.get("rate_code"))
    description = _clean_multiline(
        raw_payload.get("description"), "Description", maximum=1200
    )
    total_rate = _parse_rate(raw_payload.get("total_rate"))
    effective_from = _parse_date(raw_payload.get("effective_from"), "Effective from")
    effective_to = _parse_date(
        raw_payload.get("effective_to"), "Effective to", required=False
    )
    if effective_to and effective_to < effective_from:
        raise ValueError("Effective to cannot be earlier than effective from.")

    supersedes_rate_id = _to_object_id(raw_payload.get("supersedes_rate_id"))
    superseded_rate = None
    if supersedes_rate_id:
        superseded_rate = mongo.db[GST_RATE_COLLECTION].find_one(
            {
                "_id": supersedes_rate_id,
                "accounting_entity_id": entity["_id"],
                "status": STATUS_ACTIVE,
                "is_active": True,
                "is_deleted": False,
            }
        )
        if not superseded_rate:
            raise ValueError("The selected rate to supersede is no longer active.")
        if superseded_rate.get("rate_code") != rate_code:
            raise ValueError("A replacement rate must keep the same rate code.")
        if effective_from <= superseded_rate.get("effective_from"):
            raise ValueError(
                "A replacement rate must start after the rate it supersedes."
            )
        if existing and superseded_rate.get("_id") == existing.get("_id"):
            raise ValueError("A GST rate cannot supersede itself.")

    exclude_id = existing.get("_id") if existing else None
    working_query = {
        "accounting_entity_id": entity["_id"],
        "rate_code": rate_code,
        "status": {"$in": list(WORKING_STATUSES)},
        "is_deleted": {"$ne": True},
    }
    if exclude_id:
        working_query["_id"] = {"$ne": exclude_id}
    if mongo.db[GST_RATE_COLLECTION].find_one(working_query, {"_id": 1}):
        raise ValueError(
            "Another draft, returned or pending GST rate already uses this rate code."
        )

    half_rate = (total_rate / Decimal("2")).quantize(Decimal("0.001"))
    return {
        "name": name,
        "rate_code": rate_code,
        "description": description,
        "taxability_code": "TAXABLE",
        "total_rate": Decimal128(total_rate),
        "cgst_rate": Decimal128(half_rate),
        "sgst_rate": Decimal128(half_rate),
        "igst_rate": Decimal128(total_rate),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "supersedes_rate_id": superseded_rate.get("_id") if superseded_rate else None,
        "supersedes_rate_code": superseded_rate.get("rate_code") if superseded_rate else "",
        "component_strategy": {
            "intra_state": [
                {"component_code": "CGST", "rate": Decimal128(half_rate)},
                {"component_code": "SGST", "rate": Decimal128(half_rate)},
            ],
            "inter_state": [
                {"component_code": "IGST", "rate": Decimal128(total_rate)}
            ],
        },
    }


def _changed_fields(existing, payload):
    fields = []
    for field in (
        "name",
        "rate_code",
        "description",
        "total_rate",
        "cgst_rate",
        "sgst_rate",
        "igst_rate",
        "effective_from",
        "effective_to",
        "supersedes_rate_id",
    ):
        left = existing.get(field)
        right = payload.get(field)
        if field in {"total_rate", "cgst_rate", "sgst_rate", "igst_rate"}:
            changed = _decimal_value(left) != _decimal_value(right)
        else:
            changed = left != right
        if changed:
            fields.append(field)
    return fields


def _periods_overlap(start_a, end_a, start_b, end_b):
    infinity = datetime.max.replace(microsecond=0)
    return start_a <= (end_b or infinity) and start_b <= (end_a or infinity)


def _assert_no_active_overlap(entity_id, rate_code, effective_from, effective_to, exclude_ids=None):
    exclude_ids = {_to_object_id(item) for item in (exclude_ids or []) if _to_object_id(item)}
    query = {
        "accounting_entity_id": entity_id,
        "rate_code": rate_code,
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": False,
    }
    if exclude_ids:
        query["_id"] = {"$nin": list(exclude_ids)}

    for row in mongo.db[GST_RATE_COLLECTION].find(query):
        if _periods_overlap(
            effective_from,
            effective_to,
            row.get("effective_from"),
            row.get("effective_to"),
        ):
            raise ValueError(
                f"The effective period overlaps active {rate_code} rate "
                f"from {_date_display(row.get('effective_from'))} to "
                f"{_date_display(row.get('effective_to')) or 'open-ended'}."
            )


# ---------------------------------------------------------------------------
# GST tax-rate workflow
# ---------------------------------------------------------------------------


def create_gst_tax_rate(accounting_entity_id, actor_user_id, raw_payload):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_gst_tax_indexes()
    _assert_foundation_ready(entity["_id"])

    payload = _validate_rate_payload(raw_payload, entity)
    timestamp = now_utc()
    document = {
        **payload,
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "rate_master_id": str(uuid4()),
        "status": STATUS_DRAFT,
        "is_active": False,
        "is_deleted": False,
        "working_scope_key": _working_scope_key(entity["_id"], payload["rate_code"]),
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [
            _change_event("create_gst_tax_rate", actor, new_status=STATUS_DRAFT)
        ],
    }
    try:
        result = mongo.db[GST_RATE_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(
            "A GST tax-rate draft or effective period already uses this code."
        ) from exc

    created = _get_rate(result.inserted_id)
    _record_audit(created, actor, "create_gst_tax_rate")
    return {
        "rate": serialize_gst_tax_rate(created),
        "message": "GST tax-rate draft created.",
    }


def update_gst_tax_rate(rate_id, actor_user_id, raw_payload, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)

    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned GST rates can be edited.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the Accounts maker can edit this GST rate.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc

    payload = _validate_rate_payload(raw_payload, entity, existing=current)
    changed_fields = _changed_fields(current, payload)
    if not changed_fields:
        return {
            "rate": serialize_gst_tax_rate(current),
            "message": "No GST rate changes were detected.",
        }

    timestamp = now_utc()
    result = mongo.db[GST_RATE_COLLECTION].update_one(
        {
            "_id": current["_id"],
            "version": expected_version,
            "status": {"$in": list(EDITABLE_STATUSES)},
        },
        {
            "$set": {
                **payload,
                "working_scope_key": _working_scope_key(entity["_id"], payload["rate_code"]),
                "version": expected_version + 1,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$unset": {
                "return_reason": "",
                "returned_by": "",
                "returned_by_name": "",
                "returned_at": "",
            },
            "$push": {
                "change_history": _change_event(
                    "update_gst_tax_rate",
                    actor,
                    previous_status=current.get("status"),
                    new_status=current.get("status"),
                    changed_fields=changed_fields,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "update_gst_tax_rate",
        previous_status=current.get("status"),
        changed_fields=changed_fields,
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax-rate draft updated.",
    }


def submit_gst_tax_rate(rate_id, actor_user_id, expected_version, submission_note=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], SUBMIT_PERMISSION)

    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only a draft or returned GST rate can be submitted.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the Accounts maker can submit this GST rate.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    note = _clean_multiline(submission_note, "Submission note", maximum=1000)
    timestamp = now_utc()
    result = mongo.db[GST_RATE_COLLECTION].update_one(
        {
            "_id": current["_id"],
            "version": expected_version,
            "status": {"$in": list(EDITABLE_STATUSES)},
        },
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "is_active": False,
                "version": expected_version + 1,
                "submission_note": note,
                "submitted_by": actor["_id"],
                "submitted_by_name": actor.get("resolved_name") or "",
                "submitted_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "submit_gst_tax_rate",
                    actor,
                    previous_status=current.get("status"),
                    new_status=STATUS_PENDING_APPROVAL,
                    remarks=note,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "submit_gst_tax_rate",
        previous_status=current.get("status"),
        remarks=note,
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax rate submitted to AVPL Admin.",
    }


def withdraw_gst_tax_rate(rate_id, actor_user_id, expected_version, reason=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], WITHDRAW_PERMISSION)

    if current.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending GST rate can be withdrawn.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the Accounts maker can withdraw this GST rate.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    reason = _clean_multiline(reason, "Withdrawal reason", maximum=1000, required=True)
    timestamp = now_utc()
    result = mongo.db[GST_RATE_COLLECTION].update_one(
        {
            "_id": current["_id"],
            "version": expected_version,
            "status": STATUS_PENDING_APPROVAL,
        },
        {
            "$set": {
                "status": STATUS_DRAFT,
                "version": expected_version + 1,
                "withdraw_reason": reason,
                "withdrawn_by": actor["_id"],
                "withdrawn_by_name": actor.get("resolved_name") or "",
                "withdrawn_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "withdraw_gst_tax_rate",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_DRAFT,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "withdraw_gst_tax_rate",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=reason,
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax rate withdrawn to draft.",
    }


def cancel_gst_tax_rate(rate_id, actor_user_id, expected_version, reason=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], CANCEL_PERMISSION)

    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only a draft or returned GST rate can be cancelled.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the Accounts maker can cancel this GST rate.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    reason = _clean_multiline(reason, "Cancellation reason", maximum=1000, required=True)
    timestamp = now_utc()
    result = mongo.db[GST_RATE_COLLECTION].update_one(
        {
            "_id": current["_id"],
            "version": expected_version,
            "status": {"$in": list(EDITABLE_STATUSES)},
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
                "is_active": False,
                "version": expected_version + 1,
                "cancel_reason": reason,
                "cancelled_by": actor["_id"],
                "cancelled_by_name": actor.get("resolved_name") or "",
                "cancelled_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$unset": {"working_scope_key": ""},
            "$push": {
                "change_history": _change_event(
                    "cancel_gst_tax_rate",
                    actor,
                    previous_status=current.get("status"),
                    new_status=STATUS_CANCELLED,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "cancel_gst_tax_rate",
        previous_status=current.get("status"),
        remarks=reason,
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax-rate draft cancelled without deleting its history.",
    }


def approve_gst_tax_rate(rate_id, actor_user_id, expected_version, approval_note=""):
    streamlined = workflow_is_streamlined("accounting.gst_tax")
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "avpl_admin", "super_admin"} if streamlined else {"avpl_admin", "super_admin"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    permission = SUBMIT_PERMISSION if streamlined and actor.get("resolved_role") == "accounts" else APPROVE_PERMISSION
    _require_permission(actor, entity["_id"], permission)

    if current.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending GST rate can be approved.")
    if str(current.get("created_by")) == str(actor["_id"]) and not streamlined:
        raise PermissionError("The GST rate maker cannot approve the same record.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    note = _clean_multiline(approval_note, "Approval note", maximum=1000)

    with _rate_scope_lock(entity["_id"], current.get("rate_code")):
        current = _get_rate(rate_id)
        if current.get("version") != expected_version or current.get("status") != STATUS_PENDING_APPROVAL:
            raise RuntimeError("This GST rate changed. Refresh and try again.")

        superseded = None
        exclude_ids = [current["_id"]]
        if current.get("supersedes_rate_id"):
            superseded = mongo.db[GST_RATE_COLLECTION].find_one(
                {
                    "_id": current.get("supersedes_rate_id"),
                    "accounting_entity_id": entity["_id"],
                    "rate_code": current.get("rate_code"),
                    "status": STATUS_ACTIVE,
                    "is_active": True,
                    "is_deleted": False,
                }
            )
            if not superseded:
                raise ValueError("The rate being superseded is no longer active.")
            if current.get("effective_from") <= superseded.get("effective_from"):
                raise ValueError("Replacement effective date must follow the existing rate.")
            exclude_ids.append(superseded["_id"])

        _assert_no_active_overlap(
            entity["_id"],
            current.get("rate_code"),
            current.get("effective_from"),
            current.get("effective_to"),
            exclude_ids=exclude_ids,
        )

        timestamp = now_utc()
        if superseded:
            close_date = current["effective_from"] - timedelta(days=1)
            old_end = superseded.get("effective_to")
            if old_end and old_end < close_date:
                raise ValueError(
                    "The selected rate already ends before the replacement begins. "
                    "Create a separate non-overlapping rate instead."
                )
            old_version = int(superseded.get("version") or 1)
            old_result = mongo.db[GST_RATE_COLLECTION].update_one(
                {
                    "_id": superseded["_id"],
                    "version": old_version,
                    "status": STATUS_ACTIVE,
                },
                {
                    "$set": {
                        "effective_to": close_date,
                        "superseded_by_rate_id": current["_id"],
                        "superseded_by_rate_code": current.get("rate_code") or "",
                        "version": old_version + 1,
                        "updated_by": actor["_id"],
                        "updated_by_str": str(actor["_id"]),
                        "updated_by_name": actor.get("resolved_name") or "",
                        "updated_at": timestamp,
                    },
                    "$push": {
                        "change_history": _change_event(
                            "close_superseded_gst_rate_period",
                            actor,
                            previous_status=STATUS_ACTIVE,
                            new_status=STATUS_ACTIVE,
                            changed_fields=["effective_to", "superseded_by_rate_id"],
                            remarks=(
                                f"Effective period closed for approved replacement {current.get('rate_code')}."
                            ),
                        )
                    },
                },
            )
            if old_result.matched_count != 1:
                raise RuntimeError(
                    "The rate being superseded changed. Refresh and try again."
                )

        result = mongo.db[GST_RATE_COLLECTION].update_one(
            {
                "_id": current["_id"],
                "version": expected_version,
                "status": STATUS_PENDING_APPROVAL,
            },
            {
                "$set": {
                    "status": STATUS_ACTIVE,
                    "is_active": True,
                    "version": expected_version + 1,
                    "approval_note": note,
                    "approved_by": actor["_id"],
                    "approved_by_name": actor.get("resolved_name") or "",
                    "approved_at": timestamp,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": timestamp,
                },
                "$unset": {"working_scope_key": "", "return_reason": ""},
                "$push": {
                    "change_history": _change_event(
                        "approve_gst_tax_rate",
                        actor,
                        previous_status=STATUS_PENDING_APPROVAL,
                        new_status=STATUS_ACTIVE,
                        remarks=note,
                    )
                },
            },
        )
        if result.matched_count != 1:
            raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "approve_gst_tax_rate",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=note,
    )
    if superseded:
        refreshed_old = _get_rate(superseded["_id"])
        _record_audit(
            refreshed_old,
            actor,
            "close_superseded_gst_rate_period",
            previous_status=STATUS_ACTIVE,
            remarks=f"Superseded by {updated.get('rate_code')} effective {_date_display(updated.get('effective_from'))}.",
            changed_fields=["effective_to", "superseded_by_rate_id"],
        )

    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax rate approved and activated for its effective period.",
    }


def return_gst_tax_rate(rate_id, actor_user_id, expected_version, return_reason):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], RETURN_PERMISSION)

    if current.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending GST rate can be returned.")
    if str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The GST rate maker cannot review the same record.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    reason = _clean_multiline(
        return_reason, "Return reason", maximum=1000, required=True
    )
    timestamp = now_utc()
    result = mongo.db[GST_RATE_COLLECTION].update_one(
        {
            "_id": current["_id"],
            "version": expected_version,
            "status": STATUS_PENDING_APPROVAL,
        },
        {
            "$set": {
                "status": STATUS_RETURNED,
                "is_active": False,
                "version": expected_version + 1,
                "return_reason": reason,
                "returned_by": actor["_id"],
                "returned_by_name": actor.get("resolved_name") or "",
                "returned_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "return_gst_tax_rate",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_RETURNED,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "return_gst_tax_rate",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=reason,
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax rate returned to Accounts for correction.",
    }


def retire_gst_tax_rate(rate_id, actor_user_id, expected_version, effective_to, reason):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    current = _get_rate(rate_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], RETIRE_PERMISSION)

    if current.get("status") != STATUS_ACTIVE or current.get("is_active") is not True:
        raise ValueError("Only an active GST rate can be retired.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid GST rate version.") from exc
    retirement_date = _parse_date(effective_to, "Retirement effective date")
    if retirement_date < current.get("effective_from"):
        raise ValueError("Retirement date cannot precede the rate effective date.")
    today_start = datetime.combine(date.today(), time.min)
    if retirement_date > today_start:
        raise ValueError(
            "A GST rate cannot be retired with a future date. Use a replacement "
            "effective-dated rate to schedule a future change."
        )
    if current.get("effective_to") and retirement_date > current.get("effective_to"):
        raise ValueError("Retirement date cannot extend the approved effective period.")
    reason = _clean_multiline(reason, "Retirement reason", maximum=1000, required=True)

    with _rate_scope_lock(entity["_id"], current.get("rate_code")):
        timestamp = now_utc()
        result = mongo.db[GST_RATE_COLLECTION].update_one(
            {
                "_id": current["_id"],
                "version": expected_version,
                "status": STATUS_ACTIVE,
            },
            {
                "$set": {
                    "status": STATUS_RETIRED,
                    "is_active": False,
                    "effective_to": retirement_date,
                    "version": expected_version + 1,
                    "retirement_reason": reason,
                    "retired_by": actor["_id"],
                    "retired_by_name": actor.get("resolved_name") or "",
                    "retired_at": timestamp,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": timestamp,
                },
                "$push": {
                    "change_history": _change_event(
                        "retire_gst_tax_rate",
                        actor,
                        previous_status=STATUS_ACTIVE,
                        new_status=STATUS_RETIRED,
                        changed_fields=["effective_to"],
                        remarks=reason,
                    )
                },
            },
        )
        if result.matched_count != 1:
            raise RuntimeError("This GST rate changed. Refresh and try again.")

    updated = _get_rate(rate_id)
    _record_audit(
        updated,
        actor,
        "retire_gst_tax_rate",
        previous_status=STATUS_ACTIVE,
        remarks=reason,
        changed_fields=["effective_to"],
    )
    return {
        "rate": serialize_gst_tax_rate(updated),
        "message": "GST tax rate retired with its historical period preserved.",
    }


# ---------------------------------------------------------------------------
# Dashboard and future posting helpers
# ---------------------------------------------------------------------------


def _foundation_health(entity_id):
    component_map = _component_definition_map()
    taxability_map = _taxability_definition_map()

    components = list(
        mongo.db[GST_COMPONENT_COLLECTION].find(
            {"accounting_entity_id": entity_id}
        ).sort("sort_order", ASCENDING)
    )
    taxabilities = list(
        mongo.db[GST_TAXABILITY_COLLECTION].find(
            {"accounting_entity_id": entity_id}
        ).sort("sort_order", ASCENDING)
    )

    missing_components = []
    drifted_components = []
    by_component_key = {row.get("system_key"): row for row in components}
    for key, definition in component_map.items():
        row = by_component_key.get(key)
        if not row:
            missing_components.append(key)
            continue
        canonical = _canonical_foundation_document(
            {"_id": entity_id, "entity_code": AVPL_ENTITY_CODE},
            definition,
            "component",
            {"_id": ObjectId("000000000000000000000000"), "resolved_name": "health-check"},
            existing=row,
        )
        changed = _foundation_drift(row, canonical, "component")
        if changed:
            drifted_components.append({"system_key": key, "changed_fields": changed})

    missing_taxabilities = []
    drifted_taxabilities = []
    by_taxability_key = {row.get("system_key"): row for row in taxabilities}
    for key, definition in taxability_map.items():
        row = by_taxability_key.get(key)
        if not row:
            missing_taxabilities.append(key)
            continue
        canonical = _canonical_foundation_document(
            {"_id": entity_id, "entity_code": AVPL_ENTITY_CODE},
            definition,
            "taxability",
            {"_id": ObjectId("000000000000000000000000"), "resolved_name": "health-check"},
            existing=row,
        )
        changed = _foundation_drift(row, canonical, "taxability")
        if changed:
            drifted_taxabilities.append({"system_key": key, "changed_fields": changed})

    return {
        "components": [serialize_gst_component(row) for row in components],
        "taxabilities": [serialize_gst_taxability(row) for row in taxabilities],
        "required_component_count": len(GST_COMPONENT_DEFINITIONS),
        "present_component_count": len(components),
        "missing_components": missing_components,
        "drifted_components": drifted_components,
        "required_taxability_count": len(GST_TAXABILITY_DEFINITIONS),
        "present_taxability_count": len(taxabilities),
        "missing_taxabilities": missing_taxabilities,
        "drifted_taxabilities": drifted_taxabilities,
        "is_complete": (
            not missing_components
            and not drifted_components
            and not missing_taxabilities
            and not drifted_taxabilities
        ),
        "audit_recovery_count": sum(
            1
            for row in components + taxabilities
            if row.get("audit_sync_required") is True
        ),
    }


def get_gst_tax_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    active_rates = []
    if entity_id:
        active_rates = [
            serialize_gst_tax_rate(row)
            for row in mongo.db[GST_RATE_COLLECTION].find(
                {
                    "accounting_entity_id": entity_id,
                    "status": STATUS_ACTIVE,
                    "is_active": True,
                    "is_deleted": False,
                }
            ).sort([("rate_code", ASCENDING), ("effective_from", DESCENDING)])
        ]
    return {
        "status_labels": dict(STATUS_LABELS),
        "taxability_types": {
            item["taxability_code"]: item["name"]
            for item in GST_TAXABILITY_DEFINITIONS
        },
        "active_rates": active_rates,
        "today": date.today().isoformat(),
    }


def get_gst_tax_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_gst_tax_indexes()

    foundation = _foundation_health(entity["_id"])
    rows = list(
        mongo.db[GST_RATE_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_deleted": {"$ne": True},
            }
        ).sort([("rate_code", ASCENDING), ("effective_from", DESCENDING)])
    )
    serialized = [serialize_gst_tax_rate(row) for row in rows]
    counts = {status: 0 for status in STATUS_LABELS}
    for row in rows:
        counts[row.get("status") or STATUS_DRAFT] = counts.get(
            row.get("status") or STATUS_DRAFT, 0
        ) + 1

    today_dt = datetime.combine(date.today(), time.min)
    current_active = [
        serialize_gst_tax_rate(row)
        for row in rows
        if _is_date_current(row, today_dt)
    ]
    future_active = [
        serialize_gst_tax_rate(row)
        for row in rows
        if row.get("status") == STATUS_ACTIVE
        and row.get("is_active") is True
        and row.get("effective_from")
        and row["effective_from"] > today_dt
    ]

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "foundation": foundation,
        "rows": serialized,
        "working_rows": [row for row in serialized if row["status"] in EDITABLE_STATUSES],
        "pending_rows": [row for row in serialized if row["status"] == STATUS_PENDING_APPROVAL],
        "active_rows": [row for row in serialized if row["status"] == STATUS_ACTIVE],
        "retired_rows": [row for row in serialized if row["status"] == STATUS_RETIRED],
        "cancelled_rows": [row for row in serialized if row["status"] == STATUS_CANCELLED],
        "current_active_rows": current_active,
        "future_active_rows": future_active,
        "counts": counts,
        "total_count": len(rows),
        "audit_recovery_count": sum(
            1 for row in rows if row.get("audit_sync_required") is True
        ) + foundation.get("audit_recovery_count", 0),
        "options": get_gst_tax_option_catalog(entity["_id"]),
        "form_defaults": {
            "effective_from": date.today().isoformat(),
            "effective_to": "",
            "taxability_code": "TAXABLE",
        },
    }


def get_effective_gst_tax_rate(accounting_entity_id, rate_id=None, rate_code=None, transaction_date=None):
    """Resolve one approved GST rate for future product and posting services."""
    entity_id = _to_object_id(accounting_entity_id)
    if not entity_id:
        raise ValueError("Invalid Accounting entity.")

    if isinstance(transaction_date, datetime):
        date_value = datetime.combine(transaction_date.date(), time.min)
    elif isinstance(transaction_date, date):
        date_value = datetime.combine(transaction_date, time.min)
    elif transaction_date:
        date_value = _parse_date(transaction_date, "Transaction date")
    else:
        date_value = datetime.combine(date.today(), time.min)

    query = {
        "accounting_entity_id": entity_id,
        "status": {"$in": [STATUS_ACTIVE, STATUS_RETIRED]},
        "is_deleted": False,
        "effective_from": {"$lte": date_value},
        "$or": [
            {"effective_to": None},
            {"effective_to": {"$exists": False}},
            {"effective_to": {"$gte": date_value}},
        ],
    }
    if rate_id:
        object_id = _to_object_id(rate_id)
        if not object_id:
            raise ValueError("Invalid GST tax rate.")
        query["_id"] = object_id
    elif rate_code:
        query["rate_code"] = _sanitize_rate_code(rate_code)
    else:
        raise ValueError("Provide a GST rate ID or rate code.")

    row = mongo.db[GST_RATE_COLLECTION].find_one(
        query,
        sort=[("effective_from", DESCENDING)],
    )
    if not row:
        raise ValueError("No approved GST rate is effective for the selected date.")
    return row
