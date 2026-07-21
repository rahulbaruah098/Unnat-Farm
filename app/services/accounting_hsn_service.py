from datetime import date
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_gst_tax_service import (
    GST_RATE_COLLECTION,
    GST_TAXABILITY_COLLECTION,
)
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.utils.helpers import now_utc


AVPL_ENTITY_CODE = "AVPL"
HSN_COLLECTION = "hsn_masters"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_CANCELLED = "cancelled"

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending approval",
    STATUS_RETURNED: "Returned for correction",
    STATUS_ACTIVE: "Active",
    STATUS_INACTIVE: "Inactive",
    STATUS_CANCELLED: "Cancelled",
}

VIEW_PERMISSION = "accounting.hsn.view"
CREATE_PERMISSION = "accounting.hsn.create"
EDIT_PERMISSION = "accounting.hsn.edit"
SUBMIT_PERMISSION = "accounting.hsn.submit"
WITHDRAW_PERMISSION = "accounting.hsn.withdraw"
CANCEL_PERMISSION = "accounting.hsn.cancel"
APPROVE_PERMISSION = "accounting.hsn.approve"
RETURN_PERMISSION = "accounting.hsn.return"
DEACTIVATE_PERMISSION = "accounting.hsn.deactivate"
REACTIVATE_PERMISSION = "accounting.hsn.reactivate"

HSN_PATTERN = re.compile(r"^(?:[0-9]{4}|[0-9]{6}|[0-9]{8})$")
ALLOWED_TAXABILITY_CODES = {"TAXABLE", "EXEMPT", "NIL_RATED", "NON_GST"}


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
        raise RuntimeError(f"Could not inspect indexes for {collection.name}.") from exc
    for existing_name, metadata in index_info.items():
        if existing_name == "_id_":
            continue
        existing_keys = _normalized_keys(metadata.get("key", []))
        if existing_name != name and existing_keys != required_keys:
            continue
        if (
            existing_keys == required_keys
            and bool(metadata.get("unique", False)) == required_unique
            and metadata.get("partialFilterExpression") == required_partial
        ):
            return existing_name
        raise RuntimeError(
            f"Conflicting index detected on {collection.name}: {existing_name}. "
            "No index was dropped automatically."
        )
    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure as exc:
        raise RuntimeError(f"Could not create Accounting index {name} on {collection.name}.") from exc


def ensure_hsn_indexes():
    collection = mongo.db[HSN_COLLECTION]
    _ensure_exact_index(
        collection,
        [("live_hsn_key", ASCENDING)],
        name="hsn_master_live_key_unique",
        unique=True,
        partialFilterExpression={"live_hsn_key": {"$type": "string"}},
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("status", ASCENDING), ("hsn_code", ASCENDING)],
        name="hsn_master_entity_status_code_idx",
    )
    _ensure_exact_index(
        collection,
        [("accounting_entity_id", ASCENDING), ("taxability_code", ASCENDING), ("gst_rate_code", ASCENDING)],
        name="hsn_master_entity_taxability_rate_idx",
    )
    _ensure_exact_index(
        collection,
        [("updated_at", DESCENDING)],
        name="hsn_master_updated_idx",
    )


def _clean_text(value, label, maximum=240, required=True):
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _clean_multiline(value, label, maximum=1200, required=False):
    text = str(value or "").strip()
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _normalize_hsn_code(value):
    code = re.sub(r"\s+", "", str(value or "")).strip()
    if not HSN_PATTERN.fullmatch(code):
        raise ValueError("HSN code must contain exactly 4, 6 or 8 digits.")
    chapter = int(code[:2])
    if chapter < 1 or chapter > 98:
        raise ValueError("HSN chapter must be between 01 and 98. Chapter 99 is reserved for services/SAC.")
    return code


