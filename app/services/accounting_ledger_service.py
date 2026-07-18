from contextlib import contextmanager
from datetime import timedelta
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_account_group_service import (
    ACCOUNT_GROUP_COLLECTION,
    ensure_account_group_indexes,
    get_account_group_overview,
)
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


LEDGER_COLLECTION = "ledgers"
AVPL_ENTITY_CODE = "AVPL"

LEDGER_STATUS_ACTIVE = "active"
LEDGER_STATUS_INACTIVE = "inactive"

BALANCE_DEBIT = "debit"
BALANCE_CREDIT = "credit"

POSTING_POLICY_MANUAL_AND_SOURCE = "manual_and_source"
POSTING_POLICY_SOURCE_CONTROLLED = "source_controlled"
POSTING_POLICY_AUTHORIZED_VOUCHER = "authorized_voucher"

VIEW_PERMISSION = "accounting.ledger.view"
BOOTSTRAP_PERMISSION = "accounting.ledger.bootstrap"

SYSTEM_LEDGER_RENAME_MESSAGE = (
    "System ledgers are protected and cannot be casually renamed."
)
SYSTEM_LEDGER_DELETE_MESSAGE = (
    "System ledgers are protected and cannot be deleted or deactivated."
)

_SAFE_SYSTEM_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_SAFE_LEDGER_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_\-]{1,39}$")


