from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_account_group_service import (
    ensure_account_group_indexes,
    get_account_group_overview,
)
from app.services.accounting_configuration_service import (
    GSTIN_PATTERN,
    GST_REGISTRATION_STATUSES,
    INDIA_STATE_CODES,
    PAN_PATTERN,
    POSTAL_CODE_PATTERN,
)
from app.services.accounting_ledger_service import (
    LEDGER_COLLECTION,
    ensure_ledger_indexes,
)
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


AVPL_ENTITY_CODE = "AVPL"

PARTY_ROLE_SUPPLIER = "supplier"
PARTY_ROLE_CUSTOMER = "customer"
PARTY_ROLES = {
    PARTY_ROLE_SUPPLIER: {
        "label": "Supplier",
        "account_group_system_key": "sundry_creditors",
        "normal_balance": "credit",
        "code_prefix": "SUP",
    },
    PARTY_ROLE_CUSTOMER: {
        "label": "Customer / Party",
        "account_group_system_key": "sundry_debtors",
        "normal_balance": "debit",
        "code_prefix": "CUS",
    },
}

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

VIEW_PERMISSION = "accounting.party_ledger.view"
CREATE_PERMISSION = "accounting.party_ledger.create"
EDIT_PERMISSION = "accounting.party_ledger.edit"
SUBMIT_PERMISSION = "accounting.party_ledger.submit"
WITHDRAW_PERMISSION = "accounting.party_ledger.withdraw"
CANCEL_PERMISSION = "accounting.party_ledger.cancel"
APPROVE_PERMISSION = "accounting.party_ledger.approve"
RETURN_PERMISSION = "accounting.party_ledger.return"
DEACTIVATE_PERMISSION = "accounting.party_ledger.deactivate"
REACTIVATE_PERMISSION = "accounting.party_ledger.reactivate"

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")
LEDGER_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_\-]{2,39}$")

REGISTERED_GST_STATUSES = {
    "registered_regular",
    "registered_composition",
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


def ensure_party_ledger_indexes():
    """Install party-ledger indexes without altering existing ledger indexes."""
    ensure_ledger_indexes()
    collection = mongo.db[LEDGER_COLLECTION]

    _ensure_exact_index(
        collection,
        [("party_master_id", ASCENDING)],
        name="ledger_party_master_id_unique",
        unique=True,
        partialFilterExpression={"party_master_id": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [("ledger_code_reservation_key", ASCENDING)],
        name="ledger_party_code_reservation_unique",
        unique=True,
        partialFilterExpression={
            "ledger_code_reservation_key": {"$type": "string"}
        },
    )
    _ensure_exact_index(
        collection,
        [("party_tax_identity_key", ASCENDING)],
        name="ledger_party_tax_identity_unique",
        unique=True,
        partialFilterExpression={
            "party_tax_identity_key": {"$type": "string"}
        },
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("is_party_ledger", ASCENDING),
            ("status", ASCENDING),
            ("updated_at", DESCENDING),
        ],
        name="ledger_party_entity_status_updated_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("party_role", ASCENDING),
            ("gstin", ASCENDING),
        ],
        name="ledger_party_entity_role_gstin_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("party_role", ASCENDING),
            ("pan", ASCENDING),
        ],
        name="ledger_party_entity_role_pan_idx",
    )


def _clean_single_line(value, label, maximum=160, required=True):
    cleaned = " ".join(str(value or "").strip().split())
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _clean_multiline(value, label, maximum=1000, required=False):
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    cleaned = "\n".join(line for line in lines if line).strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _normalize_name(value):
    return " ".join(str(value or "").strip().lower().split())


def _parse_int(value, label, minimum=0, maximum=3650):
    try:
        parsed = int(str(value if value is not None else "0").strip() or "0")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _parse_decimal(value, label, minimum="0", maximum="999999999999.99"):
    text = str(value if value is not None else "0").strip() or "0"
    try:
        parsed = Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a valid amount.") from exc
    if parsed < Decimal(minimum) or parsed > Decimal(maximum):
        raise ValueError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return parsed