def _get_actor(actor_user_id, allowed_roles=None):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one(
        {"_id": actor_id},
        {"name": 1, "full_name": 1, "username": 1, "phone": 1, "role": 1, "active": 1, "is_active": 1, "status": 1},
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")
    if actor.get("active", True) is False or actor.get("is_active", True) is False or actor.get("status") == "inactive":
        raise PermissionError("Inactive users cannot perform Accounting actions.")
    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in set(allowed_roles):
        raise PermissionError("You are not authorized to perform this HSN-master action.")
    actor["resolved_role"] = role
    actor["resolved_name"] = actor.get("name") or actor.get("full_name") or actor.get("username") or actor.get("phone") or role.replace("_", " ").title()
    return actor


def _assert_active_avpl_entity(entity_id=None):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    if entity_id:
        object_id = _to_object_id(entity_id)
        if not object_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = object_id
    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is not available.")
    return entity


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")
    if actor.get("resolved_role") != "super_admin":
        entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")
    if not has_accounting_permission(access, permission):
        raise PermissionError("Your Accounting access mapping does not allow this HSN-master action.")
    return access


def _change_event(action, actor, previous_status=None, new_status=None, remarks="", changed_fields=None):
    return {
        "event_id": str(uuid4()),
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "remarks": remarks or "",
        "changed_fields": sorted(set(changed_fields or [])),
        "created_at": now_utc(),
    }


def _record_audit(document, actor, action, previous_status=None, remarks="", changed_fields=None):
    timestamp = now_utc()
    audit = {
        "event_id": str(uuid4()),
        "accounting_entity_id": document.get("accounting_entity_id"),
        "entity_code": document.get("entity_code") or AVPL_ENTITY_CODE,
        "module": "hsn_master",
        "resource_type": "hsn_master",
        "resource_id": document.get("_id"),
        "resource_id_str": str(document.get("_id") or ""),
        "resource_code": document.get("hsn_code") or "",
        "action": action,
        "previous_status": previous_status,
        "new_status": document.get("status"),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or actor.get("role") or "",
        "remarks": remarks or "",
        "changed_fields": sorted(set(changed_fields or [])),
        "created_at": timestamp,
    }
    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
        mongo.db[HSN_COLLECTION].update_one(
            {"_id": document["_id"]},
            {"$set": {"audit_sync_required": False, "last_audit_event_id": audit["event_id"], "last_audited_at": timestamp}, "$unset": {"audit_sync_error": ""}},
        )
    except Exception as exc:
        mongo.db[HSN_COLLECTION].update_one(
            {"_id": document["_id"]},
            {"$set": {"audit_sync_required": True, "audit_sync_error": str(exc)[:500], "audit_recovery_required_at": timestamp}},
        )


def _taxability_catalog(entity_id):
    rows = mongo.db[GST_TAXABILITY_COLLECTION].find(
        {"accounting_entity_id": entity_id, "is_active": True, "is_deleted": False}
    ).sort("sort_order", ASCENDING)
    return {
        row.get("taxability_code"): {
            "code": row.get("taxability_code") or "",
            "name": row.get("name") or "",
            "requires_tax_rate": row.get("requires_tax_rate") is True,
        }
        for row in rows
        if row.get("taxability_code")
    }


def _rate_catalog(entity_id):
    rows = list(
        mongo.db[GST_RATE_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "status": "active",
                "is_active": True,
                "is_deleted": False,
            }
        ).sort([("rate_code", ASCENDING), ("effective_from", DESCENDING)])
    )
    by_code = {}
    for row in rows:
        code = row.get("rate_code")
        if code and code not in by_code:
            by_code[code] = {
                "rate_code": code,
                "name": row.get("name") or code,
                "total_rate": str(row.get("total_rate").to_decimal() if hasattr(row.get("total_rate"), "to_decimal") else row.get("total_rate") or 0),
                "effective_from": row.get("effective_from"),
                "effective_to": row.get("effective_to"),
            }
    return by_code


def _payload(entity_id, form):
    hsn_code = _normalize_hsn_code(form.get("hsn_code"))
    description = _clean_text(form.get("description"), "HSN description", maximum=300)
    taxability_code = str(form.get("taxability_code") or "").strip().upper()
    if taxability_code not in ALLOWED_TAXABILITY_CODES:
        raise ValueError("Select a valid GST taxability classification.")
    taxability = _taxability_catalog(entity_id).get(taxability_code)
    if not taxability:
        raise ValueError("The selected GST taxability master is not active. Initialize the GST foundation first.")
    gst_rate_code = str(form.get("gst_rate_code") or "").strip().upper()
    if taxability.get("requires_tax_rate"):
        if not gst_rate_code:
            raise ValueError("A taxable HSN master must reference an approved GST rate code.")
        if gst_rate_code not in _rate_catalog(entity_id):
            raise ValueError("The selected GST rate code has no approved active rate period.")
    else:
        gst_rate_code = ""
    return {
        "hsn_code": hsn_code,
        "chapter_code": hsn_code[:2],
        "description": description,
        "taxability_code": taxability_code,
        "taxability_name": taxability.get("name") or taxability_code,
        "gst_rate_code": gst_rate_code,
        "source_reference": _clean_text(form.get("source_reference"), "Source reference", maximum=300, required=False),
        "notes": _clean_multiline(form.get("notes"), "Notes", maximum=1000),
        "classification_type": "goods_hsn",
    }


