from datetime import datetime, timedelta
from hashlib import sha256
import json
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, UpdateOne
from pymongo.errors import BulkWriteError, DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_financial_year_service import (
    assert_financial_year_usable_for_posting,
)
from app.services.accounting_number_series_service import (
    commit_reserved_number,
    reserve_document_number,
)
from app.services.accounting_voucher_service import (
    STATUS_DRAFT,
    STATUS_POSTED,
    VOUCHER_COLLECTION,
    VOUCHER_LINE_COLLECTION,
    _assert_active_avpl_entity,
    _change_event,
    _get_actor,
    _get_voucher,
    _record_audit,
    _require_permission,
    serialize_voucher,
)
from app.services.accounting_voucher_validation_service import (
    VALIDATION_STATUS_VALID,
    money_string,
    validate_draft_lines,
)
from app.utils.helpers import now_utc


POST_PERMISSION = "accounting.voucher.post"

POSTING_STATE_NOT_STARTED = "not_started"
POSTING_STATE_NUMBER_RESERVED = "number_reserved"
POSTING_STATE_LINES_WRITTEN = "lines_written"
POSTING_STATE_NUMBER_COMMITTED = "number_committed"
POSTING_STATE_COMPLETED = "completed"
POSTING_STATE_RECOVERY_REQUIRED = "recovery_required"

LINE_STATUS_PENDING_COMMIT = "pending_commit"
LINE_STATUS_POSTED = "posted"

LOCK_MINUTES = 5


# ---------------------------------------------------------------------------
# Index foundation
# ---------------------------------------------------------------------------


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

        if (
            same_keys
            and bool(metadata.get("unique", False)) == required_unique
            and metadata.get("partialFilterExpression") == required_partial
        ):
            return existing_name

        raise RuntimeError(
            f"Conflicting index detected on {collection.name}: {existing_name}. "
            "No existing index was dropped automatically."
        )

    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure as exc:
        raise RuntimeError(
            f"Could not create Accounting index {name} on {collection.name}."
        ) from exc


VOUCHER_LINE_INDEX_DEFINITIONS = (
    (
        [("posting_line_key", ASCENDING)],
        "voucher_line_posting_key_unique",
        {
            "unique": True,
            "partialFilterExpression": {"posting_line_key": {"$type": "string"}},
        },
    ),
    (
        [("voucher_document_id", ASCENDING), ("line_number", ASCENDING)],
        "voucher_line_document_sequence_unique",
        {
            "unique": True,
            "partialFilterExpression": {
                "voucher_document_id": {"$type": "objectId"},
                "line_number": {"$type": "number"},
            },
        },
    ),
    (
        [
            ("accounting_entity_id", ASCENDING),
            ("ledger_id", ASCENDING),
            ("transaction_date", ASCENDING),
            ("posting_status", ASCENDING),
        ],
        "voucher_line_ledger_date_status_idx",
        {},
    ),
    (
        [
            ("financial_year_id", ASCENDING),
            ("transaction_date", ASCENDING),
            ("posting_status", ASCENDING),
        ],
        "voucher_line_fy_date_status_idx",
        {},
    ),
    (
        [("accounting_entity_id", ASCENDING), ("voucher_number", ASCENDING)],
        "voucher_line_number_lookup_idx",
        {},
    ),
    (
        [
            ("accounting_entity_id", ASCENDING),
            ("business_event_type", ASCENDING),
            ("business_event_id", ASCENDING),
            ("voucher_type", ASCENDING),
        ],
        "voucher_line_business_event_idx",
        {},
    ),
    (
        [("posted_at", DESCENDING)],
        "voucher_line_posted_at_idx",
        {},
    ),
)


def ensure_voucher_posting_indexes():
    """Install immutable voucher-line indexes without changing existing indexes."""
    collection = mongo.db[VOUCHER_LINE_COLLECTION]
    return [
        _ensure_exact_index(collection, keys, name=name, **options)
        for keys, name, options in VOUCHER_LINE_INDEX_DEFINITIONS
    ]


