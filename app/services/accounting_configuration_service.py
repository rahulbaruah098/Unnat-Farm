from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import re
from uuid import uuid4

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


PROFILE_COLLECTION = "accounting_entity_settings"
POLICY_COLLECTION = "accounting_settings"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_APPROVED = "approved"
STATUS_SUPERSEDED = "superseded"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}

INDIA_STATE_CODES = {
    "Jammu and Kashmir": "01",
    "Himachal Pradesh": "02",
    "Punjab": "03",
    "Chandigarh": "04",
    "Uttarakhand": "05",
    "Haryana": "06",
    "Delhi": "07",
    "Rajasthan": "08",
    "Uttar Pradesh": "09",
    "Bihar": "10",
    "Sikkim": "11",
    "Arunachal Pradesh": "12",
    "Nagaland": "13",
    "Manipur": "14",
    "Mizoram": "15",
    "Tripura": "16",
    "Meghalaya": "17",
    "Assam": "18",
    "West Bengal": "19",
    "Jharkhand": "20",
    "Odisha": "21",
    "Chhattisgarh": "22",
    "Madhya Pradesh": "23",
    "Gujarat": "24",
    "Daman and Diu (Legacy code)": "25",
    "Dadra and Nagar Haveli and Daman and Diu": "26",
    "Maharashtra": "27",
    "Andhra Pradesh (Legacy code)": "28",
    "Karnataka": "29",
    "Goa": "30",
    "Lakshadweep": "31",
    "Kerala": "32",
    "Tamil Nadu": "33",
    "Puducherry": "34",
    "Andaman and Nicobar Islands": "35",
    "Telangana": "36",
    "Andhra Pradesh": "37",
    "Ladakh": "38",
    "Other Territory": "97",
    "Centre Jurisdiction": "99",
}

GST_REGISTRATION_STATUSES = {
    "registered_regular": "Registered - Regular",
    "registered_composition": "Registered - Composition",
    "unregistered": "Unregistered",
    "exempt": "Exempt / not liable",
}

VALUATION_METHODS = {
    "weighted_average": "Weighted Average",
}

NEGATIVE_STOCK_POLICIES = {
    "block": "Block negative stock",
    "require_approval": "Require authorized approval",
}

BACKDATED_ENTRY_POLICIES = {
    "block": "Block backdated entries",
    "require_approval": "Allow only after approval",
    "within_open_year": "Allow within open Financial Year",
}

ROUNDING_METHODS = {
    "two_decimals": "Keep two decimal places",
    "nearest_rupee": "Round to nearest rupee",
    "none": "No automatic rounding",
}

SUPPORTING_DOCUMENT_POLICIES = {
    "required_for_sensitive": "Required for sensitive transactions",
    "required_for_all": "Required for all posted transactions",
    "optional": "Optional",
}

DEFAULT_PAYMENT_MODES = {
    "cash": "Cash",
    "bank": "Bank transfer",
    "upi": "UPI",
    "cheque": "Cheque",
    "credit": "Credit",
}

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_PATTERN = re.compile(
    r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$"
)
POSTAL_CODE_PATTERN = re.compile(r"^[1-9][0-9]{5}$")


PROFILE_PERMISSION_PREFIX = "accounting.entity_settings"
POLICY_PERMISSION_PREFIX = "accounting.settings"


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


def ensure_accounting_configuration_indexes():
    """Create configuration indexes without dropping any existing index."""
    for collection_name, prefix in (
        (PROFILE_COLLECTION, "accounting_entity_settings"),
        (POLICY_COLLECTION, "accounting_settings"),
    ):
        collection = mongo.db[collection_name]

        _ensure_exact_index(
            collection,
            [
                ("accounting_entity_id", ASCENDING),
                ("revision_number", ASCENDING),
            ],
            name=f"{prefix}_entity_revision_unique",
            unique=True,
            partialFilterExpression={"is_deleted": False},
        )
        _ensure_exact_index(
            collection,
            [
                ("accounting_entity_id", ASCENDING),
                ("is_working_copy", ASCENDING),
            ],
            name=f"{prefix}_working_copy_unique",
            unique=True,
            partialFilterExpression={
                "is_deleted": False,
                "is_working_copy": True,
            },
        )
        _ensure_exact_index(
            collection,
            [
                ("accounting_entity_id", ASCENDING),
                ("is_active", ASCENDING),
            ],
            name=f"{prefix}_active_unique",
            unique=True,
            partialFilterExpression={
                "is_deleted": False,
                "is_active": True,
            },
        )
        _ensure_exact_index(
            collection,
            [("status", ASCENDING), ("submitted_at", ASCENDING)],
            name=f"{prefix}_approval_queue_idx",
        )
        _ensure_exact_index(
            collection,
            [
                ("accounting_entity_id", ASCENDING),
                ("updated_at", DESCENDING),
            ],
            name=f"{prefix}_entity_updated_idx",
        )


