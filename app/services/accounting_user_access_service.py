from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    CURRENT_PERMISSION_SCHEMA_VERSION,
    MANDATORY_ENABLED_PERMISSIONS,
    ROLE_DEFAULT_PERMISSIONS,
    get_accounting_access,
    get_permission_catalog,
    get_permission_schema_additions,
    get_role_assignable_permissions,
)
from app.utils.helpers import now_utc


MANAGEABLE_ACCOUNTING_ROLES = frozenset({"avpl_admin", "accounts"})
AVPL_ADMIN_MANAGEABLE_ROLES = frozenset({"accounts"})


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


def _normalize_existing_access_identifiers():
    """Safely normalize legacy access documents before creating uniqueness.

    A semantic duplicate such as one document storing an ObjectId and another
    storing the same value as a string is never auto-merged. Setup stops and
    asks for manual review instead of choosing which permissions to keep.
    """
    collection = mongo.db.accounting_user_access
    rows = list(collection.find({}, {"user_id": 1, "user_id_str": 1}))
    canonical_owner = {}

    for row in rows:
        object_id = _to_object_id(row.get("user_id") or row.get("user_id_str"))
        if not object_id:
            raise RuntimeError(
                "An Accounting user-access record contains an invalid user identifier."
            )

        canonical = str(object_id)
        existing_owner = canonical_owner.get(canonical)

        if existing_owner and existing_owner != row.get("_id"):
            raise RuntimeError(
                "Duplicate Accounting user-access records were found for the same user. "
                "No records were merged automatically."
            )

        canonical_owner[canonical] = row.get("_id")

    for row in rows:
        object_id = _to_object_id(row.get("user_id") or row.get("user_id_str"))
        canonical = str(object_id)
        updates = {}

        if row.get("user_id") != object_id:
            updates["user_id"] = object_id
        if row.get("user_id_str") != canonical:
            updates["user_id_str"] = canonical

        if updates:
            collection.update_one(
                {"_id": row["_id"]},
                {"$set": updates},
            )


def ensure_accounting_user_access_indexes():
    _normalize_existing_access_identifiers()

    _ensure_exact_index(
        mongo.db.accounting_user_access,
        [("user_id_str", ASCENDING)],
        name="accounting_user_access_user_unique",
        unique=True,
        partialFilterExpression={"user_id_str": {"$type": "string"}},
    )
    _ensure_exact_index(
        mongo.db.accounting_user_access,
        [("user_role", ASCENDING), ("accounting_enabled", ASCENDING)],
        name="accounting_user_access_role_enabled_idx",
    )
    _ensure_exact_index(
        mongo.db.accounting_user_access,
        [("entity_ids", ASCENDING), ("accounting_enabled", ASCENDING)],
        name="accounting_user_access_entity_enabled_idx",
    )
    _ensure_exact_index(
        mongo.db.accounting_user_access,
        [("updated_at", DESCENDING)],
        name="accounting_user_access_updated_idx",
    )


def _active_user_query(extra=None):
    query = {
        "active": {"$ne": False},
        "is_active": {"$ne": False},
        "status": {"$ne": "inactive"},
    }
    if extra:
        query.update(extra)
    return query


