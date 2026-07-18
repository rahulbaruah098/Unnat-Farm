import re
import uuid
from collections import Counter, defaultdict

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.utils.helpers import now_utc


AVPL_ENTITY_CODE = "AVPL"
SOURCE_COLLECTIONS = {
    "centre": "ufc_admin_master",
    "mitra": "ufc_mitra_master",
    "farmer": "farmer_master",
}
MAPPING_COLLECTION = "accounting_entity_mappings"
SYNC_RUN_COLLECTION = "accounting_entity_mapping_sync_runs"


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


def ensure_accounting_entity_mapping_indexes():
    _ensure_exact_index(
        mongo.db.accounting_entities,
        [("source_key", ASCENDING)],
        name="accounting_entity_source_key_unique",
        unique=True,
        partialFilterExpression={"source_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        mongo.db[MAPPING_COLLECTION],
        [("mapping_key", ASCENDING)],
        name="accounting_entity_mapping_key_unique",
        unique=True,
    )
    _ensure_exact_index(
        mongo.db[MAPPING_COLLECTION],
        [("mapping_type", ASCENDING), ("mapping_status", ASCENDING)],
        name="accounting_entity_mapping_type_status_idx",
    )
    _ensure_exact_index(
        mongo.db[MAPPING_COLLECTION],
        [("parent_accounting_entity_id", ASCENDING), ("mapping_type", ASCENDING)],
        name="accounting_entity_mapping_parent_type_idx",
    )
    _ensure_exact_index(
        mongo.db[MAPPING_COLLECTION],
        [("centre_uid", ASCENDING), ("mitra_uid", ASCENDING), ("mapping_type", ASCENDING)],
        name="accounting_entity_mapping_hierarchy_idx",
    )
    _ensure_exact_index(
        mongo.db[MAPPING_COLLECTION],
        [("accounting_entity_id", ASCENDING)],
        name="accounting_entity_mapping_entity_idx",
    )
    _ensure_exact_index(
        mongo.db[SYNC_RUN_COLLECTION],
        [("run_id", ASCENDING)],
        name="accounting_entity_mapping_run_id_unique",
        unique=True,
    )
    _ensure_exact_index(
        mongo.db[SYNC_RUN_COLLECTION],
        [("started_at", DESCENDING)],
        name="accounting_entity_mapping_started_idx",
    )


def _clean_text(value):
    return str(value or "").strip()


def _source_key(collection_name, source_id):
    return f"{collection_name}:{source_id}"


def _mapping_key(mapping_type, source_id):
    return f"{mapping_type}:{source_id}"


def _find_user(linked_user_id):
    object_id = _to_object_id(linked_user_id)
    if not object_id:
        return {}
    return mongo.db.users.find_one({"_id": object_id}) or {}


def _source_approval_status(master, user):
    return (
        _clean_text(master.get("approval_status"))
        or _clean_text(user.get("approval_status"))
        or "unknown"
    ).lower()


def _source_operational_status(master, user):
    if master.get("is_deleted") is True:
        return "deleted"
    if user and (user.get("active") is False or user.get("status") == "inactive"):
        return "inactive"

    approval_status = _source_approval_status(master, user)
    if approval_status in {"rejected", "inactive", "suspended"}:
        return approval_status
    if approval_status == "approved":
        return "approved"
    return approval_status or "unknown"


def _is_activation_eligible(master, user):
    return _source_operational_status(master, user) == "approved"


def _display_name(*values, fallback):
    for value in values:
        text = _clean_text(value)
        if text:
            return text
    return fallback


def _farmer_entity_code(master, user):
    user_ref = re.sub(r"[^A-Z0-9-]", "", _clean_text(user.get("user_ref_id")).upper())
    if user_ref:
        candidate = f"FRM-{user_ref}"
    else:
        candidate = f"FRM-{str(master['_id'])[-12:].upper()}"

    candidate = candidate[:60]
    conflict = mongo.db.accounting_entities.find_one(
        {"entity_code": candidate},
        {"source_key": 1},
    )
    expected_source_key = _source_key(SOURCE_COLLECTIONS["farmer"], master["_id"])
    if not conflict or conflict.get("source_key") == expected_source_key:
        return candidate

    return f"FRM-{str(master['_id']).upper()}"[:60]


def _changed(existing, updates):
    for key, value in updates.items():
        if existing.get(key) != value:
            return True
    return False


def _write_entity(existing, base_document, actor_user_id, run_id, counters):
    timestamp = now_utc()
    if not existing:
        document = {
            **base_document,
            "version": 1,
            "created_by": actor_user_id,
            "created_by_str": str(actor_user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_sync_run_id": run_id,
        }
        try:
            result = mongo.db.accounting_entities.insert_one(document)
            document["_id"] = result.inserted_id
            counters["entities_created"] += 1
            return document
        except DuplicateKeyError as exc:
            raise RuntimeError(
                f"Could not create unique Accounting entity {base_document.get('entity_code')}."
            ) from exc

    updates = {
        **base_document,
        "last_sync_run_id": run_id,
    }
    if _changed(existing, updates):
        updates["updated_at"] = timestamp
        updates["version"] = int(existing.get("version") or 1) + 1
        mongo.db.accounting_entities.update_one(
            {"_id": existing["_id"]},
            {"$set": updates},
        )
        existing.update(updates)
        counters["entities_updated"] += 1
    return existing


def _write_mapping(existing, base_document, actor_user_id, run_id, counters):
    timestamp = now_utc()
    if not existing:
        document = {
            **base_document,
            "version": 1,
            "created_by": actor_user_id,
            "created_by_str": str(actor_user_id),
            "created_at": timestamp,
            "updated_at": timestamp,
            "last_sync_run_id": run_id,
            "is_deleted": False,
        }
        try:
            result = mongo.db[MAPPING_COLLECTION].insert_one(document)
            document["_id"] = result.inserted_id
            counters["mappings_created"] += 1
            return document
        except DuplicateKeyError as exc:
            raise RuntimeError(
                f"Could not create unique hierarchy mapping {base_document.get('mapping_key')}."
            ) from exc

    updates = {
        **base_document,
        "last_sync_run_id": run_id,
        "is_deleted": False,
    }
    if _changed(existing, updates):
        updates["updated_at"] = timestamp
        updates["version"] = int(existing.get("version") or 1) + 1
        mongo.db[MAPPING_COLLECTION].update_one(
            {"_id": existing["_id"]},
            {"$set": updates},
        )
        existing.update(updates)
        counters["mappings_updated"] += 1
    return existing


def _validate_actor(actor_user_id):
    actor_object_id = _to_object_id(actor_user_id)
    if not actor_object_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_object_id},
        {"name": 1, "username": 1, "role": 1, "active": 1, "status": 1},
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")
    if actor.get("active", True) is False or actor.get("status") == "inactive":
        raise PermissionError("Inactive users cannot synchronize Accounting entities.")
    if actor.get("role") != "super_admin":
        raise PermissionError("Only Super Admin can synchronize future Accounting entity mappings.")
    return actor_object_id, actor


def _get_avpl_entity():
    entity = mongo.db.accounting_entities.find_one({
        "entity_code": AVPL_ENTITY_CODE,
        "is_deleted": {"$ne": True},
        "status": "active",
        "accounting_enabled": {"$ne": False},
    })
    if not entity:
        raise ValueError("Initialize and activate the AVPL Accounting entity first.")
    return entity


def _assert_future_entities_are_disabled():
    active_rows = list(mongo.db.accounting_entities.find({
        "entity_type": {"$in": ["centre", "farmer"]},
        "is_deleted": {"$ne": True},
        "$or": [
            {"status": "active"},
            {"accounting_enabled": True},
        ],
    }, {"entity_code": 1, "entity_type": 1}).limit(20))

    if active_rows:
        codes = ", ".join(row.get("entity_code") or str(row.get("_id")) for row in active_rows)
        raise RuntimeError(
            "One or more Centre/Farmer Accounting entities are already active "
            f"({codes}). Batch 8 will not silently disable them. Review them before synchronization."
        )


def synchronize_future_accounting_entity_mappings(actor_user_id):
    actor_object_id, actor = _validate_actor(actor_user_id)
    ensure_accounting_entity_mapping_indexes()
    avpl_entity = _get_avpl_entity()
    _assert_future_entities_are_disabled()

    run_id = str(uuid.uuid4())
    timestamp = now_utc()
    counters = Counter({
        "entities_created": 0,
        "entities_updated": 0,
        "mappings_created": 0,
        "mappings_updated": 0,
        "centres_processed": 0,
        "mitras_processed": 0,
        "farmers_processed": 0,
        "unresolved_centres": 0,
        "unresolved_mitras": 0,
        "unresolved_farmers": 0,
        "stale_mappings_marked": 0,
    })
    warnings = []

    run_document = {
        "run_id": run_id,
        "status": "running",
        "actor_user_id": actor_object_id,
        "actor_user_id_str": str(actor_object_id),
        "actor_name": actor.get("name") or actor.get("username") or "Super Admin",
        "started_at": timestamp,
        "completed_at": None,
        "counts": {},
        "warnings": [],
        "error": "",
        "recovery_required": False,
    }
    mongo.db[SYNC_RUN_COLLECTION].insert_one(run_document)

    current_mapping_keys = set()
    centre_entities_by_uid = {}
    mitra_mappings_by_key = {}

    try:
        centres = list(mongo.db.ufc_admin_master.find({"is_deleted": {"$ne": True}}).sort("centre_uid", ASCENDING))
        mitras = list(mongo.db.ufc_mitra_master.find({"is_deleted": {"$ne": True}}).sort("mitra_uid", ASCENDING))
        farmers = list(mongo.db.farmer_master.find({"is_deleted": {"$ne": True}}).sort("created_at", ASCENDING))

        for master in centres:
            counters["centres_processed"] += 1
            source_id = master["_id"]
            source_id_str = str(source_id)
            mapping_key = _mapping_key("centre", source_id)
            current_mapping_keys.add(mapping_key)
            user = _find_user(master.get("linked_user_id"))
            centre_uid = _clean_text(master.get("centre_uid") or user.get("centre_uid")).upper()

            if not centre_uid:
                counters["unresolved_centres"] += 1
                warnings.append(f"Centre master {source_id_str} has no centre_uid and was recorded as unresolved.")
                mapping_document = {
                    "mapping_key": mapping_key,
                    "mapping_type": "centre",
                    "mapping_status": "unresolved_identifier",
                    "source_collection": SOURCE_COLLECTIONS["centre"],
                    "source_master_id": source_id,
                    "source_master_id_str": source_id_str,
                    "source_present": True,
                    "source_operational_status": _source_operational_status(master, user),
                    "activation_eligible": False,
                    "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                    "display_name": _display_name(master.get("name_of_enterprise"), master.get("name_of_owner"), user.get("name"), fallback="Unresolved Centre"),
                    "centre_uid": "",
                    "mitra_uid": "",
                    "accounting_entity_id": None,
                    "accounting_entity_id_str": "",
                    "parent_accounting_entity_id": avpl_entity["_id"],
                    "parent_accounting_entity_id_str": str(avpl_entity["_id"]),
                    "accounting_enabled": False,
                }
                existing_mapping = mongo.db[MAPPING_COLLECTION].find_one({"mapping_key": mapping_key})
                _write_mapping(existing_mapping, mapping_document, actor_object_id, run_id, counters)
                continue

            source_key = _source_key(SOURCE_COLLECTIONS["centre"], source_id)
            existing_entity = mongo.db.accounting_entities.find_one({
                "$or": [
                    {"source_key": source_key},
                    {
                        "source_collection": SOURCE_COLLECTIONS["centre"],
                        "source_master_id_str": source_id_str,
                    },
                    {"entity_code": centre_uid, "entity_type": "centre"},
                ]
            })
            if existing_entity and existing_entity.get("entity_type") not in {None, "centre"}:
                counters["unresolved_centres"] += 1
                warnings.append(f"Entity code {centre_uid} is already used by another entity type.")
                continue

            display_name = _display_name(
                master.get("name_of_enterprise"),
                master.get("name_of_owner"),
                user.get("name"),
                fallback=f"UnnatFarm Centre {centre_uid}",
            )
            source_status = _source_operational_status(master, user)
            entity_document = {
                "entity_code": existing_entity.get("entity_code") if existing_entity else centre_uid,
                "entity_slug": f"centre-{centre_uid.lower()}",
                "entity_type": "centre",
                "display_name": display_name,
                "legal_name": _clean_text(master.get("name_of_enterprise")),
                "trade_name": _clean_text(master.get("name_of_enterprise")),
                "books_mode": "full",
                "base_currency": "INR",
                "country_code": "IN",
                "state": _clean_text(master.get("state") or user.get("state")),
                "district": _clean_text(master.get("district") or user.get("district")),
                "city": _clean_text(master.get("village") or user.get("village")),
                "pan": _clean_text(master.get("pan_number")),
                "gstin": _clean_text(master.get("gst_number")),
                "profile_status": "pre_mapped",
                "accounting_enabled": False,
                "activation_status": "pre_mapped_disabled",
                "activation_eligible": _is_activation_eligible(master, user),
                "is_system_entity": False,
                "is_deleted": False,
                "status": "inactive",
                "parent_entity_id": avpl_entity["_id"],
                "parent_entity_id_str": str(avpl_entity["_id"]),
                "parent_entity_code": AVPL_ENTITY_CODE,
                "hierarchy_path": [AVPL_ENTITY_CODE, centre_uid],
                "source_key": source_key,
                "source_collection": SOURCE_COLLECTIONS["centre"],
                "source_master_id": source_id,
                "source_master_id_str": source_id_str,
                "source_present": True,
                "source_operational_status": source_status,
                "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                "centre_uid": centre_uid,
                "mitra_uid": "",
            }
            entity = _write_entity(existing_entity, entity_document, actor_object_id, run_id, counters)
            centre_entities_by_uid[centre_uid] = entity

            mapping_document = {
                "mapping_key": mapping_key,
                "mapping_type": "centre",
                "mapping_status": "mapped_disabled",
                "source_collection": SOURCE_COLLECTIONS["centre"],
                "source_master_id": source_id,
                "source_master_id_str": source_id_str,
                "source_present": True,
                "source_operational_status": source_status,
                "activation_eligible": _is_activation_eligible(master, user),
                "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                "display_name": display_name,
                "centre_uid": centre_uid,
                "mitra_uid": "",
                "accounting_entity_id": entity["_id"],
                "accounting_entity_id_str": str(entity["_id"]),
                "parent_accounting_entity_id": avpl_entity["_id"],
                "parent_accounting_entity_id_str": str(avpl_entity["_id"]),
                "accounting_enabled": False,
            }
            existing_mapping = mongo.db[MAPPING_COLLECTION].find_one({"mapping_key": mapping_key})
            _write_mapping(existing_mapping, mapping_document, actor_object_id, run_id, counters)

        for master in mitras:
            counters["mitras_processed"] += 1
            source_id = master["_id"]
            source_id_str = str(source_id)
            mapping_key = _mapping_key("mitra", source_id)
            current_mapping_keys.add(mapping_key)
            user = _find_user(master.get("linked_user_id"))
            centre_uid = _clean_text(master.get("mapped_centre_uid") or master.get("centre_uid") or user.get("mapped_centre_uid")).upper()
            mitra_uid = _clean_text(master.get("mitra_uid") or user.get("mitra_uid")).upper()
            parent_entity = centre_entities_by_uid.get(centre_uid)
            mapping_status = "mapped_disabled"
            if not centre_uid or not parent_entity:
                mapping_status = "unresolved_parent"
                counters["unresolved_mitras"] += 1
                warnings.append(f"Mitra {mitra_uid or source_id_str} has no mapped Centre entity.")
            elif not mitra_uid:
                mapping_status = "unresolved_identifier"
                counters["unresolved_mitras"] += 1
                warnings.append(f"Mitra master {source_id_str} has no mitra_uid.")

            source_status = _source_operational_status(master, user)
            display_name = _display_name(master.get("name"), user.get("name"), fallback=f"UnnatFarm Mitra {mitra_uid or source_id_str}")
            mapping_document = {
                "mapping_key": mapping_key,
                "mapping_type": "mitra",
                "mapping_status": mapping_status,
                "source_collection": SOURCE_COLLECTIONS["mitra"],
                "source_master_id": source_id,
                "source_master_id_str": source_id_str,
                "source_present": True,
                "source_operational_status": source_status,
                "activation_eligible": bool(parent_entity and mitra_uid and _is_activation_eligible(master, user)),
                "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                "display_name": display_name,
                "centre_uid": centre_uid,
                "mitra_uid": mitra_uid,
                "accounting_entity_id": None,
                "accounting_entity_id_str": "",
                "parent_accounting_entity_id": parent_entity.get("_id") if parent_entity else None,
                "parent_accounting_entity_id_str": str(parent_entity.get("_id")) if parent_entity else "",
                "accounting_enabled": False,
            }
            existing_mapping = mongo.db[MAPPING_COLLECTION].find_one({"mapping_key": mapping_key})
            mapping = _write_mapping(existing_mapping, mapping_document, actor_object_id, run_id, counters)
            if centre_uid and mitra_uid:
                mitra_mappings_by_key[(centre_uid, mitra_uid)] = mapping

        for master in farmers:
            counters["farmers_processed"] += 1
            source_id = master["_id"]
            source_id_str = str(source_id)
            mapping_key = _mapping_key("farmer", source_id)
            current_mapping_keys.add(mapping_key)
            user = _find_user(master.get("linked_user_id"))
            centre_uid = _clean_text(master.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid")).upper()
            mitra_uid = _clean_text(master.get("mitra_uid") or user.get("mapped_mitra_uid") or user.get("mitra_uid")).upper()
            parent_entity = centre_entities_by_uid.get(centre_uid)
            mitra_mapping = mitra_mappings_by_key.get((centre_uid, mitra_uid))

            if not parent_entity:
                counters["unresolved_farmers"] += 1
                warnings.append(f"Farmer {master.get('name') or source_id_str} has no mapped Centre entity.")
                mapping_status = "unresolved_parent"
                entity = None
            else:
                mapping_status = "mapped_disabled" if mitra_mapping else "unresolved_mitra"
                if not mitra_mapping:
                    counters["unresolved_farmers"] += 1
                    warnings.append(f"Farmer {master.get('name') or source_id_str} has no mapped Mitra link.")

                source_key = _source_key(SOURCE_COLLECTIONS["farmer"], source_id)
                existing_entity = mongo.db.accounting_entities.find_one({
                    "$or": [
                        {"source_key": source_key},
                        {
                            "source_collection": SOURCE_COLLECTIONS["farmer"],
                            "source_master_id_str": source_id_str,
                        },
                    ]
                })
                display_name = _display_name(master.get("name"), user.get("name"), master.get("contact_no"), fallback=f"Farmer {source_id_str}")
                entity_code = existing_entity.get("entity_code") if existing_entity else _farmer_entity_code(master, user)
                source_status = _source_operational_status(master, user)
                hierarchy_path = [AVPL_ENTITY_CODE, centre_uid]
                if mitra_uid:
                    hierarchy_path.append(mitra_uid)
                hierarchy_path.append(entity_code)
                entity_document = {
                    "entity_code": entity_code,
                    "entity_slug": f"farmer-{str(source_id).lower()}",
                    "entity_type": "farmer",
                    "display_name": display_name,
                    "legal_name": display_name,
                    "trade_name": "",
                    "books_mode": "simplified",
                    "base_currency": "INR",
                    "country_code": "IN",
                    "state": _clean_text(master.get("state") or user.get("state")),
                    "district": _clean_text(master.get("district") or user.get("district")),
                    "city": _clean_text(master.get("village") or user.get("village")),
                    "profile_status": "pre_mapped",
                    "accounting_enabled": False,
                    "activation_status": "pre_mapped_disabled",
                    "activation_eligible": bool(mitra_mapping and _is_activation_eligible(master, user)),
                    "is_system_entity": False,
                    "is_deleted": False,
                    "status": "inactive",
                    "parent_entity_id": parent_entity["_id"],
                    "parent_entity_id_str": str(parent_entity["_id"]),
                    "parent_entity_code": centre_uid,
                    "hierarchy_path": hierarchy_path,
                    "source_key": source_key,
                    "source_collection": SOURCE_COLLECTIONS["farmer"],
                    "source_master_id": source_id,
                    "source_master_id_str": source_id_str,
                    "source_present": True,
                    "source_operational_status": source_status,
                    "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                    "centre_uid": centre_uid,
                    "mitra_uid": mitra_uid,
                    "mitra_mapping_id": mitra_mapping.get("_id") if mitra_mapping else None,
                    "mitra_mapping_id_str": str(mitra_mapping.get("_id")) if mitra_mapping else "",
                }
                entity = _write_entity(existing_entity, entity_document, actor_object_id, run_id, counters)

            source_status = _source_operational_status(master, user)
            display_name = _display_name(master.get("name"), user.get("name"), master.get("contact_no"), fallback=f"Farmer {source_id_str}")
            mapping_document = {
                "mapping_key": mapping_key,
                "mapping_type": "farmer",
                "mapping_status": mapping_status,
                "source_collection": SOURCE_COLLECTIONS["farmer"],
                "source_master_id": source_id,
                "source_master_id_str": source_id_str,
                "source_present": True,
                "source_operational_status": source_status,
                "activation_eligible": bool(entity and mitra_mapping and _is_activation_eligible(master, user)),
                "linked_user_id_str": _clean_text(master.get("linked_user_id")),
                "display_name": display_name,
                "centre_uid": centre_uid,
                "mitra_uid": mitra_uid,
                "accounting_entity_id": entity.get("_id") if entity else None,
                "accounting_entity_id_str": str(entity.get("_id")) if entity else "",
                "parent_accounting_entity_id": parent_entity.get("_id") if parent_entity else None,
                "parent_accounting_entity_id_str": str(parent_entity.get("_id")) if parent_entity else "",
                "mitra_mapping_id": mitra_mapping.get("_id") if mitra_mapping else None,
                "mitra_mapping_id_str": str(mitra_mapping.get("_id")) if mitra_mapping else "",
                "accounting_enabled": False,
            }
            existing_mapping = mongo.db[MAPPING_COLLECTION].find_one({"mapping_key": mapping_key})
            _write_mapping(existing_mapping, mapping_document, actor_object_id, run_id, counters)

        stale_query = {
            "mapping_type": {"$in": ["centre", "mitra", "farmer"]},
            "mapping_key": {"$nin": list(current_mapping_keys)},
            "source_present": {"$ne": False},
            "is_deleted": {"$ne": True},
        }
        stale_rows = list(mongo.db[MAPPING_COLLECTION].find(stale_query))
        for stale in stale_rows:
            mongo.db[MAPPING_COLLECTION].update_one(
                {"_id": stale["_id"]},
                {"$set": {
                    "source_present": False,
                    "source_operational_status": "missing",
                    "mapping_status": "source_missing",
                    "activation_eligible": False,
                    "accounting_enabled": False,
                    "last_sync_run_id": run_id,
                    "updated_at": now_utc(),
                    "version": int(stale.get("version") or 1) + 1,
                }},
            )
            counters["stale_mappings_marked"] += 1
            entity_id = stale.get("accounting_entity_id")
            if entity_id:
                mongo.db.accounting_entities.update_one(
                    {
                        "_id": entity_id,
                        "entity_type": {"$in": ["centre", "farmer"]},
                        "accounting_enabled": {"$ne": True},
                    },
                    {"$set": {
                        "source_present": False,
                        "source_operational_status": "missing",
                        "activation_eligible": False,
                        "last_sync_run_id": run_id,
                        "updated_at": now_utc(),
                    }},
                )

        completed_at = now_utc()
        count_dict = dict(counters)
        mongo.db[SYNC_RUN_COLLECTION].update_one(
            {"run_id": run_id},
            {"$set": {
                "status": "completed",
                "completed_at": completed_at,
                "counts": count_dict,
                "warnings": warnings[:100],
                "recovery_required": False,
            }},
        )
        mongo.db.accounting_audit_logs.insert_one({
            "module": "accounting",
            "action": "synchronize_future_accounting_entity_mappings",
            "accounting_entity_id": avpl_entity["_id"],
            "accounting_entity_id_str": str(avpl_entity["_id"]),
            "entity_type": "accounting_entity_hierarchy",
            "entity_id": avpl_entity["_id"],
            "entity_id_str": str(avpl_entity["_id"]),
            "actor_user_id": actor_object_id,
            "actor_user_id_str": str(actor_object_id),
            "previous_status": None,
            "new_status": "synchronized",
            "metadata": {
                "run_id": run_id,
                "counts": count_dict,
                "warning_count": len(warnings),
                "avpl_remains_only_active_entity": True,
            },
            "remarks": "Future Centre, Mitra and Farmer Accounting hierarchy synchronized in disabled mode.",
            "created_at": completed_at,
        })

        return {
            "message": (
                "Future Accounting hierarchy synchronized successfully. "
                f"Processed {counters['centres_processed']} Centre(s), "
                f"{counters['mitras_processed']} Mitra(s) and "
                f"{counters['farmers_processed']} Farmer(s)."
            ),
            "run_id": run_id,
            "counts": count_dict,
            "warnings": warnings,
        }
    except Exception as exc:
        mongo.db[SYNC_RUN_COLLECTION].update_one(
            {"run_id": run_id},
            {"$set": {
                "status": "failed",
                "completed_at": now_utc(),
                "counts": dict(counters),
                "warnings": warnings[:100],
                "error": str(exc),
                "recovery_required": True,
            }},
        )
        raise


def _serialize_sync_run(run):
    if not run:
        return None
    return {
        "run_id": run.get("run_id") or "",
        "status": run.get("status") or "",
        "actor_name": run.get("actor_name") or "",
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "counts": run.get("counts") or {},
        "warnings": run.get("warnings") or [],
        "error": run.get("error") or "",
        "recovery_required": run.get("recovery_required") is True,
    }


def get_future_accounting_entity_mapping_overview():
    ensure_accounting_entity_mapping_indexes()

    source_counts = {
        "centres": mongo.db.ufc_admin_master.count_documents({"is_deleted": {"$ne": True}}),
        "mitras": mongo.db.ufc_mitra_master.count_documents({"is_deleted": {"$ne": True}}),
        "farmers": mongo.db.farmer_master.count_documents({"is_deleted": {"$ne": True}}),
    }

    mappings = list(mongo.db[MAPPING_COLLECTION].find({
        "mapping_type": {"$in": ["centre", "mitra", "farmer"]},
        "is_deleted": {"$ne": True},
    }))
    current_mappings = [row for row in mappings if row.get("source_present", True)]
    mapping_type_counts = Counter(row.get("mapping_type") for row in current_mappings)
    status_counts = Counter(row.get("mapping_status") for row in current_mappings)

    centre_entities = list(mongo.db.accounting_entities.find({
        "entity_type": "centre",
        "is_deleted": {"$ne": True},
    }, {"centre_uid": 1, "display_name": 1, "accounting_enabled": 1, "status": 1}))
    farmer_entities_count = mongo.db.accounting_entities.count_documents({
        "entity_type": "farmer",
        "is_deleted": {"$ne": True},
    })
    active_non_avpl_count = mongo.db.accounting_entities.count_documents({
        "entity_type": {"$in": ["centre", "farmer"]},
        "is_deleted": {"$ne": True},
        "$or": [
            {"status": "active"},
            {"accounting_enabled": True},
        ],
    })

    centre_entity_by_uid = {
        _clean_text(row.get("centre_uid")).upper(): row
        for row in centre_entities
        if _clean_text(row.get("centre_uid"))
    }
    mitra_counts = Counter()
    farmer_counts = Counter()
    unresolved_by_centre = Counter()

    for row in current_mappings:
        centre_uid = _clean_text(row.get("centre_uid")).upper() or "UNRESOLVED"
        if row.get("mapping_type") == "mitra":
            mitra_counts[centre_uid] += 1
        elif row.get("mapping_type") == "farmer":
            farmer_counts[centre_uid] += 1
        if str(row.get("mapping_status") or "").startswith("unresolved"):
            unresolved_by_centre[centre_uid] += 1

    centre_rows = []
    for mapping in sorted(
        [row for row in current_mappings if row.get("mapping_type") == "centre"],
        key=lambda row: (_clean_text(row.get("centre_uid")), _clean_text(row.get("display_name"))),
    ):
        centre_uid = _clean_text(mapping.get("centre_uid")).upper() or "UNRESOLVED"
        entity = centre_entity_by_uid.get(centre_uid)
        centre_rows.append({
            "centre_uid": centre_uid,
            "display_name": mapping.get("display_name") or centre_uid,
            "mapping_status": mapping.get("mapping_status") or "",
            "source_operational_status": mapping.get("source_operational_status") or "unknown",
            "activation_eligible": mapping.get("activation_eligible") is True,
            "entity_created": bool(entity),
            "accounting_enabled": bool(entity and entity.get("accounting_enabled") is True),
            "entity_status": entity.get("status") if entity else "missing",
            "mitra_count": mitra_counts.get(centre_uid, 0),
            "farmer_count": farmer_counts.get(centre_uid, 0),
            "unresolved_count": unresolved_by_centre.get(centre_uid, 0),
        })

    unresolved_count = sum(
        count for status, count in status_counts.items()
        if str(status).startswith("unresolved")
    )
    expected_mapping_count = sum(source_counts.values())
    current_mapping_count = sum(mapping_type_counts.values())
    latest_run = mongo.db[SYNC_RUN_COLLECTION].find_one(sort=[("started_at", DESCENDING)])

    is_complete = (
        current_mapping_count == expected_mapping_count
        and mapping_type_counts.get("centre", 0) == source_counts["centres"]
        and mapping_type_counts.get("mitra", 0) == source_counts["mitras"]
        and mapping_type_counts.get("farmer", 0) == source_counts["farmers"]
        and len(centre_entities) == source_counts["centres"]
        and farmer_entities_count >= source_counts["farmers"] - status_counts.get("unresolved_parent", 0)
        and unresolved_count == 0
        and active_non_avpl_count == 0
    )

    return {
        "source_counts": source_counts,
        "mapping_counts": {
            "centres": mapping_type_counts.get("centre", 0),
            "mitras": mapping_type_counts.get("mitra", 0),
            "farmers": mapping_type_counts.get("farmer", 0),
            "total": current_mapping_count,
            "expected_total": expected_mapping_count,
        },
        "entity_counts": {
            "centres": len(centre_entities),
            "farmers": farmer_entities_count,
            "active_non_avpl": active_non_avpl_count,
        },
        "status_counts": dict(status_counts),
        "unresolved_count": unresolved_count,
        "stale_count": sum(1 for row in mappings if row.get("source_present") is False),
        "centre_rows": centre_rows,
        "latest_run": _serialize_sync_run(latest_run),
        "is_complete": is_complete,
        "avpl_only_active": active_non_avpl_count == 0,
    }