def get_configuration_option_catalog():
    return {
        "india_states": [
            {"name": name, "code": code}
            for name, code in sorted(
                INDIA_STATE_CODES.items(),
                key=lambda row: (int(row[1]), row[0]),
            )
        ],
        "gst_registration_statuses": GST_REGISTRATION_STATUSES,
        "valuation_methods": VALUATION_METHODS,
        "negative_stock_policies": NEGATIVE_STOCK_POLICIES,
        "backdated_entry_policies": BACKDATED_ENTRY_POLICIES,
        "rounding_methods": ROUNDING_METHODS,
        "supporting_document_policies": SUPPORTING_DOCUMENT_POLICIES,
        "default_payment_modes": DEFAULT_PAYMENT_MODES,
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
        raise PermissionError("You are not authorized to perform this configuration action.")

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
    })
    if not entity:
        raise ValueError("The Accounting entity was not found or is inactive.")

    return entity


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


@contextmanager
def _configuration_lock(entity_id, lock_name):
    token = uuid4().hex
    timestamp = now_utc()
    stale_before = timestamp - timedelta(seconds=45)
    field_name = f"configuration_locks.{lock_name}"

    locked_entity = mongo.db.accounting_entities.find_one_and_update(
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

    if not locked_entity:
        raise RuntimeError(
            "Another configuration update is in progress. Please wait a moment and try again."
        )

    try:
        yield
    finally:
        mongo.db.accounting_entities.update_one(
            {"_id": entity_id, f"{field_name}.token": token},
            {"$unset": {field_name: ""}},
        )


def _clean_text(value, label, maximum=200, required=False):
    cleaned = " ".join(str(value or "").strip().split())
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _clean_multiline(value, label, maximum=300, required=False):
    cleaned = str(value or "").strip()
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _parse_date(value, label, required=True):
    if value in (None, ""):
        if required:
            raise ValueError(f"{label} is required.")
        return None

    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        try:
            parsed = datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{label} must use the YYYY-MM-DD format.") from exc

    return datetime.combine(parsed, time.min)


def _parse_int(value, label, minimum=0, maximum=None):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc

    if parsed < minimum or (maximum is not None and parsed > maximum):
        if maximum is None:
            raise ValueError(f"{label} must be at least {minimum}.")
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _parse_decimal(value, label, minimum=Decimal("0")):
    raw = str(value or "").strip().replace(",", "")
    if not raw:
        raise ValueError(f"{label} is required.")

    try:
        parsed = Decimal(raw).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} must be a valid amount.") from exc

    if parsed < minimum:
        raise ValueError(f"{label} cannot be less than {minimum}.")

    return Decimal128(parsed)


def _resolve_state(state_name, label="State"):
    cleaned = _clean_text(state_name, label, maximum=100, required=True)
    state_code = INDIA_STATE_CODES.get(cleaned)
    if not state_code:
        raise ValueError(f"Select a valid {label.lower()}.")
    return cleaned, state_code


def _validate_financial_year(entity_id, financial_year_id):
    financial_year_object_id = _to_object_id(financial_year_id)
    if not financial_year_object_id:
        raise ValueError("Select an approved open Financial Year.")

    financial_year = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "accounting_entity_id": entity_id,
        "status": "open",
        "is_open": True,
        "is_locked": {"$ne": True},
        "is_deleted": {"$ne": True},
    })
    if not financial_year:
        raise ValueError("The selected default Financial Year is not open and usable.")

    return financial_year


