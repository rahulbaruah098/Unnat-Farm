from datetime import date, datetime, time
from hashlib import sha256
from uuid import uuid4

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.services.accounting_financial_year_service import (
    assert_financial_year_usable_for_posting,
)
from app.services.accounting_voucher_posting_service import post_voucher_draft
from app.services.accounting_voucher_service import (
    STATUS_CANCELLED,
    STATUS_DRAFT,
    STATUS_POSTED,
    STATUS_REVERSED,
    VOUCHER_COLLECTION,
    VOUCHER_LINE_COLLECTION,
    _assert_active_avpl_entity,
    _change_event,
    _clean_multiline,
    _financial_year_snapshot,
    _fingerprint_payload,
    _get_actor,
    _get_voucher,
    _record_audit,
    _require_permission,
    _voucher_type_map,
    serialize_voucher,
)
from app.services.accounting_voucher_validation_service import (
    VALIDATION_STATUS_VALID,
    build_draft_line,
    calculate_draft_totals,
    draft_lines_fingerprint,
    money_decimal128,
    money_string,
    normalize_line_sequences,
    validate_draft_lines,
)
from app.utils.helpers import now_utc


CANCEL_PERMISSION = "accounting.voucher.cancel"
REVERSE_PERMISSION = "accounting.voucher.reverse"

REVERSAL_EVENT_TYPE = "voucher_reversal"
REVERSAL_ROLE = "reversal"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _parse_expected_version(value):
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Voucher version is required. Refresh and try again.") from exc
    if version < 1:
        raise ValueError("Invalid voucher version.")
    return version


def _parse_date(value, label):
    if isinstance(value, datetime):
        return datetime.combine(value.date(), time.min)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{label} is required.")
    try:
        return datetime.combine(datetime.strptime(raw, "%Y-%m-%d").date(), time.min)
    except ValueError as exc:
        raise ValueError(f"{label} must use the YYYY-MM-DD format.") from exc


def _scope_hash(*parts):
    raw = "|".join(str(part or "") for part in parts)
    return sha256(raw.encode("utf-8")).hexdigest()


def _assert_no_official_lines(voucher):
    line_count = mongo.db[VOUCHER_LINE_COLLECTION].count_documents(
        {"voucher_document_id": voucher["_id"]}
    )
    if line_count:
        raise RuntimeError(
            "This draft already has official voucher lines and cannot be cancelled."
        )


def _load_original_lines(voucher):
    rows = list(
        mongo.db[VOUCHER_LINE_COLLECTION]
        .find(
            {
                "voucher_document_id": voucher["_id"],
                "posting_status": "posted",
            }
        )
        .sort("line_number", 1)
    )
    expected = int(voucher.get("posted_line_count") or 0)
    if not rows or (expected and len(rows) != expected):
        raise RuntimeError(
            "The original official voucher lines are incomplete. Reversal is blocked for review."
        )
    return rows


# ---------------------------------------------------------------------------
# Draft cancellation
# ---------------------------------------------------------------------------