# The order is permanent and intentionally grouped for predictable UI and
# future reporting. These masters are entity-level, not Financial-Year-level.
DEFAULT_AVPL_LEDGERS = (
    {
        "system_key": "main_cash",
        "ledger_code": "MAIN_CASH",
        "name": "Main Cash",
        "account_group_system_key": "cash_in_hand",
        "ledger_type": "cash",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_MANUAL_AND_SOURCE,
        "catalog_section": "cash_and_settlement",
        "section_label": "Cash and settlement",
        "payment_mode": "cash",
        "sort_order": 10,
    },
    {
        "system_key": "upi_clearing",
        "ledger_code": "UPI_CLEARING",
        "name": "UPI Clearing",
        "account_group_system_key": "current_assets",
        "ledger_type": "clearing",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_AUTHORIZED_VOUCHER,
        "catalog_section": "cash_and_settlement",
        "section_label": "Cash and settlement",
        "payment_mode": "upi",
        "clearing_mode": "upi",
        "sort_order": 20,
    },
    {
        "system_key": "cheque_clearing",
        "ledger_code": "CHEQUE_CLEARING",
        "name": "Cheque Clearing",
        "account_group_system_key": "current_assets",
        "ledger_type": "clearing",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_AUTHORIZED_VOUCHER,
        "catalog_section": "cash_and_settlement",
        "section_label": "Cash and settlement",
        "payment_mode": "cheque",
        "clearing_mode": "cheque",
        "sort_order": 30,
    },
    {
        "system_key": "purchase",
        "ledger_code": "PURCHASE",
        "name": "Purchase",
        "account_group_system_key": "purchase_accounts",
        "ledger_type": "purchase",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "trade_operations",
        "section_label": "Trade operations",
        "document_role": "purchase_base",
        "sort_order": 110,
    },
    {
        "system_key": "purchase_return",
        "ledger_code": "PURCHASE_RETURN",
        "name": "Purchase Return",
        "account_group_system_key": "purchase_accounts",
        "ledger_type": "purchase_return",
        "normal_balance": BALANCE_CREDIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "trade_operations",
        "section_label": "Trade operations",
        "document_role": "purchase_return",
        "contra_of_system_key": "purchase",
        "sort_order": 120,
    },
    {
        "system_key": "sales",
        "ledger_code": "SALES",
        "name": "Sales",
        "account_group_system_key": "sales_accounts",
        "ledger_type": "sales",
        "normal_balance": BALANCE_CREDIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "trade_operations",
        "section_label": "Trade operations",
        "document_role": "sales_base",
        "sort_order": 130,
    },
    {
        "system_key": "sales_return",
        "ledger_code": "SALES_RETURN",
        "name": "Sales Return",
        "account_group_system_key": "sales_accounts",
        "ledger_type": "sales_return",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "trade_operations",
        "section_label": "Trade operations",
        "document_role": "sales_return",
        "contra_of_system_key": "sales",
        "sort_order": 140,
    },
    {
        "system_key": "input_cgst",
        "ledger_code": "INPUT_CGST",
        "name": "Input CGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "input_credit",
        "tax_component": "cgst",
        "sort_order": 210,
    },
    {
        "system_key": "input_sgst",
        "ledger_code": "INPUT_SGST",
        "name": "Input SGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "input_credit",
        "tax_component": "sgst",
        "sort_order": 220,
    },
    {
        "system_key": "input_igst",
        "ledger_code": "INPUT_IGST",
        "name": "Input IGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "input_credit",
        "tax_component": "igst",
        "sort_order": 230,
    },
    {
        "system_key": "output_cgst",
        "ledger_code": "OUTPUT_CGST",
        "name": "Output CGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_CREDIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "output_liability",
        "tax_component": "cgst",
        "sort_order": 240,
    },
    {
        "system_key": "output_sgst",
        "ledger_code": "OUTPUT_SGST",
        "name": "Output SGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_CREDIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "output_liability",
        "tax_component": "sgst",
        "sort_order": 250,
    },
    {
        "system_key": "output_igst",
        "ledger_code": "OUTPUT_IGST",
        "name": "Output IGST",
        "account_group_system_key": "duties_and_taxes",
        "ledger_type": "tax",
        "normal_balance": BALANCE_CREDIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "gst_controls",
        "section_label": "GST controls",
        "tax_direction": "output_liability",
        "tax_component": "igst",
        "sort_order": 260,
    },
    {
        "system_key": "stock_in_hand",
        "ledger_code": "STOCK_IN_HAND",
        "name": "Stock-in-Hand",
        "account_group_system_key": "stock_in_hand",
        "ledger_type": "inventory",
        "normal_balance": BALANCE_DEBIT,
        "posting_policy": POSTING_POLICY_SOURCE_CONTROLLED,
        "catalog_section": "inventory_control",
        "section_label": "Inventory control",
        "inventory_role": "stock_value_control",
        "sort_order": 310,
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
    """Create a required index without dropping or rebuilding old indexes."""
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


def ensure_ledger_indexes():
    """Install Stage 3 ledger-master indexes safely and idempotently."""
    collection = mongo.db[LEDGER_COLLECTION]

    # System keys remain permanently reserved for AVPL system ledgers.
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("system_key", ASCENDING)],
        name="ledger_entity_system_key_unique",
        unique=True,
        partialFilterExpression={"system_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("ledger_code", ASCENDING)],
        name="ledger_entity_code_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("normalized_name", ASCENDING)],
        name="ledger_entity_name_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("account_group_id", ASCENDING),
            ("sort_order", ASCENDING),
        ],
        name="ledger_entity_group_sort_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("ledger_type", ASCENDING),
            ("status", ASCENDING),
        ],
        name="ledger_entity_type_status_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("tax_direction", ASCENDING),
            ("tax_component", ASCENDING),
        ],
        name="ledger_entity_tax_control_idx",
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("updated_at", DESCENDING)],
        name="ledger_entity_updated_idx",
    )


def _clean_single_line(value, label, maximum=160, required=True):
    cleaned = " ".join(str(value or "").strip().split())
    if required and not cleaned:
        raise ValueError(f"{label} is required.")
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _normalize_name(value):
    return " ".join(str(value or "").strip().lower().split())


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
            "You are not authorized to perform this ledger-master action."
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
        "is_deleted": {"$ne": True},
        "status": "active",
        "accounting_enabled": {"$ne": False},
    }

    if entity_id not in (None, ""):
        entity_object_id = _to_object_id(entity_id)
        if not entity_object_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = entity_object_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise ValueError(
            "The active AVPL Accounting entity was not found. Initialize or reactivate AVPL before creating ledgers."
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
        allowed_entity_ids = {
            str(value) for value in access.get("entity_ids") or []
        }
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError(
                "You do not have access to this Accounting entity."
            )

    if not has_accounting_permission(access, permission):
        raise PermissionError(
            "You do not have permission to perform this Accounting action."
        )
    return access


@contextmanager
def _default_ledger_seed_lock(entity_id):
    token = uuid4().hex
    timestamp = now_utc()
    stale_before = timestamp - timedelta(seconds=60)
    field_name = "setup_locks.default_ledgers"

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
    )

    if not locked_entity:
        raise RuntimeError(
            "Another default-ledger setup is in progress. Please try again shortly."
        )

    try:
        yield
    finally:
        mongo.db.accounting_entities.update_one(
            {"_id": entity_id, f"{field_name}.token": token},
            {"$unset": {field_name: ""}},
        )