def _normalize_profile_payload(entity_id, raw_payload):
    legal_name = _clean_text(
        raw_payload.get("legal_name"),
        "Legal business name",
        maximum=160,
        required=True,
    )
    trade_name = _clean_text(
        raw_payload.get("trade_name"),
        "Trade name",
        maximum=160,
    )
    address_line_1 = _clean_multiline(
        raw_payload.get("address_line_1"),
        "Address line 1",
        maximum=180,
        required=True,
    )
    address_line_2 = _clean_multiline(
        raw_payload.get("address_line_2"),
        "Address line 2",
        maximum=180,
    )
    city = _clean_text(raw_payload.get("city"), "City", maximum=100, required=True)
    district = _clean_text(
        raw_payload.get("district"),
        "District",
        maximum=100,
        required=True,
    )
    state_name, state_code = _resolve_state(raw_payload.get("state_name"))

    postal_code = str(raw_payload.get("postal_code") or "").strip()
    if not POSTAL_CODE_PATTERN.fullmatch(postal_code):
        raise ValueError("Postal code must be a valid 6-digit Indian PIN code.")

    pan = str(raw_payload.get("pan") or "").strip().upper()
    if not PAN_PATTERN.fullmatch(pan):
        raise ValueError("PAN must use the standard 10-character format.")

    gst_registration_status = str(
        raw_payload.get("gst_registration_status") or ""
    ).strip().lower()
    if gst_registration_status not in GST_REGISTRATION_STATUSES:
        raise ValueError("Select a valid GST registration status.")

    gstin = str(raw_payload.get("gstin") or "").strip().upper()
    gst_registered = gst_registration_status in {
        "registered_regular",
        "registered_composition",
    }

    if gst_registered:
        if not GSTIN_PATTERN.fullmatch(gstin):
            raise ValueError("GSTIN must use the standard 15-character format.")
        if gstin[:2] != state_code:
            raise ValueError("GSTIN state code must match the selected business state.")
        if gstin[2:12] != pan:
            raise ValueError("The PAN embedded in GSTIN must match the entered PAN.")
    elif gstin:
        raise ValueError("GSTIN must be blank when the entity is not GST registered.")

    books_beginning_date = _parse_date(
        raw_payload.get("books_beginning_date"),
        "Books beginning date",
    )
    financial_year = _validate_financial_year(
        entity_id,
        raw_payload.get("default_financial_year_id"),
    )

    if books_beginning_date > financial_year.get("end_date"):
        raise ValueError(
            "Books beginning date cannot be after the selected Financial Year end date."
        )

    return {
        "legal_name": legal_name,
        "trade_name": trade_name,
        "address_line_1": address_line_1,
        "address_line_2": address_line_2,
        "city": city,
        "district": district,
        "state_name": state_name,
        "state_code": state_code,
        "postal_code": postal_code,
        "country_code": "IN",
        "pan": pan,
        "gst_registration_status": gst_registration_status,
        "gstin": gstin,
        "base_currency": "INR",
        "books_beginning_date": books_beginning_date,
        "default_financial_year_id": financial_year["_id"],
        "default_financial_year_id_str": str(financial_year["_id"]),
        "default_financial_year_name": financial_year.get("display_name") or "",
        "accounting_activation_status": "enabled",
    }


def _normalize_policy_payload(raw_payload):
    valuation_method = str(
        raw_payload.get("valuation_method") or "weighted_average"
    ).strip().lower()
    if valuation_method not in VALUATION_METHODS:
        raise ValueError("Only Weighted Average valuation is enabled in the current rollout.")

    negative_stock_policy = str(
        raw_payload.get("negative_stock_policy") or "block"
    ).strip().lower()
    if negative_stock_policy not in NEGATIVE_STOCK_POLICIES:
        raise ValueError("Select a valid negative-stock policy.")

    backdated_entry_policy = str(
        raw_payload.get("backdated_entry_policy") or "require_approval"
    ).strip().lower()
    if backdated_entry_policy not in BACKDATED_ENTRY_POLICIES:
        raise ValueError("Select a valid backdated-entry policy.")

    rounding_method = str(
        raw_payload.get("rounding_method") or "two_decimals"
    ).strip().lower()
    if rounding_method not in ROUNDING_METHODS:
        raise ValueError("Select a valid rounding method.")

    supporting_document_policy = str(
        raw_payload.get("supporting_document_policy")
        or "required_for_sensitive"
    ).strip().lower()
    if supporting_document_policy not in SUPPORTING_DOCUMENT_POLICIES:
        raise ValueError("Select a valid supporting-document policy.")

    default_payment_mode = str(
        raw_payload.get("default_payment_mode") or "bank"
    ).strip().lower()
    if default_payment_mode not in DEFAULT_PAYMENT_MODES:
        raise ValueError("Select a valid default payment mode.")

    place_of_supply_state, place_of_supply_state_code = _resolve_state(
        raw_payload.get("default_place_of_supply_state"),
        label="Default place-of-supply state",
    )

    maker_checker_enabled = str(
        raw_payload.get("maker_checker_enabled") or "1"
    ).strip() in {"1", "true", "yes", "on"}
    if not maker_checker_enabled:
        raise ValueError(
            "Maker-checker control must remain enabled during the AVPL Accounting rollout."
        )

    return {
        "inventory_valuation_method": valuation_method,
        "standard_cost_reference_enabled": str(
            raw_payload.get("standard_cost_reference_enabled") or ""
        ).strip() in {"1", "true", "yes", "on"},
        "standard_cost_change_requires_approval": True,
        "negative_stock_policy": negative_stock_policy,
        "backdated_entry_policy": backdated_entry_policy,
        "maximum_backdated_days": _parse_int(
            raw_payload.get("maximum_backdated_days") or 30,
            "Maximum backdated days",
            minimum=0,
            maximum=366,
        ),
        "default_credit_days": _parse_int(
            raw_payload.get("default_credit_days") or 0,
            "Default credit period",
            minimum=0,
            maximum=365,
        ),
        "rounding_method": rounding_method,
        "maker_checker_enabled": True,
        "high_value_approval_threshold": _parse_decimal(
            raw_payload.get("high_value_approval_threshold") or "0",
            "High-value approval threshold",
        ),
        "supporting_document_policy": supporting_document_policy,
        "default_place_of_supply_state": place_of_supply_state,
        "default_place_of_supply_state_code": place_of_supply_state_code,
        "default_payment_mode": default_payment_mode,
        "posting_mode": "controlled_service_posting",
        "allow_direct_ledger_mutation": False,
        "allow_hard_delete": False,
    }