def _get_actor(actor_user_id):
    actor_object_id = _to_object_id(actor_user_id)
    if not actor_object_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_object_id},
        {
            "role": 1,
            "name": 1,
            "full_name": 1,
            "username": 1,
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
        raise PermissionError("Inactive users cannot manage Accounting access.")

    actor["role"] = str(actor.get("role") or "").strip().lower()
    actor["display_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor["role"].replace("_", " ").title()
    )
    return actor


def _get_target_user(target_user_id):
    target_object_id = _to_object_id(target_user_id)
    if not target_object_id:
        raise ValueError("Invalid target user.")

    target = mongo.db.users.find_one(
        {"_id": target_object_id},
        {
            "role": 1,
            "name": 1,
            "full_name": 1,
            "username": 1,
            "email": 1,
            "phone": 1,
            "contact_no": 1,
            "active": 1,
            "is_active": 1,
            "status": 1,
        },
    )

    if not target:
        raise ValueError("The selected user was not found.")

    if (
        target.get("active", True) is False
        or target.get("is_active", True) is False
        or target.get("status") == "inactive"
    ):
        raise ValueError("Accounting access cannot be assigned to an inactive user.")

    target["role"] = str(target.get("role") or "").strip().lower()
    target["display_name"] = (
        target.get("name")
        or target.get("full_name")
        or target.get("username")
        or target.get("email")
        or target.get("phone")
        or "Accounting User"
    )
    return target


def _assert_actor_can_manage_target(actor, target):
    actor_role = actor.get("role")
    target_role = target.get("role")

    if target_role not in MANAGEABLE_ACCOUNTING_ROLES:
        raise PermissionError(
            "Only AVPL Admin and Accounts users can receive editable Accounting mappings."
        )

    if actor_role == "super_admin":
        return

    if actor_role == "avpl_admin" and target_role in AVPL_ADMIN_MANAGEABLE_ROLES:
        return

    raise PermissionError("You cannot manage Accounting access for this user.")


def _get_allowed_entity_ids(actor):
    active_entities = list(
        mongo.db.accounting_entities.find(
            {
                "is_deleted": {"$ne": True},
                "status": "active",
                "accounting_enabled": {"$ne": False},
            },
            {"_id": 1},
        )
    )
    active_ids = {str(row["_id"]) for row in active_entities}

    if actor.get("role") == "super_admin":
        return active_ids

    actor_access = get_accounting_access(
        actor.get("_id"),
        session_role=actor.get("role"),
    )
    return active_ids.intersection(
        {str(value) for value in actor_access.get("entity_ids") or []}
    )


def _validate_entity_ids(actor, raw_entity_ids, accounting_enabled):
    requested_ids = []

    for value in raw_entity_ids or []:
        object_id = _to_object_id(value)
        if object_id and object_id not in requested_ids:
            requested_ids.append(object_id)

    if accounting_enabled and not requested_ids:
        raise ValueError("Select at least one Accounting entity for an enabled user.")

    allowed_ids = _get_allowed_entity_ids(actor)

    for object_id in requested_ids:
        if str(object_id) not in allowed_ids:
            raise PermissionError(
                "You cannot assign one or more of the selected Accounting entities."
            )

    return requested_ids


def _validate_permissions(target_role, raw_permissions, accounting_enabled):
    allowed_permissions = get_role_assignable_permissions(target_role)
    requested_permissions = {
        str(value).strip()
        for value in raw_permissions or []
        if str(value).strip()
    }

    unknown_permissions = requested_permissions.difference(allowed_permissions)
    if unknown_permissions:
        raise PermissionError(
            "One or more selected permissions are not allowed for this role."
        )

    if accounting_enabled:
        requested_permissions.update(MANDATORY_ENABLED_PERMISSIONS)

    return sorted(requested_permissions)


def _find_access_document(user_object_id):
    return mongo.db.accounting_user_access.find_one({
        "$or": [
            {"user_id": user_object_id},
            {"user_id": str(user_object_id)},
            {"user_id_str": str(user_object_id)},
        ]
    })


def _actor_snapshot(actor):
    return {
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("role"),
        "actor_name": actor.get("display_name"),
    }


def _record_access_audit(access_document, actor, action, previous_snapshot=None):
    timestamp = now_utc()
    try:
        mongo.db.accounting_audit_logs.insert_one({
            "module": "accounting",
            "action": action,
            "accounting_entity_id": (
                access_document.get("entity_ids") or [None]
            )[0],
            "accounting_entity_id_str": (
                access_document.get("entity_id_strs") or [""]
            )[0],
            "entity_type": "accounting_user_access",
            "entity_id": access_document.get("_id"),
            "entity_id_str": str(access_document.get("_id") or ""),
            **_actor_snapshot(actor),
            "previous_status": (
                previous_snapshot or {}
            ).get("status"),
            "new_status": access_document.get("status"),
            "metadata": {
                "target_user_id": access_document.get("user_id_str"),
                "target_role": access_document.get("user_role"),
                "accounting_enabled": access_document.get("accounting_enabled"),
                "entity_id_strs": access_document.get("entity_id_strs") or [],
                "permissions": access_document.get("permissions") or [],
                "permission_mode": access_document.get("permission_mode"),
                "permission_schema_version": access_document.get("permission_schema_version"),
                "version": access_document.get("version"),
                "previous": previous_snapshot or {},
            },
            "remarks": "Permanent Accounting user-to-entity access updated.",
            "created_at": timestamp,
        })
    except Exception:
        # The successful access update is not repeated merely because the audit
        # write failed. A recovery marker lets a later maintenance process repair
        # the audit trail deterministically.
        try:
            mongo.db.accounting_user_access.update_one(
                {"_id": access_document.get("_id")},
                {
                    "$set": {
                        "audit_sync_required": True,
                        "audit_sync_action": action,
                        "audit_sync_marked_at": timestamp,
                    }
                },
            )
        except Exception:
            pass


def _build_access_history_event(action, actor, note=""):
    return {
        "action": action,
        **_actor_snapshot(actor),
        "note": str(note or "").strip(),
        "at": now_utc(),
    }


def initialize_default_user_access_mappings(actor_user_id):
    """Create missing mappings and safely add newly introduced permissions.

    Existing enabled/disabled status, entity assignments and previously selected
    permissions are preserved. A schema upgrade only appends permissions that did
    not exist when the older mapping was created.
    """
    actor = _get_actor(actor_user_id)
    if actor.get("role") != "super_admin":
        raise PermissionError("Only Super Admin can initialize permanent access mappings.")

    ensure_accounting_user_access_indexes()

    avpl_entity = mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "is_deleted": {"$ne": True},
        "status": "active",
        "accounting_enabled": {"$ne": False},
    })
    if not avpl_entity:
        raise ValueError("Initialize the AVPL Accounting entity first.")

    users = list(
        mongo.db.users.find(
            _active_user_query({"role": {"$in": ["avpl_admin", "accounts"]}}),
            {
                "role": 1,
                "name": 1,
                "full_name": 1,
                "username": 1,
                "email": 1,
            },
        ).sort([("role", ASCENDING), ("name", ASCENDING), ("username", ASCENDING)])
    )

    created_count = 0
    existing_count = 0
    upgraded_count = 0
    timestamp = now_utc()

    for user in users:
        role = str(user.get("role") or "").strip().lower()
        existing = _find_access_document(user["_id"])

        if existing:
            existing_count += 1
            current_schema = int(existing.get("permission_schema_version") or 1)

            if current_schema < CURRENT_PERMISSION_SCHEMA_VERSION:
                current_permissions = set(existing.get("permissions") or [])
                current_permissions.update(
                    get_permission_schema_additions(
                        role,
                        current_schema,
                        CURRENT_PERMISSION_SCHEMA_VERSION,
                    )
                )
                current_version = int(existing.get("version") or 1)
                history_event = _build_access_history_event(
                    "permission_schema_upgraded",
                    actor,
                    (
                        f"Applied approved Accounting permission schema "
                        f"{CURRENT_PERMISSION_SCHEMA_VERSION}."
                    ),
                )
                result = mongo.db.accounting_user_access.update_one(
                    {
                        "_id": existing["_id"],
                        "version": current_version,
                    },
                    {
                        "$set": {
                            "permissions": sorted(current_permissions),
                            "permission_schema_version": CURRENT_PERMISSION_SCHEMA_VERSION,
                            "updated_by": actor["_id"],
                            "updated_by_str": str(actor["_id"]),
                            "updated_at": timestamp,
                            "version": current_version + 1,
                        },
                        "$push": {"access_history": history_event},
                    },
                )

                if result.modified_count != 1:
                    raise RuntimeError(
                        "An Accounting access mapping changed during permission synchronization. "
                        "Run the synchronization again."
                    )

                upgraded_count += 1
                upgraded = mongo.db.accounting_user_access.find_one(
                    {"_id": existing["_id"]}
                )
                _record_access_audit(
                    upgraded,
                    actor,
                    "upgrade_accounting_permission_schema",
                    previous_snapshot={
                        "permissions": existing.get("permissions") or [],
                        "permission_schema_version": current_schema,
                        "version": current_version,
                    },
                )
            continue

        permissions = sorted(ROLE_DEFAULT_PERMISSIONS.get(role, set()))
        history_event = _build_access_history_event(
            "default_mapping_initialized",
            actor,
            "Created from the approved role defaults and current permission schema.",
        )
        document = {
            "user_id": user["_id"],
            "user_id_str": str(user["_id"]),
            "user_role": role,
            "accounting_enabled": True,
            "status": "active",
            "entity_ids": [avpl_entity["_id"]],
            "entity_id_strs": [str(avpl_entity["_id"])],
            "permission_mode": "replace",
            "permissions": permissions,
            "denied_permissions": [],
            "permission_schema_version": CURRENT_PERMISSION_SCHEMA_VERSION,
            "version": 1,
            "created_by": actor["_id"],
            "created_by_str": str(actor["_id"]),
            "created_at": timestamp,
            "updated_by": actor["_id"],
            "updated_by_str": str(actor["_id"]),
            "updated_at": timestamp,
            "access_history": [history_event],
            "audit_sync_required": False,
        }

        try:
            result = mongo.db.accounting_user_access.insert_one(document)
            document["_id"] = result.inserted_id
            created_count += 1
            _record_access_audit(
                document,
                actor,
                "initialize_accounting_user_access",
            )
        except DuplicateKeyError:
            existing_count += 1

    return {
        "created_count": created_count,
        "existing_count": existing_count,
        "upgraded_count": upgraded_count,
        "eligible_count": len(users),
        "message": (
            f"Created {created_count} permanent mapping(s), upgraded "
            f"{upgraded_count} mapping(s), and reviewed {existing_count} existing mapping(s)."
        ),
    }


