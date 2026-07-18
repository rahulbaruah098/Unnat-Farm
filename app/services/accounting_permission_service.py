from bson import ObjectId

from app.extensions import mongo


ACCOUNTING_ROLES = frozenset({
    "super_admin",
    "avpl_admin",
    "accounts",
})

# Incremented only when a newly developed Accounting stage introduces new
# permissions that existing permanent mappings could not previously contain.
CURRENT_PERMISSION_SCHEMA_VERSION = 8

# Only permissions introduced by a later schema are appended to older exact
# mappings. Previously removed permissions are never silently re-granted.
PERMISSION_SCHEMA_ADDITIONS = {
    2: {
        "avpl_admin": {
            "accounting.entity_settings.view",
            "accounting.entity_settings.create",
            "accounting.entity_settings.edit",
            "accounting.entity_settings.submit",
            "accounting.entity_settings.withdraw",
            "accounting.settings.view",
            "accounting.settings.create",
            "accounting.settings.edit",
            "accounting.settings.submit",
            "accounting.settings.withdraw",
        },
        "accounts": {
            "accounting.entity_settings.view",
            "accounting.settings.view",
        },
    },
    3: {
        "avpl_admin": {
            "accounting.number_series.view",
            "accounting.number_series.approve",
            "accounting.number_series.return",
        },
        "accounts": {
            "accounting.number_series.view",
            "accounting.number_series.create",
            "accounting.number_series.edit",
            "accounting.number_series.submit",
            "accounting.number_series.withdraw",
        },
    },
    4: {
        "avpl_admin": {
            "accounting.financial_year.control.view",
            "accounting.financial_year.control.create",
            "accounting.financial_year.control.edit",
            "accounting.financial_year.control.submit",
            "accounting.financial_year.control.withdraw",
            "accounting.financial_year.control.cancel",
        },
        "accounts": {
            "accounting.financial_year.control.view",
        },
    },
    5: {
        "avpl_admin": {
            "accounting.entity_mapping.view",
        },
        "accounts": {
            "accounting.entity_mapping.view",
        },
    },
    6: {
        "avpl_admin": {
            "accounting.account_group.view",
        },
        "accounts": {
            "accounting.account_group.view",
        },
    },
    7: {
        "avpl_admin": {
            "accounting.ledger.view",
        },
        "accounts": {
            "accounting.ledger.view",
        },
    },
    8: {
        "avpl_admin": {
            "accounting.party_ledger.view",
            "accounting.party_ledger.approve",
            "accounting.party_ledger.return",
            "accounting.party_ledger.deactivate",
            "accounting.party_ledger.reactivate",
        },
        "accounts": {
            "accounting.party_ledger.view",
            "accounting.party_ledger.create",
            "accounting.party_ledger.edit",
            "accounting.party_ledger.submit",
            "accounting.party_ledger.withdraw",
            "accounting.party_ledger.cancel",
        },
    },
}

ROLE_DEFAULT_PERMISSIONS = {
    "super_admin": {"*"},
    "avpl_admin": {
        "accounting.access",
        "accounting.dashboard.view",
        "accounting.entity.view",
        "accounting.entity_mapping.view",
        "accounting.account_group.view",
        "accounting.ledger.view",
        "accounting.party_ledger.view",
        "accounting.party_ledger.approve",
        "accounting.party_ledger.return",
        "accounting.party_ledger.deactivate",
        "accounting.party_ledger.reactivate",
        "accounting.financial_year.view",
        "accounting.financial_year.create",
        "accounting.financial_year.edit",
        "accounting.financial_year.submit",
        "accounting.financial_year.withdraw",
        "accounting.financial_year.control.view",
        "accounting.financial_year.control.create",
        "accounting.financial_year.control.edit",
        "accounting.financial_year.control.submit",
        "accounting.financial_year.control.withdraw",
        "accounting.financial_year.control.cancel",
        "accounting.user_access.view",
        "accounting.user_access.manage_accounts",
        "accounting.entity_settings.view",
        "accounting.entity_settings.create",
        "accounting.entity_settings.edit",
        "accounting.entity_settings.submit",
        "accounting.entity_settings.withdraw",
        "accounting.settings.view",
        "accounting.settings.create",
        "accounting.settings.edit",
        "accounting.settings.submit",
        "accounting.settings.withdraw",
        "accounting.number_series.view",
        "accounting.number_series.approve",
        "accounting.number_series.return",
    },
    "accounts": {
        "accounting.access",
        "accounting.dashboard.view",
        "accounting.entity.view",
        "accounting.entity_mapping.view",
        "accounting.account_group.view",
        "accounting.ledger.view",
        "accounting.party_ledger.view",
        "accounting.party_ledger.create",
        "accounting.party_ledger.edit",
        "accounting.party_ledger.submit",
        "accounting.party_ledger.withdraw",
        "accounting.party_ledger.cancel",
        "accounting.financial_year.view",
        "accounting.financial_year.use",
        "accounting.financial_year.control.view",
        "accounting.entity_settings.view",
        "accounting.settings.view",
        "accounting.number_series.view",
        "accounting.number_series.create",
        "accounting.number_series.edit",
        "accounting.number_series.submit",
        "accounting.number_series.withdraw",
    },
}

