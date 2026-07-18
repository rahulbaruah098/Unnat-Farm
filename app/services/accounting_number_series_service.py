from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha1
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


INVOICE_COLLECTION = "invoice_number_series"
VOUCHER_COLLECTION = "voucher_number_series"
RESERVATION_COLLECTION = "accounting_number_reservations"

CATEGORY_INVOICE = "invoice"
CATEGORY_VOUCHER = "voucher"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_DISABLED = "disabled"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
WORKING_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_APPROVAL,
    STATUS_RETURNED,
}

DEFAULT_PREFIX_TEMPLATE = "{ENTITY}/{TYPE}/{FY}/"
DEFAULT_SUFFIX = ""
DEFAULT_STARTING_NUMBER = 1
DEFAULT_PADDING = 6
RESET_POLICY_FINANCIAL_YEAR = "financial_year"

ALLOWED_TEMPLATE_TOKENS = {"ENTITY", "TYPE", "FY"}
REQUIRED_TEMPLATE_TOKENS = {"ENTITY", "TYPE", "FY"}
TEMPLATE_TOKEN_PATTERN = re.compile(r"\{([A-Z_]+)\}")
SAFE_TEMPLATE_PATTERN = re.compile(r"^[A-Za-z0-9{}._/\-]+$")
SAFE_SUFFIX_PATTERN = re.compile(r"^[A-Za-z0-9._/\-]*$")

DOCUMENT_TYPES = {
    CATEGORY_INVOICE: {
        "sales_invoice": {
            "label": "Sales Invoice",
            "short_code": "SI",
            "description": "Commercial sales document issued to a buyer.",
            "future_stage": "Stage 9",
        },
        "purchase_invoice": {
            "label": "Purchase Invoice",
            "short_code": "PI",
            "description": "Supplier purchase document recorded in AVPL books.",
            "future_stage": "Stage 6",
        },
        "credit_note": {
            "label": "Credit Note",
            "short_code": "CN",
            "description": "Sales return or buyer credit adjustment document.",
            "future_stage": "Stage 9",
        },
        "debit_note": {
            "label": "Debit Note",
            "short_code": "DN",
            "description": "Purchase return or supplier debit adjustment document.",
            "future_stage": "Stage 6",
        },
    },
    CATEGORY_VOUCHER: {
        "sales_voucher": {
            "label": "Sales Voucher",
            "short_code": "SV",
            "description": "Double-entry voucher linked to a posted sales document.",
            "future_stage": "Stage 9",
        },
        "purchase_voucher": {
            "label": "Purchase Voucher",
            "short_code": "PUV",
            "description": "Double-entry voucher linked to a posted purchase document.",
            "future_stage": "Stage 6",
        },
        "receipt_voucher": {
            "label": "Receipt Voucher",
            "short_code": "RV",
            "description": "Cash, bank, UPI or cheque receipt voucher.",
            "future_stage": "Stage 7",
        },
        "payment_voucher": {
            "label": "Payment Voucher",
            "short_code": "PV",
            "description": "Cash, bank, UPI or cheque payment voucher.",
            "future_stage": "Stage 7",
        },
        "contra_voucher": {
            "label": "Contra Voucher",
            "short_code": "CV",
            "description": "Internal cash, bank and clearing transfer voucher.",
            "future_stage": "Stage 8",
        },
        "journal_voucher": {
            "label": "Journal Voucher",
            "short_code": "JV",
            "description": "Controlled non-cash Accounting adjustment voucher.",
            "future_stage": "Stage 8",
        },
        "stock_adjustment_voucher": {
            "label": "Stock Adjustment Voucher",
            "short_code": "SAV",
            "description": "Authorized stock adjustment and reconciliation voucher.",
            "future_stage": "Stage 10",
        },
    },
}


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in keys]


def _ensure_exact_index(collection, keys, name, **options):
    """Create a required Accounting index without dropping any existing index."""
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


def ensure_number_series_indexes():
    """Install number-series and reservation indexes safely and idempotently."""
    for collection_name in (INVOICE_COLLECTION, VOUCHER_COLLECTION):
        collection = mongo.db[collection_name]

        _ensure_exact_index(
            collection,
            [("active_scope_key", ASCENDING)],
            name=f"{collection_name}_active_scope_unique",
            unique=True,
            partialFilterExpression={"active_scope_key": {"$exists": True}},
        )
        _ensure_exact_index(
            collection,
            [("working_scope_key", ASCENDING)],
            name=f"{collection_name}_working_scope_unique",
            unique=True,
            partialFilterExpression={"working_scope_key": {"$exists": True}},
        )
        _ensure_exact_index(
            collection,
            [
                ("accounting_entity_id", ASCENDING),
                ("financial_year_id", ASCENDING),
                ("document_type", ASCENDING),
                ("revision_number", DESCENDING),
            ],
            name=f"{collection_name}_scope_revision_idx",
        )
        _ensure_exact_index(
            collection,
            [("status", ASCENDING), ("submitted_at", ASCENDING)],
            name=f"{collection_name}_approval_queue_idx",
        )
        _ensure_exact_index(
            collection,
            [("created_by", ASCENDING), ("updated_at", DESCENDING)],
            name=f"{collection_name}_maker_updated_idx",
        )

    reservation_collection = mongo.db[RESERVATION_COLLECTION]
    _ensure_exact_index(
        reservation_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("financial_year_id", ASCENDING),
            ("full_number", ASCENDING),
        ],
        name="accounting_number_full_number_unique",
        unique=True,
    )
    _ensure_exact_index(
        reservation_collection,
        [("series_id", ASCENDING), ("sequence_number", ASCENDING)],
        name="accounting_number_series_sequence_unique",
        unique=True,
    )
    _ensure_exact_index(
        reservation_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("document_category", ASCENDING),
            ("document_type", ASCENDING),
            ("idempotency_key", ASCENDING),
        ],
        name="accounting_number_idempotency_unique",
        unique=True,
        partialFilterExpression={"idempotency_key": {"$exists": True}},
    )
    _ensure_exact_index(
        reservation_collection,
        [("status", ASCENDING), ("reserved_at", ASCENDING)],
        name="accounting_number_reservation_status_idx",
    )


def _collection_name_for_category(category):
    normalized = str(category or "").strip().lower()
    if normalized == CATEGORY_INVOICE:
        return INVOICE_COLLECTION
    if normalized == CATEGORY_VOUCHER:
        return VOUCHER_COLLECTION
    raise ValueError("Invalid number-series category.")


def _category_label(category):
    return "Invoice" if category == CATEGORY_INVOICE else "Voucher"


def _document_meta(category, document_type):
    normalized_category = str(category or "").strip().lower()
    normalized_type = str(document_type or "").strip().lower()
    metadata = DOCUMENT_TYPES.get(normalized_category, {}).get(normalized_type)
    if not metadata:
        raise ValueError("Invalid document type for the selected number-series category.")
    return normalized_category, normalized_type, metadata