def update_user_access_mapping(
    actor_user_id,
    target_user_id,
    accounting_enabled,
    entity_ids,
    permissions,
    expected_version,
    change_note="",
):
    actor = _get_actor(actor_user_id)
    target = _get_target_user(target_user_id)
    _assert_actor_can_manage_target(actor, target)
    ensure_accounting_user_access_indexes()

    enabled = accounting_enabled is True
    validated_entity_ids = _validate_entity_ids(actor, entity_ids, enabled)
    validated_permissions = _validate_permissions(
        target.get("role"),
        permissions,
        enabled,
    )

    try:
        expected_version_value = int(expected_version or 0)
    except (TypeError, ValueError):
        raise ValueError("The access form version is invalid. Refresh and try again.")

    existing = _find_access_document(target["_id"])
    previous_snapshot = None
    timestamp = now_utc()

    if existing:
        current_version = int(existing.get("version") or 1)
        if expected_version_value != current_version:
            raise RuntimeError(
                "This Accounting access mapping changed in another session. Refresh and try again."
            )

        previous_snapshot = {
            "status": existing.get("status"),
            "accounting_enabled": existing.get("accounting_enabled"),
            "entity_id_strs": existing.get("entity_id_strs") or [],
            "permissions": existing.get("permissions") or [],
            "permission_schema_version": int(existing.get("permission_schema_version") or 1),
            "version": current_version,
        }
        next_version = current_version + 1
        action = "update_accounting_user_access"
    else:
        if expected_version_value not in {0, 1}:
            raise RuntimeError(
                "This Accounting access mapping changed in another session. Refresh and try again."
            )
        next_version = 1
        action = "create_accounting_user_access"

    status = "active" if enabled else "disabled"
    entity_id_strs = [str(value) for value in validated_entity_ids]
    history_event = _build_access_history_event(
        "access_enabled" if enabled else "access_disabled",
        actor,
        change_note,
    )

    update_fields = {
        "user_id": target["_id"],
        "user_id_str": str(target["_id"]),
        "user_role": target.get("role"),
        "accounting_enabled": enabled,
        "status": status,
        "entity_ids": validated_entity_ids,
        "entity_id_strs": entity_id_strs,
        "permission_mode": "replace",
        "permissions": validated_permissions,
        "denied_permissions": [],
        "permission_schema_version": CURRENT_PERMISSION_SCHEMA_VERSION,
        "version": next_version,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_at": timestamp,
        "audit_sync_required": False,
    }

    if existing:
        result = mongo.db.accounting_user_access.update_one(
            {"_id": existing["_id"], "version": current_version},
            {
                "$set": update_fields,
                "$push": {"access_history": history_event},
            },
        )
        if result.modified_count != 1:
            raise RuntimeError(
                "This Accounting access mapping changed in another session. Refresh and try again."
            )
        access_document = mongo.db.accounting_user_access.find_one(
            {"_id": existing["_id"]}
        )
    else:
        document = {
            **update_fields,
            "created_by": actor["_id"],
            "created_by_str": str(actor["_id"]),
            "created_at": timestamp,
            "access_history": [history_event],
        }
        try:
            result = mongo.db.accounting_user_access.insert_one(document)
            document["_id"] = result.inserted_id
            access_document = document
        except DuplicateKeyError as exc:
            raise RuntimeError(
                "This user received an Accounting mapping in another session. Refresh and try again."
            ) from exc

    _record_access_audit(
        access_document,
        actor,
        action,
        previous_snapshot=previous_snapshot,
    )

    return {
        "mapping": serialize_user_access_mapping(target, access_document),
        "message": (
            f"Accounting access updated for {target.get('display_name')}."
        ),
    }


