from contextlib import contextmanager
from datetime import timedelta
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


ACCOUNT_GROUP_COLLECTION = "account_groups"
AVPL_ENTITY_CODE = "AVPL"

GROUP_STATUS_ACTIVE = "active"
GROUP_STATUS_INACTIVE = "inactive"

GROUP_NATURE_EQUITY = "equity"
GROUP_NATURE_ASSET = "asset"
GROUP_NATURE_LIABILITY = "liability"
GROUP_NATURE_INCOME = "income"
GROUP_NATURE_EXPENSE = "expense"

STATEMENT_BALANCE_SHEET = "balance_sheet"
STATEMENT_PROFIT_AND_LOSS = "profit_and_loss"

BALANCE_DEBIT = "debit"
BALANCE_CREDIT = "credit"

VIEW_PERMISSION = "accounting.account_group.view"
BOOTSTRAP_PERMISSION = "accounting.account_group.bootstrap"

SYSTEM_GROUP_RENAME_MESSAGE = (
    "System account groups are protected and cannot be renamed."
)
SYSTEM_GROUP_DELETE_MESSAGE = (
    "System account groups are protected and cannot be deleted or deactivated."
)

_SAFE_SYSTEM_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_SAFE_GROUP_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_\-]{1,39}$")


# Parent groups appear before their children so an interrupted seed can be
# safely resumed without creating unresolved parent references.
PROTECTED_ACCOUNT_GROUPS = (
    {
        "system_key": "capital_account",
        "group_code": "CAPITAL",
        "name": "Capital Account",
        "nature": GROUP_NATURE_EQUITY,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": None,
        "sort_order": 10,
    },
    {
        "system_key": "current_assets",
        "group_code": "CURRENT_ASSETS",
        "name": "Current Assets",
        "nature": GROUP_NATURE_ASSET,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": None,
        "sort_order": 20,
    },
    {
        "system_key": "current_liabilities",
        "group_code": "CURRENT_LIABILITIES",
        "name": "Current Liabilities",
        "nature": GROUP_NATURE_LIABILITY,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": None,
        "sort_order": 30,
    },
    {
        "system_key": "sales_accounts",
        "group_code": "SALES_ACCOUNTS",
        "name": "Sales Accounts",
        "nature": GROUP_NATURE_INCOME,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 40,
    },
    {
        "system_key": "purchase_accounts",
        "group_code": "PURCHASE_ACCOUNTS",
        "name": "Purchase Accounts",
        "nature": GROUP_NATURE_EXPENSE,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 50,
    },
    {
        "system_key": "direct_expenses",
        "group_code": "DIRECT_EXPENSES",
        "name": "Direct Expenses",
        "nature": GROUP_NATURE_EXPENSE,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 60,
    },
    {
        "system_key": "indirect_expenses",
        "group_code": "INDIRECT_EXPENSES",
        "name": "Indirect Expenses",
        "nature": GROUP_NATURE_EXPENSE,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 70,
    },
    {
        "system_key": "direct_incomes",
        "group_code": "DIRECT_INCOMES",
        "name": "Direct Incomes",
        "nature": GROUP_NATURE_INCOME,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 80,
    },
    {
        "system_key": "indirect_incomes",
        "group_code": "INDIRECT_INCOMES",
        "name": "Indirect Incomes",
        "nature": GROUP_NATURE_INCOME,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_PROFIT_AND_LOSS,
        "parent_system_key": None,
        "sort_order": 90,
    },
    {
        "system_key": "sundry_debtors",
        "group_code": "SUNDRY_DEBTORS",
        "name": "Sundry Debtors",
        "nature": GROUP_NATURE_ASSET,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_assets",
        "sort_order": 110,
    },
    {
        "system_key": "cash_in_hand",
        "group_code": "CASH_IN_HAND",
        "name": "Cash-in-Hand",
        "nature": GROUP_NATURE_ASSET,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_assets",
        "sort_order": 120,
    },
    {
        "system_key": "bank_accounts",
        "group_code": "BANK_ACCOUNTS",
        "name": "Bank Accounts",
        "nature": GROUP_NATURE_ASSET,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_assets",
        "sort_order": 130,
    },
    {
        "system_key": "stock_in_hand",
        "group_code": "STOCK_IN_HAND",
        "name": "Stock-in-Hand",
        "nature": GROUP_NATURE_ASSET,
        "balance_nature": BALANCE_DEBIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_assets",
        "sort_order": 140,
    },
    {
        "system_key": "sundry_creditors",
        "group_code": "SUNDRY_CREDITORS",
        "name": "Sundry Creditors",
        "nature": GROUP_NATURE_LIABILITY,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_liabilities",
        "sort_order": 210,
    },
    {
        "system_key": "duties_and_taxes",
        "group_code": "DUTIES_AND_TAXES",
        "name": "Duties & Taxes",
        "nature": GROUP_NATURE_LIABILITY,
        "balance_nature": BALANCE_CREDIT,
        "statement_type": STATEMENT_BALANCE_SHEET,
        "parent_system_key": "current_liabilities",
        "sort_order": 220,
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
    """Create the required index without dropping or rebuilding old indexes."""
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


def ensure_account_group_indexes():
    """Install Stage 3 account-group indexes safely and idempotently."""
    collection = mongo.db[ACCOUNT_GROUP_COLLECTION]

    # A system key remains reserved forever, even if a protected record was
    # changed directly in the database and temporarily marked deleted.
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("system_key", ASCENDING)],
        name="account_group_entity_system_key_unique",
        unique=True,
        partialFilterExpression={"system_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("group_code", ASCENDING)],
        name="account_group_entity_code_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("normalized_name", ASCENDING)],
        name="account_group_entity_name_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("parent_group_id", ASCENDING),
            ("sort_order", ASCENDING),
        ],
        name="account_group_entity_parent_sort_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("is_system", ASCENDING),
            ("status", ASCENDING),
        ],
        name="account_group_entity_system_status_idx",
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("updated_at", DESCENDING)],
        name="account_group_entity_updated_idx",
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
            "You are not authorized to perform this account-group action."
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
            "The active AVPL Accounting entity was not found. Initialize or reactivate AVPL before creating account groups."
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
def _account_group_seed_lock(entity_id):
    token = uuid4().hex
    timestamp = now_utc()
    stale_before = timestamp - timedelta(seconds=60)
    field_name = "setup_locks.protected_account_groups"

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
            "Another protected account-group setup is in progress. Please try again shortly."
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


