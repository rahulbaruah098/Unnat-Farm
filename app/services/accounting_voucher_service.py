from datetime import date, datetime, time
from hashlib import sha256
import json
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_financial_year_service import (
    assert_financial_year_usable_for_posting,
    list_financial_years,
)
from app.services.accounting_number_series_service import get_number_series_catalog
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.services.accounting_voucher_validation_service import (
    VALIDATION_STATUS_INVALID,
    VALIDATION_STATUS_NOT_VALIDATED,
    VALIDATION_STATUS_VALID,
    build_draft_line,
    calculate_draft_totals,
    list_active_ledger_options,
    money_decimal128,
    money_string,
    normalize_line_sequences,
    serialize_draft_line,
    serialize_validation_result,
    validate_draft_lines,
)
from app.utils.helpers import now_utc


VOUCHER_COLLECTION = "accounting_vouchers"
VOUCHER_LINE_COLLECTION = "voucher_lines"
AVPL_ENTITY_CODE = "AVPL"

STATUS_DRAFT = "draft"
STATUS_POSTED = "posted"
STATUS_CANCELLED = "cancelled"
STATUS_REVERSED = "reversed"

POSTING_STATE_NOT_STARTED = "not_started"

STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_POSTED: "Posted",
    STATUS_CANCELLED: "Cancelled",
    STATUS_REVERSED: "Reversed",
}

VIEW_PERMISSION = "accounting.voucher.view"
CREATE_PERMISSION = "accounting.voucher.create"
EDIT_PERMISSION = "accounting.voucher.edit"
VALIDATE_PERMISSION = "accounting.voucher.validate"

SAFE_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-]{7,159}$")
SAFE_EVENT_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_\-]{1,79}$")
SAFE_COLLECTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


# ---------------------------------------------------------------------------
# Generic safety helpers
# ---------------------------------------------------------------------------


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in keys]


def _ensure_exact_index(collection, keys, name, **options):
    """Create one required index without dropping or rewriting existing indexes."""
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
            "No existing index was dropped automatically."
        )

    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure as exc:
        raise RuntimeError(
            f"Could not create Accounting index {name} on {collection.name}."
        ) from exc


VOUCHER_INDEX_DEFINITIONS = (
    (
        [("voucher_id", ASCENDING)],
        "accounting_voucher_id_unique",
        {
            "unique": True,
            "partialFilterExpression": {"voucher_id": {"$type": "string"}},
        },
    ),
    (
        [("idempotency_scope_key", ASCENDING)],
        "accounting_voucher_idempotency_unique",
        {
            "unique": True,
            "partialFilterExpression": {
                "idempotency_scope_key": {"$type": "string"}
            },
        },
    ),
    (
        [("posted_number_key", ASCENDING)],
        "accounting_voucher_posted_number_unique",
        {
            "unique": True,
            "partialFilterExpression": {"posted_number_key": {"$type": "string"}},
        },
    ),
    (
        [("business_event_uniqueness_key", ASCENDING)],
        "accounting_voucher_business_event_unique",
        {
            "unique": True,
            "partialFilterExpression": {
                "business_event_uniqueness_key": {"$type": "string"}
            },
        },
    ),
    (
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("transaction_date", DESCENDING),
        ],
        "accounting_voucher_entity_status_date_idx",
        {},
    ),
    (
        [
            ("financial_year_id", ASCENDING),
            ("status", ASCENDING),
            ("transaction_date", DESCENDING),
        ],
        "accounting_voucher_fy_status_date_idx",
        {},
    ),
    (
        [
            ("created_by", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
        ],
        "accounting_voucher_maker_status_updated_idx",
        {},
    ),
    (
        [
            ("accounting_entity_id", ASCENDING),
            ("business_event_type", ASCENDING),
            ("business_event_id", ASCENDING),
            ("voucher_type", ASCENDING),
        ],
        "accounting_voucher_business_event_lookup_idx",
        {},
    ),
)


def ensure_voucher_indexes():
    """Install the Stage 5 voucher-header indexes safely and idempotently."""
    collection = mongo.db[VOUCHER_COLLECTION]
    created_or_verified = []
    for keys, name, options in VOUCHER_INDEX_DEFINITIONS:
        created_or_verified.append(
            _ensure_exact_index(collection, keys, name=name, **options)
        )
    return created_or_verified


def get_voucher_index_health():
    collection = mongo.db[VOUCHER_COLLECTION]
    try:
        index_info = collection.index_information()
    except Exception as exc:
        raise RuntimeError("Could not inspect Accounting voucher indexes.") from exc

    required_names = [definition[1] for definition in VOUCHER_INDEX_DEFINITIONS]
    present = [name for name in required_names if name in index_info]
    missing = [name for name in required_names if name not in index_info]
    return {
        "required_count": len(required_names),
        "present_count": len(present),
        "missing_count": len(missing),
        "present": present,
        "missing": missing,
        "is_complete": not missing,
    }