# ---------------------------------------------------------------------------
# Posting helpers
# ---------------------------------------------------------------------------


def _parse_expected_version(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Voucher version is required. Refresh and try again.") from exc
    if parsed < 1:
        raise ValueError("Invalid voucher version.")
    return parsed


def _posting_idempotency_key(voucher):
    existing = str(voucher.get("posting_idempotency_key") or "").strip()
    if existing:
        return existing
    return f"accounting-voucher-post:{voucher.get('voucher_id')}"


def _posted_number_key(entity_id, voucher_number):
    return f"{entity_id}:{str(voucher_number or '').strip()}"


def _official_line_fingerprint(document):
    payload = {
        "voucher_document_id": str(document.get("voucher_document_id") or ""),
        "line_number": int(document.get("line_number") or 0),
        "source_line_id": document.get("source_line_id") or "",
        "ledger_id": str(document.get("ledger_id") or ""),
        "debit_amount": money_string(document.get("debit_amount")),
        "credit_amount": money_string(document.get("credit_amount")),
        "line_narration": document.get("line_narration") or "",
        "voucher_number": document.get("voucher_number") or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _assert_validation_current(voucher, validation):
    if voucher.get("validation_status") != VALIDATION_STATUS_VALID:
        raise ValueError(
            "Validate the voucher successfully before posting it."
        )

    saved = voucher.get("last_validation_result") or {}
    current_version = int(voucher.get("version") or 1)
    if int(saved.get("voucher_version") or 0) != current_version:
        raise ValueError(
            "The voucher changed after validation. Validate it again before posting."
        )
    if saved.get("header_fingerprint") != (voucher.get("header_fingerprint") or ""):
        raise ValueError(
            "The voucher header changed after validation. Validate it again before posting."
        )
    if saved.get("draft_lines_fingerprint") != validation.get(
        "draft_lines_fingerprint"
    ):
        raise ValueError(
            "The voucher lines changed after validation. Validate them again before posting."
        )
    if not validation.get("is_valid"):
        errors = validation.get("errors") or []
        detail = errors[0].get("message") if errors else "Double-entry validation failed."
        raise ValueError(detail)


def _acquire_posting_lock(voucher, actor, expected_version):
    now = now_utc()
    lock_token = uuid4().hex
    expires_at = now + timedelta(minutes=LOCK_MINUTES)
    posting_key = _posting_idempotency_key(voucher)

    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher["_id"],
            "status": STATUS_DRAFT,
            "version": expected_version,
            "$or": [
                {"posting_lock_token": {"$exists": False}},
                {"posting_lock_token": None},
                {"posting_lock_expires_at": {"$lte": now}},
            ],
        },
        {
            "$set": {
                "posting_lock_token": lock_token,
                "posting_lock_acquired_by": actor["_id"],
                "posting_lock_acquired_by_str": str(actor["_id"]),
                "posting_lock_acquired_by_name": actor.get("resolved_name") or "",
                "posting_lock_acquired_at": now,
                "posting_lock_expires_at": expires_at,
                "posting_idempotency_key": posting_key,
                "posting_started_at": voucher.get("posting_started_at") or now,
                "posting_last_attempt_at": now,
                "posting_last_attempt_by": actor["_id"],
                "posting_last_attempt_by_str": str(actor["_id"]),
                "posting_last_attempt_by_name": actor.get("resolved_name") or "",
                "posting_error": None,
            }
        },
    )
    if result.matched_count == 1:
        return lock_token, posting_key

    current = _get_voucher(voucher["_id"])
    if current.get("status") == STATUS_POSTED:
        return None, _posting_idempotency_key(current)
    if int(current.get("version") or 1) != expected_version:
        raise RuntimeError(
            "This voucher changed in another session. Refresh before posting."
        )
    raise RuntimeError(
        "Voucher posting is already in progress. Wait a moment, refresh, and retry."
    )