def serialize_hsn_master(document):
    return {
        "id": str(document.get("_id") or ""),
        "hsn_code": document.get("hsn_code") or "",
        "chapter_code": document.get("chapter_code") or "",
        "description": document.get("description") or "",
        "taxability_code": document.get("taxability_code") or "",
        "taxability_name": document.get("taxability_name") or document.get("taxability_code") or "",
        "gst_rate_code": document.get("gst_rate_code") or "",
        "source_reference": document.get("source_reference") or "",
        "notes": document.get("notes") or "",
        "status": document.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(document.get("status"), document.get("status") or "Draft"),
        "is_active": document.get("is_active") is True,
        "version": int(document.get("version") or 1),
        "created_by": str(document.get("created_by") or ""),
        "created_by_name": document.get("created_by_name") or "",
        "submitted_by_name": document.get("submitted_by_name") or "",
        "approved_by_name": document.get("approved_by_name") or "",
        "submission_note": document.get("submission_note") or "",
        "approval_note": document.get("approval_note") or "",
        "return_reason": document.get("return_reason") or "",
        "deactivation_reason": document.get("deactivation_reason") or "",
        "audit_sync_required": document.get("audit_sync_required") is True,
        "change_history": document.get("change_history") or [],
        "updated_at": document.get("updated_at"),
    }


def create_hsn_master(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], CREATE_PERMISSION)
    ensure_hsn_indexes()
    payload = _payload(entity["_id"], form)
    timestamp = now_utc()
    document = {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        **payload,
        "live_hsn_key": f"{entity['_id']}:{payload['hsn_code']}",
        "status": STATUS_DRAFT,
        "is_active": False,
        "is_deleted": False,
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "audit_sync_required": False,
        "change_history": [_change_event("create_hsn_master", actor, new_status=STATUS_DRAFT, changed_fields=list(payload))],
    }
    try:
        result = mongo.db[HSN_COLLECTION].insert_one(document)
    except DuplicateKeyError as exc:
        raise ValueError("A live HSN master with this code already exists for AVPL.") from exc
    document["_id"] = result.inserted_id
    _record_audit(document, actor, "create_hsn_master", changed_fields=list(payload))
    return {"hsn": serialize_hsn_master(document), "message": "HSN master draft created."}


def _get_hsn(hsn_id):
    object_id = _to_object_id(hsn_id)
    if not object_id:
        raise ValueError("Invalid HSN master.")
    row = mongo.db[HSN_COLLECTION].find_one({"_id": object_id, "is_deleted": {"$ne": True}})
    if not row:
        raise ValueError("HSN master was not found.")
    return row


def update_hsn_master(hsn_id, actor_user_id, expected_version, form):
    actor = _get_actor(actor_user_id, allowed_roles={"accounts"})
    current = _get_hsn(hsn_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], EDIT_PERMISSION)
    if current.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned HSN masters can be edited.")
    if str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can edit this HSN master.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid HSN master version.") from exc
    payload = _payload(entity["_id"], form)
    live_key = f"{entity['_id']}:{payload['hsn_code']}"
    changed = [key for key, value in payload.items() if current.get(key) != value]
    if current.get("live_hsn_key") != live_key:
        changed.append("hsn_code")
    try:
        result = mongo.db[HSN_COLLECTION].update_one(
            {"_id": current["_id"], "version": expected_version, "status": {"$in": list(EDITABLE_STATUSES)}},
            {
                "$set": {
                    **payload,
                    "live_hsn_key": live_key,
                    "version": expected_version + 1,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": now_utc(),
                },
                "$push": {"change_history": _change_event("update_hsn_master", actor, previous_status=current.get("status"), new_status=current.get("status"), changed_fields=changed)},
            },
        )
    except DuplicateKeyError as exc:
        raise ValueError("A live HSN master with this code already exists for AVPL.") from exc
    if result.matched_count != 1:
        raise RuntimeError("This HSN master changed. Refresh and try again.")
    updated = _get_hsn(hsn_id)
    _record_audit(updated, actor, "update_hsn_master", previous_status=current.get("status"), changed_fields=changed)
    return {"hsn": serialize_hsn_master(updated), "message": "HSN master draft updated."}