def serialize_user_access_mapping(user, access_document=None, can_manage=False):
    role = str(user.get("role") or "").strip().lower()
    access_document = access_document or {}
    explicit = bool(access_document)
    permission_mode = str(
        access_document.get("permission_mode") or "inherit_role"
    ).strip().lower()

    if explicit and permission_mode == "replace":
        permissions = sorted(set(access_document.get("permissions") or []))
        entity_ids = [str(value) for value in access_document.get("entity_ids") or []]
        enabled = access_document.get("accounting_enabled", True) is not False
        source = "Permanent mapping"
    else:
        resolved = get_accounting_access(user.get("_id"), session_role=role)
        permissions = resolved.get("permissions") or []
        entity_ids = resolved.get("entity_ids") or []
        enabled = resolved.get("enabled") is True
        source = "Migration fallback" if not explicit else "Legacy override"

    return {
        "user_id": str(user.get("_id") or ""),
        "display_name": (
            user.get("name")
            or user.get("full_name")
            or user.get("username")
            or user.get("email")
            or user.get("phone")
            or "Accounting User"
        ),
        "username": user.get("username") or "",
        "email": user.get("email") or "",
        "phone": user.get("phone") or user.get("contact_no") or "",
        "role": role,
        "role_display": role.replace("_", " ").title(),
        "accounting_enabled": enabled,
        "status": "active" if enabled else "disabled",
        "entity_ids": entity_ids,
        "permissions": permissions,
        "permission_mode": permission_mode,
        "source": source,
        "is_explicit": explicit and permission_mode == "replace",
        "version": int(access_document.get("version") or 0),
        "permission_schema_version": int(access_document.get("permission_schema_version") or 1),
        "permission_schema_current": (
            int(access_document.get("permission_schema_version") or 1)
            >= CURRENT_PERMISSION_SCHEMA_VERSION
        ),
        "updated_at": access_document.get("updated_at"),
        "updated_by_str": access_document.get("updated_by_str") or "",
        "access_history": access_document.get("access_history") or [],
        "can_manage": can_manage,
    }