ROLE_ASSIGNABLE_PERMISSIONS = {
    "avpl_admin": set(ROLE_DEFAULT_PERMISSIONS["avpl_admin"]),
    "accounts": set(ROLE_DEFAULT_PERMISSIONS["accounts"]),
}

PERMISSION_LABELS = {
    "accounting.access": {
        "label": "Accounting module access",
        "description": "Allows the user to enter the protected Accounting module.",
        "group": "Core access",
    },
    "accounting.dashboard.view": {
        "label": "View Accounting dashboard",
        "description": "Allows access to the Accounting dashboard and setup status.",
        "group": "Core access",
    },
    "accounting.entity.view": {
        "label": "View assigned entity",
        "description": "Allows the user to view Accounting entities assigned to them.",
        "group": "Core access",
    },
    "accounting.entity_mapping.view": {
        "label": "View future entity hierarchy",
        "description": "Allows visibility of pre-mapped Centre, Mitra and Farmer Accounting hierarchy records.",
        "group": "Entity mapping",
    },
    "accounting.entity_mapping.sync": {
        "label": "Synchronize future entity hierarchy",
        "description": "Allows Super Admin to create or refresh disabled Centre, Mitra and Farmer Accounting mappings.",
        "group": "Entity mapping",
    },
    "accounting.account_group.view": {
        "label": "View account groups",
        "description": "Allows visibility of AVPL protected account-group masters and hierarchy health.",
        "group": "Account groups",
    },
    "accounting.account_group.bootstrap": {
        "label": "Initialize protected account groups",
        "description": "Allows Super Admin to seed or repair the permanent AVPL system account groups.",
        "group": "Account groups",
    },
    "accounting.ledger.view": {
        "label": "View ledger masters",
        "description": "Allows visibility of protected AVPL default ledgers, group assignments and master health.",
        "group": "Ledger masters",
    },
    "accounting.ledger.bootstrap": {
        "label": "Initialize default AVPL ledgers",
        "description": "Allows Super Admin to seed or repair the protected AVPL default ledger catalog.",
        "group": "Ledger masters",
    },
    "accounting.party_ledger.view": {
        "label": "View supplier and party ledgers",
        "description": "Allows visibility of AVPL supplier and customer party-ledger masters and their workflow status.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.create": {
        "label": "Create party-ledger draft",
        "description": "Allows Accounts to create supplier and customer party-ledger drafts.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.edit": {
        "label": "Edit party-ledger draft",
        "description": "Allows the original Accounts maker to edit draft or returned party ledgers.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.submit": {
        "label": "Submit party ledger",
        "description": "Allows Accounts to submit or resubmit a party ledger for maker-checker approval.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.withdraw": {
        "label": "Withdraw pending party ledger",
        "description": "Allows the original maker to withdraw a pending party ledger for correction.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.cancel": {
        "label": "Cancel party-ledger draft",
        "description": "Allows the original maker to cancel a draft or returned party ledger without hard deletion.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.approve": {
        "label": "Approve party ledger",
        "description": "Allows AVPL Admin to approve and activate a submitted supplier or customer ledger.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.return": {
        "label": "Return party ledger",
        "description": "Allows AVPL Admin to return a submitted party ledger with a correction reason.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.deactivate": {
        "label": "Deactivate party ledger",
        "description": "Allows AVPL Admin to deactivate an active party ledger while preserving all history.",
        "group": "Party ledgers",
    },
    "accounting.party_ledger.reactivate": {
        "label": "Reactivate party ledger",
        "description": "Allows AVPL Admin to reactivate a previously deactivated party ledger with a reason.",
        "group": "Party ledgers",
    },
    "accounting.financial_year.view": {
        "label": "View Financial Years",
        "description": "Allows visibility of Financial Year records and status.",
        "group": "Financial Year",
    },
    "accounting.financial_year.create": {
        "label": "Create Financial Year draft",
        "description": "Allows AVPL Admin to create a Financial Year draft.",
        "group": "Financial Year",
    },
    "accounting.financial_year.edit": {
        "label": "Edit Financial Year draft",
        "description": "Allows editing of draft or returned Financial Years.",
        "group": "Financial Year",
    },
    "accounting.financial_year.submit": {
        "label": "Submit Financial Year",
        "description": "Allows submission or resubmission to Super Admin.",
        "group": "Financial Year",
    },
    "accounting.financial_year.withdraw": {
        "label": "Withdraw pending Financial Year",
        "description": "Allows the maker to withdraw a pending submission for correction.",
        "group": "Financial Year",
    },
    "accounting.financial_year.use": {
        "label": "Use open Financial Year",
        "description": "Allows future Accounting postings in approved, open years.",
        "group": "Financial Year",
    },
    "accounting.financial_year.control.view": {
        "label": "View Financial Year lifecycle controls",
        "description": "Allows visibility of close, lock, unlock and reopen requests and history.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.create": {
        "label": "Create lifecycle request",
        "description": "Allows AVPL Admin to create close, lock, unlock or reopen request drafts.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.edit": {
        "label": "Edit lifecycle request",
        "description": "Allows the maker to edit draft or returned lifecycle requests.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.submit": {
        "label": "Submit lifecycle request",
        "description": "Allows the maker to submit or resubmit lifecycle requests to Super Admin.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.withdraw": {
        "label": "Withdraw pending lifecycle request",
        "description": "Allows the maker to withdraw a pending lifecycle request for correction.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.cancel": {
        "label": "Cancel lifecycle request",
        "description": "Allows the maker to cancel a draft or returned request without deleting history.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.approve": {
        "label": "Approve Financial Year lifecycle request",
        "description": "Allows Super Admin to apply an approved close, lock, unlock or reopen transition.",
        "group": "Financial Year lifecycle",
    },
    "accounting.financial_year.control.return": {
        "label": "Send lifecycle request back",
        "description": "Allows Super Admin to return a pending lifecycle request with a correction reason.",
        "group": "Financial Year lifecycle",
    },
    "accounting.user_access.view": {
        "label": "View Accounting user access",
        "description": "Allows visibility of Accounting user-to-entity mappings.",
        "group": "Access control",
    },
    "accounting.user_access.manage_accounts": {
        "label": "Manage Accounts users",
        "description": "Allows AVPL Admin to manage Accounts-role Accounting access.",
        "group": "Access control",
    },
    "accounting.entity_settings.view": {
        "label": "View entity configuration",
        "description": "Allows visibility of approved and pending entity profile settings.",
        "group": "Entity configuration",
    },
    "accounting.entity_settings.create": {
        "label": "Create entity configuration draft",
        "description": "Allows AVPL Admin to start a new version of the entity profile.",
        "group": "Entity configuration",
    },
    "accounting.entity_settings.edit": {
        "label": "Edit entity configuration draft",
        "description": "Allows editing of draft or returned entity profile settings.",
        "group": "Entity configuration",
    },
    "accounting.entity_settings.submit": {
        "label": "Submit entity configuration",
        "description": "Allows submission of the entity profile to Super Admin.",
        "group": "Entity configuration",
    },
    "accounting.entity_settings.withdraw": {
        "label": "Withdraw entity configuration",
        "description": "Allows the maker to withdraw a pending entity profile.",
        "group": "Entity configuration",
    },
    "accounting.settings.view": {
        "label": "View Accounting settings",
        "description": "Allows visibility of approved and pending Accounting policy settings.",
        "group": "Accounting settings",
    },
    "accounting.settings.create": {
        "label": "Create Accounting settings draft",
        "description": "Allows AVPL Admin to start a new settings version.",
        "group": "Accounting settings",
    },
    "accounting.settings.edit": {
        "label": "Edit Accounting settings draft",
        "description": "Allows editing of draft or returned Accounting policy settings.",
        "group": "Accounting settings",
    },
    "accounting.settings.submit": {
        "label": "Submit Accounting settings",
        "description": "Allows submission of Accounting settings to Super Admin.",
        "group": "Accounting settings",
    },
    "accounting.settings.withdraw": {
        "label": "Withdraw Accounting settings",
        "description": "Allows the maker to withdraw pending Accounting settings.",
        "group": "Accounting settings",
    },
    "accounting.number_series.view": {
        "label": "View invoice and voucher series",
        "description": "Allows visibility of entity- and Financial-Year-wise document numbering.",
        "group": "Number series",
    },
    "accounting.number_series.create": {
        "label": "Create number-series drafts",
        "description": "Allows Accounts to create missing invoice and voucher series drafts.",
        "group": "Number series",
    },
    "accounting.number_series.edit": {
        "label": "Edit number-series drafts",
        "description": "Allows Accounts to edit draft or returned numbering revisions.",
        "group": "Number series",
    },
    "accounting.number_series.submit": {
        "label": "Submit number series",
        "description": "Allows Accounts to submit or resubmit numbering revisions to AVPL Admin.",
        "group": "Number series",
    },
    "accounting.number_series.withdraw": {
        "label": "Withdraw pending number series",
        "description": "Allows the Accounts maker to withdraw a pending series for correction.",
        "group": "Number series",
    },
    "accounting.number_series.approve": {
        "label": "Approve and activate number series",
        "description": "Allows AVPL Admin to approve a submitted series and make it available to posting services.",
        "group": "Number series",
    },
    "accounting.number_series.return": {
        "label": "Send number series back",
        "description": "Allows AVPL Admin to return a submitted series with a correction reason.",
        "group": "Number series",
    },
}

MANDATORY_ENABLED_PERMISSIONS = {
    "accounting.access",
    "accounting.dashboard.view",
    "accounting.entity.view",
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


def get_permission_schema_additions(role, from_version, to_version=None):
    role_key = str(role or "").strip().lower()
    try:
        current_version = int(from_version or 1)
    except (TypeError, ValueError):
        current_version = 1

    target_version = int(to_version or CURRENT_PERMISSION_SCHEMA_VERSION)
    additions = set()

    for schema_version in range(current_version + 1, target_version + 1):
        additions.update(
            PERMISSION_SCHEMA_ADDITIONS.get(schema_version, {}).get(
                role_key,
                set(),
            )
        )

    return additions


def get_role_assignable_permissions(role):
    return set(
        ROLE_ASSIGNABLE_PERMISSIONS.get(
            str(role or "").strip().lower(),
            set(),
        )
    )


def get_permission_catalog(role):
    permission_codes = sorted(
        get_role_assignable_permissions(role),
        key=lambda code: (
            PERMISSION_LABELS.get(code, {}).get("group", "Other"),
            PERMISSION_LABELS.get(code, {}).get("label", code),
        ),
    )

    return [
        {
            "code": code,
            "label": PERMISSION_LABELS.get(code, {}).get("label", code),
            "description": PERMISSION_LABELS.get(code, {}).get("description", ""),
            "group": PERMISSION_LABELS.get(code, {}).get("group", "Other"),
            "mandatory": code in MANDATORY_ENABLED_PERMISSIONS,
        }
        for code in permission_codes
    ]


def get_accounting_access(user_id, session_role=None):
    """Resolve access from a verified user and an exact permanent mapping."""
    user_object_id = _to_object_id(user_id)
    denied = {
        "enabled": False,
        "role": None,
        "permissions": [],
        "entity_ids": [],
        "access_source": "denied",
        "mapping_version": None,
        "permission_schema_version": None,
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
            "is_active": 1,
            "status": 1,
            "approval_status": 1,
        },
    )
    if not user:
        denied["message"] = "Authenticated user was not found."
        return denied

    if (
        user.get("active", True) is False
        or user.get("is_active", True) is False
        or user.get("status") == "inactive"
    ):
        denied["message"] = "This user account is inactive."
        return denied

    role = str(user.get("role") or session_role or "").strip().lower()
    if role not in ACCOUNTING_ROLES:
        denied["role"] = role
        denied["message"] = "Your role is not enabled for Accounting."
        return denied

    if role == "super_admin":
        return {
            "enabled": True,
            "role": role,
            "permissions": ["*"],
            "entity_ids": _default_entity_ids(),
            "access_source": "system_super_admin",
            "mapping_version": None,
            "permission_schema_version": CURRENT_PERMISSION_SCHEMA_VERSION,
            "message": "Accounting access granted.",
        }

    permissions = set(ROLE_DEFAULT_PERMISSIONS.get(role, set()))
    entity_ids = _default_entity_ids()
    access_source = "role_default_fallback"
    enabled = True
    mapping_version = None
    permission_schema_version = None

    access_document = mongo.db.accounting_user_access.find_one({
        "$or": [
            {"user_id": user_object_id},
            {"user_id": str(user_object_id)},
            {"user_id_str": str(user_object_id)},
        ]
    })

    if access_document:
        mapping_version = access_document.get("version")
        permission_schema_version = int(
            access_document.get("permission_schema_version") or 1
        )
        permission_mode = str(
            access_document.get("permission_mode") or "inherit_role"
        ).strip().lower()
        enabled = access_document.get(
            "accounting_enabled",
            access_document.get("enabled", True),
        ) is not False

        if permission_mode == "replace":
            permissions = _clean_permissions(access_document.get("permissions"))
            access_source = "permanent_user_mapping"
        else:
            permissions.update(
                _clean_permissions(access_document.get("permissions"))
            )
            permissions.difference_update(
                _clean_permissions(access_document.get("denied_permissions"))
            )
            access_source = "legacy_user_override"

        if "entity_ids" in access_document:
            entity_ids = [
                str(value)
                for value in access_document.get("entity_ids") or []
                if value
            ]

    return {
        "enabled": enabled,
        "role": role,
        "permissions": sorted(permissions),
        "entity_ids": entity_ids,
        "access_source": access_source,
        "mapping_version": mapping_version,
        "permission_schema_version": permission_schema_version,
        "message": (
            "Accounting access granted."
            if enabled
            else "Accounting access is disabled."
        ),
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