def cancel_voucher_draft(voucher_id, actor_user_id, expected_version, reason):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"accounts", "super_admin"},
    )
    voucher = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(voucher.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], CANCEL_PERMISSION)

    if voucher.get("status") == STATUS_CANCELLED:
        return {
            "voucher": serialize_voucher(voucher),
            "message": "Voucher draft was already cancelled.",
            "category": "info",
            "idempotent_replay": True,
        }
    if voucher.get("status") != STATUS_DRAFT:
        raise ValueError("Only an unposted draft voucher can be cancelled.")
    if str(voucher.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Only the original voucher maker can cancel this draft.")
    if voucher.get("voucher_number") or int(voucher.get("posted_line_count") or 0):
        raise RuntimeError("A voucher containing official posting data cannot be cancelled.")

    expected_version = _parse_expected_version(expected_version)
    if expected_version != int(voucher.get("version") or 1):
        raise RuntimeError("This voucher changed in another session. Refresh and retry.")

    reason = _clean_multiline(
        reason,
        "Cancellation reason",
        maximum=1000,
        required=True,
    )
    _assert_no_official_lines(voucher)

    timestamp = now_utc()
    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": voucher["_id"],
            "status": STATUS_DRAFT,
            "version": expected_version,
            "voucher_number": None,
            "posted_line_count": {"$in": [0, None]},
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
                "cancelled_by": actor["_id"],
                "cancelled_by_str": str(actor["_id"]),
                "cancelled_by_name": actor.get("resolved_name") or "",
                "cancelled_at": timestamp,
                "cancellation_reason": reason,
                "validation_status": "not_validated",
                "last_validation_result": None,
                "version": expected_version + 1,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "cancel_voucher_draft",
                    actor,
                    previous_status=STATUS_DRAFT,
                    new_status=STATUS_CANCELLED,
                    changed_fields=[
                        "status",
                        "cancelled_by",
                        "cancelled_at",
                        "cancellation_reason",
                    ],
                    remarks=reason,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This voucher changed in another session. Refresh and retry.")

    updated = _get_voucher(voucher["_id"])
    _record_audit(
        updated,
        actor,
        "cancel_voucher_draft",
        previous_status=STATUS_DRAFT,
        changed_fields=[
            "status",
            "cancelled_by",
            "cancelled_at",
            "cancellation_reason",
        ],
        remarks=reason,
    )
    return {
        "voucher": serialize_voucher(updated),
        "message": "Voucher draft cancelled. No official number or financial entry was created.",
        "category": "success",
        "idempotent_replay": False,
    }


# ---------------------------------------------------------------------------
# Posted-voucher reversal
# ---------------------------------------------------------------------------


def _build_reversal_draft(original, original_lines, financial_year, reversal_date, reason, actor):
    voucher_type = str(original.get("voucher_type") or "").strip().lower()
    type_meta = _voucher_type_map().get(voucher_type)
    if not type_meta:
        raise RuntimeError("The original voucher type is no longer available in the number-series catalogue.")

    reversal_voucher_id = f"VCH-{uuid4().hex.upper()}"
    idempotency_key = f"voucher-reversal:{original.get('voucher_id')}"
    event_id = str(original.get("voucher_id") or original.get("_id"))

    draft_lines = []
    for sequence, original_line in enumerate(original_lines, start=1):
        raw_payload = {
            "ledger_id": str(original_line.get("ledger_id") or ""),
            "debit_amount": money_string(original_line.get("credit_amount")),
            "credit_amount": money_string(original_line.get("debit_amount")),
            "line_narration": (
                f"Reversal of {original.get('voucher_number') or original.get('voucher_id')}"
                + (f" — {original_line.get('line_narration')}" if original_line.get("line_narration") else "")
            )[:500],
        }
        line = build_draft_line(
            original["accounting_entity_id"],
            raw_payload,
            line_id=uuid4().hex,
            sequence=sequence,
        )
        line.update(
            {
                "created_by": original.get("created_by"),
                "created_by_str": str(original.get("created_by") or ""),
                "created_by_name": original.get("created_by_name") or "Original maker",
                "created_at": now_utc(),
                "reverses_original_line_id": original_line.get("_id"),
                "reverses_original_line_id_str": str(original_line.get("_id") or ""),
                "reverses_original_line_number": int(original_line.get("line_number") or sequence),
            }
        )
        draft_lines.append(line)

    draft_lines = normalize_line_sequences(draft_lines)
    totals = calculate_draft_totals(draft_lines)
    validation = validate_draft_lines(original["accounting_entity_id"], draft_lines)
    if not validation.get("is_valid"):
        errors = validation.get("errors") or []
        detail = errors[0].get("message") if errors else "Reversal double-entry validation failed."
        raise RuntimeError(detail)

    event_key = _scope_hash(original.get("accounting_entity_id"), REVERSAL_EVENT_TYPE, event_id)
    uniqueness_key = _scope_hash(
        original.get("accounting_entity_id"),
        REVERSAL_EVENT_TYPE,
        event_id,
        voucher_type,
        REVERSAL_ROLE,
    )

    header = {
        "voucher_type": voucher_type,
        "voucher_type_label": type_meta.get("label") or original.get("voucher_type_label") or "",
        "voucher_type_short_code": type_meta.get("short_code") or original.get("voucher_type_short_code") or "",
        "voucher_type_description": type_meta.get("description") or "",
        **_financial_year_snapshot(financial_year),
        "transaction_date": reversal_date,
        "reference_number": original.get("voucher_number") or original.get("voucher_id") or "",
        "reference_date": original.get("transaction_date"),
        "narration": f"Reversal of {original.get('voucher_number') or original.get('voucher_id')}: {reason}",
        "business_event_type": REVERSAL_EVENT_TYPE,
        "business_event_id": event_id,
        "source_collection": VOUCHER_COLLECTION,
        "source_document_id": str(original.get("_id") or ""),
        "source_document_number": original.get("voucher_number") or "",
        "voucher_role": REVERSAL_ROLE,
        "business_event_key": event_key,
        "business_event_uniqueness_key": uniqueness_key,
    }
    header["header_fingerprint"] = _fingerprint_payload(header)

    timestamp = now_utc()
    version = 1
    reversal_maker_id = original.get("created_by")
    reversal_maker_name = original.get("created_by_name") or "Original maker"
    if str(reversal_maker_id or "") == str(actor["_id"]):
        reversal_maker_id = original.get("posted_by")
        reversal_maker_name = original.get("posted_by_name") or "Original posting checker"
    if not reversal_maker_id or str(reversal_maker_id) == str(actor["_id"]):
        raise RuntimeError(
            "A different maker identity is required to preserve maker-checker control for the reversal."
        )
    validation.update(
        {
            "validated_by": actor["_id"],
            "validated_by_str": str(actor["_id"]),
            "validated_by_name": actor.get("resolved_name") or "",
            "validated_at": timestamp,
            "voucher_version": version,
            "header_fingerprint": header["header_fingerprint"],
        }
    )

    return {
        "voucher_id": reversal_voucher_id,
        "accounting_entity_id": original.get("accounting_entity_id"),
        "accounting_entity_id_str": str(original.get("accounting_entity_id") or ""),
        "accounting_entity_code": original.get("accounting_entity_code") or "AVPL",
        "accounting_entity_name": original.get("accounting_entity_name") or "AVPL",
        **header,
        "idempotency_key": idempotency_key,
        "idempotency_scope_key": _scope_hash(
            original.get("accounting_entity_id"), "voucher_header", idempotency_key
        ),
        "status": STATUS_DRAFT,
        "posting_state": "not_started",
        "validation_status": VALIDATION_STATUS_VALID,
        "voucher_number": None,
        "posted_number_key": None,
        "draft_lines": draft_lines,
        "draft_line_count": len(draft_lines),
        "draft_debit_total": money_decimal128(totals["debit_total"]),
        "draft_credit_total": money_decimal128(totals["credit_total"]),
        "draft_balance_difference": money_decimal128(totals["difference"]),
        "draft_absolute_difference": money_decimal128(totals["absolute_difference"]),
        "draft_is_balanced": totals["is_balanced"],
        "draft_lines_fingerprint": draft_lines_fingerprint(draft_lines),
        "last_validation_result": validation,
        "last_validated_at": timestamp,
        "last_validated_by": actor["_id"],
        "last_validated_by_str": str(actor["_id"]),
        "last_validated_by_name": actor.get("resolved_name") or "",
        "posted_line_count": 0,
        "is_reversal": True,
        "reverses_voucher_id": original.get("_id"),
        "reverses_voucher_id_str": str(original.get("_id") or ""),
        "reverses_voucher_internal_id": original.get("voucher_id") or "",
        "reverses_voucher_number": original.get("voucher_number") or "",
        "reversal_reason": reason,
        # Keep the original maker as maker so the authorized reversing checker can post.
        "created_by": reversal_maker_id,
        "created_by_str": str(reversal_maker_id),
        "created_by_name": reversal_maker_name,
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "version": version,
        "is_deleted": False,
        "audit_sync_required": False,
        "change_history": [
            _change_event(
                "create_reversal_draft",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                changed_fields=[
                    "is_reversal",
                    "reverses_voucher_id",
                    "draft_lines",
                    "validation_status",
                ],
                remarks=reason,
            )
        ],
    }


def _get_or_create_reversal_draft(original, original_lines, financial_year, reversal_date, reason, actor):
    existing = mongo.db[VOUCHER_COLLECTION].find_one(
        {
            "reverses_voucher_id": original["_id"],
            "is_reversal": True,
            "status": {"$in": [STATUS_DRAFT, STATUS_POSTED]},
        }
    )
    if existing:
        return existing

    document = _build_reversal_draft(
        original,
        original_lines,
        financial_year,
        reversal_date,
        reason,
        actor,
    )
    try:
        result = mongo.db[VOUCHER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except DuplicateKeyError:
        existing = mongo.db[VOUCHER_COLLECTION].find_one(
            {
                "reverses_voucher_id": original["_id"],
                "is_reversal": True,
                "status": {"$in": [STATUS_DRAFT, STATUS_POSTED]},
            }
        )
        if existing:
            return existing
        raise RuntimeError("The reversal request conflicted with another session. Refresh and retry.")


def _link_reversal_lines(original, reversal):
    original_by_number = {
        int(row.get("line_number") or 0): row
        for row in mongo.db[VOUCHER_LINE_COLLECTION].find(
            {"voucher_document_id": original["_id"], "posting_status": "posted"}
        )
    }
    reversal_rows = list(
        mongo.db[VOUCHER_LINE_COLLECTION].find(
            {"voucher_document_id": reversal["_id"], "posting_status": "posted"}
        )
    )
    for row in reversal_rows:
        original_line = original_by_number.get(int(row.get("line_number") or 0))
        if not original_line:
            raise RuntimeError("A reversal line could not be matched to its original line.")
        mongo.db[VOUCHER_LINE_COLLECTION].update_one(
            {"_id": row["_id"]},
            {
                "$set": {
                    "is_reversal": True,
                    "original_voucher_id": original["_id"],
                    "original_voucher_id_str": str(original["_id"]),
                    "original_voucher_number": original.get("voucher_number") or "",
                    "original_voucher_line_id": original_line["_id"],
                    "original_voucher_line_id_str": str(original_line["_id"]),
                    "reversal_reason": reversal.get("reversal_reason") or "",
                    "updated_at": now_utc(),
                }
            },
        )


def reverse_posted_voucher(
    voucher_id,
    actor_user_id,
    expected_version,
    financial_year_id,
    reversal_date,
    reason,
):
    actor = _get_actor(
        actor_user_id,
        allowed_roles={"avpl_admin", "super_admin"},
    )
    original = _get_voucher(voucher_id)
    entity = _assert_active_avpl_entity(original.get("accounting_entity_id"))
    _require_permission(actor, entity["_id"], REVERSE_PERMISSION)

    if original.get("status") == STATUS_REVERSED:
        reversal = None
        reversal_id = _to_object_id(original.get("reversal_voucher_id"))
        if reversal_id:
            reversal = mongo.db[VOUCHER_COLLECTION].find_one({"_id": reversal_id})
        return {
            "voucher": serialize_voucher(original),
            "reversal_voucher": serialize_voucher(reversal),
            "message": "This voucher was already reversed. No duplicate reversal was created.",
            "category": "info",
            "idempotent_replay": True,
        }
    if original.get("status") != STATUS_POSTED:
        raise ValueError("Only a posted voucher can be reversed.")
    if original.get("is_reversal") is True:
        raise ValueError("A reversal voucher cannot be reversed again in Stage 5 Batch 4.")

    expected_version = _parse_expected_version(expected_version)
    if expected_version != int(original.get("version") or 1):
        raise RuntimeError("This voucher changed in another session. Refresh and retry.")

    reason = _clean_multiline(
        reason,
        "Reversal reason",
        maximum=1000,
        required=True,
    )
    reversal_date = _parse_date(reversal_date, "Reversal date")
    financial_year = assert_financial_year_usable_for_posting(
        financial_year_id,
        entity_id=entity["_id"],
        transaction_date=reversal_date,
    )
    original_lines = _load_original_lines(original)

    reversal = _get_or_create_reversal_draft(
        original,
        original_lines,
        financial_year,
        reversal_date,
        reason,
        actor,
    )

    if reversal.get("status") != STATUS_POSTED:
        posting_result = post_voucher_draft(
            voucher_id=reversal["_id"],
            actor_user_id=actor["_id"],
            expected_version=int(reversal.get("version") or 1),
        )
        reversal = _get_voucher(posting_result["voucher"]["id"])

    _link_reversal_lines(original, reversal)

    timestamp = now_utc()
    result = mongo.db[VOUCHER_COLLECTION].update_one(
        {
            "_id": original["_id"],
            "status": STATUS_POSTED,
            "version": expected_version,
            "$or": [
                {"reversal_voucher_id": {"$exists": False}},
                {"reversal_voucher_id": None},
                {"reversal_voucher_id": reversal["_id"]},
            ],
        },
        {
            "$set": {
                "status": STATUS_REVERSED,
                "reversal_voucher_id": reversal["_id"],
                "reversal_voucher_id_str": str(reversal["_id"]),
                "reversal_voucher_internal_id": reversal.get("voucher_id") or "",
                "reversal_voucher_number": reversal.get("voucher_number") or "",
                "reversal_reason": reason,
                "reversed_by": actor["_id"],
                "reversed_by_str": str(actor["_id"]),
                "reversed_by_name": actor.get("resolved_name") or "",
                "reversed_at": timestamp,
                "version": expected_version + 1,
                "updated_by": actor["_id"],
                "updated_by_str": str(actor["_id"]),
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _change_event(
                    "reverse_posted_voucher",
                    actor,
                    previous_status=STATUS_POSTED,
                    new_status=STATUS_REVERSED,
                    changed_fields=[
                        "status",
                        "reversal_voucher_id",
                        "reversal_voucher_number",
                        "reversal_reason",
                    ],
                    remarks=(
                        f"Reversed by {reversal.get('voucher_number') or reversal.get('voucher_id')}: {reason}"
                    ),
                )
            },
        },
    )
    if result.matched_count != 1:
        current = _get_voucher(original["_id"])
        if current.get("status") != STATUS_REVERSED or str(current.get("reversal_voucher_id")) != str(reversal["_id"]):
            raise RuntimeError(
                "The reversal voucher posted, but the original header needs controlled recovery. Do not create another reversal."
            )

    updated_original = _get_voucher(original["_id"])
    _record_audit(
        updated_original,
        actor,
        "reverse_posted_voucher",
        previous_status=STATUS_POSTED,
        changed_fields=[
            "status",
            "reversal_voucher_id",
            "reversal_voucher_number",
            "reversal_reason",
        ],
        remarks=(
            f"Voucher {original.get('voucher_number')} reversed through "
            f"{reversal.get('voucher_number')}: {reason}"
        ),
    )
    return {
        "voucher": serialize_voucher(updated_original),
        "reversal_voucher": serialize_voucher(reversal),
        "message": (
            f"Voucher {original.get('voucher_number')} reversed successfully through "
            f"{reversal.get('voucher_number')}."
        ),
        "category": "success",
        "idempotent_replay": False,
    }
