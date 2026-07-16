from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.utils.helpers import now_utc


AVPL_ENTITY_CODE = "AVPL"


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in keys]


def _ensure_exact_index(collection, keys, name, **options):
    """Create an index only when the required definition is absent.

    Accounting indexes are never dropped automatically. If an index with the
    requested name or key pattern exists with incompatible options, setup stops
    with a clear error so production data is not exposed to an unsafe rebuild.
    """
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


def ensure_accounting_entity_indexes():
    """Install the small Batch 2 entity indexes without altering old indexes."""
    _ensure_exact_index(
        mongo.db.accounting_entities,
        [("entity_code", ASCENDING)],
        name="accounting_entity_code_unique",
        unique=True,
    )
    _ensure_exact_index(
        mongo.db.accounting_entities,
        [("entity_type", ASCENDING), ("status", ASCENDING)],
        name="accounting_entity_type_status_idx",
    )
    _ensure_exact_index(
        mongo.db.accounting_entities,
        [("source_collection", ASCENDING), ("source_master_id", ASCENDING)],
        name="accounting_entity_source_idx",
    )
    _ensure_exact_index(
        mongo.db.accounting_audit_logs,
        [("accounting_entity_id", ASCENDING), ("created_at", DESCENDING)],
        name="accounting_audit_entity_created_idx",
    )


def get_avpl_entity(include_inactive=False):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "is_deleted": {"$ne": True},
    }

    if not include_inactive:
        query.update({
            "status": "active",
            "accounting_enabled": {"$ne": False},
        })

    return mongo.db.accounting_entities.find_one(query)


def serialize_accounting_entity(entity):
    if not entity:
        return None

    return {
        "id": str(entity.get("_id")),
        "entity_code": entity.get("entity_code") or "",
        "entity_type": entity.get("entity_type") or "",
        "display_name": entity.get("display_name") or "",
        "legal_name": entity.get("legal_name") or "",
        "trade_name": entity.get("trade_name") or "",
        "books_mode": entity.get("books_mode") or "",
        "base_currency": entity.get("base_currency") or "INR",
        "country_code": entity.get("country_code") or "IN",
        "address_line_1": entity.get("address_line_1") or "",
        "address_line_2": entity.get("address_line_2") or "",
        "city": entity.get("city") or "",
        "district": entity.get("district") or "",
        "state": entity.get("state") or "",
        "state_code": entity.get("state_code") or "",
        "postal_code": entity.get("postal_code") or "",
        "pan": entity.get("pan") or "",
        "gst_registration_status": entity.get("gst_registration_status") or "",
        "gstin": entity.get("gstin") or "",
        "books_beginning_date": entity.get("books_beginning_date"),
        "default_financial_year_id": str(entity.get("default_financial_year_id") or ""),
        "entity_profile_revision": int(entity.get("entity_profile_revision") or 0),
        "profile_status": entity.get("profile_status") or "incomplete",
        "status": entity.get("status") or "inactive",
        "accounting_enabled": entity.get("accounting_enabled", False) is not False,
        "is_system_entity": entity.get("is_system_entity", False) is True,
        "created_at": entity.get("created_at"),
        "updated_at": entity.get("updated_at"),
    }


def bootstrap_avpl_entity(actor_user_id):
    """Create the permanent AVPL Accounting entity once, safely and idempotently."""
    actor_object_id = _to_object_id(actor_user_id)

    if not actor_object_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_object_id},
        {"role": 1, "active": 1, "status": 1},
    )

    if not actor:
        raise ValueError("Authenticated user was not found.")

    if actor.get("active", True) is False or actor.get("status") == "inactive":
        raise ValueError("Inactive users cannot initialize Accounting entities.")

    if actor.get("role") != "super_admin":
        raise PermissionError("Only Super Admin can initialize the AVPL Accounting entity.")

    ensure_accounting_entity_indexes()

    existing = get_avpl_entity(include_inactive=True)
    if existing:
        return {
            "created": False,
            "entity": serialize_accounting_entity(existing),
            "message": "The AVPL Accounting entity is already initialized.",
        }

    timestamp = now_utc()
    entity_document = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_slug": "avpl",
        "entity_type": "avpl",
        "display_name": "AVPL",
        "legal_name": "",
        "books_mode": "full",
        "base_currency": "INR",
        "country_code": "IN",
        "state": "",
        "gstin": "",
        "pan": "",
        "profile_status": "incomplete",
        "accounting_enabled": True,
        "is_system_entity": True,
        "is_deleted": False,
        "status": "active",
        "version": 1,
        "created_by": actor_object_id,
        "created_by_str": str(actor_object_id),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    try:
        result = mongo.db.accounting_entities.insert_one(entity_document)
        entity_document["_id"] = result.inserted_id
        created = True
    except DuplicateKeyError:
        # Another request may have initialized the same permanent entity first.
        entity_document = get_avpl_entity(include_inactive=True)
        created = False

    if not entity_document:
        raise RuntimeError("The AVPL Accounting entity could not be initialized.")

    if created:
        mongo.db.accounting_audit_logs.insert_one({
            "module": "accounting",
            "action": "bootstrap_accounting_entity",
            "accounting_entity_id": entity_document["_id"],
            "accounting_entity_id_str": str(entity_document["_id"]),
            "entity_type": "accounting_entity",
            "entity_id": entity_document["_id"],
            "entity_id_str": str(entity_document["_id"]),
            "actor_user_id": actor_object_id,
            "actor_user_id_str": str(actor_object_id),
            "previous_status": None,
            "new_status": "active",
            "metadata": {
                "entity_code": AVPL_ENTITY_CODE,
                "entity_type": "avpl",
                "books_mode": "full",
                "base_currency": "INR",
                "profile_status": "incomplete",
            },
            "remarks": "Initial AVPL Accounting entity created.",
            "created_at": timestamp,
        })

    return {
        "created": created,
        "entity": serialize_accounting_entity(entity_document),
        "message": (
            "AVPL Accounting entity initialized successfully."
            if created
            else "The AVPL Accounting entity is already initialized."
        ),
    }


def list_accessible_entities(entity_ids):
    object_ids = []

    for value in entity_ids or []:
        object_id = _to_object_id(value)
        if object_id and object_id not in object_ids:
            object_ids.append(object_id)

    if not object_ids:
        return []

    rows = list(
        mongo.db.accounting_entities.find({
            "_id": {"$in": object_ids},
            "is_deleted": {"$ne": True},
            "status": "active",
            "accounting_enabled": {"$ne": False},
        }).sort([
            ("is_system_entity", DESCENDING),
            ("display_name", ASCENDING),
        ])
    )

    return [serialize_accounting_entity(row) for row in rows]