# ---------------------------------------------------------------------------
# Protection, serialization and audit helpers
# ---------------------------------------------------------------------------


def assert_ledger_can_be_renamed(ledger):
    if not ledger:
        raise ValueError("The ledger was not found.")
    if ledger.get("is_system") is True or ledger.get("is_protected") is True:
        raise PermissionError(SYSTEM_LEDGER_RENAME_MESSAGE)
    return True


def assert_ledger_can_be_deleted(ledger):
    if not ledger:
        raise ValueError("The ledger was not found.")
    if ledger.get("is_system") is True or ledger.get("is_protected") is True:
        raise PermissionError(SYSTEM_LEDGER_DELETE_MESSAGE)
    return True


def _change_event(action, actor, changed_fields=None, seed_run_id=None):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "changed_fields": sorted(set(changed_fields or [])),
        "seed_run_id": str(seed_run_id or ""),
        "at": now_utc(),
    }


def _record_audit(ledger, actor, action, changed_fields=None, remarks=""):
    timestamp = now_utc()
    audit_document = {
        "module": "accounting",
        "action": action,
        "accounting_entity_id": ledger.get("accounting_entity_id"),
        "accounting_entity_id_str": str(
            ledger.get("accounting_entity_id") or ""
        ),
        "entity_type": "ledger",
        "entity_id": ledger.get("_id"),
        "entity_id_str": str(ledger.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": None,
        "new_status": ledger.get("status") or LEDGER_STATUS_ACTIVE,
        "metadata": {
            "system_key": ledger.get("system_key"),
            "ledger_code": ledger.get("ledger_code"),
            "ledger_name": ledger.get("name"),
            "account_group_system_key": ledger.get(
                "account_group_system_key"
            ),
            "ledger_type": ledger.get("ledger_type"),
            "normal_balance": ledger.get("normal_balance"),
            "posting_policy": ledger.get("posting_policy"),
            "is_system": ledger.get("is_system") is True,
            "is_protected": ledger.get("is_protected") is True,
            "version": int(ledger.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": remarks or "Protected default ledger updated.",
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


def serialize_ledger(ledger, account_group_name=""):
    if not ledger:
        return None

    return {
        "id": str(ledger.get("_id") or ""),
        "accounting_entity_id": str(
            ledger.get("accounting_entity_id") or ""
        ),
        "ledger_code": ledger.get("ledger_code") or "",
        "name": ledger.get("name") or "",
        "normalized_name": ledger.get("normalized_name") or "",
        "system_key": ledger.get("system_key") or "",
        "account_group_id": str(ledger.get("account_group_id") or ""),
        "account_group_system_key": ledger.get(
            "account_group_system_key"
        ) or "",
        "account_group_name": account_group_name or "",
        "ledger_type": ledger.get("ledger_type") or "",
        "normal_balance": ledger.get("normal_balance") or "",
        "posting_policy": ledger.get("posting_policy") or "",
        "catalog_section": ledger.get("catalog_section") or "",
        "section_label": ledger.get("section_label") or "",
        "document_role": ledger.get("document_role") or "",
        "payment_mode": ledger.get("payment_mode") or "",
        "clearing_mode": ledger.get("clearing_mode") or "",
        "tax_direction": ledger.get("tax_direction") or "",
        "tax_component": ledger.get("tax_component") or "",
        "inventory_role": ledger.get("inventory_role") or "",
        "contra_of_system_key": ledger.get("contra_of_system_key") or "",
        "is_system": ledger.get("is_system") is True,
        "is_protected": ledger.get("is_protected") is True,
        "name_locked": ledger.get("name_locked") is True,
        "deletion_locked": ledger.get("deletion_locked") is True,
        "balance_managed_by_postings": ledger.get(
            "balance_managed_by_postings"
        ) is True,
        "opening_balance_locked": ledger.get("opening_balance_locked") is True,
        "status": ledger.get("status") or LEDGER_STATUS_INACTIVE,
        "is_active": ledger.get("is_active") is True,
        "is_deleted": ledger.get("is_deleted") is True,
        "sort_order": int(ledger.get("sort_order") or 0),
        "version": int(ledger.get("version") or 1),
        "audit_sync_required": ledger.get("audit_sync_required") is True,
        "created_at": ledger.get("created_at"),
        "updated_at": ledger.get("updated_at"),
    }


def get_default_avpl_ledger_catalog():
    return [dict(item) for item in DEFAULT_AVPL_LEDGERS]


# ---------------------------------------------------------------------------
# Protected group dependency and canonical ledger definition
# ---------------------------------------------------------------------------


def _resolve_required_groups(entity_id):
    ensure_account_group_indexes()
    required_keys = {
        definition["account_group_system_key"]
        for definition in DEFAULT_AVPL_LEDGERS
    }
    rows = list(
        mongo.db[ACCOUNT_GROUP_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "system_key": {"$in": sorted(required_keys)},
                "is_system": True,
                "is_protected": True,
                "is_deleted": False,
                "is_active": True,
                "status": "active",
            }
        )
    )
    by_key = {row.get("system_key"): row for row in rows}
    missing = sorted(required_keys - set(by_key))
    if missing:
        raise RuntimeError(
            "Protected account groups are incomplete. Run Stage 3 Batch 1 verification before initializing ledgers. "
            f"Missing group keys: {', '.join(missing)}."
        )
    return by_key


def _validate_catalog_definition(definition):
    system_key = str(definition.get("system_key") or "").strip()
    ledger_code = str(definition.get("ledger_code") or "").strip()
    name = _clean_single_line(definition.get("name"), "Ledger name")

    if not _SAFE_SYSTEM_KEY_PATTERN.fullmatch(system_key):
        raise RuntimeError(
            f"Invalid protected ledger system key: {system_key or 'blank'}."
        )
    if not _SAFE_LEDGER_CODE_PATTERN.fullmatch(ledger_code):
        raise RuntimeError(
            f"Invalid protected ledger code: {ledger_code or 'blank'}."
        )
    if definition.get("normal_balance") not in {
        BALANCE_DEBIT,
        BALANCE_CREDIT,
    }:
        raise RuntimeError(f"Invalid normal balance configured for {name}.")
    if definition.get("posting_policy") not in {
        POSTING_POLICY_MANUAL_AND_SOURCE,
        POSTING_POLICY_SOURCE_CONTROLLED,
        POSTING_POLICY_AUTHORIZED_VOUCHER,
    }:
        raise RuntimeError(f"Invalid posting policy configured for {name}.")


def _canonical_fields(definition, entity, account_group):
    group_id = account_group["_id"]
    return {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "ledger_code": definition["ledger_code"],
        "name": definition["name"],
        "normalized_name": _normalize_name(definition["name"]),
        "system_key": definition["system_key"],
        "account_group_id": group_id,
        "account_group_id_str": str(group_id),
        "account_group_system_key": definition["account_group_system_key"],
        "ledger_type": definition["ledger_type"],
        "normal_balance": definition["normal_balance"],
        "posting_policy": definition["posting_policy"],
        "catalog_section": definition["catalog_section"],
        "section_label": definition["section_label"],
        "document_role": definition.get("document_role"),
        "payment_mode": definition.get("payment_mode"),
        "clearing_mode": definition.get("clearing_mode"),
        "tax_direction": definition.get("tax_direction"),
        "tax_component": definition.get("tax_component"),
        "inventory_role": definition.get("inventory_role"),
        "contra_of_system_key": definition.get("contra_of_system_key"),
        "is_party_ledger": False,
        "is_system": True,
        "is_protected": True,
        "name_locked": True,
        "deletion_locked": True,
        "balance_managed_by_postings": True,
        "opening_balance_locked": True,
        "status": LEDGER_STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": False,
        "sort_order": int(definition["sort_order"]),
    }


def _find_conflicting_ledger(entity_id, canonical, exclude_id=None):
    query = {
        "accounting_entity_id": entity_id,
        "is_deleted": False,
        "$or": [
            {"ledger_code": canonical["ledger_code"]},
            {"normalized_name": canonical["normalized_name"]},
        ],
    }
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    return mongo.db[LEDGER_COLLECTION].find_one(query)


def _seed_one_ledger(
    entity,
    definition,
    actor,
    account_group,
    seed_run_id,
):
    _validate_catalog_definition(definition)
    collection = mongo.db[LEDGER_COLLECTION]
    timestamp = now_utc()
    canonical = _canonical_fields(definition, entity, account_group)

    existing = collection.find_one(
        {
            "accounting_entity_id": entity["_id"],
            "system_key": definition["system_key"],
        }
    )

    if not existing:
        conflict = _find_conflicting_ledger(entity["_id"], canonical)
        if conflict:
            raise RuntimeError(
                f"Cannot seed {definition['name']}: an existing ledger already uses "
                f"the code or name ({conflict.get('name') or conflict.get('ledger_code')}). "
                "No record was overwritten automatically."
            )

        document = {
            **canonical,
            "version": 1,
            "created_by": actor["_id"],
            "created_by_str": str(actor["_id"]),
            "created_by_name": actor.get("resolved_name") or "",
            "created_at": timestamp,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
            "seed_run_id": seed_run_id,
            "change_history": [
                _change_event(
                    "seed_default_avpl_ledger",
                    actor,
                    changed_fields=sorted(canonical.keys()),
                    seed_run_id=seed_run_id,
                )
            ],
            "audit_sync_required": False,
        }

        try:
            result = collection.insert_one(document)
            document["_id"] = result.inserted_id
        except DuplicateKeyError as exc:
            raise RuntimeError(
                f"A duplicate ledger-master conflict occurred while creating {definition['name']}. "
                "No existing record was modified automatically."
            ) from exc

        _record_audit(
            document,
            actor,
            "seed_default_avpl_ledger",
            changed_fields=sorted(canonical.keys()),
            remarks=f"Protected AVPL ledger {definition['name']} created.",
        )
        return "created", document

    if existing.get("is_system") is not True:
        raise RuntimeError(
            f"The reserved system key {definition['system_key']} is attached to a non-system ledger. "
            "Manual review is required; no record was converted automatically."
        )

    conflict = _find_conflicting_ledger(
        entity["_id"],
        canonical,
        exclude_id=existing["_id"],
    )
    if conflict:
        raise RuntimeError(
            f"Cannot restore {definition['name']}: another active ledger already uses "
            f"the protected code or name ({conflict.get('name') or conflict.get('ledger_code')}). "
            "Manual review is required."
        )

    changed_fields = [
        field
        for field, expected_value in canonical.items()
        if existing.get(field) != expected_value
    ]
    if not changed_fields:
        return "unchanged", existing

    next_version = int(existing.get("version") or 1) + 1
    updates = {field: canonical[field] for field in changed_fields}
    updates.update(
        {
            "version": next_version,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_by_name": actor.get("resolved_name") or "",
            "updated_at": timestamp,
            "seed_run_id": seed_run_id,
        }
    )

    update_query = {"_id": existing["_id"]}
    if "version" in existing:
        update_query["version"] = existing.get("version")
    else:
        update_query["version"] = {"$exists": False}

    result = collection.update_one(
        update_query,
        {
            "$set": updates,
            "$push": {
                "change_history": _change_event(
                    "repair_default_avpl_ledger",
                    actor,
                    changed_fields=changed_fields,
                    seed_run_id=seed_run_id,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError(
            f"{definition['name']} changed during ledger synchronization. Refresh and run synchronization again."
        )

    repaired = collection.find_one({"_id": existing["_id"]})
    _record_audit(
        repaired,
        actor,
        "repair_default_avpl_ledger",
        changed_fields=changed_fields,
        remarks=(
            f"Protected AVPL ledger {definition['name']} was restored to its canonical system definition."
        ),
    )
    return "repaired", repaired


# ---------------------------------------------------------------------------
# Idempotent default-ledger seed
# ---------------------------------------------------------------------------


def seed_default_avpl_ledgers(actor_user_id, accounting_entity_id=None):
    """Create or repair the 14 protected AVPL default ledgers.

    The operation is idempotent and requires the protected Stage 3 Batch 1
    account groups. It never creates Centre or Farmer ledgers, never writes
    opening balances and never stores a mutable live ledger balance.
    """
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], BOOTSTRAP_PERMISSION)
    ensure_ledger_indexes()

    account_group_overview = get_account_group_overview(
        entity["_id"],
        actor["_id"],
    )
    if not account_group_overview.get("health", {}).get("is_complete"):
        raise RuntimeError(
            "Protected account groups are missing or have definition drift. Run Stage 3 Batch 1 verification and repair before initializing ledgers."
        )

    groups_by_key = _resolve_required_groups(entity["_id"])

    seed_run_id = uuid4().hex
    results = {
        "created": 0,
        "repaired": 0,
        "unchanged": 0,
        "ledgers": [],
    }

    with _default_ledger_seed_lock(entity["_id"]):
        for definition in DEFAULT_AVPL_LEDGERS:
            account_group = groups_by_key[
                definition["account_group_system_key"]
            ]
            outcome, ledger = _seed_one_ledger(
                entity,
                definition,
                actor,
                account_group,
                seed_run_id,
            )
            results[outcome] += 1
            results["ledgers"].append(ledger)

    results.update(
        {
            "seed_run_id": seed_run_id,
            "total": len(DEFAULT_AVPL_LEDGERS),
            "entity_id": str(entity["_id"]),
            "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
            "message": (
                f"Default AVPL ledgers synchronized: {results['created']} created, "
                f"{results['repaired']} restored and {results['unchanged']} already correct."
            ),
        }
    )
    return results


# ---------------------------------------------------------------------------
# Read model and future posting validation
# ---------------------------------------------------------------------------


def _list_entity_ledger_documents(entity_id, include_inactive=False):
    query = {
        "accounting_entity_id": entity_id,
        "is_deleted": False,
    }
    if not include_inactive:
        query.update(
            {
                "status": LEDGER_STATUS_ACTIVE,
                "is_active": True,
            }
        )

    return list(
        mongo.db[LEDGER_COLLECTION].find(query).sort(
            [("sort_order", ASCENDING), ("name", ASCENDING)]
        )
    )


def _default_ledger_health(rows, groups_by_key):
    by_system_key = {
        row.get("system_key"): row
        for row in rows
        if row.get("system_key")
    }
    missing = []
    drifted = []

    for definition in DEFAULT_AVPL_LEDGERS:
        row = by_system_key.get(definition["system_key"])
        if not row:
            missing.append(definition["name"])
            continue

        account_group = groups_by_key.get(
            definition["account_group_system_key"]
        )
        if not account_group:
            drifted.append(
                {
                    "name": definition["name"],
                    "system_key": definition["system_key"],
                    "changed_fields": ["account_group_missing"],
                }
            )
            continue

        canonical = {
            "ledger_code": definition["ledger_code"],
            "name": definition["name"],
            "normalized_name": _normalize_name(definition["name"]),
            "account_group_id": account_group["_id"],
            "account_group_system_key": definition[
                "account_group_system_key"
            ],
            "ledger_type": definition["ledger_type"],
            "normal_balance": definition["normal_balance"],
            "posting_policy": definition["posting_policy"],
            "catalog_section": definition["catalog_section"],
            "section_label": definition["section_label"],
            "document_role": definition.get("document_role"),
            "payment_mode": definition.get("payment_mode"),
            "clearing_mode": definition.get("clearing_mode"),
            "tax_direction": definition.get("tax_direction"),
            "tax_component": definition.get("tax_component"),
            "inventory_role": definition.get("inventory_role"),
            "contra_of_system_key": definition.get("contra_of_system_key"),
            "is_party_ledger": False,
            "is_system": True,
            "is_protected": True,
            "name_locked": True,
            "deletion_locked": True,
            "balance_managed_by_postings": True,
            "opening_balance_locked": True,
            "status": LEDGER_STATUS_ACTIVE,
            "is_active": True,
            "is_deleted": False,
            "sort_order": int(definition["sort_order"]),
        }
        changed = [
            field
            for field, expected in canonical.items()
            if row.get(field) != expected
        ]
        if changed:
            drifted.append(
                {
                    "name": definition["name"],
                    "system_key": definition["system_key"],
                    "changed_fields": changed,
                }
            )

    return {
        "required_count": len(DEFAULT_AVPL_LEDGERS),
        "present_count": len(DEFAULT_AVPL_LEDGERS) - len(missing),
        "missing": missing,
        "missing_count": len(missing),
        "drifted": drifted,
        "drifted_count": len(drifted),
        "is_complete": not missing and not drifted,
    }


def list_ledgers(accounting_entity_id, actor_user_id, include_inactive=False):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_ledger_indexes()

    rows = _list_entity_ledger_documents(
        entity["_id"],
        include_inactive=include_inactive,
    )
    group_ids = {
        row.get("account_group_id")
        for row in rows
        if row.get("account_group_id")
    }
    group_names = {
        row["_id"]: row.get("name") or ""
        for row in mongo.db[ACCOUNT_GROUP_COLLECTION].find(
            {"_id": {"$in": list(group_ids)}}
        )
    } if group_ids else {}

    return [
        serialize_ledger(
            row,
            account_group_name=group_names.get(
                row.get("account_group_id"), ""
            ),
        )
        for row in rows
    ]


def get_ledger_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_ledger_indexes()

    all_rows = _list_entity_ledger_documents(
        entity["_id"],
        include_inactive=True,
    )
    active_rows = [
        row
        for row in all_rows
        if row.get("is_active") is True
        and row.get("status") == LEDGER_STATUS_ACTIVE
        and row.get("is_deleted") is not True
    ]

    groups_by_key = {
        row.get("system_key"): row
        for row in mongo.db[ACCOUNT_GROUP_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_deleted": False,
            }
        )
        if row.get("system_key")
    }
    group_names = {
        row["_id"]: row.get("name") or ""
        for row in groups_by_key.values()
    }
    serialized = [
        serialize_ledger(
            row,
            account_group_name=group_names.get(
                row.get("account_group_id"), ""
            ),
        )
        for row in all_rows
    ]

    section_order = []
    sections_by_key = {}
    for definition in DEFAULT_AVPL_LEDGERS:
        section_key = definition["catalog_section"]
        if section_key not in sections_by_key:
            section_order.append(section_key)
            sections_by_key[section_key] = {
                "key": section_key,
                "label": definition["section_label"],
                "ledgers": [],
            }

    for ledger in serialized:
        section_key = ledger.get("catalog_section") or "other"
        if section_key not in sections_by_key:
            section_order.append(section_key)
            sections_by_key[section_key] = {
                "key": section_key,
                "label": "Other ledgers",
                "ledgers": [],
            }
        sections_by_key[section_key]["ledgers"].append(ledger)

    health = _default_ledger_health(all_rows, groups_by_key)

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": (
            entity.get("display_name")
            or entity.get("legal_name")
            or AVPL_ENTITY_CODE
        ),
        "ledgers": serialized,
        "sections": [sections_by_key[key] for key in section_order],
        "ledger_count": len(serialized),
        "active_count": len(active_rows),
        "system_count": sum(
            1 for row in all_rows if row.get("is_system") is True
        ),
        "protected_count": sum(
            1 for row in all_rows if row.get("is_protected") is True
        ),
        "inactive_count": len(serialized) - len(active_rows),
        "tax_ledger_count": sum(
            1 for row in all_rows if row.get("ledger_type") == "tax"
        ),
        "clearing_ledger_count": sum(
            1 for row in all_rows if row.get("ledger_type") == "clearing"
        ),
        "audit_recovery_count": sum(
            1 for row in all_rows if row.get("audit_sync_required") is True
        ),
        "health": health,
    }


def get_ledger_for_posting(accounting_entity_id, ledger_id):
    """Resolve an active entity-owned ledger for future posting services.

    The caller must perform its own actor permission and document-state checks.
    No live balance is read from the ledger master; future balances must be
    calculated from voucher lines.
    """
    entity_object_id = _to_object_id(accounting_entity_id)
    ledger_object_id = _to_object_id(ledger_id)
    if not entity_object_id or not ledger_object_id:
        raise ValueError("Invalid Accounting entity or ledger.")

    ledger = mongo.db[LEDGER_COLLECTION].find_one(
        {
            "_id": ledger_object_id,
            "accounting_entity_id": entity_object_id,
            "is_deleted": False,
            "status": LEDGER_STATUS_ACTIVE,
            "is_active": True,
        }
    )
    if not ledger:
        raise ValueError(
            "The selected ledger was not found, is inactive or belongs to another Accounting entity."
        )
    return ledger