def _transition(hsn_id, actor_user_id, expected_version, action, permission, allowed_roles, source_statuses, target_status, reason="", note=""):
    actor = _get_actor(actor_user_id, allowed_roles=allowed_roles)
    current = _get_hsn(hsn_id)
    entity = _assert_active_avpl_entity(current.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], permission)
    if current.get("status") not in set(source_statuses):
        raise ValueError("This HSN master is not in a valid state for the requested action.")
    if action in {"submit_hsn_master", "withdraw_hsn_master", "cancel_hsn_master"} and str(current.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original Accounts maker can perform this action.")
    if action in {"approve_hsn_master", "return_hsn_master"} and str(current.get("created_by")) == str(actor["_id"]):
        raise PermissionError("The maker cannot approve or return their own HSN master.")
    try:
        expected_version = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid HSN master version.") from exc
    reason = _clean_multiline(reason, "Reason", maximum=1000, required=action in {"withdraw_hsn_master", "cancel_hsn_master", "return_hsn_master", "deactivate_hsn_master", "reactivate_hsn_master"})
    note = _clean_multiline(note, "Note", maximum=1000)
    updates = {
        "status": target_status,
        "is_active": target_status == STATUS_ACTIVE,
        "version": expected_version + 1,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": now_utc(),
    }
    if action in {"submit_hsn_master", "approve_hsn_master", "reactivate_hsn_master"}:
        # Revalidate the complete master against the currently active GST
        # taxability and rate-code catalogs before it can become usable.
        validated = _payload(entity["_id"], current)
        updates.update(validated)
    if action == "submit_hsn_master":
        updates.update({"submitted_by": actor["_id"], "submitted_by_name": actor.get("resolved_name") or "", "submitted_at": now_utc(), "submission_note": note, "return_reason": ""})
    elif action == "withdraw_hsn_master":
        updates.update({"withdraw_reason": reason, "withdrawn_at": now_utc()})
    elif action == "cancel_hsn_master":
        updates.update({"cancel_reason": reason, "cancelled_at": now_utc()})
    elif action == "approve_hsn_master":
        updates.update({"approved_by": actor["_id"], "approved_by_name": actor.get("resolved_name") or "", "approved_at": now_utc(), "approval_note": note, "return_reason": ""})
    elif action == "return_hsn_master":
        updates.update({"return_reason": reason, "returned_at": now_utc()})
    elif action == "deactivate_hsn_master":
        if mongo.db.product_accounting_mappings.count_documents({"accounting_entity_id": entity["_id"], "status": "active", "hsn_master_id": current["_id"]}) > 0:
            raise ValueError("This HSN master is used by active product Accounting mappings and cannot be deactivated.")
        updates.update({"deactivation_reason": reason, "deactivated_at": now_utc()})
    elif action == "reactivate_hsn_master":
        updates.update({"reactivation_reason": reason, "reactivated_at": now_utc()})
    update_doc = {
        "$set": updates,
        "$push": {"change_history": _change_event(action, actor, previous_status=current.get("status"), new_status=target_status, remarks=reason or note)},
    }
    if target_status == STATUS_CANCELLED:
        update_doc["$unset"] = {"live_hsn_key": ""}
    result = mongo.db[HSN_COLLECTION].update_one(
        {"_id": current["_id"], "version": expected_version, "status": {"$in": list(source_statuses)}},
        update_doc,
    )
    if result.matched_count != 1:
        raise RuntimeError("This HSN master changed. Refresh and try again.")
    updated = _get_hsn(hsn_id)
    _record_audit(updated, actor, action, previous_status=current.get("status"), remarks=reason or note)
    return {"hsn": serialize_hsn_master(updated), "message": f"HSN master {STATUS_LABELS[target_status].lower()}."}


def submit_hsn_master(hsn_id, actor_user_id, expected_version, submission_note=""):
    return _transition(hsn_id, actor_user_id, expected_version, "submit_hsn_master", SUBMIT_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_PENDING_APPROVAL, note=submission_note)


def withdraw_hsn_master(hsn_id, actor_user_id, expected_version, reason):
    return _transition(hsn_id, actor_user_id, expected_version, "withdraw_hsn_master", WITHDRAW_PERMISSION, {"accounts"}, {STATUS_PENDING_APPROVAL}, STATUS_DRAFT, reason=reason)


def cancel_hsn_master(hsn_id, actor_user_id, expected_version, reason):
    return _transition(hsn_id, actor_user_id, expected_version, "cancel_hsn_master", CANCEL_PERMISSION, {"accounts"}, EDITABLE_STATUSES, STATUS_CANCELLED, reason=reason)


def approve_hsn_master(hsn_id, actor_user_id, expected_version, approval_note=""):
    return _transition(hsn_id, actor_user_id, expected_version, "approve_hsn_master", APPROVE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_ACTIVE, note=approval_note)


def return_hsn_master(hsn_id, actor_user_id, expected_version, return_reason):
    return _transition(hsn_id, actor_user_id, expected_version, "return_hsn_master", RETURN_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_PENDING_APPROVAL}, STATUS_RETURNED, reason=return_reason)


