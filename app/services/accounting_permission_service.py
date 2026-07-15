from bson import ObjectId

from app.extensions import mongo


ACCOUNTING_ROLES = frozenset({
    "super_admin",
    "avpl_admin",
    "accounts",
})

# Bootstrap permissions let the isolated Accounting shell work before the
# accounting_user_access management screen is introduced in a later batch.
ROLE_DEFAULT_PERMISSIONS = {
    "super_admin": {"*"},
    "avpl_admin": {
        "accounting.access",
        "accounting.dashboard.view",
        "accounting.entity.view",
    },
    "accounts": {
        "accounting.access",
        "accounting.dashboard.view",
        "accounting.entity.view",
    },
}


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _clean_permissions(values):
    if not isinstance(values, (list, tuple, set)):
        return set()

    return {
        str(value).strip()
        for value in values
        if str(value).strip()
    }


def _default_entity_ids():
    entity = mongo.db.accounting_entities.find_one(
        {
            "entity_code": "AVPL",
            "is_deleted": {"$ne": True},
            "status": "active",
            "accounting_enabled": {"$ne": False},
        },
        {"_id": 1},
    )

    return [str(entity["_id"])] if entity else []


def get_accounting_access(user_id, session_role=None):
    """Resolve Accounting access from the verified user plus optional overrides.

    The service re-reads the user on every protected request so deactivation and
    role changes take effect without requiring a new login.
    """
    user_object_id = _to_object_id(user_id)

    denied = {
        "enabled": False,
        "role": None,
        "permissions": [],
        "entity_ids": [],
        "access_source": "denied",
        "message": "Accounting access is not available.",
    }

    if not user_object_id:
        denied["message"] = "Invalid authenticated user."
        return denied

    user = mongo.db.users.find_one(
        {"_id": user_object_id},
        {
            "role": 1,
            "active": 1,
            "status": 1,
            "approval_status": 1,
        },
    )

    if not user:
        denied["message"] = "Authenticated user was not found."
        return denied

    if user.get("active", True) is False or user.get("status") == "inactive":
        denied["message"] = "This user account is inactive."
        return denied

    role = str(user.get("role") or session_role or "").strip().lower()

    if role not in ACCOUNTING_ROLES:
        denied["role"] = role
        denied["message"] = "Your role is not enabled for Accounting."
        return denied

    permissions = set(ROLE_DEFAULT_PERMISSIONS.get(role, set()))
    entity_ids = _default_entity_ids()
    access_source = "role_default"
    enabled = True

    access_document = mongo.db.accounting_user_access.find_one({
        "$or": [
            {"user_id": user_object_id},
            {"user_id": str(user_object_id)},
        ]
    })

    if access_document:
        access_source = "user_override"
        enabled = access_document.get(
            "accounting_enabled",
            access_document.get("enabled", True),
        ) is not False

        permissions.update(
            _clean_permissions(access_document.get("permissions"))
        )
        permissions.difference_update(
            _clean_permissions(access_document.get("denied_permissions"))
        )

        # An explicit entity_ids field replaces the bootstrap entity mapping.
        # Omitting the field keeps the default AVPL mapping during rollout.
        if "entity_ids" in access_document:
            raw_entity_ids = access_document.get("entity_ids") or []
            entity_ids = [str(value) for value in raw_entity_ids if value]

    if role == "super_admin":
        permissions.add("*")
        enabled = True

    return {
        "enabled": enabled,
        "role": role,
        "permissions": sorted(permissions),
        "entity_ids": entity_ids,
        "access_source": access_source,
        "message": "Accounting access granted." if enabled else "Accounting access is disabled.",
    }


def has_accounting_permission(access, permission):
    if not access or not access.get("enabled"):
        return False

    required_permission = str(permission or "").strip()
    permissions = set(access.get("permissions") or [])

    return (
        not required_permission
        or "*" in permissions
        or required_permission in permissions
    )
