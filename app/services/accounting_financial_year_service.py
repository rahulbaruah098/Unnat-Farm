from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_permission_service import get_accounting_access
from app.utils.helpers import now_utc


STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_OPEN = "open"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
ACTIVE_STATUSES = {
    STATUS_DRAFT,
    STATUS_PENDING_APPROVAL,
    STATUS_RETURNED,
    STATUS_OPEN,
}


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in keys]


def _ensure_exact_index(collection, keys, name, **options):
    """Create an Accounting index without dropping any existing index."""
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


def ensure_financial_year_indexes():
    """Install the Financial Year indexes safely and idempotently."""
    _ensure_exact_index(
        mongo.db.financial_years,
        [
            ("accounting_entity_id", ASCENDING),
            ("start_date", ASCENDING),
            ("end_date", ASCENDING),
        ],
        name="financial_year_entity_period_unique",
        unique=True,
        partialFilterExpression={"is_deleted": False},
    )
    _ensure_exact_index(
        mongo.db.financial_years,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("start_date", DESCENDING),
        ],
        name="financial_year_entity_status_start_idx",
    )
    _ensure_exact_index(
        mongo.db.financial_years,
        [("status", ASCENDING), ("submitted_at", ASCENDING)],
        name="financial_year_approval_queue_idx",
    )
    _ensure_exact_index(
        mongo.db.financial_years,
        [("created_by", ASCENDING), ("updated_at", DESCENDING)],
        name="financial_year_creator_updated_idx",
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
            "status": 1,
        },
    )

    if not actor:
        raise ValueError("Authenticated user was not found.")

    if actor.get("active", True) is False or actor.get("status") == "inactive":
        raise ValueError("Inactive users cannot perform Accounting actions.")

    role = str(actor.get("role") or "").strip().lower()

    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError("You are not authorized to perform this Financial Year action.")

    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )

    return actor




def _assert_actor_entity_access(actor, entity_id):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )

    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")

    if actor.get("resolved_role") == "super_admin":
        return access

    allowed_entity_ids = {str(value) for value in access.get("entity_ids") or []}
    if str(entity_id) not in allowed_entity_ids:
        raise PermissionError("You do not have access to this Accounting entity.")

    return access


