"""Business-event bridge for the Stage 5 Accounting voucher engine.

Future purchase, sales, receipt, payment and stock modules must use this
service instead of writing accounting_vouchers or voucher_lines directly.
"""
from bson import ObjectId

from app.extensions import mongo
from app.services.accounting_voucher_posting_service import post_voucher_draft
from app.services.accounting_voucher_service import (
    STATUS_POSTED,
    VOUCHER_COLLECTION,
    add_voucher_draft_line,
    create_voucher_draft,
    serialize_voucher,
    validate_voucher_draft,
)
from app.services.accounting_voucher_validation_service import money_string


def _required_text(value, label, maximum=200):
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _object_id(value, label):
    try:
        return ObjectId(str(value))
    except Exception as exc:
        raise ValueError(f"{label} is invalid.") from exc


def _event_uniqueness_query(entity_id, event_type, event_id, voucher_type, voucher_role):
    return {
        "accounting_entity_id": _object_id(entity_id, "Accounting entity"),
        "business_event_type": _required_text(event_type, "Business event type", 100).lower(),
        "business_event_id": _required_text(event_id, "Business event ID", 200),
        "voucher_type": _required_text(voucher_type, "Voucher type", 100).lower(),
        "voucher_role": (_required_text(voucher_role or "primary", "Voucher role", 100).lower()),
        "is_deleted": {"$ne": True},
    }


def find_business_event_voucher(
    accounting_entity_id,
    business_event_type,
    business_event_id,
    voucher_type,
    voucher_role="primary",
):
    """Return the one voucher linked to a source business event, if present."""
    query = _event_uniqueness_query(
        accounting_entity_id,
        business_event_type,
        business_event_id,
        voucher_type,
        voucher_role,
    )
    document = mongo.db[VOUCHER_COLLECTION].find_one(query)
    return serialize_voucher(document) if document else None


def prepare_voucher_for_business_event(
    accounting_entity_id,
    actor_user_id,
    *,
    financial_year_id,
    voucher_type,
    transaction_date,
    narration,
    business_event_type,
    business_event_id,
    source_collection,
    source_document_id,
    source_document_number="",
    voucher_role="primary",
    reference_number="",
    reference_date="",
    lines,
    idempotency_key=None,
):
    """Create and validate exactly one draft for a linked business event.

    This function is intentionally non-posting. A different authorized checker
    must call ``post_voucher_for_business_event``. This preserves maker-checker.
    """
    if not isinstance(lines, (list, tuple)) or len(lines) < 2:
        raise ValueError("At least two voucher lines are required.")

    event_type = _required_text(business_event_type, "Business event type", 100).lower()
    event_id = _required_text(business_event_id, "Business event ID", 200)
    voucher_type = _required_text(voucher_type, "Voucher type", 100).lower()
    voucher_role = _required_text(voucher_role or "primary", "Voucher role", 100).lower()
    source_collection = _required_text(source_collection, "Source collection", 100)
    source_document_id = _required_text(source_document_id, "Source document ID", 200)
    stable_key = str(idempotency_key or f"business-event:{event_type}:{event_id}:{voucher_type}:{voucher_role}")

    existing = find_business_event_voucher(
        accounting_entity_id,
        event_type,
        event_id,
        voucher_type,
        voucher_role,
    )
    if existing:
        return {
            "voucher": existing,
            "message": "Existing linked voucher returned. No duplicate business-event voucher was created.",
            "idempotent_replay": True,
        }

    header_result = create_voucher_draft(
        accounting_entity_id=accounting_entity_id,
        actor_user_id=actor_user_id,
        raw_payload={
            "financial_year_id": str(financial_year_id),
            "voucher_type": voucher_type,
            "transaction_date": transaction_date,
            "narration": narration,
            "reference_number": reference_number,
            "reference_date": reference_date,
            "idempotency_key": stable_key,
            "business_event_type": event_type,
            "business_event_id": event_id,
            "voucher_role": voucher_role,
            "source_collection": source_collection,
            "source_document_id": source_document_id,
            "source_document_number": source_document_number,
        },
    )
    voucher = header_result["voucher"]

    # An idempotent replay can happen after a timeout. Do not append lines to an
    # existing prepared draft; return it and let the caller inspect its state.
    if header_result.get("idempotent_replay"):
        return {
            "voucher": voucher,
            "message": "Existing linked voucher draft returned safely.",
            "idempotent_replay": True,
        }

    for raw_line in lines:
        line_result = add_voucher_draft_line(
            voucher_id=voucher["id"],
            actor_user_id=actor_user_id,
            raw_payload={
                "ledger_id": raw_line.get("ledger_id"),
                "debit_amount": raw_line.get("debit_amount"),
                "credit_amount": raw_line.get("credit_amount"),
                "line_narration": raw_line.get("line_narration") or "",
            },
            expected_version=voucher["version"],
        )
        voucher = line_result["voucher"]

    validation_result = validate_voucher_draft(
        voucher_id=voucher["id"],
        actor_user_id=actor_user_id,
        expected_version=voucher["version"],
    )
    voucher = validation_result["voucher"]
    if voucher.get("validation_status") != "valid":
        raise ValueError("The linked business-event voucher did not pass double-entry validation.")

    return {
        "voucher": voucher,
        "message": "Linked business-event voucher prepared and validated. It is waiting for an authorized checker to post it.",
        "idempotent_replay": False,
    }


def post_voucher_for_business_event(
    accounting_entity_id,
    actor_user_id,
    *,
    business_event_type,
    business_event_id,
    voucher_type,
    voucher_role="primary",
):
    """Post the existing validated voucher for a business event exactly once."""
    query = _event_uniqueness_query(
        accounting_entity_id,
        business_event_type,
        business_event_id,
        voucher_type,
        voucher_role,
    )
    document = mongo.db[VOUCHER_COLLECTION].find_one(query)
    if not document:
        raise ValueError("No prepared voucher exists for this business event.")

    result = post_voucher_draft(
        voucher_id=document["_id"],
        actor_user_id=actor_user_id,
        expected_version=int(document.get("version") or 1),
    )
    return result


def assert_business_event_accounting_posted(
    accounting_entity_id,
    *,
    business_event_type,
    business_event_id,
    voucher_type,
    voucher_role="primary",
):
    """Block a source invoice/event from becoming posted without Accounting."""
    query = _event_uniqueness_query(
        accounting_entity_id,
        business_event_type,
        business_event_id,
        voucher_type,
        voucher_role,
    )
    document = mongo.db[VOUCHER_COLLECTION].find_one(query)
    if not document:
        raise RuntimeError("Accounting posting is missing for this business event.")
    if document.get("status") != STATUS_POSTED:
        raise RuntimeError("The linked Accounting voucher has not been posted.")
    if int(document.get("posted_line_count") or 0) < 2:
        raise RuntimeError("The linked Accounting voucher does not contain complete official lines.")
    if money_string(document.get("posted_debit_total")) != money_string(document.get("posted_credit_total")):
        raise RuntimeError("The linked Accounting voucher is not balanced.")
    if not document.get("voucher_number"):
        raise RuntimeError("The linked Accounting voucher has no official number.")
    return serialize_voucher(document)