def _clean_single_line(value, label, maximum=160, required=False):
    cleaned = " ".join(str(value or "").strip().split())
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _clean_multiline(value, label, maximum=2000, required=False):
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    cleaned = "\n".join(line for line in lines if line).strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _parse_date(value, label, required=True):
    if value in (None, "") and not required:
        return None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise ValueError(f"{label} is required.")
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{label} must use the YYYY-MM-DD format.") from exc
    return datetime.combine(parsed, time.min)


def _date_input(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return ""


def _date_display(value):
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y")
    if isinstance(value, date):
        return value.strftime("%d %b %Y")
    return ""


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
        raise PermissionError("You are not authorized to perform this voucher action.")

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
    if entity_id is not None:
        entity_object_id = _to_object_id(entity_id)
        if not entity_object_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = entity_object_id

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
        allowed_entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")

    if not has_accounting_permission(access, permission):
        raise PermissionError(
            "Your Accounting access mapping does not allow this voucher action."
        )
    return access


def _voucher_type_catalog():
    catalog = get_number_series_catalog()
    return catalog.get("voucher") or []


def _voucher_type_map():
    return {row["document_type"]: row for row in _voucher_type_catalog()}


def _normalize_idempotency_key(value):
    key = str(value or "").strip()
    if not key:
        raise ValueError("Idempotency key is required for safe voucher creation.")
    if not SAFE_IDEMPOTENCY_PATTERN.fullmatch(key):
        raise ValueError(
            "Idempotency key must be 8 to 160 characters and use only letters, numbers, dots, colons, slashes, underscores or hyphens."
        )
    return key


def _idempotency_scope_key(entity_id, idempotency_key):
    raw = f"{entity_id}|voucher_header|{idempotency_key}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _validate_business_event(raw_payload, entity_id, voucher_type):
    event_type = _clean_single_line(
        raw_payload.get("business_event_type"),
        "Business event type",
        maximum=80,
    ).lower()
    event_id = _clean_single_line(
        raw_payload.get("business_event_id"),
        "Business event ID",
        maximum=160,
    )
    source_collection = _clean_single_line(
        raw_payload.get("source_collection"),
        "Source collection",
        maximum=80,
    )
    source_document_id = _clean_single_line(
        raw_payload.get("source_document_id"),
        "Source document ID",
        maximum=160,
    )
    source_document_number = _clean_single_line(
        raw_payload.get("source_document_number"),
        "Source document number",
        maximum=160,
    )
    voucher_role = _clean_single_line(
        raw_payload.get("voucher_role"),
        "Voucher role",
        maximum=80,
    ).lower()

    has_link_fields = any(
        [
            event_type,
            event_id,
            source_collection,
            source_document_id,
            source_document_number,
        ]
    )
    if not has_link_fields:
        return {
            "business_event_type": "",
            "business_event_id": "",
            "source_collection": "",
            "source_document_id": "",
            "source_document_number": "",
            "voucher_role": "",
            "business_event_key": None,
            "business_event_uniqueness_key": None,
        }

    if not event_type or not event_id:
        raise ValueError(
            "Business event type and Business event ID are both required when a voucher is linked to another business record."
        )
    if not SAFE_EVENT_TOKEN_PATTERN.fullmatch(event_type):
        raise ValueError(
            "Business event type must start with a lowercase letter and use lowercase letters, numbers, underscores or hyphens."
        )
    if source_collection and not SAFE_COLLECTION_PATTERN.fullmatch(source_collection):
        raise ValueError("Source collection contains unsupported characters.")

    voucher_role = voucher_role or "primary"
    if not SAFE_EVENT_TOKEN_PATTERN.fullmatch(voucher_role):
        raise ValueError(
            "Voucher role must start with a lowercase letter and use lowercase letters, numbers, underscores or hyphens."
        )

    event_raw = f"{entity_id}|{event_type}|{event_id}"
    uniqueness_raw = f"{event_raw}|{voucher_type}|{voucher_role}"
    return {
        "business_event_type": event_type,
        "business_event_id": event_id,
        "source_collection": source_collection,
        "source_document_id": source_document_id,
        "source_document_number": source_document_number,
        "voucher_role": voucher_role,
        "business_event_key": sha256(event_raw.encode("utf-8")).hexdigest(),
        "business_event_uniqueness_key": sha256(
            uniqueness_raw.encode("utf-8")
        ).hexdigest(),
    }


def _financial_year_snapshot(financial_year):
    return {
        "financial_year_id": financial_year["_id"],
        "financial_year_id_str": str(financial_year["_id"]),
        "financial_year_code": financial_year.get("fy_code") or "",
        "financial_year_name": financial_year.get("display_name") or "",
        "financial_year_start_date": financial_year.get("start_date"),
        "financial_year_end_date": financial_year.get("end_date"),
    }


def _fingerprint_payload(payload):
    fingerprint_source = {
        "voucher_type": payload.get("voucher_type"),
        "financial_year_id": str(payload.get("financial_year_id") or ""),
        "transaction_date": _date_input(payload.get("transaction_date")),
        "reference_number": payload.get("reference_number") or "",
        "reference_date": _date_input(payload.get("reference_date")),
        "narration": payload.get("narration") or "",
        "business_event_type": payload.get("business_event_type") or "",
        "business_event_id": payload.get("business_event_id") or "",
        "source_collection": payload.get("source_collection") or "",
        "source_document_id": payload.get("source_document_id") or "",
        "source_document_number": payload.get("source_document_number") or "",
        "voucher_role": payload.get("voucher_role") or "",
    }
    encoded = json.dumps(
        fingerprint_source,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def _validate_header_payload(raw_payload, entity, existing=None):
    voucher_type = str(raw_payload.get("voucher_type") or "").strip().lower()
    type_meta = _voucher_type_map().get(voucher_type)
    if not type_meta:
        raise ValueError("Select a valid voucher type.")

    transaction_date = _parse_date(
        raw_payload.get("transaction_date"), "Transaction date"
    )
    financial_year = assert_financial_year_usable_for_posting(
        raw_payload.get("financial_year_id"),
        entity_id=entity["_id"],
        transaction_date=transaction_date,
    )

    reference_number = _clean_single_line(
        raw_payload.get("reference_number"),
        "Reference number",
        maximum=160,
    )
    reference_date = _parse_date(
        raw_payload.get("reference_date"),
        "Reference date",
        required=False,
    )
    narration = _clean_multiline(
        raw_payload.get("narration"),
        "Voucher narration",
        maximum=2000,
        required=True,
    )
    event_payload = _validate_business_event(
        raw_payload,
        entity["_id"],
        voucher_type,
    )

    canonical = {
        "voucher_type": voucher_type,
        "voucher_type_label": type_meta.get("label") or voucher_type.replace("_", " ").title(),
        "voucher_type_short_code": type_meta.get("short_code") or "",
        "voucher_type_description": type_meta.get("description") or "",
        **_financial_year_snapshot(financial_year),
        "transaction_date": transaction_date,
        "reference_number": reference_number,
        "reference_date": reference_date,
        "narration": narration,
        **event_payload,
    }
    canonical["header_fingerprint"] = _fingerprint_payload(canonical)

    if existing and existing.get("status") != STATUS_DRAFT:
        raise ValueError("Only draft vouchers can be edited.")
    return canonical


def _get_voucher(voucher_id):
    voucher_object_id = _to_object_id(voucher_id)
    query = (
        {"_id": voucher_object_id}
        if voucher_object_id
        else {"voucher_id": str(voucher_id or "").strip()}
    )
    voucher = mongo.db[VOUCHER_COLLECTION].find_one(query)
    if not voucher:
        raise ValueError("Accounting voucher was not found.")
    return voucher


# ---------------------------------------------------------------------------
# Audit and serialization
# ---------------------------------------------------------------------------


def _change_event(
    action,
    actor,
    previous_status=None,
    new_status=None,
    changed_fields=None,
    remarks="",
):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "changed_fields": sorted(set(changed_fields or [])),
        "remarks": str(remarks or "")[:2000],
        "at": now_utc(),
    }


def _record_audit(
    voucher,
    actor,
    action,
    previous_status=None,
    changed_fields=None,
    remarks="",
):
    timestamp = now_utc()
    audit_document = {
        "module": "accounting",
        "action": action,
        "accounting_entity_id": voucher.get("accounting_entity_id"),
        "accounting_entity_id_str": str(voucher.get("accounting_entity_id") or ""),
        "entity_type": "accounting_voucher",
        "entity_id": voucher.get("_id"),
        "entity_id_str": str(voucher.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": voucher.get("status"),
        "metadata": {
            "voucher_id": voucher.get("voucher_id"),
            "voucher_type": voucher.get("voucher_type"),
            "voucher_number": voucher.get("voucher_number"),
            "financial_year_id": str(voucher.get("financial_year_id") or ""),
            "transaction_date": _date_input(voucher.get("transaction_date")),
            "idempotency_key": voucher.get("idempotency_key"),
            "business_event_type": voucher.get("business_event_type"),
            "business_event_id": voucher.get("business_event_id"),
            "version": int(voucher.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": remarks or "Accounting voucher header updated.",
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit_document)
    except Exception as exc:
        try:
            mongo.db[VOUCHER_COLLECTION].update_one(
                {"_id": voucher.get("_id")},
                {
                    "$set": {
                        "audit_sync_required": True,
                        "audit_sync_action": action,
                        "audit_sync_marked_at": timestamp,
                    },
                    "$push": {
                        "audit_sync_errors": {
                            "action": action,
                            "message": str(exc)[:500],
                            "at": timestamp,
                        }
                    },
                },
            )
        except Exception:
            pass
        return False

    mongo.db[VOUCHER_COLLECTION].update_one(
        {"_id": voucher.get("_id")},
        {
            "$set": {
                "audit_sync_required": False,
                "audit_sync_action": None,
                "audit_sync_completed_at": timestamp,
            }
        },
    )
    return True


def serialize_voucher(voucher):
    if not voucher:
        return None

    return {
        "id": str(voucher.get("_id") or ""),
        "voucher_id": voucher.get("voucher_id") or "",
        "accounting_entity_id": str(voucher.get("accounting_entity_id") or ""),
        "accounting_entity_code": voucher.get("accounting_entity_code") or AVPL_ENTITY_CODE,
        "accounting_entity_name": voucher.get("accounting_entity_name") or "AVPL",
        "voucher_type": voucher.get("voucher_type") or "",
        "voucher_type_label": voucher.get("voucher_type_label") or "",
        "voucher_type_short_code": voucher.get("voucher_type_short_code") or "",
        "voucher_type_description": voucher.get("voucher_type_description") or "",
        "voucher_number": voucher.get("voucher_number") or "",
        "status": voucher.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(
            voucher.get("status"), str(voucher.get("status") or "").replace("_", " ").title()
        ),
        "posting_state": voucher.get("posting_state") or POSTING_STATE_NOT_STARTED,
        "validation_status": voucher.get("validation_status") or VALIDATION_STATUS_NOT_VALIDATED,
        "financial_year_id": str(voucher.get("financial_year_id") or ""),
        "financial_year_code": voucher.get("financial_year_code") or "",
        "financial_year_name": voucher.get("financial_year_name") or "",
        "financial_year_start_date_input": _date_input(voucher.get("financial_year_start_date")),
        "financial_year_end_date_input": _date_input(voucher.get("financial_year_end_date")),
        "transaction_date": voucher.get("transaction_date"),
        "transaction_date_input": _date_input(voucher.get("transaction_date")),
        "transaction_date_display": _date_display(voucher.get("transaction_date")),
        "reference_number": voucher.get("reference_number") or "",
        "reference_date": voucher.get("reference_date"),
        "reference_date_input": _date_input(voucher.get("reference_date")),
        "reference_date_display": _date_display(voucher.get("reference_date")),
        "narration": voucher.get("narration") or "",
        "business_event_type": voucher.get("business_event_type") or "",
        "business_event_id": voucher.get("business_event_id") or "",
        "source_collection": voucher.get("source_collection") or "",
        "source_document_id": voucher.get("source_document_id") or "",
        "source_document_number": voucher.get("source_document_number") or "",
        "voucher_role": voucher.get("voucher_role") or "",
        "has_business_event": bool(
            voucher.get("business_event_type") and voucher.get("business_event_id")
        ),
        "idempotency_key": voucher.get("idempotency_key") or "",
        "draft_lines": [serialize_draft_line(line) for line in voucher.get("draft_lines") or []],
        "draft_line_count": int(voucher.get("draft_line_count") or 0),
        "draft_debit_total": money_string(voucher.get("draft_debit_total")),
        "draft_credit_total": money_string(voucher.get("draft_credit_total")),
        "draft_balance_difference": money_string(voucher.get("draft_balance_difference")),
        "draft_absolute_difference": money_string(voucher.get("draft_absolute_difference")),
        "draft_is_balanced": voucher.get("draft_is_balanced") is True,
        "last_validation_result": serialize_validation_result(voucher.get("last_validation_result")),
        "last_validated_at": voucher.get("last_validated_at"),
        "last_validated_by_name": voucher.get("last_validated_by_name") or "",
        "is_validation_current": (
            voucher.get("validation_status") == VALIDATION_STATUS_VALID
            and (voucher.get("last_validation_result") or {}).get("voucher_version")
            == int(voucher.get("version") or 1)
        ),
        "posted_line_count": int(voucher.get("posted_line_count") or 0),
         "is_reversal": voucher.get("is_reversal") is True,
        "reverses_voucher_id": str(voucher.get("reverses_voucher_id") or ""),
        "reverses_voucher_internal_id": voucher.get("reverses_voucher_internal_id") or "",
        "reverses_voucher_number": voucher.get("reverses_voucher_number") or "",
        "reversal_voucher_id": str(voucher.get("reversal_voucher_id") or ""),
        "reversal_voucher_internal_id": voucher.get("reversal_voucher_internal_id") or "",
        "reversal_voucher_number": voucher.get("reversal_voucher_number") or "",
        "reversal_reason": voucher.get("reversal_reason") or "",
        "reversed_by_name": voucher.get("reversed_by_name") or "",
        "reversed_at": voucher.get("reversed_at"),
        "cancellation_reason": voucher.get("cancellation_reason") or "",
        "cancelled_by_name": voucher.get("cancelled_by_name") or "",
        "cancelled_at": voucher.get("cancelled_at"),
        "version": int(voucher.get("version") or 1),
        "created_by": str(voucher.get("created_by") or ""),
        "created_by_name": voucher.get("created_by_name") or "",
        "created_at": voucher.get("created_at"),
        "updated_by_name": voucher.get("updated_by_name") or "",
        "updated_at": voucher.get("updated_at"),
        "audit_sync_required": voucher.get("audit_sync_required") is True,
        "change_history": voucher.get("change_history") or [],
    }


# ---------------------------------------------------------------------------
# Stage 5 Batch 1: draft header creation and editing
# ---------------------------------------------------------------------------


def create_voucher_draft(accounting_entity_id, actor_user_id, raw_payload):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_voucher_indexes()

    idempotency_key = _normalize_idempotency_key(
        raw_payload.get("idempotency_key")
    )
    idempotency_scope_key = _idempotency_scope_key(entity["_id"], idempotency_key)
    canonical = _validate_header_payload(raw_payload, entity)

    existing = mongo.db[VOUCHER_COLLECTION].find_one(
        {"idempotency_scope_key": idempotency_scope_key}
    )
    if existing:
        if existing.get("header_fingerprint") != canonical.get("header_fingerprint"):
            raise ValueError(
                "This idempotency key was already used with different voucher details. Use the original details or create a new key."
            )
        return {
            "voucher": serialize_voucher(existing),
            "message": "Existing voucher draft returned safely for this idempotency key.",
            "idempotent_replay": True,
        }

    if canonical.get("business_event_uniqueness_key"):
        linked_existing = mongo.db[VOUCHER_COLLECTION].find_one(
            {
                "business_event_uniqueness_key": canonical[
                    "business_event_uniqueness_key"
                ]
            },
            {"voucher_id": 1, "status": 1},
        )
        if linked_existing:
            raise ValueError(
                "A voucher already exists for this business event, voucher type and voucher role."
            )

    timestamp = now_utc()
    voucher_uid = f"VCH-{uuid4().hex.upper()}"
    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "accounting_entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "accounting_entity_name": entity.get("name")
        or entity.get("legal_name")
        or "AVPL",
        "voucher_id": voucher_uid,
        **canonical,
        "idempotency_key": idempotency_key,
        "idempotency_scope_key": idempotency_scope_key,
        "status": STATUS_DRAFT,
        "posting_state": POSTING_STATE_NOT_STARTED,
        "validation_status": VALIDATION_STATUS_NOT_VALIDATED,
        "voucher_number": None,
        "posted_number_key": None,
        "number_reservation_id": None,
        "draft_lines": [],
        "draft_line_count": 0,
        "draft_debit_total": money_decimal128("0.00"),
        "draft_credit_total": money_decimal128("0.00"),
        "draft_balance_difference": money_decimal128("0.00"),
        "draft_absolute_difference": money_decimal128("0.00"),
        "draft_is_balanced": False,
        "posted_line_count": 0,
        "version": 1,
        "revision_number": 1,
        "is_deleted": False,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "change_history": [
            _change_event(
                "create_voucher_draft",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                changed_fields=sorted(canonical.keys()),
                remarks="Voucher header draft created without consuming an official number.",
            )
        ],
        "audit_sync_required": False,
    }

    try:
        result = mongo.db[VOUCHER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        existing = mongo.db[VOUCHER_COLLECTION].find_one(
            {"idempotency_scope_key": idempotency_scope_key}
        )
        if existing and existing.get("header_fingerprint") == canonical.get(
            "header_fingerprint"
        ):
            return {
                "voucher": serialize_voucher(existing),
                "message": "Existing voucher draft returned safely after a concurrent retry.",
                "idempotent_replay": True,
            }
        raise RuntimeError(
            "A voucher with the same idempotency key, internal ID or business-event link already exists. Refresh and review the existing drafts."
        ) from exc

    _record_audit(
        document,
        actor,
        "create_voucher_draft",
        previous_status=None,
        changed_fields=sorted(canonical.keys()),
        remarks="Voucher header draft created. No official voucher number or voucher lines were generated.",
    )
    return {
        "voucher": serialize_voucher(document),
        "message": "Voucher header draft created. No official number was consumed.",
        "idempotent_replay": False,
    }


def update_voucher_draft(voucher_id, actor_user_id, raw_payload, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)

    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only draft vouchers can be edited.")
    if str(voucher.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can edit this voucher draft.")
    if voucher.get("voucher_number") or int(voucher.get("posted_line_count") or 0) > 0:
        raise RuntimeError("This voucher has posting data and cannot be edited.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid voucher version. Refresh and try again.") from exc
    if expected_version != int(voucher.get("version") or 1):
        raise RuntimeError("This voucher changed in another session. Refresh before saving.")

    canonical = _validate_header_payload(raw_payload, entity, existing=voucher)
    editable_fields = sorted(canonical.keys())
    changed_fields = [
        field for field in editable_fields if voucher.get(field) != canonical.get(field)
    ]
    if not changed_fields:
        return {
            "voucher": serialize_voucher(voucher),
            "message": "No voucher-header changes were detected.",
        }

    timestamp = now_utc()
    next_version = expected_version + 1
    updates = {field: canonical[field] for field in changed_fields}
    updates.update(
        {
            "version": next_version,
            "validation_status": VALIDATION_STATUS_NOT_VALIDATED,
            "last_validated_at": None,
            "last_validated_by": None,
            "last_validated_by_str": "",
            "last_validated_by_name": "",
            "last_validation_result": None,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
        }
    )

    try:
        result = mongo.db[VOUCHER_COLLECTION].update_one(
            {
                "_id": voucher["_id"],
                "status": STATUS_DRAFT,
                "version": expected_version,
                "created_by": actor["_id"],
                "voucher_number": None,
                "posted_line_count": {"$in": [0, None]},
            },
            {
                "$set": updates,
                "$push": {
                    "change_history": _change_event(
                        "update_voucher_draft",
                        actor,
                        previous_status=STATUS_DRAFT,
                        new_status=STATUS_DRAFT,
                        changed_fields=changed_fields,
                        remarks="Voucher header draft updated; any future validation state was reset.",
                    )
                },
            },
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "Another voucher already uses the selected business-event link."
        ) from exc

    if result.matched_count != 1:
        raise RuntimeError("This voucher changed in another session. Refresh and try again.")

    updated = _get_voucher(voucher["_id"])
    _record_audit(
        updated,
        actor,
        "update_voucher_draft",
        previous_status=STATUS_DRAFT,
        changed_fields=changed_fields,
        remarks="Voucher header draft updated.",
    )
    return {
        "voucher": serialize_voucher(updated),
        "message": "Voucher header draft updated.",
    }



# ---------------------------------------------------------------------------
# Stage 5 Batch 2: editable draft lines and non-posting validation
# ---------------------------------------------------------------------------


def _parse_expected_version(expected_version):
    try:
        return int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid voucher version. Refresh and try again.") from exc


def _assert_draft_mutable(voucher, actor, expected_version):
    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only draft vouchers can be changed.")
    if str(voucher.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can change voucher draft lines.")
    if voucher.get("voucher_number") or int(voucher.get("posted_line_count") or 0) > 0:
        raise RuntimeError("This voucher has official posting data and cannot be changed.")

    assert_financial_year_usable_for_posting(
        voucher.get("financial_year_id"),
        entity_id=voucher.get("accounting_entity_id"),
        transaction_date=voucher.get("transaction_date"),
    )

    parsed_version = _parse_expected_version(expected_version)
    if parsed_version != int(voucher.get("version") or 1):
        raise RuntimeError("This voucher changed in another session. Refresh before saving.")
    return parsed_version


def _line_totals_update(lines):
    totals = calculate_draft_totals(lines)
    return {
        "draft_lines": lines,
        "draft_line_count": len(lines),
        "draft_debit_total": money_decimal128(totals["debit_total"]),
        "draft_credit_total": money_decimal128(totals["credit_total"]),
        "draft_balance_difference": money_decimal128(totals["difference"]),
        "draft_absolute_difference": money_decimal128(totals["absolute_difference"]),
        "draft_is_balanced": totals["is_balanced"],
        "validation_status": VALIDATION_STATUS_NOT_VALIDATED,
        "last_validated_at": None,
        "last_validated_by": None,
        "last_validated_by_str": "",
        "last_validated_by_name": "",
        "last_validation_result": None,
    }


def _save_draft_lines(
    voucher,
    actor,
    expected_version,
    lines,
    *,
    action,
    changed_fields,
    remarks,
):
    timestamp = now_utc()
    next_version = expected_version + 1
    lines = normalize_line_sequences(lines)
    updates = _line_totals_update(lines)
    updates.update(
        {
            "version": next_version,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
        }
    )

    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher["_id"],
            "status": STATUS_DRAFT,
            "version": expected_version,
            "created_by": actor["_id"],
            "voucher_number": None,
            "posted_line_count": {"$in": [0, None]},
        },
        {
            "$set": updates,
            "$push": {
                "change_history": _change_event(
                    action,
                    actor,
                    previous_status=STATUS_DRAFT,
                    new_status=STATUS_DRAFT,
                    changed_fields=changed_fields,
                    remarks=remarks,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This voucher changed in another session. Refresh and try again.")

    updated = _get_voucher(voucher["_id"])
    _record_audit(
        updated,
        actor,
        action,
        previous_status=STATUS_DRAFT,
        changed_fields=changed_fields,
        remarks=remarks,
    )
    return updated


def add_voucher_draft_line(voucher_id, actor_user_id, raw_payload, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    expected_version = _assert_draft_mutable(voucher, actor, expected_version)

    existing_lines = list(voucher.get("draft_lines") or [])
    line_id = f"VLN-{uuid4().hex.upper()}"
    line = build_draft_line(
        entity["_id"],
        raw_payload,
        line_id=line_id,
        sequence=len(existing_lines) + 1,
    )
    timestamp = now_utc()
    line.update(
        {
            "accounting_entity_id": entity["_id"],
            "accounting_entity_id_str": str(entity["_id"]),
            "created_by": actor["_id"],
            "created_by_str": str(actor["_id"]),
            "created_by_name": actor.get("resolved_name") or "",
            "created_at": timestamp,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
        }
    )
    updated = _save_draft_lines(
        voucher,
        actor,
        expected_version,
        existing_lines + [line],
        action="add_voucher_draft_line",
        changed_fields=["draft_lines", "draft_line_count", "draft_totals", "validation_status"],
        remarks=f"Voucher draft line {line_id} added. Any previous validation result was reset.",
    )
    return {
        "voucher": serialize_voucher(updated),
        "message": "Voucher line added. Validate the complete voucher after all lines are ready.",
    }


def update_voucher_draft_line(
    voucher_id,
    line_id,
    actor_user_id,
    raw_payload,
    expected_version,
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    expected_version = _assert_draft_mutable(voucher, actor, expected_version)

    existing_lines = list(voucher.get("draft_lines") or [])
    line_index = next(
        (index for index, line in enumerate(existing_lines) if line.get("line_id") == line_id),
        None,
    )
    if line_index is None:
        raise ValueError("Voucher draft line was not found.")

    old_line = existing_lines[line_index]
    new_line = build_draft_line(
        entity["_id"],
        raw_payload,
        line_id=line_id,
        sequence=int(old_line.get("sequence") or line_index + 1),
        existing_line=old_line,
    )
    timestamp = now_utc()
    new_line.update(
        {
            "accounting_entity_id": entity["_id"],
            "accounting_entity_id_str": str(entity["_id"]),
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
        }
    )
    comparable_fields = [
        "ledger_id",
        "debit_amount",
        "credit_amount",
        "line_narration",
    ]
    if all(old_line.get(field) == new_line.get(field) for field in comparable_fields):
        return {
            "voucher": serialize_voucher(voucher),
            "message": "No voucher-line changes were detected.",
        }

    existing_lines[line_index] = new_line
    updated = _save_draft_lines(
        voucher,
        actor,
        expected_version,
        existing_lines,
        action="update_voucher_draft_line",
        changed_fields=["draft_lines", "draft_totals", "validation_status"],
        remarks=f"Voucher draft line {line_id} updated. Any previous validation result was reset.",
    )
    return {
        "voucher": serialize_voucher(updated),
        "message": "Voucher line updated.",
    }


def remove_voucher_draft_line(voucher_id, line_id, actor_user_id, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    expected_version = _assert_draft_mutable(voucher, actor, expected_version)

    existing_lines = list(voucher.get("draft_lines") or [])
    filtered_lines = [line for line in existing_lines if line.get("line_id") != line_id]
    if len(filtered_lines) == len(existing_lines):
        raise ValueError("Voucher draft line was not found.")

    updated = _save_draft_lines(
        voucher,
        actor,
        expected_version,
        filtered_lines,
        action="remove_voucher_draft_line",
        changed_fields=["draft_lines", "draft_line_count", "draft_totals", "validation_status"],
        remarks=f"Voucher draft line {line_id} removed. Any previous validation result was reset.",
    )
    return {
        "voucher": serialize_voucher(updated),
        "message": "Voucher line removed.",
    }


def validate_voucher_draft(voucher_id, actor_user_id, expected_version):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"accounts", "avpl_admin", "super_admin"},
    )
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], VALIDATE_PERMISSION)

    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only draft vouchers can be validated.")
    if voucher.get("voucher_number") or int(voucher.get("posted_line_count") or 0) > 0:
        raise RuntimeError("This voucher already contains official posting data.")

    expected_version = _parse_expected_version(expected_version)
    if expected_version != int(voucher.get("version") or 1):
        raise RuntimeError("This voucher changed in another session. Refresh before validating.")

    # Revalidate the Financial Year and transaction-date boundary every time.
    assert_financial_year_usable_for_posting(
        voucher.get("financial_year_id"),
        entity_id=entity["_id"],
        transaction_date=voucher.get("transaction_date"),
    )

    validation = validate_draft_lines(entity["_id"], voucher.get("draft_lines") or [])
    timestamp = now_utc()
    next_version = expected_version + 1
    validation.update(
        {
            "validated_by": actor["_id"],
            "validated_by_str": str(actor["_id"]),
            "validated_by_name": actor.get("resolved_name") or "",
            "validated_at": timestamp,
            "voucher_version": next_version,
            "header_fingerprint": voucher.get("header_fingerprint") or "",
        }
    )
    action = "validate_voucher_draft" if validation["is_valid"] else "voucher_draft_validation_failed"
    remarks = (
        "Voucher draft passed non-posting double-entry validation."
        if validation["is_valid"]
        else f"Voucher draft validation failed with {validation['error_count']} error(s)."
    )

    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher["_id"],
            "status": STATUS_DRAFT,
            "version": expected_version,
            "voucher_number": None,
            "posted_line_count": {"$in": [0, None]},
        },
        {
            "$set": {
                "validation_status": validation["status"],
                "last_validated_at": timestamp,
                "last_validated_by": actor["_id"],
                "last_validated_by_str": str(actor["_id"]),
                "last_validated_by_name": actor.get("resolved_name") or "",
                "last_validation_result": validation,
                "version": next_version,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    action,
                    actor,
                    previous_status=STATUS_DRAFT,
                    new_status=STATUS_DRAFT,
                    changed_fields=["validation_status", "last_validation_result"],
                    remarks=remarks,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This voucher changed in another session. Refresh and validate again.")

    updated = _get_voucher(voucher["_id"])
    _record_audit(
        updated,
        actor,
        action,
        previous_status=STATUS_DRAFT,
        changed_fields=["validation_status", "last_validation_result"],
        remarks=remarks,
    )
    return {
        "voucher": serialize_voucher(updated),
        "validation": serialize_validation_result(validation),
        "message": (
            "Voucher is balanced and passed Batch 2 validation. No official posting was created."
            if validation["is_valid"]
            else f"Voucher validation failed with {validation['error_count']} error(s). Review the displayed result."
        ),
        "category": "success" if validation["is_valid"] else "warning",
        "validation_failed": not validation["is_valid"],
    }


# ---------------------------------------------------------------------------
# Dashboard read model
# ---------------------------------------------------------------------------


def get_voucher_option_catalog():
    return {
        "voucher_types": _voucher_type_catalog(),
        "status_labels": dict(STATUS_LABELS),
        "posting_states": [
            POSTING_STATE_NOT_STARTED,
            "number_reserved",
            "lines_written",
            "number_committed",
            "completed",
            "recovery_required",
        ],
    }


def _default_financial_year(open_financial_years):
    today_value = date.today()
    for financial_year in open_financial_years:
        start_value = financial_year.get("start_date")
        end_value = financial_year.get("end_date")
        if isinstance(start_value, datetime):
            start_value = start_value.date()
        if isinstance(end_value, datetime):
            end_value = end_value.date()
        if start_value and end_value and start_value <= today_value <= end_value:
            return financial_year
    return open_financial_years[0] if open_financial_years else None


def get_voucher_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_voucher_indexes()

    rows = list(
        mongo.db[VOUCHER_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "is_deleted": {"$ne": True},
            }
        )
        .sort([("updated_at", DESCENDING), ("created_at", DESCENDING)])
        .limit(250)
    )
    serialized = [serialize_voucher(row) for row in rows]

    counts = {status: 0 for status in STATUS_LABELS}
    voucher_type_counts = {
        item["document_type"]: 0 for item in _voucher_type_catalog()
    }
    audit_recovery_count = 0
    for row in rows:
        status = row.get("status") or STATUS_DRAFT
        counts[status] = counts.get(status, 0) + 1
        voucher_type = row.get("voucher_type")
        if voucher_type:
            voucher_type_counts[voucher_type] = voucher_type_counts.get(voucher_type, 0) + 1
        if row.get("audit_sync_required") is True:
            audit_recovery_count += 1

    validation_counts = {
        VALIDATION_STATUS_NOT_VALIDATED: 0,
        VALIDATION_STATUS_VALID: 0,
        VALIDATION_STATUS_INVALID: 0,
    }
    for row in rows:
        validation_status = row.get("validation_status") or VALIDATION_STATUS_NOT_VALIDATED
        validation_counts[validation_status] = validation_counts.get(validation_status, 0) + 1

    ledger_options = list_active_ledger_options(entity["_id"])

    financial_years = list_financial_years(entity["_id"])
    open_financial_years = [
        financial_year
        for financial_year in financial_years
        if financial_year.get("usable_for_posting")
    ]
    default_financial_year = _default_financial_year(open_financial_years)
    default_transaction_date = date.today()
    if default_financial_year:
        start_value = default_financial_year.get("start_date")
        end_value = default_financial_year.get("end_date")
        if isinstance(start_value, datetime):
            start_value = start_value.date()
        if isinstance(end_value, datetime):
            end_value = end_value.date()
        if start_value and default_transaction_date < start_value:
            default_transaction_date = start_value
        if end_value and default_transaction_date > end_value:
            default_transaction_date = end_value

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "rows": serialized,
        "draft_rows": [row for row in serialized if row["status"] == STATUS_DRAFT],
        "posted_rows": [row for row in serialized if row["status"] == STATUS_POSTED],
        "cancelled_rows": [row for row in serialized if row["status"] == STATUS_CANCELLED],
        "reversed_rows": [row for row in serialized if row["status"] == STATUS_REVERSED],
        "counts": counts,
        "voucher_type_counts": voucher_type_counts,
        "validation_counts": validation_counts,
        "ledger_options": ledger_options,
        "active_ledger_count": len(ledger_options),
        "total_count": len(rows),
        "audit_recovery_count": audit_recovery_count,
        "voucher_line_count": mongo.db[VOUCHER_LINE_COLLECTION].count_documents(
            {"accounting_entity_id": entity["_id"]}
        ),
        "index_health": get_voucher_index_health(),
        "open_financial_years": open_financial_years,
        "options": get_voucher_option_catalog(),
        "form_defaults": {
            "voucher_type": "journal_voucher",
            "financial_year_id": (
                default_financial_year.get("id") if default_financial_year else ""
            ),
            "transaction_date": default_transaction_date.isoformat()
            if default_financial_year
            else "",
            "reference_date": "",
            "voucher_role": "primary",
            "idempotency_key": uuid4().hex,
        },
        "prerequisites": {
            "has_open_financial_year": bool(open_financial_years),
            "indexes_ready": get_voucher_index_health().get("is_complete") is True,
            "is_ready_for_draft_headers": bool(open_financial_years),
            "has_active_ledgers": bool(ledger_options),
            "is_ready_for_line_entry": bool(open_financial_years and ledger_options),
            "is_ready_for_posting": False,
        },
    }