def list_user_access_mappings(actor_user_id):
    actor = _get_actor(actor_user_id)
    actor_role = actor.get("role")

    if actor_role == "super_admin":
        visible_roles = ["avpl_admin", "accounts"]
    elif actor_role == "avpl_admin":
        visible_roles = ["accounts"]
    else:
        raise PermissionError("You cannot view Accounting user-access mappings.")

    ensure_accounting_user_access_indexes()

    users = list(
        mongo.db.users.find(
            _active_user_query({"role": {"$in": visible_roles}}),
            {
                "role": 1,
                "name": 1,
                "full_name": 1,
                "username": 1,
                "email": 1,
                "phone": 1,
                "contact_no": 1,
            },
        ).sort([("role", ASCENDING), ("name", ASCENDING), ("username", ASCENDING)])
    )

    user_ids = [str(user["_id"]) for user in users]
    access_rows = list(
        mongo.db.accounting_user_access.find({"user_id_str": {"$in": user_ids}})
    ) if user_ids else []
    access_by_user = {
        str(row.get("user_id_str") or row.get("user_id") or ""): row
        for row in access_rows
    }

    rows = []
    for user in users:
        can_manage = (
            actor_role == "super_admin"
            or (
                actor_role == "avpl_admin"
                and user.get("role") in AVPL_ADMIN_MANAGEABLE_ROLES
            )
        )
        rows.append(
            serialize_user_access_mapping(
                user,
                access_by_user.get(str(user["_id"])),
                can_manage=can_manage,
            )
        )

    explicit_count = sum(1 for row in rows if row.get("is_explicit"))
    enabled_count = sum(1 for row in rows if row.get("accounting_enabled"))
    schema_outdated_count = sum(
        1
        for row in rows
        if row.get("is_explicit") and not row.get("permission_schema_current")
    )

    return {
        "rows": rows,
        "eligible_count": len(rows),
        "explicit_count": explicit_count,
        "fallback_count": len(rows) - explicit_count,
        "enabled_count": enabled_count,
        "schema_outdated_count": schema_outdated_count,
        "permission_schema_version": CURRENT_PERMISSION_SCHEMA_VERSION,
        "permission_catalog_by_role": {
            role: get_permission_catalog(role)
            for role in visible_roles
        },
    }