def _set_posting_progress(voucher_id, lock_token, state, **fields):
    update_fields = {
        "posting_state": state,
        "posting_progress_updated_at": now_utc(),
        **fields,
    }
    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher_id,
            "status": STATUS_DRAFT,
            "posting_lock_token": lock_token,
        },
        {"$set": update_fields},
    )
    if result.matched_count != 1:
        current = _get_voucher(voucher_id)
        if current.get("status") == STATUS_POSTED:
            return current
        raise RuntimeError(
            "The voucher posting state changed unexpectedly. Refresh before retrying."
        )
    return _get_voucher(voucher_id)


def _build_official_line_documents(voucher, validation, voucher_number, actor):
    timestamp = now_utc()
    documents = []

    for index, snapshot in enumerate(
        validation.get("live_ledger_snapshots") or [], start=1
    ):
        line_number = int(snapshot.get("sequence") or index)
        posting_line_key = f"{voucher['_id']}:{line_number}"
        document = {
            "posting_line_key": posting_line_key,
            "voucher_document_id": voucher["_id"],
            "voucher_document_id_str": str(voucher["_id"]),
            "voucher_id": voucher.get("voucher_id") or "",
            "voucher_number": voucher_number,
            "voucher_type": voucher.get("voucher_type") or "",
            "voucher_type_label": voucher.get("voucher_type_label") or "",
            "voucher_type_short_code": voucher.get("voucher_type_short_code") or "",
            "voucher_role": voucher.get("voucher_role") or "primary",
            "accounting_entity_id": voucher.get("accounting_entity_id"),
            "accounting_entity_id_str": str(voucher.get("accounting_entity_id") or ""),
            "accounting_entity_code": voucher.get("accounting_entity_code") or "AVPL",
            "financial_year_id": voucher.get("financial_year_id"),
            "financial_year_id_str": str(voucher.get("financial_year_id") or ""),
            "financial_year_code": voucher.get("financial_year_code") or "",
            "transaction_date": voucher.get("transaction_date"),
            "line_number": line_number,
            "source_line_id": snapshot.get("line_id") or "",
            "ledger_id": snapshot.get("ledger_id"),
            "ledger_id_str": snapshot.get("ledger_id_str") or str(snapshot.get("ledger_id") or ""),
            "ledger_code": snapshot.get("ledger_code") or "",
            "ledger_name": snapshot.get("ledger_name") or "",
            "ledger_system_key": snapshot.get("ledger_system_key") or "",
            "ledger_type": snapshot.get("ledger_type") or "",
            "normal_balance": snapshot.get("normal_balance") or "",
            "posting_policy": snapshot.get("posting_policy") or "",
            "account_group_id": snapshot.get("account_group_id"),
            "account_group_id_str": snapshot.get("account_group_id_str") or "",
            "account_group_system_key": snapshot.get("account_group_system_key") or "",
            "account_group_name": snapshot.get("account_group_name") or "",
            "ledger_version_snapshot": int(snapshot.get("ledger_version_snapshot") or 1),
            "debit_amount": snapshot.get("debit_amount"),
            "credit_amount": snapshot.get("credit_amount"),
            "entry_side": "debit"
            if money_string(snapshot.get("debit_amount")) != "0.00"
            else "credit",
            "line_narration": snapshot.get("line_narration") or "",
            "voucher_narration": voucher.get("narration") or "",
            "reference_number": voucher.get("reference_number") or "",
            "reference_date": voucher.get("reference_date"),
            "business_event_type": voucher.get("business_event_type") or "",
            "business_event_id": voucher.get("business_event_id") or "",
            "source_collection": voucher.get("source_collection") or "",
            "source_document_id": voucher.get("source_document_id") or "",
            "source_document_number": voucher.get("source_document_number") or "",
            "posting_status": LINE_STATUS_PENDING_COMMIT,
            "status": LINE_STATUS_PENDING_COMMIT,
            "is_reversal": False,
            "original_voucher_line_id": None,
            "created_by": actor["_id"],
            "created_by_str": str(actor["_id"]),
            "created_by_name": actor.get("resolved_name") or "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        document["official_line_fingerprint"] = _official_line_fingerprint(document)
        documents.append(document)

    return documents


def _write_official_lines(voucher, validation, voucher_number, actor):
    documents = _build_official_line_documents(
        voucher, validation, voucher_number, actor
    )
    if not documents:
        raise RuntimeError("No validated voucher lines are available for posting.")

    operations = [
        UpdateOne(
            {"posting_line_key": document["posting_line_key"]},
            {"$setOnInsert": document},
            upsert=True,
        )
        for document in documents
    ]
    try:
        mongo.db[VOUCHER_LINE_COLLECTION].bulk_write(operations, ordered=True)
    except (BulkWriteError, DuplicateKeyError) as exc:
        raise RuntimeError(
            "Official voucher lines could not be written safely. The posting is retained for idempotent recovery."
        ) from exc

    existing = list(
        mongo.db[VOUCHER_LINE_COLLECTION]
        .find({"voucher_document_id": voucher["_id"]})
        .sort("line_number", ASCENDING)
    )
    if len(existing) != len(documents):
        raise RuntimeError(
            "The official line count does not match the validated draft. Retry posting to recover safely."
        )

    expected_by_key = {row["posting_line_key"]: row for row in documents}
    for row in existing:
        expected = expected_by_key.get(row.get("posting_line_key"))
        if not expected:
            raise RuntimeError(
                "Unexpected official voucher line detected. Posting requires review."
            )
        if row.get("official_line_fingerprint") != expected.get(
            "official_line_fingerprint"
        ):
            raise RuntimeError(
                "An existing official voucher line does not match the validated draft. Posting requires review."
            )

    return existing


def _activate_official_lines(voucher_id, voucher_number, actor):
    timestamp = now_utc()
    mongo.db[VOUCHER_LINE_COLLECTION].update_many(
        {
            "voucher_document_id": voucher_id,
            "posting_status": LINE_STATUS_PENDING_COMMIT,
        },
        {
            "$set": {
                "posting_status": LINE_STATUS_POSTED,
                "status": LINE_STATUS_POSTED,
                "voucher_number": voucher_number,
                "posted_by": actor["_id"],
                "posted_by_str": str(actor["_id"]),
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "updated_at": timestamp,
            }
        },
    )

    remaining = mongo.db[VOUCHER_LINE_COLLECTION].count_documents(
        {
            "voucher_document_id": voucher_id,
            "posting_status": {"$ne": LINE_STATUS_POSTED},
        }
    )
    if remaining:
        raise RuntimeError(
            "One or more official voucher lines were not activated. Retry posting to recover safely."
        )


def _mark_recovery_required(voucher_id, lock_token, error, previous_state):
    timestamp = now_utc()
    mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher_id,
            "status": STATUS_DRAFT,
            "posting_lock_token": lock_token,
        },
        {
            "$set": {
                "posting_state": POSTING_STATE_RECOVERY_REQUIRED,
                "posting_recovery_from_state": previous_state,
                "posting_error": str(error)[:1000],
                "posting_error_at": timestamp,
                "posting_lock_token": None,
                "posting_lock_expires_at": None,
                "posting_progress_updated_at": timestamp,
            }
        },
    )