def _configuration_meta(collection_name):
    if collection_name == PROFILE_COLLECTION:
        return {
            "type": "entity_profile",
            "label": "Entity profile",
            "permission_prefix": PROFILE_PERMISSION_PREFIX,
            "lock_name": "entity_profile",
        }
    if collection_name == POLICY_COLLECTION:
        return {
            "type": "accounting_policy",
            "label": "Accounting settings",
            "permission_prefix": POLICY_PERMISSION_PREFIX,
            "lock_name": "accounting_policy",
        }
    raise ValueError("Invalid Accounting configuration type.")


def _next_revision(collection, entity_id):
    latest = collection.find_one(
        {
            "accounting_entity_id": entity_id,
            "is_deleted": {"$ne": True},
        },
        {"revision_number": 1},
        sort=[("revision_number", DESCENDING)],
    )
    return int((latest or {}).get("revision_number") or 0) + 1


def _workflow_event(action, actor, from_status, to_status, revision, reason="", note=""):
    return {
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "revision_number": int(revision or 1),
        "reason": str(reason or "").strip(),
        "note": str(note or "").strip(),
        "at": now_utc(),
    }


def _record_audit(collection_name, document, actor, action, previous_status=None, remarks=""):
    meta = _configuration_meta(collection_name)
    timestamp = now_utc()
    try:
        mongo.db.accounting_audit_logs.insert_one({
            "module": "accounting",
            "action": action,
            "accounting_entity_id": document.get("accounting_entity_id"),
            "accounting_entity_id_str": str(document.get("accounting_entity_id") or ""),
            "entity_type": meta["type"],
            "entity_id": document.get("_id"),
            "entity_id_str": str(document.get("_id") or ""),
            "actor_user_id": actor["_id"],
            "actor_user_id_str": str(actor["_id"]),
            "actor_role": actor.get("resolved_role") or "",
            "actor_name": actor.get("resolved_name") or "",
            "previous_status": previous_status,
            "new_status": document.get("status"),
            "metadata": {
                "configuration_type": meta["type"],
                "revision_number": document.get("revision_number"),
                "version": document.get("version"),
                "is_active": document.get("is_active", False),
            },
            "remarks": remarks or f"{meta['label']} workflow updated.",
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


def _find_working_copy(collection_name, entity_id):
    return mongo.db[collection_name].find_one({
        "accounting_entity_id": entity_id,
        "is_working_copy": True,
        "is_deleted": {"$ne": True},
    })


def _find_active(collection_name, entity_id):
    return mongo.db[collection_name].find_one({
        "accounting_entity_id": entity_id,
        "is_active": True,
        "status": STATUS_APPROVED,
        "is_deleted": {"$ne": True},
    })


def _assert_creator(document, actor):
    if str(document.get("created_by") or "") != str(actor.get("_id") or ""):
        raise PermissionError("Only the original maker can edit or submit this configuration revision.")


def _parse_expected_version(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("The configuration form version is invalid. Refresh and try again.") from exc


def _save_draft(collection_name, entity_id, actor_user_id, raw_payload, expected_version=None):
    meta = _configuration_meta(collection_name)
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    entity = _assert_entity(entity_id)
    access = get_accounting_access(actor["_id"], session_role=actor["resolved_role"])
    if not (
        has_accounting_permission(access, f"{meta['permission_prefix']}.create")
        or has_accounting_permission(access, f"{meta['permission_prefix']}.edit")
    ):
        raise PermissionError("You do not have permission to save this configuration.")
    _require_permission(actor, entity["_id"], f"{meta['permission_prefix']}.view")

    payload = (
        _normalize_profile_payload(entity["_id"], raw_payload)
        if collection_name == PROFILE_COLLECTION
        else _normalize_policy_payload(raw_payload)
    )

    ensure_accounting_configuration_indexes()
    collection = mongo.db[collection_name]

    with _configuration_lock(entity["_id"], meta["lock_name"]):
        working = _find_working_copy(collection_name, entity["_id"])
        timestamp = now_utc()

        if working:
            _assert_creator(working, actor)
            if working.get("status") not in EDITABLE_STATUSES:
                raise ValueError(
                    f"{meta['label']} is awaiting approval and cannot be edited."
                )

            expected = _parse_expected_version(expected_version)
            current_version = int(working.get("version") or 1)
            if expected != current_version:
                raise RuntimeError(
                    "This configuration changed in another session. Refresh and try again."
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
                        "payload": payload,
                        "status": STATUS_DRAFT,
                        "return_reason": "",
                        "returned_by": None,
                        "returned_at": None,
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
                    "This configuration changed in another session. Refresh and try again."
                )
            document = collection.find_one({"_id": working["_id"]})
            action = "update_configuration_draft"
            previous_status = working.get("status")
            created = False
        else:
            revision = _next_revision(collection, entity["_id"])
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
                "configuration_type": meta["type"],
                "revision_number": revision,
                "status": STATUS_DRAFT,
                "is_working_copy": True,
                "is_active": False,
                "is_deleted": False,
                "payload": payload,
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
                    "Another configuration draft was created at the same time. Refresh and try again."
                ) from exc
            action = "create_configuration_draft"
            previous_status = None
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
        "document": serialize_configuration(document),
        "message": f"{meta['label']} draft saved successfully.",
    }


def save_entity_profile_draft(entity_id, actor_user_id, raw_payload, expected_version=None):
    return _save_draft(
        PROFILE_COLLECTION,
        entity_id,
        actor_user_id,
        raw_payload,
        expected_version=expected_version,
    )


def save_accounting_policy_draft(entity_id, actor_user_id, raw_payload, expected_version=None):
    return _save_draft(
        POLICY_COLLECTION,
        entity_id,
        actor_user_id,
        raw_payload,
        expected_version=expected_version,
    )


def _get_configuration_for_action(collection_name, configuration_id):
    object_id = _to_object_id(configuration_id)
    if not object_id:
        raise ValueError("Invalid configuration record.")

    document = mongo.db[collection_name].find_one({
        "_id": object_id,
        "is_deleted": {"$ne": True},
    })
    if not document:
        raise ValueError("The configuration record was not found.")
    return document


def _submit_configuration(collection_name, configuration_id, actor_user_id, expected_version, note=""):
    meta = _configuration_meta(collection_name)
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    document = _get_configuration_for_action(collection_name, configuration_id)
    _require_permission(actor, document["accounting_entity_id"], f"{meta['permission_prefix']}.submit")
    _assert_creator(document, actor)

    if document.get("status") not in EDITABLE_STATUSES:
        raise ValueError(f"Only draft or returned {meta['label'].lower()} can be submitted.")

    current_version = int(document.get("version") or 1)
    if _parse_expected_version(expected_version) != current_version:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

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
                "last_submission_note": str(note or "").strip(),
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "submit_configuration",
        previous_status=document.get("status"),
    )
    return {"document": serialize_configuration(updated), "message": f"{meta['label']} submitted for approval."}


