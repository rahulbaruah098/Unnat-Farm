from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


REQUEST_CLOSE = "close"
REQUEST_LOCK = "lock"
REQUEST_UNLOCK = "unlock"
REQUEST_REOPEN = "reopen"

REQUEST_TYPES = {
    REQUEST_CLOSE: {
        "label": "Close Financial Year",
        "short_label": "Close",
        "description": (
            "Stop new Accounting posting in this year after final operational review."
        ),
        "source_status": "open",
        "target_status": "closed",
    },
    REQUEST_LOCK: {
        "label": "Lock Financial Year",
        "short_label": "Lock",
        "description": (
            "Apply final protection to a closed year so it cannot be reopened without an unlock approval."
        ),
        "source_status": "closed",
        "target_status": "locked",
    },
    REQUEST_UNLOCK: {
        "label": "Unlock Financial Year",
        "short_label": "Unlock",
        "description": (
            "Remove the final lock while keeping the Financial Year closed."
        ),
        "source_status": "locked",
        "target_status": "closed",
    },
    REQUEST_REOPEN: {
        "label": "Reopen Financial Year",
        "short_label": "Reopen",
        "description": (
            "Reopen a closed and unlocked year for approved corrections or late posting."
        ),
        "source_status": "closed",
        "target_status": "open",
    },
}

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_APPROVED = "approved"
STATUS_CANCELLED = "cancelled"

EDITABLE_REQUEST_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
ACTIVE_REQUEST_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_APPROVAL,
    STATUS_RETURNED,
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