def get_number_series_catalog():
    return {
        category: [
            {
                "category": category,
                "category_label": _category_label(category),
                "document_type": document_type,
                **metadata,
                "default_prefix_template": DEFAULT_PREFIX_TEMPLATE,
                "default_suffix": DEFAULT_SUFFIX,
                "default_starting_number": DEFAULT_STARTING_NUMBER,
                "default_padding": DEFAULT_PADDING,
                "reset_policy": RESET_POLICY_FINANCIAL_YEAR,
            }
            for document_type, metadata in document_types.items()
        ]
        for category, document_types in DOCUMENT_TYPES.items()
    }


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
        raise PermissionError("You are not authorized to perform this number-series action.")

    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )
    return actor


def _assert_entity(entity_id):
    entity_object_id = _to_object_id(entity_id)
    if not entity_object_id:
        raise ValueError("Invalid Accounting entity.")

    entity = mongo.db.accounting_entities.find_one({
        "_id": entity_object_id,
        "is_deleted": {"$ne": True},
        "status": "active",
        "accounting_enabled": {"$ne": False},
    })
    if not entity:
        raise ValueError("The Accounting entity was not found or is inactive.")
    return entity


def _assert_open_financial_year(financial_year_id, entity_id):
    financial_year_object_id = _to_object_id(financial_year_id)
    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    financial_year = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "accounting_entity_id": entity_id,
        "status": "open",
        "is_locked": {"$ne": True},
        "is_deleted": {"$ne": True},
    })
    if not financial_year:
        raise ValueError(
            "Number series can be configured only for an approved, open and unlocked Financial Year."
        )
    return financial_year


def _assert_active_configuration(entity_id):
    entity_profile = mongo.db.accounting_entity_settings.find_one({
        "accounting_entity_id": entity_id,
        "status": "approved",
        "is_active": True,
        "is_deleted": {"$ne": True},
    })
    accounting_policy = mongo.db.accounting_settings.find_one({
        "accounting_entity_id": entity_id,
        "status": "approved",
        "is_active": True,
        "is_deleted": {"$ne": True},
    })

    if not entity_profile or not accounting_policy:
        raise ValueError(
            "Approve the AVPL entity profile and Accounting policy settings before configuring number series."
        )


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")

    if actor.get("resolved_role") != "super_admin":
        allowed_entity_ids = {
            str(value) for value in access.get("entity_ids") or []
        }
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")

    if not has_accounting_permission(access, permission):
        raise PermissionError("You do not have permission to perform this Accounting action.")
    return access


def _scope_key(entity_id, financial_year_id, document_type):
    return f"{entity_id}:{financial_year_id}:{document_type}"


def _lock_field_name(scope_key):
    digest = sha1(scope_key.encode("utf-8")).hexdigest()
    return f"configuration_locks.number_series_{digest}"