def _release_unused_lock(voucher_id, lock_token, error=None):
    timestamp = now_utc()
    fields = {
        "posting_lock_token": None,
        "posting_lock_expires_at": None,
        "posting_progress_updated_at": timestamp,
    }
    if error:
        fields["posting_error"] = str(error)[:1000]
        fields["posting_error_at"] = timestamp
    mongo.db[VOUCHER_COLLECTION].update_one(
        {"_id": voucher_id, "posting_lock_token": lock_token},
        {"$set": fields},
    )


# ---------------------------------------------------------------------------
# Public posting action
# ---------------------------------------------------------------------------


def post_voucher_draft(
    voucher_id, actor_user_id, expected_version, *, allow_creator_post=False,
    allowed_roles=None, required_permission=None,
):
    """Post one validated voucher exactly once without requiring MongoDB transactions."""
    actor = _get_actor(
        actor_user_id,
        allowed_roles=allowed_roles or {"avpl_admin", "super_admin"},
    )
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    # ``None`` means use the normal voucher-post permission.  An explicit
    # empty permission is allowed only for a caller that has already enforced
    # a narrower domain authorization before entering this service.
    posting_permission = POST_PERMISSION if required_permission is None else required_permission
    _require_permission(actor, entity["_id"], posting_permission)
    ensure_voucher_posting_indexes()

    if voucher.get("status") == STATUS_POSTED:
        return {
            "voucher": serialize_voucher(voucher),
            "message": f"Voucher {voucher.get('voucher_number') or voucher.get('voucher_id')} was already posted. No duplicate posting was created.",
            "idempotent_replay": True,
        }
    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only a validated draft voucher can be posted.")
    if str(voucher.get("created_by")) == str(actor["_id"]) and not allow_creator_post:
        raise PermissionError(
            "Maker-checker control: the voucher maker cannot post the same voucher."
        )

    expected_version = _parse_expected_version(expected_version)
    if expected_version != int(voucher.get("version") or 1):
        raise RuntimeError(
            "This voucher changed in another session. Refresh before posting."
        )

    assert_financial_year_usable_for_posting(
        voucher.get("financial_year_id"),
        entity_id=entity["_id"],
        transaction_date=voucher.get("transaction_date"),
    )
    validation = validate_draft_lines(
        entity["_id"], voucher.get("draft_lines") or []
    )
    _assert_validation_current(voucher, validation)

    lock_token, posting_key = _acquire_posting_lock(
        voucher, actor, expected_version
    )
    if lock_token is None:
        current = _get_voucher(voucher["_id"])
        return {
            "voucher": serialize_voucher(current),
            "message": f"Voucher {current.get('voucher_number') or current.get('voucher_id')} was already posted. No duplicate posting was created.",
            "idempotent_replay": True,
        }

    current_state = voucher.get("posting_state") or POSTING_STATE_NOT_STARTED
    reservation = None
    try:
        reservation = reserve_document_number(
            entity_id=entity["_id"],
            financial_year_id=voucher.get("financial_year_id"),
            document_category="voucher",
            document_type=voucher.get("voucher_type"),
            idempotency_key=posting_key,
            actor_user_id=actor["_id"],
            required_permission=posting_permission,
            source_collection=VOUCHER_COLLECTION,
            source_id=voucher["_id"],
            metadata={
                "voucher_id": voucher.get("voucher_id") or "",
                "business_event_type": voucher.get("business_event_type") or "",
                "business_event_id": voucher.get("business_event_id") or "",
            },
        )
        current_state = POSTING_STATE_NUMBER_RESERVED
        voucher_number = reservation.get("full_number") or ""
        if not voucher_number:
            raise RuntimeError("The reserved voucher number is unavailable.")

        voucher = _set_posting_progress(
            voucher["_id"],
            lock_token,
            POSTING_STATE_NUMBER_RESERVED,
            number_reservation_id=ObjectId(reservation["id"]),
            number_reservation_id_str=reservation["id"],
            reserved_voucher_number=voucher_number,
            number_reserved_at=reservation.get("reserved_at") or now_utc(),
        )

        official_lines = _write_official_lines(
            voucher, validation, voucher_number, actor
        )
        current_state = POSTING_STATE_LINES_WRITTEN
        voucher = _set_posting_progress(
            voucher["_id"],
            lock_token,
            POSTING_STATE_LINES_WRITTEN,
            staged_line_count=len(official_lines),
            staged_debit_total=validation.get("debit_total"),
            staged_credit_total=validation.get("credit_total"),
            lines_written_at=now_utc(),
        )

        reservation = commit_reserved_number(
            reservation_id=reservation["id"],
            actor_user_id=actor["_id"],
            required_permission=posting_permission,
            source_collection=VOUCHER_COLLECTION,
            source_id=voucher["_id"],
            source_reference=voucher.get("voucher_id") or "",
        )
        current_state = POSTING_STATE_NUMBER_COMMITTED
        voucher = _set_posting_progress(
            voucher["_id"],
            lock_token,
            POSTING_STATE_NUMBER_COMMITTED,
            voucher_number=voucher_number,
            posted_number_key=_posted_number_key(entity["_id"], voucher_number),
            number_committed_at=reservation.get("committed_at") or now_utc(),
        )

        _activate_official_lines(voucher["_id"], voucher_number, actor)

        timestamp = now_utc()
        final_result = mongo.db[VOUCHER_COLLECTION].update_one(
            {
                "_id": voucher["_id"],
                "status": STATUS_DRAFT,
                "version": expected_version,
                "posting_lock_token": lock_token,
                "posting_state": POSTING_STATE_NUMBER_COMMITTED,
            },
            {
                "$set": {
                    "status": STATUS_POSTED,
                    "posting_state": POSTING_STATE_COMPLETED,
                    "voucher_number": voucher_number,
                    "posted_number_key": _posted_number_key(
                        entity["_id"], voucher_number
                    ),
                    "posted_line_count": int(validation.get("line_count") or 0),
                    "posted_debit_total": validation.get("debit_total"),
                    "posted_credit_total": validation.get("credit_total"),
                    "posted_lines_fingerprint": validation.get(
                        "draft_lines_fingerprint"
                    ),
                    "posted_header_fingerprint": voucher.get(
                        "header_fingerprint"
                    ) or "",
                    "posted_by": actor["_id"],
                    "posted_by_str": str(actor["_id"]),
                    "posted_by_name": actor.get("resolved_name") or "",
                    "posted_at": timestamp,
                    "posting_completed_at": timestamp,
                    "posting_error": None,
                    "posting_lock_token": None,
                    "posting_lock_expires_at": None,
                    "version": expected_version + 1,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": timestamp,
                },
                "$push": {
                    "change_history": _change_event(
                        "post_voucher",
                        actor,
                        previous_status=STATUS_DRAFT,
                        new_status=STATUS_POSTED,
                        changed_fields=[
                            "status",
                            "posting_state",
                            "voucher_number",
                            "posted_line_count",
                            "posted_debit_total",
                            "posted_credit_total",
                        ],
                        remarks=(
                            f"Voucher posted as {voucher_number} with "
                            f"{int(validation.get('line_count') or 0)} immutable official line(s)."
                        ),
                    )
                },
            },
        )
        if final_result.matched_count != 1:
            current = _get_voucher(voucher["_id"])
            if current.get("status") != STATUS_POSTED:
                raise RuntimeError(
                    "Official lines and number were committed, but the voucher header requires recovery. Retry posting safely."
                )

        posted = _get_voucher(voucher["_id"])
        _record_audit(
            posted,
            actor,
            "post_voucher",
            previous_status=STATUS_DRAFT,
            changed_fields=[
                "status",
                "posting_state",
                "voucher_number",
                "posted_line_count",
                "posted_debit_total",
                "posted_credit_total",
            ],
            remarks=(
                f"Voucher {voucher_number} posted successfully. Total Debit "
                f"₹{money_string(validation.get('debit_total'))} equals Total Credit "
                f"₹{money_string(validation.get('credit_total'))}."
            ),
        )
        return {
            "voucher": serialize_voucher(posted),
            "message": f"Voucher posted successfully as {voucher_number}.",
            "category": "success",
            "idempotent_replay": False,
        }

    except (PermissionError, ValueError):
        _release_unused_lock(voucher["_id"], lock_token)
        raise
    except Exception as exc:
        if reservation or current_state != POSTING_STATE_NOT_STARTED:
            _mark_recovery_required(
                voucher["_id"], lock_token, exc, current_state
            )
        else:
            _release_unused_lock(voucher["_id"], lock_token, exc)
        raise RuntimeError(
            f"Voucher posting did not finish: {exc} Retry the same voucher; the engine will not duplicate its number or lines."
        ) from exc