def ensure_financial_year_control_indexes():
    collection = mongo.db.financial_year_control_requests

    _ensure_exact_index(
        collection,
        [("financial_year_id", ASCENDING), ("active_slot", ASCENDING)],
        name="fy_control_one_active_request_unique",
        unique=True,
        partialFilterExpression={"active_slot": True, "is_deleted": False},
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("submitted_at", ASCENDING),
        ],
        name="fy_control_entity_approval_queue_idx",
    )
    _ensure_exact_index(
        collection,
        [("financial_year_id", ASCENDING), ("created_at", DESCENDING)],
        name="fy_control_year_history_idx",
    )
    _ensure_exact_index(
        collection,
        [("created_by", ASCENDING), ("updated_at", DESCENDING)],
        name="fy_control_creator_updated_idx",
    )


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
        raise PermissionError("Inactive users cannot perform Financial Year controls.")

    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError(
            "You are not authorized to perform this Financial Year lifecycle action."
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


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")

    role = actor.get("resolved_role") or actor.get("role")
    if role != "super_admin":
        entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")

    if not has_accounting_permission(access, permission):
        raise PermissionError(
            "You do not have permission to perform this Financial Year lifecycle action."
        )
    return access


def _get_financial_year(financial_year_id):
    object_id = _to_object_id(financial_year_id)
    if not object_id:
        raise ValueError("Invalid Financial Year.")

    row = mongo.db.financial_years.find_one({
        "_id": object_id,
        "is_deleted": {"$ne": True},
    })
    if not row:
        raise ValueError("Financial Year was not found.")
    return row


def _get_control_request(request_id):
    object_id = _to_object_id(request_id)
    if not object_id:
        raise ValueError("Invalid Financial Year control request.")

    row = mongo.db.financial_year_control_requests.find_one({
        "_id": object_id,
        "is_deleted": {"$ne": True},
    })
    if not row:
        raise ValueError("Financial Year control request was not found.")
    return row


def _parse_expected_version(value, label="request"):
    try:
        parsed = int(value)
    except Exception as exc:
        raise ValueError(
            f"The {label} version is missing. Refresh and try again."
        ) from exc

    if parsed < 1:
        raise ValueError(f"Invalid {label} version. Refresh and try again.")
    return parsed


def _financial_year_state(financial_year):
    status = str(financial_year.get("status") or "").strip().lower()

    if status == "locked" or financial_year.get("is_locked") is True:
        return "locked"
    if status == "closed":
        return "closed"
    if status == "open" and financial_year.get("is_open") is True:
        return "open"
    return status or "draft"


def _validate_request_type_for_year(financial_year, request_type):
    request_type = str(request_type or "").strip().lower()
    definition = REQUEST_TYPES.get(request_type)
    if not definition:
        raise ValueError("Select a valid Financial Year lifecycle action.")

    current_state = _financial_year_state(financial_year)
    expected_state = definition["source_status"]
    if current_state != expected_state:
        raise ValueError(
            f"{definition['label']} is available only when the Financial Year is "
            f"{expected_state.replace('_', ' ')}. Current state: "
            f"{current_state.replace('_', ' ')}."
        )

    return request_type, definition, current_state


def _clean_reason(value, label="Business reason"):
    reason = str(value or "").strip()
    if not reason:
        raise ValueError(f"{label} is required.")
    if len(reason) < 8:
        raise ValueError(f"{label} must contain at least 8 characters.")
    if len(reason) > 2000:
        raise ValueError(f"{label} cannot exceed 2000 characters.")
    return reason


def _clean_optional_note(value, max_length=2000):
    note = str(value or "").strip()
    if len(note) > max_length:
        raise ValueError(f"The note cannot exceed {max_length} characters.")
    return note


def _workflow_event(
    action,
    actor,
    from_status,
    to_status,
    revision_number,
    reason="",
    note="",
    timestamp=None,
):
    timestamp = timestamp or now_utc()
    return {
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "reason": str(reason or "").strip(),
        "note": str(note or "").strip(),
        "revision_number": int(revision_number or 1),
        "at": timestamp,
    }


def _write_audit(
    action,
    request_document,
    actor,
    previous_status,
    new_status,
    remarks="",
    metadata=None,
    timestamp=None,
):
    timestamp = timestamp or now_utc()
    audit_document = {
        "module": "accounting",
        "action": action,
        "accounting_entity_id": request_document["accounting_entity_id"],
        "accounting_entity_id_str": str(request_document["accounting_entity_id"]),
        "entity_type": "financial_year_control_request",
        "entity_id": request_document["_id"],
        "entity_id_str": str(request_document["_id"]),
        "financial_year_id": request_document["financial_year_id"],
        "financial_year_id_str": str(request_document["financial_year_id"]),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "metadata": {
            "request_type": request_document.get("request_type"),
            "request_label": request_document.get("request_label"),
            "financial_year_code": request_document.get("financial_year_code"),
            "financial_year_name": request_document.get("financial_year_name"),
            "revision_number": request_document.get("revision_number", 1),
            **(metadata or {}),
        },
        "remarks": str(remarks or "").strip(),
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit_document)
    except Exception as exc:
        mongo.db.financial_year_control_requests.update_one(
            {"_id": request_document["_id"]},
            {
                "$set": {
                    "audit_sync_status": "pending_recovery",
                    "audit_sync_updated_at": timestamp,
                },
                "$push": {
                    "audit_sync_errors": {
                        "action": action,
                        "message": str(exc)[:500],
                        "created_at": timestamp,
                    }
                },
            },
        )
        return False

    mongo.db.financial_year_control_requests.update_one(
        {"_id": request_document["_id"]},
        {
            "$set": {
                "audit_sync_status": "synced",
                "audit_sync_updated_at": timestamp,
            }
        },
    )
    return True


# ---------------------------------------------------------------------------
# Period readiness checks
# ---------------------------------------------------------------------------


def _matching_financial_year_query(financial_year):
    financial_year_id = financial_year["_id"]
    return {
        "$or": [
            {"financial_year_id": financial_year_id},
            {"financial_year_id": str(financial_year_id)},
            {"financial_year_id_str": str(financial_year_id)},
        ]
    }


def get_financial_year_control_readiness(financial_year, request_type):
    """Return blockers and warnings without mutating any record.

    More transaction collections will use these standard fields in later stages.
    The checks are intentionally additive so Batch 7 remains safe before those
    collections exist.
    """
    request_type = str(request_type or "").strip().lower()
    blockers = []
    warnings = []

    if request_type not in REQUEST_TYPES:
        return {
            "ready": False,
            "blockers": ["Invalid lifecycle request type."],
            "warnings": [],
        }

    if request_type in {REQUEST_CLOSE, REQUEST_LOCK}:
        period_query = _matching_financial_year_query(financial_year)

        reserved_query = {
            "$and": [
                period_query,
                {"status": "reserved"},
            ]
        }
        reserved_count = mongo.db.accounting_number_reservations.count_documents(
            reserved_query
        )
        if reserved_count:
            blockers.append(
                f"{reserved_count} official number reservation(s) are still pending commit or void."
            )

        posting_failure_query = {
            "$and": [
                period_query,
                {
                    "status": {
                        "$in": [
                            "open",
                            "pending",
                            "retry_required",
                            "pending_recovery",
                        ]
                    }
                },
            ]
        }
        posting_failure_count = mongo.db.posting_failures.count_documents(
            posting_failure_query
        )
        if posting_failure_count:
            blockers.append(
                f"{posting_failure_count} unresolved posting failure(s) must be recovered first."
            )

        unfinished_statuses = [
            "draft",
            "pending_supporting_documents",
            "pending_accounts_review",
            "pending_business_approval",
            "pending_approval",
            "approved",
            "posting",
            "post_failed",
            "retry_required",
            "cancel_requested",
        ]
        future_period_collections = (
            ("business_events", "business event"),
            ("purchase_invoices", "purchase invoice"),
            ("sales_invoices", "sales invoice"),
            ("vouchers", "voucher"),
            ("payments", "payment"),
            ("receipt_confirmations", "receipt confirmation"),
        )
        for collection_name, item_label in future_period_collections:
            unfinished_count = mongo.db[collection_name].count_documents({
                "$and": [
                    period_query,
                    {"status": {"$in": unfinished_statuses}},
                    {"is_deleted": {"$ne": True}},
                ]
            })
            if unfinished_count:
                plural = "s" if unfinished_count != 1 else ""
                blockers.append(
                    f"{unfinished_count} unfinished {item_label}{plural} must be posted, cancelled or resolved first."
                )

    if request_type == REQUEST_CLOSE:
        next_open = mongo.db.financial_years.find_one({
            "accounting_entity_id": financial_year["accounting_entity_id"],
            "_id": {"$ne": financial_year["_id"]},
            "status": "open",
            "is_open": True,
            "is_locked": {"$ne": True},
            "is_deleted": {"$ne": True},
        })
        if not next_open:
            warnings.append(
                "No other open Financial Year currently exists. Posting will pause after closure until another year is opened."
            )

    if request_type == REQUEST_REOPEN:
        warnings.append(
            "Reopening permits new entries in a previously closed period. Every later posting must remain separately authorized and audited."
        )

    if request_type == REQUEST_UNLOCK:
        warnings.append(
            "Unlocking removes final protection but does not itself reopen the Financial Year."
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "warnings": warnings,
    }


def _assert_control_ready(financial_year, request_type):
    readiness = get_financial_year_control_readiness(financial_year, request_type)
    if readiness["blockers"]:
        raise ValueError(
            "Financial Year control cannot continue: "
            + " ".join(readiness["blockers"])
        )
    return readiness


# ---------------------------------------------------------------------------
# Serialization and dashboard overview
# ---------------------------------------------------------------------------


def _user_name_map(requests):
    user_ids = set()
    for row in requests:
        for key in (
            "created_by",
            "submitted_by",
            "approved_by",
            "returned_by",
            "withdrawn_by",
            "cancelled_by",
            "updated_by",
        ):
            object_id = _to_object_id(row.get(key))
            if object_id:
                user_ids.add(object_id)

    if not user_ids:
        return {}

    result = {}
    for user in mongo.db.users.find(
        {"_id": {"$in": list(user_ids)}},
        {"name": 1, "full_name": 1, "username": 1, "phone": 1, "role": 1},
    ):
        result[str(user["_id"])] = (
            user.get("name")
            or user.get("full_name")
            or user.get("username")
            or user.get("phone")
            or str(user.get("role") or "User").replace("_", " ").title()
        )
    return result


def serialize_financial_year_control_request(document, user_names=None):
    if not document:
        return None

    user_names = user_names or {}

    def user_name(key):
        value = document.get(key)
        return user_names.get(str(value), "") if value else ""

    status = str(document.get("status") or STATUS_DRAFT)
    request_type = str(document.get("request_type") or "")
    definition = REQUEST_TYPES.get(request_type, {})

    return {
        "id": str(document.get("_id") or ""),
        "accounting_entity_id": str(document.get("accounting_entity_id") or ""),
        "financial_year_id": str(document.get("financial_year_id") or ""),
        "financial_year_code": document.get("financial_year_code") or "",
        "financial_year_name": document.get("financial_year_name") or "",
        "request_type": request_type,
        "request_label": document.get("request_label") or definition.get("label") or request_type,
        "request_short_label": definition.get("short_label") or request_type.title(),
        "request_description": definition.get("description") or "",
        "source_status": document.get("source_status") or definition.get("source_status") or "",
        "target_status": document.get("target_status") or definition.get("target_status") or "",
        "status": status,
        "status_display": status.replace("_", " ").title(),
        "active_slot": document.get("active_slot") is True,
        "reason": document.get("reason") or "",
        "correction_response": document.get("correction_response") or "",
        "submission_note": document.get("submission_note") or "",
        "return_reason": document.get("return_reason") or "",
        "withdraw_reason": document.get("withdraw_reason") or "",
        "cancel_reason": document.get("cancel_reason") or "",
        "approval_note": document.get("approval_note") or "",
        "revision_number": int(document.get("revision_number") or 1),
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": user_name("created_by"),
        "submitted_by_name": user_name("submitted_by"),
        "approved_by_name": user_name("approved_by"),
        "returned_by_name": user_name("returned_by"),
        "withdrawn_by_name": user_name("withdrawn_by"),
        "cancelled_by_name": user_name("cancelled_by"),
        "created_at": document.get("created_at"),
        "updated_at": document.get("updated_at"),
        "submitted_at": document.get("submitted_at"),
        "approved_at": document.get("approved_at"),
        "returned_at": document.get("returned_at"),
        "workflow_history": document.get("workflow_history") or [],
        "audit_sync_status": document.get("audit_sync_status") or "",
        "apply_recovery_required": document.get("apply_recovery_required") is True,
    }


def get_allowed_control_types(financial_year):
    state = _financial_year_state(financial_year)
    result = []
    for code, definition in REQUEST_TYPES.items():
        if definition["source_status"] != state:
            continue
        readiness = get_financial_year_control_readiness(financial_year, code)
        result.append({
            "code": code,
            "label": definition["label"],
            "short_label": definition["short_label"],
            "description": definition["description"],
            "target_status": definition["target_status"],
            "ready": readiness["ready"],
            "blockers": readiness["blockers"],
            "warnings": readiness["warnings"],
        })
    return result


def get_financial_year_control_overview(entity_id, financial_years):
    entity_object_id = _to_object_id(entity_id)
    if not entity_object_id:
        return {
            "by_financial_year": {},
            "pending_count": 0,
            "returned_count": 0,
            "active_count": 0,
            "history_count": 0,
        }

    ensure_financial_year_control_indexes()

    financial_year_rows = {}
    financial_year_ids = []
    for item in financial_years or []:
        object_id = _to_object_id(item.get("id") or item.get("_id"))
        if object_id:
            financial_year_ids.append(object_id)
            financial_year_rows[str(object_id)] = item

    if not financial_year_ids:
        return {
            "by_financial_year": {},
            "pending_count": 0,
            "returned_count": 0,
            "active_count": 0,
            "history_count": 0,
        }

    rows = list(
        mongo.db.financial_year_control_requests.find({
            "accounting_entity_id": entity_object_id,
            "financial_year_id": {"$in": financial_year_ids},
            "is_deleted": {"$ne": True},
        }).sort([("created_at", DESCENDING)])
    )
    user_names = _user_name_map(rows)
    serialized = [serialize_financial_year_control_request(row, user_names) for row in rows]

    by_year = {
        str(financial_year_id): {
            "active_request": None,
            "history": [],
            "allowed_types": [],
        }
        for financial_year_id in financial_year_ids
    }

    pending_count = 0
    returned_count = 0
    active_count = 0
    history_count = 0

    for request_row in serialized:
        year_id = request_row["financial_year_id"]
        slot = by_year.setdefault(
            year_id,
            {"active_request": None, "history": [], "allowed_types": []},
        )
        if request_row["status"] in ACTIVE_REQUEST_STATUSES and request_row["active_slot"]:
            if slot["active_request"] is None:
                slot["active_request"] = request_row
                active_count += 1
            if request_row["status"] == STATUS_PENDING_APPROVAL:
                pending_count += 1
            if request_row["status"] == STATUS_RETURNED:
                returned_count += 1
        else:
            slot["history"].append(request_row)
            history_count += 1

    for year_id, slot in by_year.items():
        if slot["active_request"] is not None:
            continue
        financial_year = mongo.db.financial_years.find_one({"_id": _to_object_id(year_id)})
        if financial_year:
            slot["allowed_types"] = get_allowed_control_types(financial_year)

    return {
        "by_financial_year": by_year,
        "pending_count": pending_count,
        "returned_count": returned_count,
        "active_count": active_count,
        "history_count": history_count,
    }


# ---------------------------------------------------------------------------
# Maker actions
# ---------------------------------------------------------------------------


def create_financial_year_control_request(
    financial_year_id,
    actor_user_id,
    request_type,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    financial_year = _get_financial_year(financial_year_id)
    _require_permission(
        actor,
        financial_year["accounting_entity_id"],
        "accounting.financial_year.control.create",
    )
    ensure_financial_year_control_indexes()

    normalized_type, definition, source_status = _validate_request_type_for_year(
        financial_year,
        request_type,
    )
    clean_reason = _clean_reason(reason)
    timestamp = now_utc()

    event = _workflow_event(
        "created",
        actor,
        None,
        STATUS_DRAFT,
        revision_number=1,
        reason=clean_reason,
        timestamp=timestamp,
    )
    document = {
        "accounting_entity_id": financial_year["accounting_entity_id"],
        "accounting_entity_id_str": str(financial_year["accounting_entity_id"]),
        "financial_year_id": financial_year["_id"],
        "financial_year_id_str": str(financial_year["_id"]),
        "financial_year_code": financial_year.get("fy_code") or "",
        "financial_year_name": financial_year.get("display_name") or "",
        "request_type": normalized_type,
        "request_label": definition["label"],
        "source_status": source_status,
        "target_status": definition["target_status"],
        "status": STATUS_DRAFT,
        "active_slot": True,
        "reason": clean_reason,
        "revision_number": 1,
        "submission_count": 0,
        "version": 1,
        "financial_year_version_at_creation": int(financial_year.get("version") or 1),
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "workflow_history": [event],
        "is_deleted": False,
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    try:
        result = mongo.db.financial_year_control_requests.insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError(
            "Another active lifecycle request already exists for this Financial Year."
        ) from exc

    document["_id"] = result.inserted_id
    _write_audit(
        "financial_year_control_created",
        document,
        actor,
        None,
        STATUS_DRAFT,
        remarks=clean_reason,
        timestamp=timestamp,
    )

    return {
        "request": serialize_financial_year_control_request(
            document,
            {str(actor["_id"]): actor["resolved_name"]},
        ),
        "message": f"{definition['label']} request draft created.",
    }


def update_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    reason,
    correction_response="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.edit",
    )

    if request_row.get("status") not in EDITABLE_REQUEST_STATUSES:
        raise ValueError("Only draft or returned lifecycle requests can be edited.")
    if str(request_row.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the AVPL Admin who created this request can edit it.")

    expected = _parse_expected_version(expected_version)
    current_version = int(request_row.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    financial_year = _get_financial_year(request_row["financial_year_id"])
    _validate_request_type_for_year(financial_year, request_row["request_type"])
    clean_reason = _clean_reason(reason)
    clean_response = _clean_optional_note(correction_response)
    if request_row.get("status") == STATUS_RETURNED and not clean_response:
        raise ValueError("Add a correction response before saving a returned request.")

    timestamp = now_utc()
    event = _workflow_event(
        "edited",
        actor,
        request_row.get("status"),
        request_row.get("status"),
        request_row.get("revision_number", 1),
        reason=clean_reason,
        note=clean_response,
        timestamp=timestamp,
    )
    result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": request_row.get("status"),
            "version": current_version,
            "active_slot": True,
        },
        {
            "$set": {
                "reason": clean_reason,
                "correction_response": clean_response,
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
            "This lifecycle request changed in another session. Refresh and try again."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_edited",
        updated,
        actor,
        request_row.get("status"),
        request_row.get("status"),
        remarks=clean_response or clean_reason,
        timestamp=timestamp,
    )
    return {
        "request": serialize_financial_year_control_request(updated),
        "message": f"{updated.get('request_label')} request updated.",
    }


def submit_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    submission_note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.submit",
    )

    previous_status = request_row.get("status")
    if previous_status not in EDITABLE_REQUEST_STATUSES:
        raise ValueError("Only draft or returned lifecycle requests can be submitted.")
    if str(request_row.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the request creator can submit this lifecycle request.")

    expected = _parse_expected_version(expected_version)
    current_version = int(request_row.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    financial_year = _get_financial_year(request_row["financial_year_id"])
    _validate_request_type_for_year(financial_year, request_row["request_type"])
    _assert_control_ready(financial_year, request_row["request_type"])

    clean_note = _clean_optional_note(submission_note)
    if previous_status == STATUS_RETURNED and not request_row.get("correction_response"):
        raise ValueError(
            "Save a correction response before resubmitting the returned request."
        )

    timestamp = now_utc()
    next_revision = int(request_row.get("revision_number") or 1)
    if previous_status == STATUS_RETURNED:
        next_revision += 1

    event = _workflow_event(
        "resubmitted" if previous_status == STATUS_RETURNED else "submitted",
        actor,
        previous_status,
        STATUS_PENDING_APPROVAL,
        next_revision,
        reason=request_row.get("reason"),
        note=clean_note,
        timestamp=timestamp,
    )
    result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": previous_status,
            "version": current_version,
            "active_slot": True,
        },
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "revision_number": next_revision,
                "submission_note": clean_note,
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_at": timestamp,
                "financial_year_version_at_submission": int(
                    financial_year.get("version") or 1
                ),
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": current_version + 1,
            },
            "$push": {"workflow_history": event},
            "$inc": {"submission_count": 1},
        },
    )
    if result.modified_count != 1:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_submitted",
        updated,
        actor,
        previous_status,
        STATUS_PENDING_APPROVAL,
        remarks=clean_note or request_row.get("reason"),
        timestamp=timestamp,
    )
    return {
        "request": serialize_financial_year_control_request(updated),
        "message": f"{updated.get('request_label')} request submitted to Super Admin.",
    }