@contextmanager
def _number_series_lock(entity_id, scope_key):
    token = uuid4().hex
    timestamp = now_utc()
    stale_before = timestamp - timedelta(seconds=45)
    field_name = _lock_field_name(scope_key)

    locked = mongo.db.accounting_entities.find_one_and_update(
        {
            "_id": entity_id,
            "$or": [
                {field_name: {"$exists": False}},
                {f"{field_name}.acquired_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                field_name: {
                    "token": token,
                    "acquired_at": timestamp,
                }
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not locked:
        raise RuntimeError(
            "Another number-series update is in progress for this document type. "
            "Please wait a moment and try again."
        )

    try:
        yield
    finally:
        mongo.db.accounting_entities.update_one(
            {"_id": entity_id, f"{field_name}.token": token},
            {"$unset": {field_name: ""}},
        )


def _parse_int(value, field_label, minimum, maximum):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} must be a whole number.") from exc

    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f"{field_label} must be between {minimum} and {maximum}."
        )
    return parsed


def _normalize_prefix_template(value):
    template = str(value or DEFAULT_PREFIX_TEMPLATE).strip()
    if not template:
        raise ValueError("Prefix template is required.")
    if len(template) > 80:
        raise ValueError("Prefix template cannot exceed 80 characters.")
    if not SAFE_TEMPLATE_PATTERN.fullmatch(template):
        raise ValueError(
            "Prefix template can contain only letters, numbers, braces, dots, slashes, hyphens and underscores."
        )

    normalized_for_tokens = template.upper()
    found_tokens = set(TEMPLATE_TOKEN_PATTERN.findall(normalized_for_tokens))
    unknown_tokens = found_tokens - ALLOWED_TEMPLATE_TOKENS
    if unknown_tokens:
        raise ValueError(
            "Unsupported prefix token(s): " + ", ".join(sorted(unknown_tokens)) + "."
        )

    missing_tokens = REQUIRED_TEMPLATE_TOKENS - found_tokens
    if missing_tokens:
        raise ValueError(
            "Prefix template must include "
            + ", ".join(f"{{{token}}}" for token in sorted(missing_tokens))
            + "."
        )

    # Normalize supported token casing without changing custom text casing.
    for token in ALLOWED_TEMPLATE_TOKENS:
        template = re.sub(
            rf"\{{{token}\}}",
            f"{{{token}}}",
            template,
            flags=re.IGNORECASE,
        )
    return template


def _normalize_suffix(value):
    suffix = str(value or "").strip()
    if len(suffix) > 20:
        raise ValueError("Suffix cannot exceed 20 characters.")
    if not SAFE_SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError(
            "Suffix can contain only letters, numbers, dots, slashes, hyphens and underscores."
        )
    return suffix


def _financial_year_token(financial_year):
    display = str(
        financial_year.get("display_name")
        or financial_year.get("fy_code")
        or ""
    ).strip()
    display = re.sub(r"^FY\s*", "", display, flags=re.IGNORECASE)
    return display.replace(" ", "")


def _render_prefix(template, entity, financial_year, document_meta):
    values = {
        "ENTITY": str(entity.get("entity_code") or "ENTITY").strip().upper(),
        "TYPE": str(document_meta.get("short_code") or "DOC").strip().upper(),
        "FY": _financial_year_token(financial_year),
    }
    rendered = template
    for token, replacement in values.items():
        rendered = rendered.replace(f"{{{token}}}", replacement)
    return rendered


def format_number(prefix, sequence_number, padding, suffix=""):
    sequence = int(sequence_number)
    width = int(padding)
    return f"{prefix}{sequence:0{width}d}{suffix}"


def _normalize_payload(entity, financial_year, category, document_type, raw_payload):
    normalized_category, normalized_type, metadata = _document_meta(
        category, document_type
    )
    prefix_template = _normalize_prefix_template(
        raw_payload.get("prefix_template")
    )
    suffix = _normalize_suffix(raw_payload.get("suffix"))
    starting_number = _parse_int(
        raw_payload.get("starting_number") or DEFAULT_STARTING_NUMBER,
        "Starting number",
        1,
        999999999,
    )
    padding = _parse_int(
        raw_payload.get("padding") or DEFAULT_PADDING,
        "Number padding",
        3,
        12,
    )
    resolved_prefix = _render_prefix(
        prefix_template,
        entity,
        financial_year,
        metadata,
    )
    preview_number = format_number(
        resolved_prefix,
        starting_number,
        padding,
        suffix,
    )
    if len(preview_number) > 120:
        raise ValueError("The generated document number cannot exceed 120 characters.")

    return {
        "document_category": normalized_category,
        "document_type": normalized_type,
        "document_label": metadata["label"],
        "document_short_code": metadata["short_code"],
        "description": metadata["description"],
        "future_stage": metadata["future_stage"],
        "prefix_template": prefix_template,
        "resolved_prefix": resolved_prefix,
        "suffix": suffix,
        "starting_number": starting_number,
        "padding": padding,
        "reset_policy": RESET_POLICY_FINANCIAL_YEAR,
        "preview_number": preview_number,
        "pattern_key": f"{resolved_prefix}|{padding}|{suffix}",
    }


def preview_number_series(entity_id, financial_year_id, category, document_type, raw_payload):
    entity = _assert_entity(entity_id)
    financial_year = _assert_open_financial_year(financial_year_id, entity["_id"])
    return _normalize_payload(
        entity,
        financial_year,
        category,
        document_type,
        raw_payload or {},
    )


def _parse_expected_version(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "The number-series form version is invalid. Refresh and try again."
        ) from exc


def _next_revision(collection, entity_id, financial_year_id, document_type):
    latest = collection.find_one(
        {
            "accounting_entity_id": entity_id,
            "financial_year_id": financial_year_id,
            "document_type": document_type,
            "is_deleted": {"$ne": True},
        },
        {"revision_number": 1},
        sort=[("revision_number", DESCENDING)],
    )
    return int((latest or {}).get("revision_number") or 0) + 1


def _workflow_event(
    action,
    actor,
    from_status,
    to_status,
    revision_number,
    reason="",
    note="",
):
    return {
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "revision_number": int(revision_number or 1),
        "reason": str(reason or "").strip(),
        "note": str(note or "").strip(),
        "at": now_utc(),
    }


def _record_audit(collection_name, document, actor, action, previous_status=None, remarks=""):
    timestamp = now_utc()
    try:
        mongo.db.accounting_audit_logs.insert_one({
            "module": "accounting",
            "action": action,
            "accounting_entity_id": document.get("accounting_entity_id"),
            "accounting_entity_id_str": str(document.get("accounting_entity_id") or ""),
            "entity_type": "number_series",
            "entity_id": document.get("_id"),
            "entity_id_str": str(document.get("_id") or ""),
            "actor_user_id": actor["_id"],
            "actor_user_id_str": str(actor["_id"]),
            "actor_role": actor.get("resolved_role") or "",
            "actor_name": actor.get("resolved_name") or "",
            "previous_status": previous_status,
            "new_status": document.get("status"),
            "metadata": {
                "collection": collection_name,
                "document_category": document.get("document_category"),
                "document_type": document.get("document_type"),
                "financial_year_id_str": document.get("financial_year_id_str"),
                "revision_number": document.get("revision_number"),
                "version": document.get("version"),
                "preview_number": document.get("preview_number"),
            },
            "remarks": remarks or "Accounting number-series workflow updated.",
            "created_at": timestamp,
        })
    except Exception as exc:
        try:
            mongo.db[collection_name].update_one(
                {"_id": document.get("_id")},
                {
                    "$set": {
                        "audit_sync_required": True,
                        "audit_sync_action": action,
                        "audit_sync_marked_at": timestamp,
                    },
                    "$push": {
                        "audit_sync_errors": {
                            "message": str(exc)[:500],
                            "at": timestamp,
                        }
                    },
                },
            )
        except Exception:
            pass


def _find_working(collection, scope_key):
    return collection.find_one({
        "working_scope_key": scope_key,
        "is_deleted": {"$ne": True},
    })


def _find_active(collection, scope_key):
    return collection.find_one({
        "active_scope_key": scope_key,
        "status": STATUS_ACTIVE,
        "is_deleted": {"$ne": True},
    })


def _assert_creator(document, actor):
    if actor.get("resolved_role") == "super_admin":
        return
    if str(document.get("created_by") or "") != str(actor.get("_id") or ""):
        raise PermissionError(
            "Only the original Accounts maker can edit, submit or withdraw this series revision."
        )


def _assert_pattern_available(
    entity_id,
    financial_year_id,
    pattern_key,
    scope_key,
    exclude_id=None,
):
    exclude_id_text = str(exclude_id or "")
    for collection_name in (INVOICE_COLLECTION, VOUCHER_COLLECTION):
        candidates = mongo.db[collection_name].find(
            {
                "accounting_entity_id": entity_id,
                "financial_year_id": financial_year_id,
                "pattern_key": pattern_key,
                "status": {"$in": [STATUS_ACTIVE, STATUS_PENDING_APPROVAL]},
                "is_deleted": {"$ne": True},
            },
            {
                "_id": 1,
                "scope_key": 1,
                "document_label": 1,
            },
        )
        for duplicate in candidates:
            if exclude_id_text and str(duplicate.get("_id") or "") == exclude_id_text:
                continue
            if str(duplicate.get("scope_key") or "") == str(scope_key):
                continue
            raise ValueError(
                "The generated numbering pattern is already used by "
                f"{duplicate.get('document_label') or 'another document type'}. "
                "Use a different prefix or suffix."
            )


def _assert_revision_start_safe(working, active):
    if not active:
        return

    same_pattern = working.get("pattern_key") == active.get("pattern_key")
    if not same_pattern:
        return

    last_consumed = max(
        int(active.get("last_reserved_number") or 0),
        int(active.get("next_number") or active.get("starting_number") or 1) - 1,
    )
    if int(working.get("starting_number") or 1) <= last_consumed:
        raise ValueError(
            "This revision uses the same number pattern as the active series. "
            f"Starting number must be greater than {last_consumed}, or the prefix/suffix must change."
        )


# ---------------------------------------------------------------------------
# Draft and approval workflow
# ---------------------------------------------------------------------------


def save_number_series_draft(
    entity_id,
    actor_user_id,
    raw_payload,
    expected_version=None,
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    entity = _assert_entity(entity_id)
    _assert_active_configuration(entity["_id"])

    category = str(raw_payload.get("document_category") or "").strip().lower()
    document_type = str(raw_payload.get("document_type") or "").strip().lower()
    category, document_type, _ = _document_meta(category, document_type)
    collection_name = _collection_name_for_category(category)
    collection = mongo.db[collection_name]

    access = get_accounting_access(actor["_id"], session_role=actor["resolved_role"])
    if not (
        has_accounting_permission(access, "accounting.number_series.create")
        or has_accounting_permission(access, "accounting.number_series.edit")
    ):
        raise PermissionError("You do not have permission to save number-series drafts.")
    _require_permission(actor, entity["_id"], "accounting.number_series.view")

    financial_year = _assert_open_financial_year(
        raw_payload.get("financial_year_id"),
        entity["_id"],
    )
    payload = _normalize_payload(
        entity,
        financial_year,
        category,
        document_type,
        raw_payload,
    )
    scope_key = _scope_key(entity["_id"], financial_year["_id"], document_type)

    ensure_number_series_indexes()

    with _number_series_lock(entity["_id"], scope_key):
        working = _find_working(collection, scope_key)
        active = _find_active(collection, scope_key)
        timestamp = now_utc()

        if working:
            _assert_creator(working, actor)
            if working.get("status") not in EDITABLE_STATUSES:
                raise ValueError(
                    "This number series is awaiting approval and cannot be edited."
                )

            expected = _parse_expected_version(expected_version)
            current_version = int(working.get("version") or 1)
            if expected != current_version:
                raise RuntimeError(
                    "This number-series draft changed in another session. Refresh and try again."
                )

            _assert_pattern_available(
                entity["_id"],
                financial_year["_id"],
                payload["pattern_key"],
                scope_key,
                exclude_id=working["_id"],
            )
            event = _workflow_event(
                "draft_updated",
                actor,
                working.get("status"),
                STATUS_DRAFT,
                working.get("revision_number"),
                note=raw_payload.get("change_note"),
            )
            result = collection.update_one(
                {"_id": working["_id"], "version": current_version},
                {
                    "$set": {
                        **payload,
                        "status": STATUS_DRAFT,
                        "return_reason": "",
                        "returned_by": None,
                        "returned_by_str": "",
                        "returned_at": None,
                        "correction_response": str(
                            raw_payload.get("correction_response") or ""
                        ).strip(),
                        "updated_by": actor["_id"],
                        "updated_by_str": str(actor["_id"]),
                        "updated_at": timestamp,
                        "version": current_version + 1,
                    },
                    "$push": {"workflow_history": event},
                },
            )
            if result.modified_count != 1:
                raise RuntimeError(
                    "This number-series draft changed in another session. Refresh and try again."
                )
            document = collection.find_one({"_id": working["_id"]})
            previous_status = working.get("status")
            action = "update_number_series_draft"
            created = False
        else:
            _assert_pattern_available(
                entity["_id"],
                financial_year["_id"],
                payload["pattern_key"],
                scope_key,
            )
            revision = _next_revision(
                collection,
                entity["_id"],
                financial_year["_id"],
                document_type,
            )
            event = _workflow_event(
                "draft_created",
                actor,
                None,
                STATUS_DRAFT,
                revision,
                note=raw_payload.get("change_note"),
            )
            document = {
                "accounting_entity_id": entity["_id"],
                "accounting_entity_id_str": str(entity["_id"]),
                "entity_code": entity.get("entity_code") or "",
                "financial_year_id": financial_year["_id"],
                "financial_year_id_str": str(financial_year["_id"]),
                "financial_year_code": financial_year.get("fy_code") or "",
                "financial_year_display": financial_year.get("display_name") or "",
                **payload,
                "scope_key": scope_key,
                "working_scope_key": scope_key,
                "revision_number": revision,
                "status": STATUS_DRAFT,
                "is_working_copy": True,
                "is_active": False,
                "is_deleted": False,
                "next_number": None,
                "reserved_count": 0,
                "committed_count": 0,
                "void_count": 0,
                "last_reserved_number": None,
                "last_committed_number": None,
                "created_by": actor["_id"],
                "created_by_str": str(actor["_id"]),
                "created_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": 1,
                "workflow_history": [event],
                "audit_sync_required": False,
            }
            try:
                result = collection.insert_one(document)
                document["_id"] = result.inserted_id
            except DuplicateKeyError as exc:
                raise RuntimeError(
                    "Another number-series draft was created at the same time. Refresh and try again."
                ) from exc
            previous_status = None
            action = "create_number_series_draft"
            created = True

    _record_audit(
        collection_name,
        document,
        actor,
        action,
        previous_status=previous_status,
    )
    return {
        "created": created,
        "series": serialize_number_series(document),
        "message": f"{document.get('document_label')} series draft saved successfully.",
    }


def initialize_missing_number_series_drafts(
    entity_id,
    financial_year_id,
    actor_user_id,
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    entity = _assert_entity(entity_id)
    _assert_active_configuration(entity["_id"])
    _require_permission(actor, entity["_id"], "accounting.number_series.create")
    financial_year = _assert_open_financial_year(
        financial_year_id,
        entity["_id"],
    )
    ensure_number_series_indexes()

    created = 0
    skipped = 0
    errors = []

    for category, document_types in DOCUMENT_TYPES.items():
        collection = mongo.db[_collection_name_for_category(category)]
        for document_type in document_types:
            scope_key = _scope_key(
                entity["_id"], financial_year["_id"], document_type
            )
            if _find_working(collection, scope_key) or _find_active(collection, scope_key):
                skipped += 1
                continue

            try:
                save_number_series_draft(
                    entity_id=entity["_id"],
                    actor_user_id=actor["_id"],
                    raw_payload={
                        "financial_year_id": str(financial_year["_id"]),
                        "document_category": category,
                        "document_type": document_type,
                        "prefix_template": DEFAULT_PREFIX_TEMPLATE,
                        "suffix": DEFAULT_SUFFIX,
                        "starting_number": DEFAULT_STARTING_NUMBER,
                        "padding": DEFAULT_PADDING,
                        "change_note": "Initial default number-series setup.",
                    },
                    expected_version=0,
                )
                created += 1
            except (PermissionError, ValueError, RuntimeError) as exc:
                errors.append(f"{document_types[document_type]['label']}: {exc}")

    if errors:
        raise RuntimeError(
            f"Created {created} draft(s), skipped {skipped}, but some series need review: "
            + " | ".join(errors[:3])
        )

    return {
        "created_count": created,
        "skipped_count": skipped,
        "message": (
            f"Created {created} missing default number-series draft(s) and skipped "
            f"{skipped} existing scope(s)."
        ),
    }


def _load_series(category, series_id):
    collection_name = _collection_name_for_category(category)
    series_object_id = _to_object_id(series_id)
    if not series_object_id:
        raise ValueError("Invalid number-series record.")

    document = mongo.db[collection_name].find_one({
        "_id": series_object_id,
        "is_deleted": {"$ne": True},
    })
    if not document:
        raise ValueError("The number-series record was not found.")
    return collection_name, document


def submit_number_series(
    category,
    series_id,
    actor_user_id,
    expected_version,
    note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    collection_name, document = _load_series(category, series_id)
    _require_permission(
        actor,
        document["accounting_entity_id"],
        "accounting.number_series.submit",
    )
    _assert_creator(document, actor)

    if document.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned number series can be submitted.")

    expected = _parse_expected_version(expected_version)
    current_version = int(document.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    _assert_open_financial_year(
        document["financial_year_id"],
        document["accounting_entity_id"],
    )
    _assert_active_configuration(document["accounting_entity_id"])
    _assert_pattern_available(
        document["accounting_entity_id"],
        document["financial_year_id"],
        document["pattern_key"],
        document["scope_key"],
        exclude_id=document["_id"],
    )

    timestamp = now_utc()
    event = _workflow_event(
        "submitted_for_approval",
        actor,
        document.get("status"),
        STATUS_PENDING_APPROVAL,
        document.get("revision_number"),
        note=note,
    )
    result = mongo.db[collection_name].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_at": timestamp,
                "submission_note": str(note or "").strip(),
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "submit_number_series",
        previous_status=document.get("status"),
    )
    return {
        "series": serialize_number_series(updated),
        "message": f"{updated.get('document_label')} series submitted to AVPL Admin.",
    }



def bulk_submit_number_series(
    selections,
    actor_user_id,
    note="",
):
    """Submit multiple saved draft/returned series in one controlled request.

    Every selected series keeps its own optimistic version check, permission check,
    workflow event and audit record. The batch performs a complete preflight before
    changing any record. A rare concurrent change after preflight is reported per
    item instead of hiding a partial result.
    """
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})

    if not isinstance(selections, list) or not selections:
        raise ValueError("Select at least one invoice or voucher series to submit.")
    if len(selections) > 50:
        raise ValueError("A maximum of 50 number-series records can be submitted together.")

    clean_note = str(note or "").strip()
    if len(clean_note) > 500:
        raise ValueError("The shared submission note cannot exceed 500 characters.")

    prepared = []
    seen = set()
    selected_patterns = {}

    # Preflight the complete selection first so normal validation errors do not
    # leave an avoidable half-submitted batch.
    for raw_item in selections:
        if not isinstance(raw_item, dict):
            raise ValueError("Invalid bulk number-series selection.")

        category = str(raw_item.get("category") or "").strip().lower()
        series_id = str(raw_item.get("series_id") or "").strip()
        expected_version = raw_item.get("expected_version")
        unique_key = (category, series_id)

        if not category or not series_id:
            raise ValueError("Invalid bulk number-series selection.")
        if unique_key in seen:
            continue
        seen.add(unique_key)

        collection_name, document = _load_series(category, series_id)
        _require_permission(
            actor,
            document["accounting_entity_id"],
            "accounting.number_series.submit",
        )
        _assert_creator(document, actor)

        if document.get("status") not in EDITABLE_STATUSES:
            raise ValueError(
                f"{document.get('document_label') or 'A selected series'} is no longer "
                "a draft or returned-for-correction record."
            )

        expected = _parse_expected_version(expected_version)
        current_version = int(document.get("version") or 1)
        if expected != current_version:
            raise RuntimeError(
                f"{document.get('document_label') or 'A selected series'} changed in "
                "another session. Refresh and select it again."
            )

        _assert_open_financial_year(
            document["financial_year_id"],
            document["accounting_entity_id"],
        )
        _assert_active_configuration(document["accounting_entity_id"])
        _assert_pattern_available(
            document["accounting_entity_id"],
            document["financial_year_id"],
            document["pattern_key"],
            document["scope_key"],
            exclude_id=document["_id"],
        )

        selected_pattern_key = (
            str(document.get("accounting_entity_id") or ""),
            str(document.get("financial_year_id") or ""),
            str(document.get("pattern_key") or ""),
        )
        earlier = selected_patterns.get(selected_pattern_key)
        if earlier and earlier.get("scope_key") != document.get("scope_key"):
            raise ValueError(
                "The selected batch contains the same resolved numbering pattern for "
                f"{earlier.get('label')} and {document.get('document_label')}. "
                "Change one prefix or suffix before submitting."
            )
        selected_patterns[selected_pattern_key] = {
            "scope_key": document.get("scope_key"),
            "label": document.get("document_label") or series_id,
        }

        prepared.append({
            "category": category,
            "series_id": series_id,
            "expected_version": current_version,
            "label": document.get("document_label") or series_id,
        })

    if not prepared:
        raise ValueError("Select at least one invoice or voucher series to submit.")

    submitted = []
    failed = []

    for item in prepared:
        try:
            result = submit_number_series(
                category=item["category"],
                series_id=item["series_id"],
                actor_user_id=actor["_id"],
                expected_version=item["expected_version"],
                note=clean_note,
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            failed.append({
                "label": item["label"],
                "error": str(exc),
            })
        else:
            submitted.append(result["series"])

    if not submitted:
        first_error = failed[0]["error"] if failed else "No series could be submitted."
        raise RuntimeError(first_error)

    message = (
        f"Submitted {len(submitted)} of {len(prepared)} selected number-series "
        "record(s) to AVPL Admin."
    )
    if failed:
        failure_summary = "; ".join(
            f"{item['label']}: {item['error']}" for item in failed[:3]
        )
        message += f" {len(failed)} failed: {failure_summary}"

    return {
        "submitted_count": len(submitted),
        "failed_count": len(failed),
        "submitted_series": submitted,
        "failures": failed,
        "message": message,
    }


def withdraw_number_series(
    category,
    series_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts", "super_admin"})
    collection_name, document = _load_series(category, series_id)
    _require_permission(
        actor,
        document["accounting_entity_id"],
        "accounting.number_series.withdraw",
    )
    _assert_creator(document, actor)

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending number series can be withdrawn.")

    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 3:
        raise ValueError("A withdrawal reason is required.")

    expected = _parse_expected_version(expected_version)
    current_version = int(document.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    timestamp = now_utc()
    event = _workflow_event(
        "withdrawn_to_draft",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_DRAFT,
        document.get("revision_number"),
        reason=clean_reason,
    )
    result = mongo.db[collection_name].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                "status": STATUS_DRAFT,
                "withdrawn_by": actor["_id"],
                "withdrawn_by_str": str(actor["_id"]),
                "withdrawn_at": timestamp,
                "withdraw_reason": clean_reason,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "withdraw_number_series",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=clean_reason,
    )
    return {
        "series": serialize_number_series(updated),
        "message": f"{updated.get('document_label')} series withdrawn to draft.",
    }


def return_number_series(
    category,
    series_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    collection_name, document = _load_series(category, series_id)
    _require_permission(
        actor,
        document["accounting_entity_id"],
        "accounting.number_series.return",
    )

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending number series can be sent back.")

    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 3:
        raise ValueError("A correction reason is required when sending a series back.")

    expected = _parse_expected_version(expected_version)
    current_version = int(document.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    timestamp = now_utc()
    event = _workflow_event(
        "returned_for_correction",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_RETURNED,
        document.get("revision_number"),
        reason=clean_reason,
    )
    result = mongo.db[collection_name].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                "status": STATUS_RETURNED,
                "returned_by": actor["_id"],
                "returned_by_str": str(actor["_id"]),
                "returned_at": timestamp,
                "return_reason": clean_reason,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "return_number_series",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=clean_reason,
    )
    return {
        "series": serialize_number_series(updated),
        "message": f"{updated.get('document_label')} series sent back for correction.",
    }


def approve_number_series(
    category,
    series_id,
    actor_user_id,
    expected_version,
    approval_note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    collection_name, document = _load_series(category, series_id)
    _require_permission(
        actor,
        document["accounting_entity_id"],
        "accounting.number_series.approve",
    )

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending number series can be approved.")
    if str(document.get("created_by") or "") == str(actor.get("_id") or ""):
        raise PermissionError("The maker cannot approve their own number-series revision.")

    expected = _parse_expected_version(expected_version)
    current_version = int(document.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This number-series record changed in another session. Refresh and try again."
        )

    _assert_open_financial_year(
        document["financial_year_id"],
        document["accounting_entity_id"],
    )
    _assert_active_configuration(document["accounting_entity_id"])
    _assert_pattern_available(
        document["accounting_entity_id"],
        document["financial_year_id"],
        document["pattern_key"],
        document["scope_key"],
        exclude_id=document["_id"],
    )

    collection = mongo.db[collection_name]
    scope_key = document["scope_key"]

    with _number_series_lock(document["accounting_entity_id"], scope_key):
        current = collection.find_one({"_id": document["_id"]})
        if not current or current.get("status") != STATUS_PENDING_APPROVAL:
            raise RuntimeError(
                "This number-series record changed in another session. Refresh and try again."
            )
        if int(current.get("version") or 1) != current_version:
            raise RuntimeError(
                "This number-series record changed in another session. Refresh and try again."
            )

        active = _find_active(collection, scope_key)
        _assert_revision_start_safe(current, active)
        timestamp = now_utc()

        if active:
            superseded_event = _workflow_event(
                "superseded_by_revision",
                actor,
                STATUS_ACTIVE,
                STATUS_SUPERSEDED,
                active.get("revision_number"),
                note=f"Superseded by revision {current.get('revision_number')}.",
            )
            active_update = collection.update_one(
                {"_id": active["_id"], "status": STATUS_ACTIVE},
                {
                    "$set": {
                        "status": STATUS_SUPERSEDED,
                        "is_active": False,
                        "superseded_by": current["_id"],
                        "superseded_by_str": str(current["_id"]),
                        "superseded_at": timestamp,
                        "updated_by": actor["_id"],
                        "updated_by_str": str(actor["_id"]),
                        "updated_at": timestamp,
                    },
                    "$unset": {"active_scope_key": ""},
                    "$push": {"workflow_history": superseded_event},
                },
            )
            if active_update.modified_count != 1:
                raise RuntimeError(
                    "The active number series changed during approval. Refresh and try again."
                )

        event = _workflow_event(
            "approved_and_activated",
            actor,
            STATUS_PENDING_APPROVAL,
            STATUS_ACTIVE,
            current.get("revision_number"),
            note=approval_note,
        )
        update = {
            "$set": {
                "status": STATUS_ACTIVE,
                "is_active": True,
                "is_working_copy": False,
                "active_scope_key": scope_key,
                "next_number": int(current.get("starting_number") or 1),
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_at": timestamp,
                "approval_note": str(approval_note or "").strip(),
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
                "recovery_required": False,
            },
            "$unset": {"working_scope_key": ""},
            "$push": {"workflow_history": event},
        }
        result = collection.update_one(
            {
                "_id": current["_id"],
                "version": current_version,
                "status": STATUS_PENDING_APPROVAL,
            },
            update,
        )
        if result.modified_count != 1:
            # Restore the old active scope if activation failed after superseding it.
            if active:
                collection.update_one(
                    {"_id": active["_id"], "status": STATUS_SUPERSEDED},
                    {
                        "$set": {
                            "status": STATUS_ACTIVE,
                            "is_active": True,
                            "active_scope_key": scope_key,
                            "updated_at": now_utc(),
                        },
                        "$unset": {
                            "superseded_by": "",
                            "superseded_by_str": "",
                            "superseded_at": "",
                        },
                    },
                )
            raise RuntimeError(
                "The number-series approval could not be completed safely. Refresh and try again."
            )

    updated = collection.find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "approve_number_series",
        previous_status=STATUS_PENDING_APPROVAL,
    )
    return {
        "series": serialize_number_series(updated),
        "message": f"{updated.get('document_label')} series approved and activated.",
    }


# ---------------------------------------------------------------------------
# Atomic reservation and recovery foundation for future posting services
# ---------------------------------------------------------------------------


def _active_series_for_scope(entity_id, financial_year_id, category, document_type):
    collection_name = _collection_name_for_category(category)
    scope_key = _scope_key(entity_id, financial_year_id, document_type)
    series = mongo.db[collection_name].find_one({
        "active_scope_key": scope_key,
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": {"$ne": True},
    })
    if not series:
        raise ValueError(
            f"No approved active {_category_label(category).lower()} series exists for this document type."
        )
    return collection_name, series


def reserve_document_number(
    entity_id,
    financial_year_id,
    document_category,
    document_type,
    idempotency_key,
    actor_user_id,
    required_permission,
    source_collection="",
    source_id=None,
    metadata=None,
):
    """Atomically reserve an official number for a future posting service.

    The caller must pass the transaction permission already required by its own
    service, such as ``accounting.purchase.post``. A stable idempotency key must
    be reused when retrying the same business document.
    """
    actor = _get_actor(actor_user_id)
    entity = _assert_entity(entity_id)
    _require_permission(actor, entity["_id"], required_permission)
    financial_year = _assert_open_financial_year(
        financial_year_id,
        entity["_id"],
    )
    category, normalized_type, _ = _document_meta(
        document_category,
        document_type,
    )
    clean_idempotency_key = str(idempotency_key or "").strip()
    if len(clean_idempotency_key) < 8:
        raise ValueError(
            "A stable idempotency key of at least 8 characters is required for number reservation."
        )
    if len(clean_idempotency_key) > 160:
        raise ValueError("Idempotency key cannot exceed 160 characters.")

    ensure_number_series_indexes()
    existing = mongo.db[RESERVATION_COLLECTION].find_one({
        "accounting_entity_id": entity["_id"],
        "document_category": category,
        "document_type": normalized_type,
        "idempotency_key": clean_idempotency_key,
    })
    if existing:
        return serialize_number_reservation(existing)

    collection_name, series = _active_series_for_scope(
        entity["_id"],
        financial_year["_id"],
        category,
        normalized_type,
    )
    timestamp = now_utc()

    before = mongo.db[collection_name].find_one_and_update(
        {
            "_id": series["_id"],
            "status": STATUS_ACTIVE,
            "is_active": True,
            "is_deleted": {"$ne": True},
            "next_number": {"$gte": 1},
        },
        {
            "$inc": {
                "next_number": 1,
                "reserved_count": 1,
                "version": 1,
            },
            "$set": {
                "last_reserved_at": timestamp,
                "updated_at": timestamp,
            },
        },
        return_document=ReturnDocument.BEFORE,
    )
    if not before:
        raise RuntimeError(
            "The active number series changed or is unavailable. Refresh and try again."
        )

    sequence_number = int(before.get("next_number") or before.get("starting_number") or 1)
    full_number = format_number(
        before.get("resolved_prefix") or "",
        sequence_number,
        before.get("padding") or DEFAULT_PADDING,
        before.get("suffix") or "",
    )
    reservation = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or "",
        "financial_year_id": financial_year["_id"],
        "financial_year_id_str": str(financial_year["_id"]),
        "financial_year_code": financial_year.get("fy_code") or "",
        "series_collection": collection_name,
        "series_id": before["_id"],
        "series_id_str": str(before["_id"]),
        "series_revision": before.get("revision_number"),
        "document_category": category,
        "document_type": normalized_type,
        "document_label": before.get("document_label") or "",
        "sequence_number": sequence_number,
        "full_number": full_number,
        "idempotency_key": clean_idempotency_key,
        "status": "reserved",
        "source_collection": str(source_collection or "").strip(),
        "source_id": _to_object_id(source_id) if source_id else None,
        "source_id_str": str(source_id or "").strip(),
        "metadata": dict(metadata or {}),
        "reserved_by": actor["_id"],
        "reserved_by_str": str(actor["_id"]),
        "reserved_at": timestamp,
        "updated_at": timestamp,
        "version": 1,
    }

    try:
        result = mongo.db[RESERVATION_COLLECTION].insert_one(reservation)
        reservation["_id"] = result.inserted_id
    except DuplicateKeyError:
        existing = mongo.db[RESERVATION_COLLECTION].find_one({
            "accounting_entity_id": entity["_id"],
            "document_category": category,
            "document_type": normalized_type,
            "idempotency_key": clean_idempotency_key,
        })
        if existing:
            return serialize_number_reservation(existing)

        _mark_reservation_gap(
            collection_name,
            before["_id"],
            sequence_number,
            full_number,
            "A duplicate full number or sequence was detected while saving the reservation.",
        )
        raise RuntimeError(
            "The number was reserved but requires recovery review. It will not be reused."
        )
    except Exception as exc:
        _mark_reservation_gap(
            collection_name,
            before["_id"],
            sequence_number,
            full_number,
            str(exc),
        )
        raise RuntimeError(
            "The number was consumed but the reservation record could not be completed. "
            "It has been marked for recovery and will not be reused."
        ) from exc

    mongo.db[collection_name].update_one(
        {"_id": before["_id"]},
        {
            "$set": {
                "last_reserved_number": sequence_number,
                "last_reserved_full_number": full_number,
            }
        },
    )
    return serialize_number_reservation(reservation)


def _mark_reservation_gap(
    collection_name,
    series_id,
    sequence_number,
    full_number,
    error_message,
):
    timestamp = now_utc()
    mongo.db[collection_name].update_one(
        {"_id": series_id},
        {
            "$set": {
                "recovery_required": True,
                "recovery_marked_at": timestamp,
            },
            "$push": {
                "number_recovery_events": {
                    "sequence_number": sequence_number,
                    "full_number": full_number,
                    "status": "reservation_record_failed",
                    "error": str(error_message or "")[:500],
                    "at": timestamp,
                }
            },
        },
    )


def commit_reserved_number(
    reservation_id,
    actor_user_id,
    required_permission,
    source_collection,
    source_id,
    source_reference="",
):
    actor = _get_actor(actor_user_id)
    reservation_object_id = _to_object_id(reservation_id)
    if not reservation_object_id:
        raise ValueError("Invalid number reservation.")

    reservation = mongo.db[RESERVATION_COLLECTION].find_one({
        "_id": reservation_object_id
    })
    if not reservation:
        raise ValueError("The number reservation was not found.")
    _require_permission(
        actor,
        reservation["accounting_entity_id"],
        required_permission,
    )

    if reservation.get("status") == "committed":
        return serialize_number_reservation(reservation)
    if reservation.get("status") != "reserved":
        raise ValueError("Only a reserved number can be committed.")

    source_id_object = _to_object_id(source_id)
    if not source_id_object and not str(source_id or "").strip():
        raise ValueError("The posted source document reference is required.")

    timestamp = now_utc()
    result = mongo.db[RESERVATION_COLLECTION].update_one(
        {"_id": reservation["_id"], "status": "reserved", "version": reservation.get("version", 1)},
        {
            "$set": {
                "status": "committed",
                "source_collection": str(source_collection or "").strip(),
                "source_id": source_id_object,
                "source_id_str": str(source_id or "").strip(),
                "source_reference": str(source_reference or "").strip(),
                "committed_by": actor["_id"],
                "committed_by_str": str(actor["_id"]),
                "committed_at": timestamp,
                "updated_at": timestamp,
                "version": int(reservation.get("version") or 1) + 1,
            }
        },
    )
    if result.modified_count != 1:
        current = mongo.db[RESERVATION_COLLECTION].find_one({"_id": reservation["_id"]})
        if current and current.get("status") == "committed":
            return serialize_number_reservation(current)
        raise RuntimeError("The number reservation changed in another process.")

    series_update = mongo.db[reservation["series_collection"]].update_one(
        {"_id": reservation["series_id"]},
        {
            "$inc": {"committed_count": 1},
            "$set": {
                "last_committed_number": reservation.get("sequence_number"),
                "last_committed_full_number": reservation.get("full_number"),
                "last_committed_at": timestamp,
            },
        },
    )
    if series_update.matched_count != 1:
        mongo.db[RESERVATION_COLLECTION].update_one(
            {"_id": reservation["_id"]},
            {"$set": {"counter_sync_required": True}},
        )

    updated = mongo.db[RESERVATION_COLLECTION].find_one({"_id": reservation["_id"]})
    return serialize_number_reservation(updated)


def void_reserved_number(
    reservation_id,
    actor_user_id,
    required_permission,
    reason,
):
    actor = _get_actor(actor_user_id)
    reservation_object_id = _to_object_id(reservation_id)
    if not reservation_object_id:
        raise ValueError("Invalid number reservation.")

    reservation = mongo.db[RESERVATION_COLLECTION].find_one({
        "_id": reservation_object_id
    })
    if not reservation:
        raise ValueError("The number reservation was not found.")
    _require_permission(
        actor,
        reservation["accounting_entity_id"],
        required_permission,
    )

    if reservation.get("status") == "void":
        return serialize_number_reservation(reservation)
    if reservation.get("status") != "reserved":
        raise ValueError("Only an uncommitted reserved number can be voided.")

    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 3:
        raise ValueError("A void reason is required. Reserved numbers are never reused.")

    timestamp = now_utc()
    result = mongo.db[RESERVATION_COLLECTION].update_one(
        {"_id": reservation["_id"], "status": "reserved", "version": reservation.get("version", 1)},
        {
            "$set": {
                "status": "void",
                "void_reason": clean_reason,
                "voided_by": actor["_id"],
                "voided_by_str": str(actor["_id"]),
                "voided_at": timestamp,
                "updated_at": timestamp,
                "version": int(reservation.get("version") or 1) + 1,
            }
        },
    )
    if result.modified_count != 1:
        current = mongo.db[RESERVATION_COLLECTION].find_one({"_id": reservation["_id"]})
        if current and current.get("status") == "void":
            return serialize_number_reservation(current)
        raise RuntimeError("The number reservation changed in another process.")

    series_update = mongo.db[reservation["series_collection"]].update_one(
        {"_id": reservation["series_id"]},
        {"$inc": {"void_count": 1}},
    )
    if series_update.matched_count != 1:
        mongo.db[RESERVATION_COLLECTION].update_one(
            {"_id": reservation["_id"]},
            {"$set": {"counter_sync_required": True}},
        )

    updated = mongo.db[RESERVATION_COLLECTION].find_one({"_id": reservation["_id"]})
    return serialize_number_reservation(updated)


# ---------------------------------------------------------------------------
# Read models used by the Stage 2 dashboard
# ---------------------------------------------------------------------------


def serialize_number_series(document):
    if not document:
        return None

    status = str(document.get("status") or STATUS_DRAFT)
    next_number = document.get("next_number")
    if next_number is None:
        next_number = document.get("starting_number") or DEFAULT_STARTING_NUMBER

    return {
        "id": str(document.get("_id") or ""),
        "accounting_entity_id": str(document.get("accounting_entity_id") or ""),
        "financial_year_id": str(document.get("financial_year_id") or ""),
        "financial_year_code": document.get("financial_year_code") or "",
        "financial_year_display": document.get("financial_year_display") or "",
        "document_category": document.get("document_category") or "",
        "category_label": _category_label(document.get("document_category")),
        "document_type": document.get("document_type") or "",
        "document_label": document.get("document_label") or "",
        "document_short_code": document.get("document_short_code") or "",
        "description": document.get("description") or "",
        "future_stage": document.get("future_stage") or "",
        "prefix_template": document.get("prefix_template") or DEFAULT_PREFIX_TEMPLATE,
        "resolved_prefix": document.get("resolved_prefix") or "",
        "suffix": document.get("suffix") or "",
        "starting_number": int(document.get("starting_number") or 1),
        "next_number": int(next_number or 1),
        "padding": int(document.get("padding") or DEFAULT_PADDING),
        "reset_policy": document.get("reset_policy") or RESET_POLICY_FINANCIAL_YEAR,
        "preview_number": document.get("preview_number") or "",
        "next_preview": format_number(
            document.get("resolved_prefix") or "",
            int(next_number or 1),
            int(document.get("padding") or DEFAULT_PADDING),
            document.get("suffix") or "",
        ),
        "status": status,
        "status_display": status.replace("_", " ").title(),
        "revision_number": int(document.get("revision_number") or 1),
        "version": int(document.get("version") or 1),
        "is_active": document.get("is_active") is True,
        "is_working_copy": document.get("is_working_copy") is True,
        "created_by_str": document.get("created_by_str") or str(document.get("created_by") or ""),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
        "submitted_at": document.get("submitted_at"),
        "approved_at": document.get("approved_at"),
        "returned_at": document.get("returned_at"),
        "return_reason": document.get("return_reason") or "",
        "correction_response": document.get("correction_response") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "reserved_count": int(document.get("reserved_count") or 0),
        "committed_count": int(document.get("committed_count") or 0),
        "void_count": int(document.get("void_count") or 0),
        "last_reserved_full_number": document.get("last_reserved_full_number") or "",
        "last_committed_full_number": document.get("last_committed_full_number") or "",
        "workflow_history": document.get("workflow_history") or [],
        "audit_sync_required": document.get("audit_sync_required") is True,
        "recovery_required": document.get("recovery_required") is True,
    }


def serialize_number_reservation(document):
    if not document:
        return None
    return {
        "id": str(document.get("_id") or ""),
        "accounting_entity_id": str(document.get("accounting_entity_id") or ""),
        "financial_year_id": str(document.get("financial_year_id") or ""),
        "series_id": str(document.get("series_id") or ""),
        "document_category": document.get("document_category") or "",
        "document_type": document.get("document_type") or "",
        "document_label": document.get("document_label") or "",
        "sequence_number": document.get("sequence_number"),
        "full_number": document.get("full_number") or "",
        "idempotency_key": document.get("idempotency_key") or "",
        "status": document.get("status") or "",
        "source_collection": document.get("source_collection") or "",
        "source_id": document.get("source_id_str") or str(document.get("source_id") or ""),
        "source_reference": document.get("source_reference") or "",
        "reserved_at": document.get("reserved_at"),
        "committed_at": document.get("committed_at"),
        "voided_at": document.get("voided_at"),
        "void_reason": document.get("void_reason") or "",
        "version": int(document.get("version") or 1),
        "counter_sync_required": document.get("counter_sync_required") is True,
    }


def get_number_series_overview(entity_id, financial_years=None):
    entity = _assert_entity(entity_id)
    ensure_number_series_indexes()

    supplied_financial_years = list(financial_years or [])
    open_years = [
        item
        for item in supplied_financial_years
        if item.get("status") == "open"
        and item.get("is_locked") is not True
        and item.get("usable_for_posting", True)
    ]
    financial_year_ids = [
        _to_object_id(item.get("id") or item.get("_id"))
        for item in open_years
    ]
    financial_year_ids = [value for value in financial_year_ids if value]

    if not financial_year_ids:
        return {
            "financial_years": [],
            "groups": [],
            "active_count": 0,
            "working_count": 0,
            "pending_count": 0,
            "returned_count": 0,
            "required_count": 0,
            "configured_scope_count": 0,
            "catalog": get_number_series_catalog(),
        }

    documents = []
    for collection_name in (INVOICE_COLLECTION, VOUCHER_COLLECTION):
        documents.extend(
            list(
                mongo.db[collection_name].find({
                    "accounting_entity_id": entity["_id"],
                    "financial_year_id": {"$in": financial_year_ids},
                    "is_deleted": {"$ne": True},
                    "status": {
                        "$in": list(WORKING_STATUSES)
                        + [STATUS_ACTIVE, STATUS_SUPERSEDED]
                    },
                })
            )
        )

    serialized = [serialize_number_series(item) for item in documents]
    by_scope = {}
    for item in serialized:
        key = (
            item["financial_year_id"],
            item["document_category"],
            item["document_type"],
        )
        slot = by_scope.setdefault(key, {"active": None, "working": None, "history": []})
        if item["status"] == STATUS_ACTIVE:
            slot["active"] = item
        elif item["status"] in WORKING_STATUSES:
            slot["working"] = item
        else:
            slot["history"].append(item)

    groups = []
    active_count = 0
    working_count = 0
    pending_count = 0
    returned_count = 0
    configured_scope_count = 0

    catalog = get_number_series_catalog()
    for financial_year in open_years:
        financial_year_id = str(financial_year.get("id") or financial_year.get("_id") or "")
        scopes = []
        for category in (CATEGORY_INVOICE, CATEGORY_VOUCHER):
            for definition in catalog[category]:
                key = (
                    financial_year_id,
                    category,
                    definition["document_type"],
                )
                slot = by_scope.get(key, {"active": None, "working": None, "history": []})
                active = slot.get("active")
                working = slot.get("working")
                if active:
                    active_count += 1
                if working:
                    working_count += 1
                    if working.get("status") == STATUS_PENDING_APPROVAL:
                        pending_count += 1
                    if working.get("status") == STATUS_RETURNED:
                        returned_count += 1
                if active or working:
                    configured_scope_count += 1

                scopes.append({
                    **definition,
                    "financial_year_id": financial_year_id,
                    "financial_year_display": financial_year.get("display_name") or financial_year.get("fy_code") or "",
                    "active": active,
                    "working": working,
                    "history": sorted(
                        slot.get("history") or [],
                        key=lambda row: row.get("revision_number") or 0,
                        reverse=True,
                    ),
                    "is_missing": not active and not working,
                })

        groups.append({
            "financial_year": financial_year,
            "financial_year_id": financial_year_id,
            "financial_year_display": financial_year.get("display_name") or financial_year.get("fy_code") or "",
            "scopes": scopes,
        })

    required_count = len(open_years) * sum(
        len(items) for items in DOCUMENT_TYPES.values()
    )
    return {
        "financial_years": open_years,
        "groups": groups,
        "active_count": active_count,
        "working_count": working_count,
        "pending_count": pending_count,
        "returned_count": returned_count,
        "required_count": required_count,
        "configured_scope_count": configured_scope_count,
        "catalog": catalog,
    }