def _decimal_string(value):
    if isinstance(value, Decimal128):
        return format(value.to_decimal(), "f")
    if isinstance(value, Decimal):
        return format(value, "f")
    try:
        return format(Decimal(str(value or "0")), "f")
    except (InvalidOperation, ValueError):
        return "0.00"


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
        raise PermissionError(
            "You are not authorized to perform this party-ledger action."
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
    if entity_id:
        object_id = _to_object_id(entity_id)
        if not object_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = object_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise RuntimeError(
            "The active AVPL Accounting entity is not available."
        )
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
        entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in entity_ids:
            raise PermissionError(
                "You do not have access to this Accounting entity."
            )

    if not has_accounting_permission(access, permission):
        raise PermissionError(
            "Your Accounting access mapping does not allow this party-ledger action."
        )
    return access


def _get_party_ledger(ledger_id):
    object_id = _to_object_id(ledger_id)
    if not object_id:
        raise ValueError("Invalid party ledger.")
    ledger = mongo.db[LEDGER_COLLECTION].find_one(
        {"_id": object_id, "is_party_ledger": True}
    )
    if not ledger:
        raise ValueError("Party ledger was not found.")
    return ledger


def _assert_party_ledger_entity_access(actor, ledger, permission):
    entity = _assert_active_avpl_entity(ledger.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)
    return entity


def _resolve_party_group(entity_id, party_role):
    definition = PARTY_ROLES.get(party_role)
    if not definition:
        raise ValueError("Select a valid party type.")

    group = mongo.db.account_groups.find_one(
        {
            "accounting_entity_id": entity_id,
            "system_key": definition["account_group_system_key"],
            "is_system": True,
            "is_protected": True,
            "is_active": True,
            "is_deleted": False,
        }
    )
    if not group:
        raise RuntimeError(
            f"The protected {definition['account_group_system_key'].replace('_', ' ').title()} group is unavailable. "
            "Verify and repair Stage 3 Batch 1 account groups first."
        )
    return group


def _default_credit_days(entity_id):
    active_policy = mongo.db.accounting_settings.find_one(
        {
            "accounting_entity_id": entity_id,
            "status": "approved",
            "is_active": True,
            "is_deleted": {"$ne": True},
        },
        sort=[("revision_number", DESCENDING)],
    )
    payload = (active_policy or {}).get("payload") or {}
    try:
        return max(0, min(3650, int(payload.get("default_credit_days") or 0)))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Validation and canonical payload
# ---------------------------------------------------------------------------


def _normalize_phone(value):
    text = re.sub(r"[\s()\-]", "", str(value or "").strip())
    if text and not PHONE_PATTERN.fullmatch(text):
        raise ValueError("Phone number must contain 7 to 15 digits.")
    return text


def _normalize_email(value):
    text = str(value or "").strip().lower()
    if text and not EMAIL_PATTERN.fullmatch(text):
        raise ValueError("Enter a valid email address.")
    if len(text) > 180:
        raise ValueError("Email address cannot exceed 180 characters.")
    return text


def _validate_tax_profile(raw_payload):
    registration_status = str(
        raw_payload.get("gst_registration_status") or "unregistered"
    ).strip().lower()
    if registration_status not in GST_REGISTRATION_STATUSES:
        raise ValueError("Select a valid GST registration status.")

    state_name = _clean_single_line(
        raw_payload.get("state_name"), "State", maximum=100
    )
    expected_state_code = INDIA_STATE_CODES.get(state_name)
    if not expected_state_code:
        raise ValueError("Select a valid Indian state or territory.")

    submitted_state_code = str(raw_payload.get("state_code") or "").strip()
    if submitted_state_code and submitted_state_code != expected_state_code:
        raise ValueError("State code does not match the selected state.")

    gstin = str(raw_payload.get("gstin") or "").strip().upper()
    pan = str(raw_payload.get("pan") or "").strip().upper()

    if registration_status in REGISTERED_GST_STATUSES:
        if not gstin:
            raise ValueError("GSTIN is required for a registered party.")
        if not GSTIN_PATTERN.fullmatch(gstin):
            raise ValueError("Enter a valid 15-character GSTIN.")
        if gstin[:2] != expected_state_code:
            raise ValueError("GSTIN state code does not match the selected state.")

        gstin_pan = gstin[2:12]
        if pan and pan != gstin_pan:
            raise ValueError("PAN does not match the PAN embedded in GSTIN.")
        pan = pan or gstin_pan
    elif gstin:
        raise ValueError(
            "Remove GSTIN when the party is unregistered or exempt."
        )

    if pan and not PAN_PATTERN.fullmatch(pan):
        raise ValueError("Enter a valid 10-character PAN.")

    postal_code = str(raw_payload.get("postal_code") or "").strip()
    if not POSTAL_CODE_PATTERN.fullmatch(postal_code):
        raise ValueError("Enter a valid 6-digit PIN code.")

    return {
        "gst_registration_status": registration_status,
        "gstin": gstin,
        "pan": pan,
        "state_name": state_name,
        "state_code": expected_state_code,
        "postal_code": postal_code,
    }


def _sanitize_ledger_code(value):
    code = re.sub(r"[^A-Z0-9_\-]", "_", str(value or "").upper())
    code = re.sub(r"_+", "_", code).strip("_-")
    return code[:40]


def _base_generated_code(party_role, name):
    prefix = PARTY_ROLES[party_role]["code_prefix"]
    stem = _sanitize_ledger_code(name)[:30] or "PARTY"
    candidate = f"{prefix}_{stem}"[:40]
    if len(candidate) < 3:
        candidate = f"{prefix}_PARTY"
    return candidate


def _ledger_code_is_reserved(entity_id, code, exclude_id=None):
    query = {
        "accounting_entity_id": entity_id,
        "$or": [
            {"ledger_code": code},
            {"ledger_code_reservation_key": f"{entity_id}:{code}"},
        ],
    }
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    return mongo.db[LEDGER_COLLECTION].find_one(query, {"_id": 1}) is not None


def _resolve_ledger_code(entity_id, party_role, name, requested_code="", exclude_id=None):
    requested = _sanitize_ledger_code(requested_code)
    if requested:
        if not LEDGER_CODE_PATTERN.fullmatch(requested):
            raise ValueError(
                "Ledger code must start with a letter and use 3 to 40 uppercase letters, numbers, underscores or hyphens."
            )
        if _ledger_code_is_reserved(entity_id, requested, exclude_id=exclude_id):
            raise ValueError("That ledger code is already reserved.")
        return requested

    base = _base_generated_code(party_role, name)
    for sequence in range(1, 1000):
        suffix = "" if sequence == 1 else f"_{sequence}"
        candidate = f"{base[:40-len(suffix)]}{suffix}"
        if not _ledger_code_is_reserved(entity_id, candidate, exclude_id=exclude_id):
            return candidate
    raise RuntimeError("Could not generate a unique ledger code. Enter one manually.")


def _validate_party_payload(raw_payload, entity, existing=None):
    party_role = str(raw_payload.get("party_role") or "").strip().lower()
    if party_role not in PARTY_ROLES:
        raise ValueError("Select Supplier or Customer / Party.")

    legal_name = _clean_single_line(
        raw_payload.get("legal_name"), "Legal name", maximum=180
    )
    display_name = _clean_single_line(
        raw_payload.get("display_name") or legal_name,
        "Ledger display name",
        maximum=180,
    )
    normalized_name = _normalize_name(display_name)

    tax_profile = _validate_tax_profile(raw_payload)
    group = _resolve_party_group(entity["_id"], party_role)

    existing_id = existing.get("_id") if existing else None
    name_conflict = mongo.db[LEDGER_COLLECTION].find_one(
        {
            "accounting_entity_id": entity["_id"],
            "normalized_name": normalized_name,
            "is_deleted": False,
            **({"_id": {"$ne": existing_id}} if existing_id else {}),
        },
        {"name": 1, "ledger_code": 1},
    )
    if name_conflict:
        raise ValueError(
            f"Another active ledger already uses this name: {name_conflict.get('name') or name_conflict.get('ledger_code')}."
        )

    requested_code = raw_payload.get("ledger_code")
    if existing:
        submitted_code = _sanitize_ledger_code(requested_code)
        existing_code = existing.get("ledger_code") or ""
        if submitted_code and submitted_code != existing_code:
            raise ValueError(
                "Ledger code is a permanent master identifier and cannot be changed after creation."
            )
        ledger_code = existing_code
    else:
        ledger_code = _resolve_ledger_code(
            entity["_id"],
            party_role,
            display_name,
            requested_code=requested_code,
            exclude_id=existing_id,
        )

    tax_identity_key = None
    if tax_profile["gstin"]:
        tax_identity_key = (
            f"{entity['_id']}:{party_role}:{tax_profile['gstin']}"
        )
        identity_query = {
            "party_tax_identity_key": tax_identity_key,
        }
        if existing_id:
            identity_query["_id"] = {"$ne": existing_id}
        conflict = mongo.db[LEDGER_COLLECTION].find_one(
            identity_query, {"name": 1, "ledger_code": 1}
        )
        if conflict:
            raise ValueError(
                f"A {PARTY_ROLES[party_role]['label'].lower()} ledger already exists for this GSTIN: "
                f"{conflict.get('name') or conflict.get('ledger_code')}."
            )

    credit_days_default = _default_credit_days(entity["_id"])
    credit_period_days = _parse_int(
        raw_payload.get("credit_period_days")
        if str(raw_payload.get("credit_period_days") or "").strip()
        else credit_days_default,
        "Credit period",
        minimum=0,
        maximum=3650,
    )
    credit_limit = _parse_decimal(
        raw_payload.get("credit_limit") or "0",
        "Credit limit",
    )

    payload = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "ledger_code": ledger_code,
        "ledger_code_reservation_key": f"{entity['_id']}:{ledger_code}",
        "name": display_name,
        "normalized_name": normalized_name,
        "legal_name": legal_name,
        "trade_name": _clean_single_line(
            raw_payload.get("trade_name"), "Trade name", maximum=180, required=False
        ),
        "party_role": party_role,
        "party_role_label": PARTY_ROLES[party_role]["label"],
        "party_type": party_role,
        "ledger_type": "party",
        "is_party_ledger": True,
        "account_group_id": group["_id"],
        "account_group_id_str": str(group["_id"]),
        "account_group_system_key": PARTY_ROLES[party_role][
            "account_group_system_key"
        ],
        "account_group_name": group.get("name") or "",
        "normal_balance": PARTY_ROLES[party_role]["normal_balance"],
        "posting_policy": "authorized_voucher",
        "balance_managed_by_postings": True,
        "opening_balance_locked": True,
        "is_system": False,
        "is_protected": False,
        "name_locked": False,
        "deletion_locked": True,
        "contact_person": _clean_single_line(
            raw_payload.get("contact_person"),
            "Contact person",
            maximum=120,
            required=False,
        ),
        "phone": _normalize_phone(raw_payload.get("phone")),
        "email": _normalize_email(raw_payload.get("email")),
        "address_line_1": _clean_single_line(
            raw_payload.get("address_line_1"),
            "Address line 1",
            maximum=220,
        ),
        "address_line_2": _clean_single_line(
            raw_payload.get("address_line_2"),
            "Address line 2",
            maximum=220,
            required=False,
        ),
        "city": _clean_single_line(
            raw_payload.get("city"), "City", maximum=100
        ),
        "district": _clean_single_line(
            raw_payload.get("district"), "District", maximum=100
        ),
        **tax_profile,
        "party_tax_identity_key": tax_identity_key,
        "credit_period_days": credit_period_days,
        "credit_limit": Decimal128(credit_limit),
        "credit_limit_currency": "INR",
        "remarks": _clean_multiline(
            raw_payload.get("remarks"), "Remarks", maximum=1000
        ),
        "requires_approval": True,
        "approval_policy": "maker_checker",
        "is_deleted": False,
    }
    return payload