def withdraw_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.withdraw",
    )

    if request_row.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending lifecycle request can be withdrawn.")
    if str(request_row.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the request creator can withdraw this request.")

    expected = _parse_expected_version(expected_version)
    current_version = int(request_row.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    clean_reason = _clean_reason(reason, "Withdrawal reason")
    financial_year = _get_financial_year(request_row["financial_year_id"])
    _validate_request_type_for_year(financial_year, request_row["request_type"])

    timestamp = now_utc()
    event = _workflow_event(
        "withdrawn_for_correction",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_DRAFT,
        request_row.get("revision_number", 1),
        reason=clean_reason,
        timestamp=timestamp,
    )
    result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": current_version,
            "active_slot": True,
        },
        {
            "$set": {
                "status": STATUS_DRAFT,
                "withdraw_reason": clean_reason,
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
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_withdrawn",
        updated,
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_DRAFT,
        remarks=clean_reason,
        timestamp=timestamp,
    )
    return {
        "request": serialize_financial_year_control_request(updated),
        "message": f"{updated.get('request_label')} request withdrawn to draft.",
    }


def cancel_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.cancel",
    )

    previous_status = request_row.get("status")
    if previous_status not in EDITABLE_REQUEST_STATUSES:
        raise ValueError(
            "Only a draft or returned lifecycle request can be cancelled. Withdraw a pending request first."
        )
    if str(request_row.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the request creator can cancel this request.")

    expected = _parse_expected_version(expected_version)
    current_version = int(request_row.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    clean_reason = _clean_reason(reason, "Cancellation reason")
    timestamp = now_utc()
    event = _workflow_event(
        "cancelled",
        actor,
        previous_status,
        STATUS_CANCELLED,
        request_row.get("revision_number", 1),
        reason=clean_reason,
        timestamp=timestamp,
    )
    result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": previous_status,
            "version": current_version,
            "active_slot": True,
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
                "active_slot": False,
                "cancel_reason": clean_reason,
                "cancelled_by": actor["_id"],
                "cancelled_by_str": str(actor["_id"]),
                "cancelled_at": timestamp,
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
            "This lifecycle request changed in another session. Refresh and try again."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_cancelled",
        updated,
        actor,
        previous_status,
        STATUS_CANCELLED,
        remarks=clean_reason,
        timestamp=timestamp,
    )
    return {
        "request": serialize_financial_year_control_request(updated),
        "message": f"{updated.get('request_label')} request cancelled without deleting its history.",
    }


# ---------------------------------------------------------------------------
# Checker actions and state application
# ---------------------------------------------------------------------------


def return_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.return",
    )

    if request_row.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending lifecycle request can be sent back.")
    if str(request_row.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The request maker cannot review their own request.")

    expected = _parse_expected_version(expected_version)
    current_version = int(request_row.get("version") or 1)
    if expected != current_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    clean_reason = _clean_reason(reason, "Correction reason")
    timestamp = now_utc()
    event = _workflow_event(
        "returned_for_correction",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_RETURNED,
        request_row.get("revision_number", 1),
        reason=clean_reason,
        timestamp=timestamp,
    )
    result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": current_version,
            "active_slot": True,
        },
        {
            "$set": {
                "status": STATUS_RETURNED,
                "return_reason": clean_reason,
                "correction_response": "",
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
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_returned",
        updated,
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_RETURNED,
        remarks=clean_reason,
        timestamp=timestamp,
    )
    return {
        "request": serialize_financial_year_control_request(updated),
        "message": f"{updated.get('request_label')} request sent back for correction.",
    }


def _transition_already_applied(financial_year, request_row):
    target_status = request_row.get("target_status")
    current_state = _financial_year_state(financial_year)
    return (
        current_state == target_status
        and str(financial_year.get("last_control_request_id") or "")
        == str(request_row.get("_id") or "")
    )


def _financial_year_transition_updates(request_row, actor, timestamp, approval_note):
    request_type = request_row["request_type"]
    reason = request_row.get("reason") or ""
    common = {
        "last_control_action": request_type,
        "last_control_request_id": request_row["_id"],
        "last_control_request_id_str": str(request_row["_id"]),
        "last_control_reason": reason,
        "last_control_approval_note": approval_note,
        "last_control_by": actor["_id"],
        "last_control_by_str": str(actor["_id"]),
        "last_control_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_at": timestamp,
    }

    if request_type == REQUEST_CLOSE:
        return {
            **common,
            "status": "closed",
            "is_open": False,
            "is_locked": False,
            "closed_by": actor["_id"],
            "closed_by_str": str(actor["_id"]),
            "closed_at": timestamp,
            "close_reason": reason,
        }
    if request_type == REQUEST_LOCK:
        return {
            **common,
            "status": "locked",
            "is_open": False,
            "is_locked": True,
            "locked_by": actor["_id"],
            "locked_by_str": str(actor["_id"]),
            "locked_at": timestamp,
            "lock_reason": reason,
        }
    if request_type == REQUEST_UNLOCK:
        return {
            **common,
            "status": "closed",
            "is_open": False,
            "is_locked": False,
            "unlocked_by": actor["_id"],
            "unlocked_by_str": str(actor["_id"]),
            "unlocked_at": timestamp,
            "unlock_reason": reason,
        }
    if request_type == REQUEST_REOPEN:
        return {
            **common,
            "status": "open",
            "is_open": True,
            "is_locked": False,
            "reopened_by": actor["_id"],
            "reopened_by_str": str(actor["_id"]),
            "reopened_at": timestamp,
            "reopen_reason": reason,
        }
    raise ValueError("Invalid lifecycle request type.")


def approve_financial_year_control_request(
    request_id,
    actor_user_id,
    expected_version,
    approval_note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    request_row = _get_control_request(request_id)
    _require_permission(
        actor,
        request_row["accounting_entity_id"],
        "accounting.financial_year.control.approve",
    )

    if request_row.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending lifecycle request can be approved.")
    if str(request_row.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The request maker cannot approve their own request.")

    expected = _parse_expected_version(expected_version)
    request_version = int(request_row.get("version") or 1)
    if expected != request_version:
        raise RuntimeError(
            "This lifecycle request changed in another session. Refresh and try again."
        )

    clean_approval_note = _clean_optional_note(approval_note)
    financial_year = _get_financial_year(request_row["financial_year_id"])

    if not _transition_already_applied(financial_year, request_row):
        _validate_request_type_for_year(financial_year, request_row["request_type"])
        _assert_control_ready(financial_year, request_row["request_type"])

        submitted_fy_version = int(
            request_row.get("financial_year_version_at_submission")
            or financial_year.get("version")
            or 1
        )
        current_fy_version = int(financial_year.get("version") or 1)
        if submitted_fy_version != current_fy_version:
            raise RuntimeError(
                "The Financial Year changed after this request was submitted. "
                "Send the request back and resubmit it from the current state."
            )

        timestamp = now_utc()
        target_updates = _financial_year_transition_updates(
            request_row,
            actor,
            timestamp,
            clean_approval_note,
        )
        fy_event = _workflow_event(
            action=f"lifecycle_{request_row['request_type']}_approved",
            actor=actor,
            from_status=_financial_year_state(financial_year),
            to_status=request_row["target_status"],
            revision_number=financial_year.get("revision_number", 1),
            reason=request_row.get("reason"),
            note=clean_approval_note,
            timestamp=timestamp,
        )
        fy_result = mongo.db.financial_years.update_one(
            {
                "_id": financial_year["_id"],
                "version": current_fy_version,
                "status": financial_year.get("status"),
                "is_deleted": {"$ne": True},
            },
            {
                "$set": target_updates,
                "$push": {"workflow_history": fy_event},
                "$inc": {"version": 1},
            },
        )
        if fy_result.modified_count != 1:
            refreshed = _get_financial_year(financial_year["_id"])
            if not _transition_already_applied(refreshed, request_row):
                raise RuntimeError(
                    "The Financial Year changed during approval. Refresh and review the current state."
                )
            financial_year = refreshed
        else:
            financial_year = _get_financial_year(financial_year["_id"])
    else:
        timestamp = now_utc()

    event = _workflow_event(
        "approved_and_applied",
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_APPROVED,
        request_row.get("revision_number", 1),
        reason=request_row.get("reason"),
        note=clean_approval_note,
        timestamp=timestamp,
    )
    request_result = mongo.db.financial_year_control_requests.update_one(
        {
            "_id": request_row["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": request_version,
            "active_slot": True,
        },
        {
            "$set": {
                "status": STATUS_APPROVED,
                "active_slot": False,
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_at": timestamp,
                "applied_at": timestamp,
                "approval_note": clean_approval_note,
                "applied_financial_year_version": int(
                    financial_year.get("version") or 1
                ),
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
                "version": request_version + 1,
                "apply_recovery_required": False,
            },
            "$push": {"workflow_history": event},
        },
    )

    if request_result.modified_count != 1:
        refreshed_request = _get_control_request(request_row["_id"])
        if refreshed_request.get("status") == STATUS_APPROVED:
            return {
                "request": serialize_financial_year_control_request(refreshed_request),
                "message": (
                    f"{refreshed_request.get('request_label')} was already approved "
                    "and applied safely."
                ),
            }

        # The Financial Year transition may already be applied. Keep a clear
        # recovery marker so the same request can be retried safely instead of
        # creating a second lifecycle request.
        mongo.db.financial_year_control_requests.update_one(
            {"_id": request_row["_id"], "status": STATUS_PENDING_APPROVAL},
            {
                "$set": {
                    "apply_recovery_required": True,
                    "apply_recovery_message": (
                        "Financial Year state was applied but request finalization needs retry."
                    ),
                    "apply_recovery_updated_at": now_utc(),
                }
            },
        )
        raise RuntimeError(
            "The Financial Year state was applied, but the request record needs recovery. "
            "Refresh and approve the same request again; no second transition will be created."
        )

    updated = _get_control_request(request_row["_id"])
    _write_audit(
        "financial_year_control_approved",
        updated,
        actor,
        STATUS_PENDING_APPROVAL,
        STATUS_APPROVED,
        remarks=clean_approval_note or request_row.get("reason"),
        metadata={
            "applied_financial_year_status": _financial_year_state(financial_year),
            "applied_financial_year_version": financial_year.get("version"),
        },
        timestamp=timestamp,
    )

    return {
        "request": serialize_financial_year_control_request(updated),
        "message": (
            f"{updated.get('request_label')} approved. "
            f"{updated.get('financial_year_name')} is now {updated.get('target_status')}."
        ),
    }