def deactivate_hsn_master(hsn_id, actor_user_id, expected_version, reason):
    return _transition(hsn_id, actor_user_id, expected_version, "deactivate_hsn_master", DEACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_ACTIVE}, STATUS_INACTIVE, reason=reason)


def reactivate_hsn_master(hsn_id, actor_user_id, expected_version, reason):
    return _transition(hsn_id, actor_user_id, expected_version, "reactivate_hsn_master", REACTIVATE_PERMISSION, {"avpl_admin", "super_admin"}, {STATUS_INACTIVE}, STATUS_ACTIVE, reason=reason)


def get_hsn_option_catalog(accounting_entity_id=None):
    entity_id = _to_object_id(accounting_entity_id) if accounting_entity_id else None
    taxability = {}
    rates = {}
    active_hsn = []
    if entity_id:
        taxability = _taxability_catalog(entity_id)
        rates = _rate_catalog(entity_id)
        active_hsn = [
            serialize_hsn_master(row)
            for row in mongo.db[HSN_COLLECTION].find(
                {"accounting_entity_id": entity_id, "status": STATUS_ACTIVE, "is_active": True, "is_deleted": False}
            ).sort("hsn_code", ASCENDING)
        ]
    return {
        "status_labels": dict(STATUS_LABELS),
        "taxability": taxability,
        "rates": rates,
        "active_hsn": active_hsn,
        "today": date.today().isoformat(),
    }


def get_hsn_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)
    ensure_hsn_indexes()
    rows = list(
        mongo.db[HSN_COLLECTION].find(
            {"accounting_entity_id": entity["_id"], "is_deleted": {"$ne": True}}
        ).sort([("status", ASCENDING), ("hsn_code", ASCENDING)])
    )
    serialized = [serialize_hsn_master(row) for row in rows]
    counts = {status: 0 for status in STATUS_LABELS}
    taxability_counts = {code: 0 for code in ALLOWED_TAXABILITY_CODES}
    for row in rows:
        status = row.get("status") or STATUS_DRAFT
        counts[status] = counts.get(status, 0) + 1
        code = row.get("taxability_code") or ""
        if code:
            taxability_counts[code] = taxability_counts.get(code, 0) + 1
    return {
        "entity_id": str(entity["_id"]),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "entity_name": entity.get("name") or entity.get("legal_name") or "AVPL",
        "rows": serialized,
        "working_rows": [row for row in serialized if row["status"] in EDITABLE_STATUSES],
        "pending_rows": [row for row in serialized if row["status"] == STATUS_PENDING_APPROVAL],
        "active_rows": [row for row in serialized if row["status"] == STATUS_ACTIVE],
        "inactive_rows": [row for row in serialized if row["status"] == STATUS_INACTIVE],
        "cancelled_rows": [row for row in serialized if row["status"] == STATUS_CANCELLED],
        "counts": counts,
        "taxability_counts": taxability_counts,
        "total_count": len(rows),
        "audit_recovery_count": sum(1 for row in rows if row.get("audit_sync_required") is True),
        "options": get_hsn_option_catalog(entity["_id"]),
    }


def get_active_hsn_master_for_mapping(accounting_entity_id, hsn_master_id):
    entity_id = _to_object_id(accounting_entity_id)
    hsn_id = _to_object_id(hsn_master_id)
    if not entity_id or not hsn_id:
        raise ValueError("Invalid Accounting entity or HSN master.")
    row = mongo.db[HSN_COLLECTION].find_one(
        {"_id": hsn_id, "accounting_entity_id": entity_id, "status": STATUS_ACTIVE, "is_active": True, "is_deleted": False}
    )
    if not row:
        raise ValueError("The selected HSN master is not active for product Accounting mapping.")
    return row