def submit_entity_profile(configuration_id, actor_user_id, expected_version, note=""):
    return _submit_configuration(
        PROFILE_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        note=note,
    )


def submit_accounting_policy(configuration_id, actor_user_id, expected_version, note=""):
    return _submit_configuration(
        POLICY_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        note=note,
    )


def _withdraw_configuration(collection_name, configuration_id, actor_user_id, expected_version, reason):
    meta = _configuration_meta(collection_name)
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    document = _get_configuration_for_action(collection_name, configuration_id)
    _require_permission(actor, document["accounting_entity_id"], f"{meta['permission_prefix']}.withdraw")
    _assert_creator(document, actor)

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending configuration can be withdrawn.")

    cleaned_reason = _clean_multiline(reason, "Withdrawal reason", maximum=500, required=True)
    current_version = int(document.get("version") or 1)
    if _parse_expected_version(expected_version) != current_version:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    timestamp = now_utc()
    event = _workflow_event(
        "withdrawn_to_draft",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_DRAFT,
        document.get("revision_number"),
        reason=cleaned_reason,
    )
    result = mongo.db[collection_name].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                "status": STATUS_DRAFT,
                "withdraw_reason": cleaned_reason,
                "withdrawn_by": actor["_id"],
                "withdrawn_by_str": str(actor["_id"]),
                "withdrawn_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "withdraw_configuration",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=cleaned_reason,
    )
    return {"document": serialize_configuration(updated), "message": f"{meta['label']} withdrawn to draft."}


def withdraw_entity_profile(configuration_id, actor_user_id, expected_version, reason):
    return _withdraw_configuration(
        PROFILE_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        reason,
    )


def withdraw_accounting_policy(configuration_id, actor_user_id, expected_version, reason):
    return _withdraw_configuration(
        POLICY_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        reason,
    )