@contextmanager
def _financial_year_period_lock(entity_id):
    """Serialize period creation/edit checks for one Accounting entity.

    MongoDB cannot enforce non-overlapping date ranges with a normal index. This
    short-lived entity lock prevents two concurrent requests from both passing
    the overlap check. A stale lock expires automatically after 30 seconds.
    """
    token = uuid4().hex
    timestamp = now_utc()
    stale_before = timestamp - timedelta(seconds=30)

    locked_entity = mongo.db.accounting_entities.find_one_and_update(
        {
            "_id": entity_id,
            "$or": [
                {"financial_year_period_lock": {"$exists": False}},
                {"financial_year_period_lock.acquired_at": {"$lt": stale_before}},
            ],
        },
        {
            "$set": {
                "financial_year_period_lock": {
                    "token": token,
                    "acquired_at": timestamp,
                }
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not locked_entity:
        raise RuntimeError(
            "Another Financial Year update is in progress. Please wait a moment and try again."
        )

    try:
        yield
    finally:
        mongo.db.accounting_entities.update_one(
            {
                "_id": entity_id,
                "financial_year_period_lock.token": token,
            },
            {"$unset": {"financial_year_period_lock": ""}},
        )


def _parse_date(value, field_label):
    if isinstance(value, datetime):
        parsed_date = value.date()
    elif isinstance(value, date):
        parsed_date = value
    else:
        raw_value = str(value or "").strip()
        if not raw_value:
            raise ValueError(f"{field_label} is required.")

        try:
            parsed_date = datetime.strptime(raw_value, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(
                f"{field_label} must use the YYYY-MM-DD format."
            ) from exc

    return datetime.combine(parsed_date, time.min)


def _validate_period(start_date, end_date):
    if end_date <= start_date:
        raise ValueError("Financial Year end date must be after the start date.")

    period_days = (end_date.date() - start_date.date()).days + 1

    if period_days > 366:
        raise ValueError("A Financial Year period cannot exceed 366 days.")


def _financial_year_identity(start_date, end_date):
    start_year = start_date.year
    end_year_short = str(end_date.year)[-2:]

    return {
        "fy_code": f"FY{start_year}-{end_year_short}",
        "display_name": f"FY {start_year}-{end_year_short}",
    }


def get_default_financial_year_values(reference_date=None):
    reference = reference_date or date.today()

    if isinstance(reference, datetime):
        reference = reference.date()

    start_year = reference.year if reference.month >= 4 else reference.year - 1
    start_date = date(start_year, 4, 1)
    end_date = date(start_year + 1, 3, 31)
    identity = _financial_year_identity(
        datetime.combine(start_date, time.min),
        datetime.combine(end_date, time.min),
    )

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        **identity,
    }


def _assert_entity(entity_id):
    entity_object_id = _to_object_id(entity_id)

    if not entity_object_id:
        raise ValueError("Invalid Accounting entity.")

    entity = mongo.db.accounting_entities.find_one({
        "_id": entity_object_id,
        "is_deleted": {"$ne": True},
        "status": "active",
        "accounting_enabled": {"$ne": False},
    })

    if not entity:
        raise ValueError("The Accounting entity is not active or was not found.")

    return entity


def _assert_no_overlap(entity_object_id, start_date, end_date, exclude_id=None):
    query = {
        "accounting_entity_id": entity_object_id,
        "is_deleted": {"$ne": True},
        "start_date": {"$lte": end_date},
        "end_date": {"$gte": start_date},
    }

    if exclude_id:
        query["_id"] = {"$ne": exclude_id}

    overlapping = mongo.db.financial_years.find_one(
        query,
        {"display_name": 1, "start_date": 1, "end_date": 1, "status": 1},
    )

    if overlapping:
        name = overlapping.get("display_name") or "another Financial Year"
        raise ValueError(
            f"The selected dates overlap with {name}. Overlapping Financial Years are not allowed."
        )


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
    financial_year,
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
        "accounting_entity_id": financial_year["accounting_entity_id"],
        "accounting_entity_id_str": str(financial_year["accounting_entity_id"]),
        "entity_type": "financial_year",
        "entity_id": financial_year["_id"],
        "entity_id_str": str(financial_year["_id"]),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "metadata": {
            "fy_code": financial_year.get("fy_code"),
            "display_name": financial_year.get("display_name"),
            "start_date": financial_year.get("start_date"),
            "end_date": financial_year.get("end_date"),
            "revision_number": financial_year.get("revision_number", 1),
            **(metadata or {}),
        },
        "remarks": str(remarks or "").strip(),
        "created_at": timestamp,
    }

    try:
        mongo.db.accounting_audit_logs.insert_one(audit_document)
    except Exception as exc:
        # The workflow update has already succeeded. Mark the record for a later
        # audit recovery job instead of encouraging a duplicate user retry.
        mongo.db.financial_years.update_one(
            {"_id": financial_year["_id"]},
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

    mongo.db.financial_years.update_one(
        {"_id": financial_year["_id"]},
        {
            "$set": {
                "audit_sync_status": "synced",
                "audit_sync_updated_at": timestamp,
            }
        },
    )
    return True


def _user_name_map(financial_years):
    user_ids = set()

    for row in financial_years:
        for key in (
            "created_by",
            "submitted_by",
            "approved_by",
            "returned_by",
            "withdrawn_by",
            "updated_by",
        ):
            value = _to_object_id(row.get(key))
            if value:
                user_ids.add(value)

    if not user_ids:
        return {}

    users = mongo.db.users.find(
        {"_id": {"$in": list(user_ids)}},
        {"name": 1, "full_name": 1, "username": 1, "phone": 1, "role": 1},
    )

    result = {}
    for user in users:
        result[str(user["_id"])] = (
            user.get("name")
            or user.get("full_name")
            or user.get("username")
            or user.get("phone")
            or str(user.get("role") or "User").replace("_", " ").title()
        )

    return result


def serialize_financial_year(financial_year, user_names=None):
    if not financial_year:
        return None

    user_names = user_names or {}

    def user_name(key):
        value = financial_year.get(key)
        return user_names.get(str(value), "") if value else ""

    start_date = financial_year.get("start_date")
    end_date = financial_year.get("end_date")
    status = financial_year.get("status") or STATUS_DRAFT

    return {
        "id": str(financial_year.get("_id")),
        "accounting_entity_id": str(financial_year.get("accounting_entity_id")),
        "fy_code": financial_year.get("fy_code") or "",
        "display_name": financial_year.get("display_name") or "",
        "start_date": start_date,
        "end_date": end_date,
        "start_date_input": start_date.strftime("%Y-%m-%d") if start_date else "",
        "end_date_input": end_date.strftime("%Y-%m-%d") if end_date else "",
        "start_date_display": start_date.strftime("%d %b %Y") if start_date else "",
        "end_date_display": end_date.strftime("%d %b %Y") if end_date else "",
        "status": status,
        "status_display": status.replace("_", " ").title(),
        "is_open": status == STATUS_OPEN and financial_year.get("is_open") is True,
        "is_locked": financial_year.get("is_locked", False) is True,
        "usable_for_posting": (
            status == STATUS_OPEN
            and financial_year.get("is_open") is True
            and financial_year.get("is_locked", False) is not True
        ),
        "revision_number": int(financial_year.get("revision_number") or 1),
        "version": int(financial_year.get("version") or 1),
        "created_by": str(financial_year.get("created_by") or ""),
        "created_by_name": user_name("created_by"),
        "submitted_by_name": user_name("submitted_by"),
        "approved_by_name": user_name("approved_by"),
        "returned_by_name": user_name("returned_by"),
        "withdrawn_by_name": user_name("withdrawn_by"),
        "return_reason": financial_year.get("return_reason") or "",
        "withdraw_reason": financial_year.get("withdraw_reason") or "",
        "last_submission_note": financial_year.get("last_submission_note") or "",
        "created_at": financial_year.get("created_at"),
        "updated_at": financial_year.get("updated_at"),
        "submitted_at": financial_year.get("submitted_at"),
        "approved_at": financial_year.get("approved_at"),
        "returned_at": financial_year.get("returned_at"),
        "workflow_history": financial_year.get("workflow_history") or [],
    }


def list_financial_years(entity_id):
    entity_object_id = _to_object_id(entity_id)
    if not entity_object_id:
        return []

    rows = list(
        mongo.db.financial_years.find({
            "accounting_entity_id": entity_object_id,
            "is_deleted": {"$ne": True},
        }).sort([
            ("start_date", DESCENDING),
            ("created_at", DESCENDING),
        ])
    )

    user_names = _user_name_map(rows)
    return [serialize_financial_year(row, user_names) for row in rows]


def create_financial_year(entity_id, actor_user_id, start_date, end_date):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    entity = _assert_entity(entity_id)
    _assert_actor_entity_access(actor, entity["_id"])
    ensure_financial_year_indexes()

    parsed_start = _parse_date(start_date, "Start date")
    parsed_end = _parse_date(end_date, "End date")
    _validate_period(parsed_start, parsed_end)

    identity = _financial_year_identity(parsed_start, parsed_end)
    timestamp = now_utc()
    workflow_event = _workflow_event(
        action="created",
        actor=actor,
        from_status=None,
        to_status=STATUS_DRAFT,
        revision_number=1,
        timestamp=timestamp,
    )

    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        **identity,
        "start_date": parsed_start,
        "end_date": parsed_end,
        "status": STATUS_DRAFT,
        "is_open": False,
        "is_locked": False,
        "is_deleted": False,
        "revision_number": 1,
        "submission_count": 0,
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "workflow_history": [workflow_event],
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    with _financial_year_period_lock(entity["_id"]):
        _assert_no_overlap(entity["_id"], parsed_start, parsed_end)

        try:
            result = mongo.db.financial_years.insert_one(document)
        except DuplicateKeyError as exc:
            raise ValueError("This Financial Year period already exists.") from exc

    document["_id"] = result.inserted_id
    _write_audit(
        action="financial_year_created",
        financial_year=document,
        actor=actor,
        previous_status=None,
        new_status=STATUS_DRAFT,
        remarks="Financial Year draft created by AVPL Admin.",
        timestamp=timestamp,
    )

    return serialize_financial_year(
        document,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def update_financial_year(
    financial_year_id,
    actor_user_id,
    start_date,
    end_date,
    expected_version,
    correction_note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    financial_year_object_id = _to_object_id(financial_year_id)

    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    current = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "is_deleted": {"$ne": True},
    })

    if not current:
        raise ValueError("Financial Year was not found.")

    _assert_actor_entity_access(actor, current["accounting_entity_id"])

    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned Financial Years can be edited.")

    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the AVPL Admin who created this Financial Year can edit it.")

    try:
        expected_version = int(expected_version)
    except Exception as exc:
        raise ValueError("The Financial Year version is missing. Refresh and try again.") from exc

    parsed_start = _parse_date(start_date, "Start date")
    parsed_end = _parse_date(end_date, "End date")
    _validate_period(parsed_start, parsed_end)
    identity = _financial_year_identity(parsed_start, parsed_end)
    timestamp = now_utc()
    event = _workflow_event(
        action="edited",
        actor=actor,
        from_status=current.get("status"),
        to_status=current.get("status"),
        revision_number=current.get("revision_number", 1),
        note=correction_note,
        timestamp=timestamp,
    )

    with _financial_year_period_lock(current["accounting_entity_id"]):
        _assert_no_overlap(
            current["accounting_entity_id"],
            parsed_start,
            parsed_end,
            exclude_id=current["_id"],
        )

        result = mongo.db.financial_years.update_one(
            {
                "_id": current["_id"],
                "status": current.get("status"),
                "version": expected_version,
                "is_deleted": {"$ne": True},
            },
            {
                "$set": {
                    **identity,
                    "start_date": parsed_start,
                    "end_date": parsed_end,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "last_correction_note": str(correction_note or "").strip(),
                    "updated_at": timestamp,
                },
                "$push": {"workflow_history": event},
                "$inc": {"version": 1},
            },
        )

    if result.modified_count != 1:
        raise RuntimeError(
            "This Financial Year changed in another session. Refresh the page and try again."
        )

    updated = mongo.db.financial_years.find_one({"_id": current["_id"]})
    _write_audit(
        action="financial_year_edited",
        financial_year=updated,
        actor=actor,
        previous_status=current.get("status"),
        new_status=current.get("status"),
        remarks=str(correction_note or "Financial Year draft updated.").strip(),
        timestamp=timestamp,
    )

    return serialize_financial_year(
        updated,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def submit_financial_year(
    financial_year_id,
    actor_user_id,
    expected_version,
    submission_note="",
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    financial_year_object_id = _to_object_id(financial_year_id)

    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    current = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "is_deleted": {"$ne": True},
    })

    if not current:
        raise ValueError("Financial Year was not found.")

    _assert_actor_entity_access(actor, current["accounting_entity_id"])

    previous_status = current.get("status")
    if previous_status not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned Financial Years can be submitted.")

    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the creator can submit this Financial Year.")

    submission_note = str(submission_note or "").strip()
    if previous_status == STATUS_RETURNED and not submission_note:
        raise ValueError("Add a correction response before resubmitting this Financial Year.")

    try:
        expected_version = int(expected_version)
    except Exception as exc:
        raise ValueError("The Financial Year version is missing. Refresh and try again.") from exc

    timestamp = now_utc()
    next_revision = int(current.get("revision_number") or 1)
    if previous_status == STATUS_RETURNED:
        next_revision += 1

    event = _workflow_event(
        action="resubmitted" if previous_status == STATUS_RETURNED else "submitted",
        actor=actor,
        from_status=previous_status,
        to_status=STATUS_PENDING_APPROVAL,
        revision_number=next_revision,
        note=submission_note,
        timestamp=timestamp,
    )

    result = mongo.db.financial_years.update_one(
        {
            "_id": current["_id"],
            "status": previous_status,
            "version": expected_version,
            "is_deleted": {"$ne": True},
        },
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "is_open": False,
                "revision_number": next_revision,
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_at": timestamp,
                "last_submission_note": submission_note,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
            },
            "$push": {"workflow_history": event},
            "$inc": {"version": 1, "submission_count": 1},
        },
    )

    if result.modified_count != 1:
        raise RuntimeError(
            "This Financial Year changed in another session. Refresh the page and try again."
        )

    updated = mongo.db.financial_years.find_one({"_id": current["_id"]})
    _write_audit(
        action=(
            "financial_year_resubmitted"
            if previous_status == STATUS_RETURNED
            else "financial_year_submitted"
        ),
        financial_year=updated,
        actor=actor,
        previous_status=previous_status,
        new_status=STATUS_PENDING_APPROVAL,
        remarks=submission_note or "Financial Year submitted for Super Admin approval.",
        timestamp=timestamp,
    )

    return serialize_financial_year(
        updated,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def withdraw_financial_year(
    financial_year_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"avpl_admin"})
    financial_year_object_id = _to_object_id(financial_year_id)
    reason = str(reason or "").strip()

    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    if not reason:
        raise ValueError("A withdrawal reason is required.")

    current = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "status": STATUS_PENDING_APPROVAL,
        "is_deleted": {"$ne": True},
    })

    if not current:
        raise ValueError("Only a pending Financial Year can be withdrawn for correction.")

    _assert_actor_entity_access(actor, current["accounting_entity_id"])

    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the creator can withdraw this Financial Year.")

    try:
        expected_version = int(expected_version)
    except Exception as exc:
        raise ValueError("The Financial Year version is missing. Refresh and try again.") from exc

    timestamp = now_utc()
    event = _workflow_event(
        action="withdrawn_for_correction",
        actor=actor,
        from_status=STATUS_PENDING_APPROVAL,
        to_status=STATUS_DRAFT,
        revision_number=current.get("revision_number", 1),
        reason=reason,
        timestamp=timestamp,
    )

    result = mongo.db.financial_years.update_one(
        {
            "_id": current["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": expected_version,
            "is_deleted": {"$ne": True},
        },
        {
            "$set": {
                "status": STATUS_DRAFT,
                "is_open": False,
                "withdrawn_by": actor["_id"],
                "withdrawn_by_str": str(actor["_id"]),
                "withdrawn_at": timestamp,
                "withdraw_reason": reason,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
            },
            "$push": {"workflow_history": event},
            "$inc": {"version": 1},
        },
    )

    if result.modified_count != 1:
        raise RuntimeError(
            "This Financial Year changed in another session. Refresh the page and try again."
        )

    updated = mongo.db.financial_years.find_one({"_id": current["_id"]})
    _write_audit(
        action="financial_year_withdrawn",
        financial_year=updated,
        actor=actor,
        previous_status=STATUS_PENDING_APPROVAL,
        new_status=STATUS_DRAFT,
        remarks=reason,
        timestamp=timestamp,
    )

    return serialize_financial_year(
        updated,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def approve_financial_year(financial_year_id, actor_user_id, expected_version):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    financial_year_object_id = _to_object_id(financial_year_id)

    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    current = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "status": STATUS_PENDING_APPROVAL,
        "is_deleted": {"$ne": True},
    })

    if not current:
        raise ValueError("Only a pending Financial Year can be approved.")

    _assert_actor_entity_access(actor, current["accounting_entity_id"])

    if str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The creator cannot approve their own Financial Year.")

    try:
        expected_version = int(expected_version)
    except Exception as exc:
        raise ValueError("The Financial Year version is missing. Refresh and try again.") from exc

    timestamp = now_utc()
    event = _workflow_event(
        action="approved_and_opened",
        actor=actor,
        from_status=STATUS_PENDING_APPROVAL,
        to_status=STATUS_OPEN,
        revision_number=current.get("revision_number", 1),
        timestamp=timestamp,
    )

    result = mongo.db.financial_years.update_one(
        {
            "_id": current["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": expected_version,
            "is_deleted": {"$ne": True},
        },
        {
            "$set": {
                "status": STATUS_OPEN,
                "is_open": True,
                "is_locked": False,
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_at": timestamp,
                "opened_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
            },
            "$push": {"workflow_history": event},
            "$inc": {"version": 1},
        },
    )

    if result.modified_count != 1:
        raise RuntimeError(
            "This Financial Year changed in another session. Refresh the page and try again."
        )

    updated = mongo.db.financial_years.find_one({"_id": current["_id"]})
    _write_audit(
        action="financial_year_approved",
        financial_year=updated,
        actor=actor,
        previous_status=STATUS_PENDING_APPROVAL,
        new_status=STATUS_OPEN,
        remarks="Financial Year approved and opened by Super Admin.",
        timestamp=timestamp,
    )

    return serialize_financial_year(
        updated,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def return_financial_year(
    financial_year_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    financial_year_object_id = _to_object_id(financial_year_id)
    reason = str(reason or "").strip()

    if not financial_year_object_id:
        raise ValueError("Invalid Financial Year.")

    if not reason:
        raise ValueError("A correction reason is required before sending it back.")

    current = mongo.db.financial_years.find_one({
        "_id": financial_year_object_id,
        "status": STATUS_PENDING_APPROVAL,
        "is_deleted": {"$ne": True},
    })

    if not current:
        raise ValueError("Only a pending Financial Year can be sent back.")

    _assert_actor_entity_access(actor, current["accounting_entity_id"])

    if str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The creator cannot review their own Financial Year.")

    try:
        expected_version = int(expected_version)
    except Exception as exc:
        raise ValueError("The Financial Year version is missing. Refresh and try again.") from exc

    timestamp = now_utc()
    event = _workflow_event(
        action="returned_for_correction",
        actor=actor,
        from_status=STATUS_PENDING_APPROVAL,
        to_status=STATUS_RETURNED,
        revision_number=current.get("revision_number", 1),
        reason=reason,
        timestamp=timestamp,
    )

    result = mongo.db.financial_years.update_one(
        {
            "_id": current["_id"],
            "status": STATUS_PENDING_APPROVAL,
            "version": expected_version,
            "is_deleted": {"$ne": True},
        },
        {
            "$set": {
                "status": STATUS_RETURNED,
                "is_open": False,
                "returned_by": actor["_id"],
                "returned_by_str": str(actor["_id"]),
                "returned_at": timestamp,
                "return_reason": reason,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_at": timestamp,
            },
            "$push": {"workflow_history": event},
            "$inc": {"version": 1},
        },
    )

    if result.modified_count != 1:
        raise RuntimeError(
            "This Financial Year changed in another session. Refresh the page and try again."
        )

    updated = mongo.db.financial_years.find_one({"_id": current["_id"]})
    _write_audit(
        action="financial_year_returned",
        financial_year=updated,
        actor=actor,
        previous_status=STATUS_PENDING_APPROVAL,
        new_status=STATUS_RETURNED,
        remarks=reason,
        timestamp=timestamp,
    )

    return serialize_financial_year(
        updated,
        {str(actor["_id"]): actor["resolved_name"]},
    )


def get_open_financial_year_for_date(entity_id, transaction_date=None):
    """Return an approved, unlocked Financial Year for future posting services."""
    entity_object_id = _to_object_id(entity_id)
    if not entity_object_id:
        return None

    target_date = _parse_date(
        transaction_date or date.today(),
        "Transaction date",
    )

    return mongo.db.financial_years.find_one({
        "accounting_entity_id": entity_object_id,
        "status": STATUS_OPEN,
        "is_open": True,
        "is_locked": {"$ne": True},
        "is_deleted": {"$ne": True},
        "start_date": {"$lte": target_date},
        "end_date": {"$gte": target_date},
    })