# ---------------------------------------------------------------------------
# Audit and serialization
# ---------------------------------------------------------------------------


def _change_event(action, actor, previous_status=None, new_status=None, changed_fields=None, remarks=""):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "changed_fields": sorted(set(changed_fields or [])),
        "remarks": str(remarks or "")[:1000],
        "at": now_utc(),
    }


def _record_audit(ledger, actor, action, previous_status=None, changed_fields=None, remarks=""):
    timestamp = now_utc()
    audit_document = {
        "module": "accounting",
        "action": action,
        "accounting_entity_id": ledger.get("accounting_entity_id"),
        "accounting_entity_id_str": str(
            ledger.get("accounting_entity_id") or ""
        ),
        "entity_type": "party_ledger",
        "entity_id": ledger.get("_id"),
        "entity_id_str": str(ledger.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": ledger.get("status"),
        "metadata": {
            "party_master_id": ledger.get("party_master_id"),
            "ledger_code": ledger.get("ledger_code"),
            "ledger_name": ledger.get("name"),
            "party_role": ledger.get("party_role"),
            "account_group_system_key": ledger.get(
                "account_group_system_key"
            ),
            "gst_registration_status": ledger.get(
                "gst_registration_status"
            ),
            "gstin": ledger.get("gstin"),
            "pan": ledger.get("pan"),
            "version": int(ledger.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": remarks or "Party ledger workflow updated.",
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit_document)
    except Exception as exc:
        try:
            mongo.db[LEDGER_COLLECTION].update_one(
                {"_id": ledger.get("_id")},
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

    mongo.db[LEDGER_COLLECTION].update_one(
        {"_id": ledger.get("_id")},
        {
            "$set": {
                "audit_sync_required": False,
                "audit_sync_action": None,
                "audit_sync_completed_at": timestamp,
            }
        },
    )
    return True


def serialize_party_ledger(ledger):
    if not ledger:
        return None

    return {
        "id": str(ledger.get("_id") or ""),
        "party_master_id": ledger.get("party_master_id") or "",
        "accounting_entity_id": str(ledger.get("accounting_entity_id") or ""),
        "ledger_code": ledger.get("ledger_code") or "",
        "name": ledger.get("name") or "",
        "legal_name": ledger.get("legal_name") or "",
        "trade_name": ledger.get("trade_name") or "",
        "party_role": ledger.get("party_role") or "",
        "party_role_label": ledger.get("party_role_label") or PARTY_ROLES.get(
            ledger.get("party_role"), {}
        ).get("label", "Party"),
        "account_group_name": ledger.get("account_group_name") or "",
        "account_group_system_key": ledger.get("account_group_system_key") or "",
        "normal_balance": ledger.get("normal_balance") or "",
        "contact_person": ledger.get("contact_person") or "",
        "phone": ledger.get("phone") or "",
        "email": ledger.get("email") or "",
        "address_line_1": ledger.get("address_line_1") or "",
        "address_line_2": ledger.get("address_line_2") or "",
        "city": ledger.get("city") or "",
        "district": ledger.get("district") or "",
        "state_name": ledger.get("state_name") or "",
        "state_code": ledger.get("state_code") or "",
        "postal_code": ledger.get("postal_code") or "",
        "gst_registration_status": ledger.get("gst_registration_status") or "",
        "gst_registration_status_label": GST_REGISTRATION_STATUSES.get(
            ledger.get("gst_registration_status"),
            ledger.get("gst_registration_status") or "",
        ),
        "gstin": ledger.get("gstin") or "",
        "pan": ledger.get("pan") or "",
        "credit_period_days": int(ledger.get("credit_period_days") or 0),
        "credit_limit": _decimal_string(ledger.get("credit_limit")),
        "credit_limit_currency": ledger.get("credit_limit_currency") or "INR",
        "remarks": ledger.get("remarks") or "",
        "requires_approval": ledger.get("requires_approval") is True,
        "approval_policy": ledger.get("approval_policy") or "maker_checker",
        "status": ledger.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(
            ledger.get("status"), str(ledger.get("status") or "").title()
        ),
        "is_active": ledger.get("is_active") is True,
        "is_deleted": ledger.get("is_deleted") is True,
        "version": int(ledger.get("version") or 1),
        "created_by": str(ledger.get("created_by") or ""),
        "created_by_name": ledger.get("created_by_name") or "",
        "created_at": ledger.get("created_at"),
        "updated_by_name": ledger.get("updated_by_name") or "",
        "updated_at": ledger.get("updated_at"),
        "submitted_by_name": ledger.get("submitted_by_name") or "",
        "submitted_at": ledger.get("submitted_at"),
        "approved_by_name": ledger.get("approved_by_name") or "",
        "approved_at": ledger.get("approved_at"),
        "return_reason": ledger.get("return_reason") or "",
        "withdraw_reason": ledger.get("withdraw_reason") or "",
        "deactivation_reason": ledger.get("deactivation_reason") or "",
        "reactivation_reason": ledger.get("reactivation_reason") or "",
        "audit_sync_required": ledger.get("audit_sync_required") is True,
        "change_history": ledger.get("change_history") or [],
    }


# ---------------------------------------------------------------------------
# Create and maker workflow
# ---------------------------------------------------------------------------


def create_party_ledger(accounting_entity_id, actor_user_id, raw_payload):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_party_ledger_indexes()
    ensure_account_group_indexes()

    group_overview = get_account_group_overview(entity["_id"], actor["_id"])
    if not group_overview.get("health", {}).get("is_complete"):
        raise RuntimeError(
            "Protected account groups are incomplete. Verify and repair Stage 3 Batch 1 before creating party ledgers."
        )

    canonical = _validate_party_payload(raw_payload, entity)
    timestamp = now_utc()
    document = {
        **canonical,
        "party_master_id": uuid4().hex,
        "status": STATUS_DRAFT,
        "is_active": False,
        "version": 1,
        "revision_number": 1,
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
                "create_party_ledger_draft",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                changed_fields=sorted(canonical.keys()),
            )
        ],
        "audit_sync_required": False,
    }

    try:
        result = mongo.db[LEDGER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "A ledger with the same code, name or GST identity was created concurrently. Refresh and try again."
        ) from exc

    _record_audit(
        document,
        actor,
        "create_party_ledger_draft",
        previous_status=None,
        changed_fields=sorted(canonical.keys()),
        remarks=f"{document['party_role_label']} ledger draft created.",
    )
    return {
        "ledger": serialize_party_ledger(document),
        "message": f"{document['party_role_label']} ledger draft created.",
    }


def update_party_ledger(ledger_id, actor_user_id, raw_payload, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    ledger = _get_party_ledger(ledger_id)
    entity = _assert_party_ledger_entity_access(actor, ledger, EDIT_PERMISSION)

    if ledger.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned party ledgers can be edited.")
    if str(ledger.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can edit this party ledger.")

    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid ledger version. Refresh and try again.") from exc
    if expected_version != int(ledger.get("version") or 1):
        raise RuntimeError("This party ledger changed. Refresh before saving.")

    canonical = _validate_party_payload(raw_payload, entity, existing=ledger)
    editable_fields = sorted(canonical.keys())
    changed_fields = [
        field for field in editable_fields if ledger.get(field) != canonical.get(field)
    ]
    if not changed_fields:
        return {
            "ledger": serialize_party_ledger(ledger),
            "message": "No party-ledger changes were detected.",
        }

    timestamp = now_utc()
    next_version = expected_version + 1
    updates = {field: canonical[field] for field in changed_fields}
    updates.update(
        {
            "version": next_version,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
            "return_reason": None,
            "withdraw_reason": None,
        }
    )

    try:
        result = mongo.db[LEDGER_COLLECTION].update_one(
            {
                "_id": ledger["_id"],
                "version": expected_version,
                "status": {"$in": list(EDITABLE_STATUSES)},
                "created_by": actor["_id"],
            },
            {
                "$set": updates,
                "$push": {
                    "change_history": _change_event(
                        "update_party_ledger_draft",
                        actor,
                        previous_status=ledger.get("status"),
                        new_status=ledger.get("status"),
                        changed_fields=changed_fields,
                    )
                },
            },
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "A ledger with the same code, name or GST identity already exists."
        ) from exc

    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "update_party_ledger_draft",
        previous_status=ledger.get("status"),
        changed_fields=changed_fields,
        remarks="Party ledger draft updated.",
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger draft updated.",
    }


def submit_party_ledger(ledger_id, actor_user_id, expected_version, submission_note=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, SUBMIT_PERMISSION)

    if ledger.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned party ledgers can be submitted.")
    if str(ledger.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can submit this party ledger.")

    expected_version = int(expected_version)
    if expected_version != int(ledger.get("version") or 1):
        raise RuntimeError("This party ledger changed. Refresh before submitting.")

    note = _clean_multiline(
        submission_note, "Submission note", maximum=1000, required=False
    )
    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": {"$in": list(EDITABLE_STATUSES)},
            "created_by": actor["_id"],
        },
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "is_active": False,
                "version": expected_version + 1,
                "submission_note": note,
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_by_name": actor.get("resolved_name") or "",
                "submitted_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
                "return_reason": None,
                "withdraw_reason": None,
            },
            "$push": {
                "change_history": _change_event(
                    "submit_party_ledger",
                    actor,
                    previous_status=ledger.get("status"),
                    new_status=STATUS_PENDING_APPROVAL,
                    remarks=note,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "submit_party_ledger",
        previous_status=ledger.get("status"),
        remarks=note or "Party ledger submitted for AVPL Admin approval.",
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger submitted for approval.",
    }


def withdraw_party_ledger(ledger_id, actor_user_id, expected_version, reason=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, WITHDRAW_PERMISSION)

    if ledger.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending party ledger can be withdrawn.")
    if str(ledger.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can withdraw this party ledger.")

    expected_version = int(expected_version)
    reason = _clean_multiline(reason, "Withdrawal reason", maximum=1000, required=True)
    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": STATUS_PENDING_APPROVAL,
            "created_by": actor["_id"],
        },
        {
            "$set": {
                "status": STATUS_DRAFT,
                "is_active": False,
                "version": expected_version + 1,
                "withdraw_reason": reason,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "withdraw_party_ledger",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_DRAFT,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "withdraw_party_ledger",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=reason,
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger withdrawn to draft.",
    }


def cancel_party_ledger(ledger_id, actor_user_id, expected_version, reason=""):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, CANCEL_PERMISSION)

    if ledger.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only a draft or returned party ledger can be cancelled.")
    if str(ledger.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original maker can cancel this party ledger.")

    expected_version = int(expected_version)
    reason = _clean_multiline(reason, "Cancellation reason", maximum=1000, required=True)
    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": {"$in": list(EDITABLE_STATUSES)},
            "created_by": actor["_id"],
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
                "is_active": False,
                "is_deleted": True,
                "party_tax_identity_key": None,
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
            "$push": {
                "change_history": _change_event(
                    "cancel_party_ledger",
                    actor,
                    previous_status=ledger.get("status"),
                    new_status=STATUS_CANCELLED,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "cancel_party_ledger",
        previous_status=ledger.get("status"),
        remarks=reason,
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger draft cancelled without deleting its audit history.",
    }


# ---------------------------------------------------------------------------
# Checker and lifecycle workflow
# ---------------------------------------------------------------------------


def approve_party_ledger(ledger_id, actor_user_id, expected_version, approval_note=""):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    ledger = _get_party_ledger(ledger_id)
    entity = _assert_party_ledger_entity_access(actor, ledger, APPROVE_PERMISSION)

    if ledger.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending party ledger can be approved.")
    if str(ledger.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot approve the same party ledger.")

    expected_version = int(expected_version)
    note = _clean_multiline(
        approval_note, "Approval note", maximum=1000, required=False
    )

    # Revalidate group and tax identity at approval time.
    _resolve_party_group(entity["_id"], ledger.get("party_role"))
    if ledger.get("party_tax_identity_key"):
        conflict = mongo.db[LEDGER_COLLECTION].find_one(
            {
                "party_tax_identity_key": ledger["party_tax_identity_key"],
                "_id": {"$ne": ledger["_id"]},
            },
            {"name": 1},
        )
        if conflict:
            raise RuntimeError(
                f"Another party ledger now uses this GST identity: {conflict.get('name')}."
            )

    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": STATUS_PENDING_APPROVAL,
        },
        {
            "$set": {
                "status": STATUS_ACTIVE,
                "is_active": True,
                "is_deleted": False,
                "version": expected_version + 1,
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_by_name": actor.get("resolved_name") or "",
                "approved_at": timestamp,
                "approval_note": note,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
                "return_reason": None,
            },
            "$push": {
                "change_history": _change_event(
                    "approve_party_ledger",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_ACTIVE,
                    remarks=note,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "approve_party_ledger",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=note or "Party ledger approved and activated.",
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger approved and activated.",
    }


def return_party_ledger(ledger_id, actor_user_id, expected_version, return_reason):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, RETURN_PERMISSION)

    if ledger.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending party ledger can be returned.")
    if str(ledger.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot review the same party ledger.")

    expected_version = int(expected_version)
    reason = _clean_multiline(
        return_reason, "Correction reason", maximum=1000, required=True
    )
    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
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
                    "return_party_ledger",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_RETURNED,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "return_party_ledger",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=reason,
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger returned to Accounts for correction.",
    }


def deactivate_party_ledger(ledger_id, actor_user_id, expected_version, reason):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, DEACTIVATE_PERMISSION)

    if ledger.get("status") != STATUS_ACTIVE:
        raise ValueError("Only an active party ledger can be deactivated.")

    expected_version = int(expected_version)
    reason = _clean_multiline(
        reason, "Deactivation reason", maximum=1000, required=True
    )
    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": STATUS_ACTIVE,
        },
        {
            "$set": {
                "status": STATUS_INACTIVE,
                "is_active": False,
                "version": expected_version + 1,
                "deactivation_reason": reason,
                "deactivated_by": actor["_id"],
                "deactivated_by_name": actor.get("resolved_name") or "",
                "deactivated_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "deactivate_party_ledger",
                    actor,
                    previous_status=STATUS_ACTIVE,
                    new_status=STATUS_INACTIVE,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "deactivate_party_ledger",
        previous_status=STATUS_ACTIVE,
        remarks=reason,
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger deactivated. Historical postings remain preserved.",
    }


def reactivate_party_ledger(ledger_id, actor_user_id, expected_version, reason):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin", "super_admin"})
    ledger = _get_party_ledger(ledger_id)
    _assert_party_ledger_entity_access(actor, ledger, REACTIVATE_PERMISSION)

    if ledger.get("status") != STATUS_INACTIVE:
        raise ValueError("Only an inactive party ledger can be reactivated.")

    expected_version = int(expected_version)
    reason = _clean_multiline(
        reason, "Reactivation reason", maximum=1000, required=True
    )
    _resolve_party_group(ledger["accounting_entity_id"], ledger.get("party_role"))

    timestamp = now_utc()
    result = mongo.db[LEDGER_COLLECTION].update_one(
        {
            "_id": ledger["_id"],
            "version": expected_version,
            "status": STATUS_INACTIVE,
        },
        {
            "$set": {
                "status": STATUS_ACTIVE,
                "is_active": True,
                "version": expected_version + 1,
                "reactivation_reason": reason,
                "reactivated_by": actor["_id"],
                "reactivated_by_name": actor.get("resolved_name") or "",
                "reactivated_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "reactivate_party_ledger",
                    actor,
                    previous_status=STATUS_INACTIVE,
                    new_status=STATUS_ACTIVE,
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This party ledger changed. Refresh and try again.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "reactivate_party_ledger",
        previous_status=STATUS_INACTIVE,
        remarks=reason,
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Party ledger reactivated.",
    }


# ---------------------------------------------------------------------------
# Dashboard read model and future posting helper
# ---------------------------------------------------------------------------


def get_party_ledger_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    default_credit_days = _default_credit_days(entity_id) if entity_id else 0
    return {
        "party_roles": {
            key: definition["label"] for key, definition in PARTY_ROLES.items()
        },
        "gst_registration_statuses": dict(GST_REGISTRATION_STATUSES),
        "states": dict(INDIA_STATE_CODES),
        "status_labels": dict(STATUS_LABELS),
        "default_credit_days": default_credit_days,
    }


def get_party_ledger_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_party_ledger_indexes()

    rows = list(
        mongo.db[LEDGER_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_party_ledger": True,
            }
        ).sort([("updated_at", DESCENDING), ("name", ASCENDING)])
    )
    serialized = [serialize_party_ledger(row) for row in rows]

    counts = {status: 0 for status in STATUS_LABELS}
    role_counts = {role: 0 for role in PARTY_ROLES}
    audit_recovery_count = 0
    for row in rows:
        status = row.get("status") or STATUS_DRAFT
        counts[status] = counts.get(status, 0) + 1
        role = row.get("party_role")
        if role in role_counts and status != STATUS_CANCELLED:
            role_counts[role] += 1
        if row.get("audit_sync_required") is True:
            audit_recovery_count += 1

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "rows": serialized,
        "active_rows": [row for row in serialized if row["status"] == STATUS_ACTIVE],
        "pending_rows": [
            row for row in serialized if row["status"] == STATUS_PENDING_APPROVAL
        ],
        "working_rows": [
            row for row in serialized if row["status"] in EDITABLE_STATUSES
        ],
        "inactive_rows": [
            row for row in serialized if row["status"] == STATUS_INACTIVE
        ],
        "cancelled_rows": [
            row for row in serialized if row["status"] == STATUS_CANCELLED
        ],
        "counts": counts,
        "role_counts": role_counts,
        "total_count": len(rows),
        "non_cancelled_count": sum(
            1 for row in serialized if row["status"] != STATUS_CANCELLED
        ),
        "audit_recovery_count": audit_recovery_count,
        "options": get_party_ledger_option_catalog(entity["_id"]),
        "form_defaults": {
            "party_role": PARTY_ROLE_SUPPLIER,
            "gst_registration_status": "unregistered",
            "state_name": "Assam",
            "state_code": INDIA_STATE_CODES.get("Assam", "18"),
            "credit_period_days": _default_credit_days(entity["_id"]),
            "credit_limit": "0.00",
        },
    }


def get_active_party_ledger_for_posting(accounting_entity_id, ledger_id, party_role=None):
    """Return a validated active party ledger for future posting services."""
    entity_id = _to_object_id(accounting_entity_id)
    ledger_object_id = _to_object_id(ledger_id)
    if not entity_id or not ledger_object_id:
        raise ValueError("Invalid Accounting entity or party ledger.")

    query = {
        "_id": ledger_object_id,
        "accounting_entity_id": entity_id,
        "is_party_ledger": True,
        "status": STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": False,
    }
    if party_role:
        if party_role not in PARTY_ROLES:
            raise ValueError("Invalid party role.")
        query["party_role"] = party_role

    ledger = mongo.db[LEDGER_COLLECTION].find_one(query)
    if not ledger:
        raise ValueError("The selected party ledger is not active for posting.")
    return ledger


# ---------------------------------------------------------------------------
# Stage 2 AVPL Supplier Master integration
# ---------------------------------------------------------------------------


def create_supplier_from_operational_master(
    accounting_entity_id,
    actor_user_id,
    raw_payload,
):
    """
    Create a supplier in the existing Accounting party-ledger master.

    Accounts users create a normal maker draft. AVPL Admin and Super Admin
    create an immediately active supplier because they are the authorized
    checker for the AVPL supplier master.
    """
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"accounts", "avpl_admin", "super_admin"},
    )
    payload = dict(raw_payload or {})
    payload["party_role"] = PARTY_ROLE_SUPPLIER

    if actor.get("resolved_role") == "accounts":
        return create_party_ledger(
            accounting_entity_id,
            actor_user_id,
            payload,
        )

    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], APPROVE_PERMISSION)
    ensure_party_ledger_indexes()
    ensure_account_group_indexes()

    canonical = _validate_party_payload(payload, entity)
    timestamp = now_utc()
    canonical.update(
        {
            "requires_approval": False,
            "approval_policy": "avpl_admin_auto_approval",
        }
    )
    document = {
        **canonical,
        "party_master_id": uuid4().hex,
        "status": STATUS_ACTIVE,
        "is_active": True,
        "version": 1,
        "revision_number": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "approved_by": actor["_id"],
        "approved_by_str": str(actor["_id"]),
        "approved_by_name": actor.get("resolved_name") or "",
        "approved_at": timestamp,
        "approval_note": "Auto-approved from AVPL Supplier Master.",
        "change_history": [
            _change_event(
                "create_and_activate_supplier",
                actor,
                previous_status=None,
                new_status=STATUS_ACTIVE,
                changed_fields=sorted(canonical.keys()),
                remarks="Supplier created and activated by AVPL Admin.",
            )
        ],
        "audit_sync_required": False,
    }

    try:
        result = mongo.db[LEDGER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "A supplier with the same code, name or GST identity already exists."
        ) from exc

    _record_audit(
        document,
        actor,
        "create_and_activate_supplier",
        previous_status=None,
        changed_fields=sorted(canonical.keys()),
        remarks="Supplier created and activated from AVPL Supplier Master.",
    )
    return {
        "ledger": serialize_party_ledger(document),
        "message": "Supplier created and activated successfully.",
    }


def update_supplier_from_operational_master(
    ledger_id,
    actor_user_id,
    raw_payload,
    expected_version,
):
    """Update a supplier without creating a duplicate operational master."""
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"accounts", "avpl_admin", "super_admin"},
    )
    payload = dict(raw_payload or {})
    payload["party_role"] = PARTY_ROLE_SUPPLIER

    if actor.get("resolved_role") == "accounts":
        return update_party_ledger(
            ledger_id,
            actor_user_id,
            payload,
            expected_version,
        )

    ledger = _get_party_ledger(ledger_id)
    if ledger.get("party_role") != PARTY_ROLE_SUPPLIER:
        raise ValueError("The selected ledger is not a supplier.")

    entity = _assert_party_ledger_entity_access(
        actor,
        ledger,
        APPROVE_PERMISSION,
    )
    if ledger.get("status") not in {STATUS_ACTIVE, STATUS_INACTIVE}:
        raise ValueError(
            "Pending supplier requests must be approved or returned before editing."
        )

    try:
        version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid supplier version. Refresh and try again.") from exc
    if version != int(ledger.get("version") or 1):
        raise RuntimeError("This supplier changed. Refresh before saving.")

    canonical = _validate_party_payload(payload, entity, existing=ledger)
    canonical.update(
        {
            "requires_approval": False,
            "approval_policy": "avpl_admin_auto_approval",
        }
    )
    changed_fields = [
        key for key, value in canonical.items() if ledger.get(key) != value
    ]
    if not changed_fields:
        return {
            "ledger": serialize_party_ledger(ledger),
            "message": "No supplier changes were detected.",
        }

    timestamp = now_utc()
    updates = {key: canonical[key] for key in changed_fields}
    updates.update(
        {
            "version": version + 1,
            "revision_number": int(ledger.get("revision_number") or 1) + 1,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
        }
    )

    try:
        result = mongo.db[LEDGER_COLLECTION].update_one(
            {
                "_id": ledger["_id"],
                "version": version,
                "status": ledger.get("status"),
            },
            {
                "$set": updates,
                "$push": {
                    "change_history": _change_event(
                        "update_active_supplier",
                        actor,
                        previous_status=ledger.get("status"),
                        new_status=ledger.get("status"),
                        changed_fields=changed_fields,
                        remarks="Supplier master updated by AVPL Admin.",
                    )
                },
            },
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "A supplier with the same code, name or GST identity already exists."
        ) from exc

    if result.matched_count != 1:
        raise RuntimeError("This supplier changed. Refresh before saving.")

    updated = _get_party_ledger(ledger_id)
    _record_audit(
        updated,
        actor,
        "update_active_supplier",
        previous_status=ledger.get("status"),
        changed_fields=changed_fields,
        remarks="Supplier master updated from the AVPL operational screen.",
    )
    return {
        "ledger": serialize_party_ledger(updated),
        "message": "Supplier updated successfully.",
    }