def assert_account_group_can_be_renamed(group):
    """Block all application-level renaming of protected system groups."""
    if not group:
        raise ValueError("The account group was not found.")
    if group.get("is_system") is True or group.get("is_protected") is True:
        raise PermissionError(SYSTEM_GROUP_RENAME_MESSAGE)
    return True


def assert_account_group_can_be_deleted(group):
    """Block hard deletion and deactivation of protected system groups."""
    if not group:
        raise ValueError("The account group was not found.")
    if group.get("is_system") is True or group.get("is_protected") is True:
        raise PermissionError(SYSTEM_GROUP_DELETE_MESSAGE)
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


def _record_audit(group, actor, action, changed_fields=None, remarks=""):
    timestamp = now_utc()
    audit_document = {
        "module": "accounting",
        "action": action,
        "accounting_entity_id": group.get("accounting_entity_id"),
        "accounting_entity_id_str": str(
            group.get("accounting_entity_id") or ""
        ),
        "entity_type": "account_group",
        "entity_id": group.get("_id"),
        "entity_id_str": str(group.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": None,
        "new_status": group.get("status") or GROUP_STATUS_ACTIVE,
        "metadata": {
            "system_key": group.get("system_key"),
            "group_code": group.get("group_code"),
            "group_name": group.get("name"),
            "parent_system_key": group.get("parent_system_key"),
            "nature": group.get("nature"),
            "statement_type": group.get("statement_type"),
            "is_system": group.get("is_system") is True,
            "is_protected": group.get("is_protected") is True,
            "version": int(group.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": remarks or "Protected account-group master updated.",
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit_document)
    except Exception as exc:
        try:
            mongo.db[ACCOUNT_GROUP_COLLECTION].update_one(
                {"_id": group.get("_id")},
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

    mongo.db[ACCOUNT_GROUP_COLLECTION].update_one(
        {"_id": group.get("_id")},
        {
            "$set": {
                "audit_sync_required": False,
                "audit_sync_action": None,
                "audit_sync_completed_at": timestamp,
            }
        },
    )
    return True


def serialize_account_group(group, parent_name=""):
    if not group:
        return None

    return {
        "id": str(group.get("_id") or ""),
        "accounting_entity_id": str(
            group.get("accounting_entity_id") or ""
        ),
        "group_code": group.get("group_code") or "",
        "name": group.get("name") or "",
        "normalized_name": group.get("normalized_name") or "",
        "system_key": group.get("system_key") or "",
        "nature": group.get("nature") or "",
        "balance_nature": group.get("balance_nature") or "",
        "statement_type": group.get("statement_type") or "",
        "parent_group_id": str(group.get("parent_group_id") or ""),
        "parent_system_key": group.get("parent_system_key") or "",
        "parent_name": parent_name or "",
        "is_primary": group.get("is_primary") is True,
        "is_system": group.get("is_system") is True,
        "is_protected": group.get("is_protected") is True,
        "name_locked": group.get("name_locked") is True,
        "deletion_locked": group.get("deletion_locked") is True,
        "status": group.get("status") or GROUP_STATUS_INACTIVE,
        "is_active": group.get("is_active") is True,
        "is_deleted": group.get("is_deleted") is True,
        "sort_order": int(group.get("sort_order") or 0),
        "version": int(group.get("version") or 1),
        "audit_sync_required": group.get("audit_sync_required") is True,
        "created_at": group.get("created_at"),
        "updated_at": group.get("updated_at"),
    }


def get_protected_account_group_catalog():
    return [dict(item) for item in PROTECTED_ACCOUNT_GROUPS]


# ---------------------------------------------------------------------------
# Idempotent protected-group seed
# ---------------------------------------------------------------------------


def _validate_catalog_definition(definition):
    system_key = str(definition.get("system_key") or "").strip()
    group_code = str(definition.get("group_code") or "").strip()
    name = _clean_single_line(definition.get("name"), "Account-group name")

    if not _SAFE_SYSTEM_KEY_PATTERN.fullmatch(system_key):
        raise RuntimeError(
            f"Invalid protected account-group system key: {system_key or 'blank'}."
        )
    if not _SAFE_GROUP_CODE_PATTERN.fullmatch(group_code):
        raise RuntimeError(
            f"Invalid protected account-group code: {group_code or 'blank'}."
        )

    if definition.get("nature") not in {
        GROUP_NATURE_EQUITY,
        GROUP_NATURE_ASSET,
        GROUP_NATURE_LIABILITY,
        GROUP_NATURE_INCOME,
        GROUP_NATURE_EXPENSE,
    }:
        raise RuntimeError(f"Invalid nature configured for {name}.")

    if definition.get("statement_type") not in {
        STATEMENT_BALANCE_SHEET,
        STATEMENT_PROFIT_AND_LOSS,
    }:
        raise RuntimeError(f"Invalid statement type configured for {name}.")

    if definition.get("balance_nature") not in {
        BALANCE_DEBIT,
        BALANCE_CREDIT,
    }:
        raise RuntimeError(f"Invalid balance nature configured for {name}.")


def _canonical_fields(definition, entity, parent):
    parent_id = parent.get("_id") if parent else None
    return {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "group_code": definition["group_code"],
        "name": definition["name"],
        "normalized_name": _normalize_name(definition["name"]),
        "system_key": definition["system_key"],
        "nature": definition["nature"],
        "balance_nature": definition["balance_nature"],
        "statement_type": definition["statement_type"],
        "parent_group_id": parent_id,
        "parent_group_id_str": str(parent_id or ""),
        "parent_system_key": definition.get("parent_system_key"),
        "is_primary": parent is None,
        "is_system": True,
        "is_protected": True,
        "name_locked": True,
        "deletion_locked": True,
        "status": GROUP_STATUS_ACTIVE,
        "is_active": True,
        "is_deleted": False,
        "sort_order": int(definition["sort_order"]),
    }


def _find_conflicting_group(entity_id, canonical, exclude_id=None):
    conditions = [
        {"group_code": canonical["group_code"]},
        {"normalized_name": canonical["normalized_name"]},
    ]
    query = {
        "accounting_entity_id": entity_id,
        "is_deleted": False,
        "$or": conditions,
    }
    if exclude_id:
        query["_id"] = {"$ne": exclude_id}
    return mongo.db[ACCOUNT_GROUP_COLLECTION].find_one(query)


def _seed_one_group(entity, definition, actor, parent, seed_run_id):
    _validate_catalog_definition(definition)
    collection = mongo.db[ACCOUNT_GROUP_COLLECTION]
    timestamp = now_utc()
    canonical = _canonical_fields(definition, entity, parent)

    existing = collection.find_one({
        "accounting_entity_id": entity["_id"],
        "system_key": definition["system_key"],
    })

    if not existing:
        conflict = _find_conflicting_group(entity["_id"], canonical)
        if conflict:
            raise RuntimeError(
                f"Cannot seed {definition['name']}: an existing account group already uses "
                f"the code or name ({conflict.get('name') or conflict.get('group_code')}). "
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
                    "seed_protected_account_group",
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
            # A concurrent request should normally be blocked by the entity lock.
            # If a database race still occurs, stop rather than guessing which
            # record is safe to retain.
            raise RuntimeError(
                f"A duplicate account-group master conflict occurred while creating {definition['name']}. "
                "No existing record was modified automatically."
            ) from exc

        _record_audit(
            document,
            actor,
            "seed_protected_account_group",
            changed_fields=sorted(canonical.keys()),
            remarks=f"Protected AVPL account group {definition['name']} created.",
        )
        return "created", document

    if existing.get("is_system") is not True:
        raise RuntimeError(
            f"The reserved system key {definition['system_key']} is attached to a non-system account group. "
            "Manual review is required; no record was converted automatically."
        )

    conflict = _find_conflicting_group(
        entity["_id"],
        canonical,
        exclude_id=existing["_id"],
    )
    if conflict:
        raise RuntimeError(
            f"Cannot restore {definition['name']}: another active account group already uses "
            f"the protected code or name ({conflict.get('name') or conflict.get('group_code')}). "
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
    updates = {
        field: canonical[field]
        for field in changed_fields
    }
    updates.update({
        "version": next_version,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "seed_run_id": seed_run_id,
    })

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
                    "repair_protected_account_group",
                    actor,
                    changed_fields=changed_fields,
                    seed_run_id=seed_run_id,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError(
            f"{definition['name']} changed during protected-group synchronization. Refresh and run synchronization again."
        )

    repaired = collection.find_one({"_id": existing["_id"]})
    _record_audit(
        repaired,
        actor,
        "repair_protected_account_group",
        changed_fields=changed_fields,
        remarks=(
            f"Protected AVPL account group {definition['name']} was restored to its canonical system definition."
        ),
    )
    return "repaired", repaired


def seed_protected_account_groups(actor_user_id, accounting_entity_id=None):
    """Create or repair the 15 permanent AVPL account groups.

    The operation is idempotent. Existing canonical groups are left untouched,
    interrupted runs can be safely repeated, and any direct rename/deletion of
    a recognized protected system group is restored to its canonical state.
    Conflicting non-system data is never overwritten automatically.
    """
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], BOOTSTRAP_PERMISSION)
    ensure_account_group_indexes()

    seed_run_id = uuid4().hex
    results = {
        "created": 0,
        "repaired": 0,
        "unchanged": 0,
        "groups": [],
    }
    resolved_by_system_key = {}

    with _account_group_seed_lock(entity["_id"]):
        for definition in PROTECTED_ACCOUNT_GROUPS:
            parent_system_key = definition.get("parent_system_key")
            parent = None

            if parent_system_key:
                parent = resolved_by_system_key.get(parent_system_key)
                if not parent:
                    parent = mongo.db[ACCOUNT_GROUP_COLLECTION].find_one({
                        "accounting_entity_id": entity["_id"],
                        "system_key": parent_system_key,
                        "is_deleted": False,
                    })
                if not parent:
                    raise RuntimeError(
                        f"The protected parent group {parent_system_key} is missing. No child group was created."
                    )

            outcome, group = _seed_one_group(
                entity,
                definition,
                actor,
                parent,
                seed_run_id,
            )
            results[outcome] += 1
            results["groups"].append(group)
            resolved_by_system_key[definition["system_key"]] = group

    total = len(PROTECTED_ACCOUNT_GROUPS)
    results.update({
        "seed_run_id": seed_run_id,
        "total": total,
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "message": (
            f"Protected AVPL account groups synchronized: {results['created']} created, "
            f"{results['repaired']} restored and {results['unchanged']} already correct."
        ),
    })
    return results


# ---------------------------------------------------------------------------
# Read model and future ledger validation
# ---------------------------------------------------------------------------


def _list_entity_group_documents(entity_id, include_inactive=False):
    query = {
        "accounting_entity_id": entity_id,
        "is_deleted": False,
    }
    if not include_inactive:
        query.update({
            "status": GROUP_STATUS_ACTIVE,
            "is_active": True,
        })

    return list(
        mongo.db[ACCOUNT_GROUP_COLLECTION].find(query).sort([
            ("sort_order", ASCENDING),
            ("name", ASCENDING),
        ])
    )


def _protected_group_health(rows):
    by_system_key = {
        row.get("system_key"): row
        for row in rows
        if row.get("system_key")
    }
    missing = []
    drifted = []

    for definition in PROTECTED_ACCOUNT_GROUPS:
        row = by_system_key.get(definition["system_key"])
        if not row:
            missing.append(definition["name"])
            continue

        parent = by_system_key.get(definition.get("parent_system_key"))
        canonical = {
            "group_code": definition["group_code"],
            "name": definition["name"],
            "normalized_name": _normalize_name(definition["name"]),
            "nature": definition["nature"],
            "balance_nature": definition["balance_nature"],
            "statement_type": definition["statement_type"],
            "parent_group_id": parent.get("_id") if parent else None,
            "parent_system_key": definition.get("parent_system_key"),
            "is_system": True,
            "is_protected": True,
            "name_locked": True,
            "deletion_locked": True,
            "status": GROUP_STATUS_ACTIVE,
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
            drifted.append({
                "name": definition["name"],
                "system_key": definition["system_key"],
                "changed_fields": changed,
            })

    return {
        "required_count": len(PROTECTED_ACCOUNT_GROUPS),
        "present_count": len(PROTECTED_ACCOUNT_GROUPS) - len(missing),
        "missing": missing,
        "missing_count": len(missing),
        "drifted": drifted,
        "drifted_count": len(drifted),
        "is_complete": not missing and not drifted,
    }


def list_account_groups(
    accounting_entity_id,
    actor_user_id,
    include_inactive=False,
):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_account_group_indexes()

    rows = _list_entity_group_documents(
        entity["_id"],
        include_inactive=include_inactive,
    )
    names_by_id = {
        row["_id"]: row.get("name") or ""
        for row in rows
    }

    return [
        serialize_account_group(
            row,
            parent_name=names_by_id.get(row.get("parent_group_id"), ""),
        )
        for row in rows
    ]


def get_account_group_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"super_admin", "avpl_admin", "accounts"},
    )
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_account_group_indexes()

    all_rows = _list_entity_group_documents(
        entity["_id"],
        include_inactive=True,
    )
    active_rows = [
        row
        for row in all_rows
        if row.get("is_active") is True
        and row.get("status") == GROUP_STATUS_ACTIVE
        and row.get("is_deleted") is not True
    ]
    names_by_id = {
        row["_id"]: row.get("name") or ""
        for row in all_rows
    }
    serialized = [
        serialize_account_group(
            row,
            parent_name=names_by_id.get(row.get("parent_group_id"), ""),
        )
        for row in all_rows
    ]

    primary_groups = [item for item in serialized if item.get("is_primary")]
    child_groups = [item for item in serialized if not item.get("is_primary")]
    health = _protected_group_health(all_rows)

    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": (
            entity.get("display_name")
            or entity.get("legal_name")
            or AVPL_ENTITY_CODE
        ),
        "groups": serialized,
        "primary_groups": primary_groups,
        "child_groups": child_groups,
        "group_count": len(serialized),
        "active_count": len(active_rows),
        "system_count": sum(
            1 for row in all_rows if row.get("is_system") is True
        ),
        "protected_count": sum(
            1 for row in all_rows if row.get("is_protected") is True
        ),
        "inactive_count": len(serialized) - len(active_rows),
        "audit_recovery_count": sum(
            1 for row in all_rows if row.get("audit_sync_required") is True
        ),
        "health": health,
    }


def get_account_group_for_ledger(accounting_entity_id, account_group_id):
    """Resolve an active group for future ledger creation services.

    This helper performs entity isolation but intentionally does not resolve a
    user permission. The calling ledger service must authorize its actor before
    using the returned master.
    """
    entity_object_id = _to_object_id(accounting_entity_id)
    group_object_id = _to_object_id(account_group_id)
    if not entity_object_id or not group_object_id:
        raise ValueError("Invalid Accounting entity or account group.")

    group = mongo.db[ACCOUNT_GROUP_COLLECTION].find_one({
        "_id": group_object_id,
        "accounting_entity_id": entity_object_id,
        "is_deleted": False,
        "status": GROUP_STATUS_ACTIVE,
        "is_active": True,
    })
    if not group:
        raise ValueError(
            "The selected account group was not found, is inactive or belongs to another Accounting entity."
        )

    return group