def _return_configuration(collection_name, configuration_id, actor_user_id, expected_version, reason):
    meta = _configuration_meta(collection_name)
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    document = _get_configuration_for_action(collection_name, configuration_id)
    _require_permission(actor, document["accounting_entity_id"], f"{meta['permission_prefix']}.return")

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending configuration can be returned for correction.")

    cleaned_reason = _clean_multiline(reason, "Correction reason", maximum=500, required=True)
    current_version = int(document.get("version") or 1)
    if _parse_expected_version(expected_version) != current_version:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    timestamp = now_utc()
    event = _workflow_event(
        "returned_for_correction",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_RETURNED,
        document.get("revision_number"),
        reason=cleaned_reason,
    )
    result = mongo.db[collection_name].update_one(
        {"_id": document["_id"], "version": current_version},
        {
            "$set": {
                "status": STATUS_RETURNED,
                "return_reason": cleaned_reason,
                "returned_by": actor["_id"],
                "returned_by_str": str(actor["_id"]),
                "returned_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    updated = mongo.db[collection_name].find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "return_configuration_for_correction",
        previous_status=STATUS_PENDING_APPROVAL,
        remarks=cleaned_reason,
    )
    return {"document": serialize_configuration(updated), "message": f"{meta['label']} returned for correction."}


def return_entity_profile(configuration_id, actor_user_id, expected_version, reason):
    return _return_configuration(
        PROFILE_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        reason,
    )


def return_accounting_policy(configuration_id, actor_user_id, expected_version, reason):
    return _return_configuration(
        POLICY_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
        reason,
    )


def _sync_entity_profile(entity_id, payload, configuration_id, revision_number, timestamp):
    result = mongo.db.accounting_entities.update_one(
        {"_id": entity_id, "is_deleted": {"$ne": True}},
        {
            "$set": {
                "display_name": payload.get("trade_name") or payload.get("legal_name"),
                "legal_name": payload.get("legal_name"),
                "trade_name": payload.get("trade_name"),
                "address_line_1": payload.get("address_line_1"),
                "address_line_2": payload.get("address_line_2"),
                "city": payload.get("city"),
                "district": payload.get("district"),
                "state": payload.get("state_name"),
                "state_code": payload.get("state_code"),
                "postal_code": payload.get("postal_code"),
                "country_code": payload.get("country_code") or "IN",
                "pan": payload.get("pan"),
                "gst_registration_status": payload.get("gst_registration_status"),
                "gstin": payload.get("gstin"),
                "base_currency": payload.get("base_currency") or "INR",
                "books_beginning_date": payload.get("books_beginning_date"),
                "default_financial_year_id": payload.get("default_financial_year_id"),
                "default_financial_year_id_str": payload.get("default_financial_year_id_str"),
                "profile_status": "complete",
                "accounting_enabled": True,
                "entity_profile_configuration_id": configuration_id,
                "entity_profile_revision": revision_number,
                "updated_at": timestamp,
            },
            "$inc": {"version": 1},
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("The approved entity profile could not be synchronized to the Accounting entity.")


def _approve_configuration(collection_name, configuration_id, actor_user_id, expected_version):
    meta = _configuration_meta(collection_name)
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    document = _get_configuration_for_action(collection_name, configuration_id)
    _require_permission(actor, document["accounting_entity_id"], f"{meta['permission_prefix']}.approve")

    if document.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending configuration can be approved.")

    if str(document.get("created_by") or "") == str(actor.get("_id") or ""):
        raise PermissionError("The maker cannot approve the same configuration revision.")

    current_version = int(document.get("version") or 1)
    if _parse_expected_version(expected_version) != current_version:
        raise RuntimeError("This configuration changed in another session. Refresh and try again.")

    collection = mongo.db[collection_name]
    entity_id = document["accounting_entity_id"]
    timestamp = now_utc()

    with _configuration_lock(entity_id, f"{meta['lock_name']}_approval"):
        current = collection.find_one({"_id": document["_id"]})
        if not current or current.get("status") != STATUS_PENDING_APPROVAL:
            raise RuntimeError("This configuration changed in another session. Refresh and try again.")
        if int(current.get("version") or 1) != current_version:
            raise RuntimeError("This configuration changed in another session. Refresh and try again.")

        marker_field = f"configuration_activation.{meta['type']}"
        mongo.db.accounting_entities.update_one(
            {"_id": entity_id},
            {
                "$set": {
                    marker_field: {
                        "target_configuration_id": current["_id"],
                        "target_revision": current.get("revision_number"),
                        "started_at": timestamp,
                        "started_by": actor["_id"],
                    }
                }
            },
        )

        if collection_name == PROFILE_COLLECTION:
            _sync_entity_profile(
                entity_id,
                current.get("payload") or {},
                current["_id"],
                current.get("revision_number"),
                timestamp,
            )

        previous_active = _find_active(collection_name, entity_id)
        if previous_active and previous_active.get("_id") != current.get("_id"):
            superseded_event = _workflow_event(
                "superseded_by_new_revision",
                actor,
                STATUS_APPROVED,
                STATUS_SUPERSEDED,
                previous_active.get("revision_number"),
                note=(
                    f"Superseded by approved revision "
                    f"{current.get('revision_number')}."
                ),
            )
            previous_result = collection.update_one(
                {
                    "_id": previous_active["_id"],
                    "is_active": True,
                    "status": STATUS_APPROVED,
                },
                {
                    "$set": {
                        "status": STATUS_SUPERSEDED,
                        "is_active": False,
                        "superseded_by": current["_id"],
                        "superseded_at": timestamp,
                        "updated_by": actor["_id"],
                        "updated_by_str": str(actor["_id"]),
                        "updated_at": timestamp,
                    },
                    "$inc": {"version": 1},
                    "$push": {"workflow_history": superseded_event},
                },
            )
            if previous_result.modified_count != 1:
                raise RuntimeError(
                    "The previous active configuration changed during approval. "
                    "Review the configuration history and retry."
                )

        event = _workflow_event(
            "approved_and_activated",
            actor,
            STATUS_PENDING_APPROVAL,
            STATUS_APPROVED,
            current.get("revision_number"),
        )
        result = collection.update_one(
            {
                "_id": current["_id"],
                "version": current_version,
                "status": STATUS_PENDING_APPROVAL,
            },
            {
                "$set": {
                    "status": STATUS_APPROVED,
                    "is_working_copy": False,
                    "is_active": True,
                    "approved_by": actor["_id"],
                    "approved_by_str": str(actor["_id"]),
                    "approved_at": timestamp,
                    "activated_at": timestamp,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_at": timestamp,
                    "version": current_version + 1,
                    "activation_status": "active",
                },
                "$push": {"workflow_history": event},
            },
        )
        if result.modified_count != 1:
            raise RuntimeError(
                "Configuration activation requires recovery. Do not create another revision; retry approval after reviewing the current record."
            )

        mongo.db.accounting_entities.update_one(
            {"_id": entity_id},
            {"$unset": {marker_field: ""}},
        )

    updated = collection.find_one({"_id": document["_id"]})
    _record_audit(
        collection_name,
        updated,
        actor,
        "approve_and_activate_configuration",
        previous_status=STATUS_PENDING_APPROVAL,
    )
    return {"document": serialize_configuration(updated), "message": f"{meta['label']} approved and activated."}


def approve_entity_profile(configuration_id, actor_user_id, expected_version):
    return _approve_configuration(
        PROFILE_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
    )


def approve_accounting_policy(configuration_id, actor_user_id, expected_version):
    return _approve_configuration(
        POLICY_COLLECTION,
        configuration_id,
        actor_user_id,
        expected_version,
    )


def _user_name_map(documents):
    user_ids = set()
    for document in documents:
        for key in (
            "created_by",
            "updated_by",
            "submitted_by",
            "approved_by",
            "returned_by",
            "withdrawn_by",
        ):
            object_id = _to_object_id(document.get(key))
            if object_id:
                user_ids.add(object_id)

    if not user_ids:
        return {}

    users = mongo.db.users.find(
        {"_id": {"$in": list(user_ids)}},
        {"name": 1, "full_name": 1, "username": 1, "phone": 1, "role": 1},
    )
    return {
        str(user["_id"]): (
            user.get("name")
            or user.get("full_name")
            or user.get("username")
            or user.get("phone")
            or str(user.get("role") or "User").replace("_", " ").title()
        )
        for user in users
    }


def _decimal_to_string(value):
    if isinstance(value, Decimal128):
        return format(value.to_decimal(), "f")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value or "0")


def _serialize_payload(payload):
    payload = dict(payload or {})
    serialized = {}
    for key, value in payload.items():
        if isinstance(value, ObjectId):
            serialized[key] = str(value)
        elif isinstance(value, Decimal128):
            serialized[key] = _decimal_to_string(value)
        elif isinstance(value, datetime):
            serialized[key] = value
            serialized[f"{key}_input"] = value.strftime("%Y-%m-%d")
            serialized[f"{key}_display"] = value.strftime("%d %b %Y")
        else:
            serialized[key] = value
    return serialized


def serialize_configuration(document, user_names=None):
    if not document:
        return None

    user_names = user_names or {}
    payload = _serialize_payload(document.get("payload"))

    def user_name(key):
        value = document.get(key)
        return user_names.get(str(value), "") if value else ""

    status = document.get("status") or STATUS_DRAFT
    return {
        "id": str(document.get("_id") or ""),
        "accounting_entity_id": str(document.get("accounting_entity_id") or ""),
        "configuration_type": document.get("configuration_type") or "",
        "revision_number": int(document.get("revision_number") or 1),
        "status": status,
        "status_display": status.replace("_", " ").title(),
        "is_working_copy": document.get("is_working_copy") is True,
        "is_active": document.get("is_active") is True,
        "version": int(document.get("version") or 1),
        "payload": payload,
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": user_name("created_by"),
        "updated_by_name": user_name("updated_by"),
        "submitted_by_name": user_name("submitted_by"),
        "approved_by_name": user_name("approved_by"),
        "returned_by_name": user_name("returned_by"),
        "withdrawn_by_name": user_name("withdrawn_by"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
        "submitted_at": document.get("submitted_at"),
        "approved_at": document.get("approved_at"),
        "return_reason": document.get("return_reason") or "",
        "withdraw_reason": document.get("withdraw_reason") or "",
        "last_submission_note": document.get("last_submission_note") or "",
        "workflow_history": document.get("workflow_history") or [],
        "audit_sync_required": document.get("audit_sync_required") is True,
    }


def _default_profile_payload(entity, open_financial_years):
    default_year = next((row for row in open_financial_years if row.get("usable_for_posting")), None)
    books_date = ""
    default_year_id = ""
    default_year_name = ""
    if default_year:
        books_date = default_year.get("start_date_input") or ""
        default_year_id = default_year.get("id") or ""
        default_year_name = default_year.get("display_name") or ""

    return {
        "legal_name": entity.get("legal_name") or "",
        "trade_name": entity.get("trade_name") or entity.get("display_name") or "",
        "address_line_1": entity.get("address_line_1") or "",
        "address_line_2": entity.get("address_line_2") or "",
        "city": entity.get("city") or "",
        "district": entity.get("district") or "",
        "state_name": entity.get("state") or "Assam",
        "state_code": entity.get("state_code") or INDIA_STATE_CODES.get("Assam"),
        "postal_code": entity.get("postal_code") or "",
        "country_code": "IN",
        "pan": entity.get("pan") or "",
        "gst_registration_status": entity.get("gst_registration_status") or "registered_regular",
        "gstin": entity.get("gstin") or "",
        "base_currency": "INR",
        "books_beginning_date_input": (
            entity.get("books_beginning_date").strftime("%Y-%m-%d")
            if isinstance(entity.get("books_beginning_date"), datetime)
            else books_date
        ),
        "default_financial_year_id": str(entity.get("default_financial_year_id") or default_year_id),
        "default_financial_year_name": default_year_name,
        "accounting_activation_status": "enabled",
    }


def _default_policy_payload(entity):
    return {
        "inventory_valuation_method": "weighted_average",
        "standard_cost_reference_enabled": True,
        "negative_stock_policy": "block",
        "backdated_entry_policy": "require_approval",
        "maximum_backdated_days": 30,
        "default_credit_days": 15,
        "rounding_method": "two_decimals",
        "maker_checker_enabled": True,
        "high_value_approval_threshold": "100000.00",
        "supporting_document_policy": "required_for_sensitive",
        "default_place_of_supply_state": entity.get("state") or "Assam",
        "default_place_of_supply_state_code": entity.get("state_code") or INDIA_STATE_CODES.get("Assam"),
        "default_payment_mode": "bank",
        "posting_mode": "controlled_service_posting",
        "allow_direct_ledger_mutation": False,
        "allow_hard_delete": False,
    }


def get_configuration_overview(entity_id, open_financial_years=None):
    entity = _assert_entity(entity_id)
    ensure_accounting_configuration_indexes()
    open_financial_years = open_financial_years or []

    profile_documents = list(
        mongo.db[PROFILE_COLLECTION].find({
            "accounting_entity_id": entity["_id"],
            "is_deleted": {"$ne": True},
            "$or": [{"is_active": True}, {"is_working_copy": True}],
        })
    )
    policy_documents = list(
        mongo.db[POLICY_COLLECTION].find({
            "accounting_entity_id": entity["_id"],
            "is_deleted": {"$ne": True},
            "$or": [{"is_active": True}, {"is_working_copy": True}],
        })
    )
    user_names = _user_name_map(profile_documents + policy_documents)

    profile_active_raw = next((row for row in profile_documents if row.get("is_active")), None)
    profile_working_raw = next((row for row in profile_documents if row.get("is_working_copy")), None)
    policy_active_raw = next((row for row in policy_documents if row.get("is_active")), None)
    policy_working_raw = next((row for row in policy_documents if row.get("is_working_copy")), None)

    profile_active = serialize_configuration(profile_active_raw, user_names)
    profile_working = serialize_configuration(profile_working_raw, user_names)
    policy_active = serialize_configuration(policy_active_raw, user_names)
    policy_working = serialize_configuration(policy_working_raw, user_names)

    profile_form = dict(
        (profile_working or profile_active or {}).get("payload")
        or _default_profile_payload(entity, open_financial_years)
    )
    if "books_beginning_date_input" not in profile_form:
        raw_date = profile_form.get("books_beginning_date")
        profile_form["books_beginning_date_input"] = (
            raw_date.strftime("%Y-%m-%d") if isinstance(raw_date, datetime) else ""
        )

    policy_form = dict(
        (policy_working or policy_active or {}).get("payload")
        or _default_policy_payload(entity)
    )

    return {
        "entity_profile": {
            "active": profile_active,
            "working": profile_working,
            "form": profile_form,
        },
        "accounting_policy": {
            "active": policy_active,
            "working": policy_working,
            "form": policy_form,
        },
    }