def get_supplier_master_overview(accounting_entity_id, actor_user_id):
    """Return only supplier party ledgers for the AVPL Supplier Master page."""
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"accounts", "avpl_admin", "super_admin"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_party_ledger_indexes()

    rows = list(
        mongo.db[LEDGER_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_party_ledger": True,
                "party_role": PARTY_ROLE_SUPPLIER,
                "is_deleted": False,
            }
        ).sort([("updated_at", DESCENDING), ("name", ASCENDING)])
    )
    serialized = [serialize_party_ledger(row) for row in rows]
    counts = {status: 0 for status in STATUS_LABELS}
    for row in rows:
        status = row.get("status") or STATUS_DRAFT
        counts[status] = counts.get(status, 0) + 1

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "rows": serialized,
        "active_rows": [row for row in serialized if row["status"] == STATUS_ACTIVE],
        "pending_rows": [
            row for row in serialized if row["status"] == STATUS_PENDING_APPROVAL
        ],
        "working_rows": [
            row for row in serialized if row["status"] in EDITABLE_STATUSES
        ],
        "inactive_rows": [
            row for row in serialized if row["status"] == STATUS_INACTIVE
        ],
        "counts": counts,
        "total_count": len(serialized),
        "options": get_party_ledger_option_catalog(entity["_id"]),
        "form_defaults": {
            "party_role": PARTY_ROLE_SUPPLIER,
            "gst_registration_status": "unregistered",
            "state_name": "Assam",
            "state_code": INDIA_STATE_CODES.get("Assam", "18"),
            "credit_period_days": _default_credit_days(entity["_id"]),
            "credit_limit": "0.00",
        },
    }
