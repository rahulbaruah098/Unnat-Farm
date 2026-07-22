
"""Controlled recovery for interrupted voucher posting.

Recovery never edits official amounts, reuses a number, deletes voucher lines,
or creates a second voucher. It resumes the same idempotent posting workflow.
"""
from datetime import timedelta

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING

from app.extensions import mongo
from app.services.accounting_voucher_posting_service import (
    POSTING_STATE_COMPLETED,
    POSTING_STATE_RECOVERY_REQUIRED,
    ensure_voucher_posting_indexes,
    post_voucher_draft,
)
from app.services.accounting_voucher_service import (
    STATUS_DRAFT,
    STATUS_POSTED,
    VOUCHER_COLLECTION,
    VOUCHER_LINE_COLLECTION,
    _assert_active_avpl_entity,
    _get_actor,
    _get_voucher,
    _record_audit,
    _require_permission,
    serialize_voucher,
)
from app.utils.helpers import now_utc


RECOVERY_PERMISSION = "accounting.voucher.recovery"
STALE_LOCK_MINUTES = 10


def _object_id(value):
    try:
        return ObjectId(str(value))
    except Exception as exc:
        raise ValueError("Voucher ID is invalid.") from exc


def ensure_voucher_recovery_indexes():
    collection = mongo.db[VOUCHER_COLLECTION]
    names = []
    names.append(
        collection.create_index(
            [
                ("accounting_entity_id", ASCENDING),
                ("posting_state", ASCENDING),
                ("posting_progress_updated_at", DESCENDING),
            ],
            name="accounting_voucher_recovery_queue_idx",
        )
    )
    names.append(
        collection.create_index(
            [("posting_lock_expires_at", ASCENDING)],
            name="accounting_voucher_stale_lock_idx",
        )
    )
    return names


def _is_stale_lock(voucher, now=None):
    now = now or now_utc()
    expires_at = voucher.get("posting_lock_expires_at")
    if not voucher.get("posting_lock_token"):
        return False
    if expires_at:
        return expires_at <= now
    acquired = voucher.get("posting_lock_acquired_at")
    return bool(acquired and acquired <= now - timedelta(minutes=STALE_LOCK_MINUTES))


def get_voucher_recovery_overview(accounting_entity_id, actor_user_id, limit=50):
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], RECOVERY_PERMISSION)
    ensure_voucher_recovery_indexes()
    ensure_voucher_posting_indexes()

    now = now_utc()
    rows = list(
        mongo.db[VOUCHER_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "status": STATUS_DRAFT,
                "$or": [
                    {"posting_state": POSTING_STATE_RECOVERY_REQUIRED},
                    {"posting_lock_token": {"$type": "string"}, "posting_lock_expires_at": {"$lte": now}},
                ],
            }
        )
        .sort("posting_progress_updated_at", DESCENDING)
        .limit(max(1, min(int(limit or 50), 200)))
    )

    serialized = []
    for document in rows:
        item = serialize_voucher(document)
        item["stale_lock"] = _is_stale_lock(document, now)
        item["official_line_count_live"] = mongo.db[VOUCHER_LINE_COLLECTION].count_documents(
            {"voucher_document_id": document["_id"]}
        )
        serialized.append(item)

    return {
        "rows": serialized,
        "count": len(serialized),
        "recovery_required_count": sum(1 for row in rows if row.get("posting_state") == POSTING_STATE_RECOVERY_REQUIRED),
        "stale_lock_count": sum(1 for row in rows if _is_stale_lock(row, now)),
    }


def recover_voucher_posting(voucher_id, actor_user_id):
    """Resume one interrupted posting with the original number and line keys."""
    actor = _get_actor(actor_user_id, allowed_roles={"super_admin"})
    voucher = _get_voucher(_object_id(voucher_id))
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], RECOVERY_PERMISSION)
    ensure_voucher_recovery_indexes()
    ensure_voucher_posting_indexes()

    if voucher.get("status") == STATUS_POSTED and voucher.get("posting_state") == POSTING_STATE_COMPLETED:
        return {
            "voucher": serialize_voucher(voucher),
            "message": "Voucher posting was already completed. No recovery change was required.",
            "category": "info",
            "idempotent_replay": True,
        }
    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only an interrupted draft posting can be recovered.")
    if voucher.get("posting_state") != POSTING_STATE_RECOVERY_REQUIRED and not _is_stale_lock(voucher):
        raise ValueError("This voucher is not marked for posting recovery.")

    # Release only an expired/stale lock. A live lock means another process may
    # still be working and must not be disturbed.
    if voucher.get("posting_lock_token"):
        if not _is_stale_lock(voucher):
            raise RuntimeError("Voucher posting is still actively locked. Wait and retry later.")
        mongo.db[VOUCHER_COLLECTION].update_one(
            {"_id": voucher["_id"], "posting_lock_token": voucher.get("posting_lock_token")},
            {
                "$set": {
                    "posting_lock_token": None,
                    "posting_lock_expires_at": None,
                    "posting_state": POSTING_STATE_RECOVERY_REQUIRED,
                    "posting_progress_updated_at": now_utc(),
                }
            },
        )

    before_state = voucher.get("posting_state") or "not_started"
    result = post_voucher_draft(
        voucher_id=voucher["_id"],
        actor_user_id=actor["_id"],
        expected_version=int(voucher.get("version") or 1),
    )
    recovered = _get_voucher(voucher["_id"])
    _record_audit(
        recovered,
        actor,
        "recover_voucher_posting",
        previous_status=STATUS_DRAFT,
        changed_fields=["posting_state", "status", "voucher_number", "posted_line_count"],
        remarks=f"Controlled recovery resumed posting from state {before_state}.",
    )
    result["message"] = f"Voucher recovery completed as {recovered.get('voucher_number') or recovered.get('voucher_id')}."
    result["category"] = "success"
    result["recovered_from_state"] = before_state
    return result
