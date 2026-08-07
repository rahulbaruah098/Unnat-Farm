from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import OperationFailure

from app.extensions import mongo
from app.services.accounting_financial_year_service import (
    get_open_financial_year_for_date,
)
from app.services.accounting_number_series_service import (
    commit_reserved_number,
    reserve_document_number,
)
from app.services.accounting_party_ledger_service import (
    PARTY_ROLE_SUPPLIER,
    get_active_party_ledger_for_posting,
)
from app.services.accounting_product_mapping_service import (
    get_product_accounting_mapping_for_posting,
)
from app.services.accounting_voucher_posting_service import (
    post_voucher_draft,
)
from app.services.accounting_voucher_service import (
    VOUCHER_COLLECTION,
    add_voucher_draft_line,
    create_voucher_draft,
    validate_voucher_draft,
)
from app.services.avpl_supplier_invoice_service import (
    STATUS_CANCELLED,
    STATUS_MATCHED,
    STATUS_MATCHED_WITH_WARNINGS,
    SUPPLIER_INVOICE_COLLECTION,
    serialize_supplier_invoice,
)
from app.services.avpl_purchase_order_service import (
    PURCHASE_ORDER_COLLECTION,
)
from app.utils.helpers import now_utc


POSTING_STATUS_NOT_POSTED = "not_posted"
POSTING_STATUS_PREPARED = "prepared"
POSTING_STATUS_POSTING = "posting"
POSTING_STATUS_POSTED = "posted"
POSTING_STATUS_RECOVERY_REQUIRED = "recovery_required"

POSTING_STATUS_LABELS = {
    POSTING_STATUS_NOT_POSTED: "Not Prepared",
    POSTING_STATUS_PREPARED: "Prepared for Posting",
    POSTING_STATUS_POSTING: "Posting in Progress",
    POSTING_STATUS_POSTED: "Posted",
    POSTING_STATUS_RECOVERY_REQUIRED: "Recovery Required",
}

PAYMENT_STATUS_UNPAID = "unpaid"
PAYMENT_STATUS_PARTIALLY_PAID = "partially_paid"
PAYMENT_STATUS_PAID = "paid"

ALLOWED_ROLES = {"accounts", "avpl_admin", "super_admin"}
PREPARER_ROLES = {"accounts", "super_admin"}
POSTER_ROLES = {"avpl_admin", "super_admin"}

MONEY_QUANTUM = Decimal("0.01")
POST_PERMISSION = "accounting.voucher.post"
PURCHASE_INVOICE_CATEGORY = "invoice"
PURCHASE_INVOICE_TYPE = "purchase_invoice"
PURCHASE_VOUCHER_TYPE = "purchase_voucher"


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _decimal(value, label="Amount"):
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite.")
    return number.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _money_string(value):
    return format(_decimal(value), "f")


def _get_actor(actor_user_id, allowed_roles=None):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_id},
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

    role = str(actor.get("role") or "").strip().lower()
    valid_roles = allowed_roles or ALLOWED_ROLES
    if role not in valid_roles:
        raise PermissionError(
            "You are not authorized for this purchase-posting action."
        )
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError(
            "Inactive users cannot prepare or post purchase documents."
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


def _get_invoice(invoice_id):
    object_id = _to_object_id(invoice_id)
    if not object_id:
        raise ValueError("Invalid Supplier Invoice reference.")
    invoice = mongo.db[SUPPLIER_INVOICE_COLLECTION].find_one({"_id": object_id})
    if not invoice:
        raise ValueError("Supplier Invoice was not found.")
    return invoice


def _expected_version(value):
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Invalid Supplier Invoice version. Refresh and try again."
        ) from exc
    if version < 1:
        raise ValueError(
            "Invalid Supplier Invoice version. Refresh and try again."
        )
    return version


def _assert_invoice_match_ready(invoice):
    if invoice.get("status") == STATUS_CANCELLED:
        raise ValueError("A cancelled Supplier Invoice cannot be posted.")
    if invoice.get("status") not in {
        STATUS_MATCHED,
        STATUS_MATCHED_WITH_WARNINGS,
    }:
        raise ValueError(
            "Resolve every blocking three-way-match exception before purchase posting."
        )
    if int(invoice.get("blocking_mismatch_count") or 0) > 0:
        raise ValueError(
            "This Supplier Invoice still contains blocking three-way-match exceptions."
        )
    if not invoice.get("items"):
        raise ValueError("The Supplier Invoice has no product lines to post.")


def _active_ledger_by_system_key(entity_id, system_key, label):
    ledger = mongo.db.ledgers.find_one(
        {
            "accounting_entity_id": entity_id,
            "system_key": str(system_key or "").strip().lower(),
            "status": "active",
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    )
    if not ledger:
        raise ValueError(f"The active {label} ledger is unavailable.")
    return ledger


def _posting_lines(invoice):
    entity_id = invoice.get("accounting_entity_id")
    invoice_date = invoice.get("invoice_date")

    supplier = get_active_party_ledger_for_posting(
        entity_id,
        invoice.get("supplier_ledger_id"),
        party_role=PARTY_ROLE_SUPPLIER,
    )

    purchase_totals = {}
    purchase_names = {}
    taxable_total = Decimal("0.00")

    for item in invoice.get("items") or []:
        product_id = item.get("source_product_id") or item.get(
            "source_product_id_str"
        )
        mapping = get_product_accounting_mapping_for_posting(
            entity_id,
            product_id,
            transaction_date=invoice_date,
            operation="purchase",
        )
        purchase_ledger = (mapping.get("ledgers") or {}).get("purchase") or {}
        ledger_id = purchase_ledger.get("_id")
        if not ledger_id:
            raise ValueError(
                f"Purchase ledger mapping is unavailable for {item.get('product_name') or 'a product'}."
            )

        amount = _decimal(item.get("taxable_value"), "Taxable value")
        taxable_total += amount
        key = str(ledger_id)
        purchase_totals[key] = purchase_totals.get(key, Decimal("0.00")) + amount
        purchase_names[key] = purchase_ledger.get("name") or purchase_ledger.get(
            "ledger_code"
        ) or "Purchase"

    grand_total = _decimal(invoice.get("grand_total"), "Invoice grand total")
    cgst_total = _decimal(invoice.get("cgst_total"), "CGST")
    sgst_total = _decimal(invoice.get("sgst_total"), "SGST")
    igst_total = _decimal(invoice.get("igst_total"), "IGST")
    tax_total = cgst_total + sgst_total + igst_total

    non_tax_adjustment = grand_total - tax_total - taxable_total
    if not purchase_totals:
        raise ValueError("No purchase ledger line could be prepared.")

    first_key = next(iter(purchase_totals))
    purchase_totals[first_key] += non_tax_adjustment
    if purchase_totals[first_key] <= 0:
        raise ValueError(
            "Freight, charges or round-off produce an invalid purchase debit. Review the Supplier Invoice totals."
        )

    lines = []
    for ledger_id, amount in purchase_totals.items():
        if amount <= 0:
            continue
        lines.append(
            {
                "ledger_id": ledger_id,
                "debit_amount": _money_string(amount),
                "credit_amount": "0.00",
                "line_narration": (
                    f"Purchase value for Supplier Invoice {invoice.get('supplier_invoice_number') or ''} "
                    f"through {purchase_names.get(ledger_id) or 'Purchase ledger'}."
                ),
            }
        )

    for amount, system_key, label in [
        (cgst_total, "input_cgst", "Input CGST"),
        (sgst_total, "input_sgst", "Input SGST"),
        (igst_total, "input_igst", "Input IGST"),
    ]:
        if amount <= 0:
            continue
        ledger = _active_ledger_by_system_key(entity_id, system_key, label)
        lines.append(
            {
                "ledger_id": str(ledger["_id"]),
                "debit_amount": _money_string(amount),
                "credit_amount": "0.00",
                "line_narration": (
                    f"{label} on Supplier Invoice {invoice.get('supplier_invoice_number') or ''}."
                ),
            }
        )

    lines.append(
        {
            "ledger_id": str(supplier["_id"]),
            "debit_amount": "0.00",
            "credit_amount": _money_string(grand_total),
            "line_narration": (
                f"Supplier payable for {invoice.get('supplier_name') or 'Supplier'} "
                f"against invoice {invoice.get('supplier_invoice_number') or ''}."
            ),
        }
    )

    debit = sum(_decimal(row.get("debit_amount")) for row in lines)
    credit = sum(_decimal(row.get("credit_amount")) for row in lines)
    if debit != credit or debit <= 0:
        raise RuntimeError(
            f"Prepared purchase posting is not balanced: Debit ₹{debit} / Credit ₹{credit}."
        )
    return lines


def _history_event(action, actor, previous_status=None, new_status=None, reason=""):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_role": actor.get("resolved_role") or "",
        "actor_name": actor.get("resolved_name") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": str(reason or "")[:1500],
        "at": now_utc(),
    }


def _record_audit(invoice, actor, action, previous_status=None, reason=""):
    try:
        mongo.db.accounting_audit_logs.insert_one(
            {
                "module": "avpl_procurement",
                "action": action,
                "accounting_entity_id": invoice.get("accounting_entity_id"),
                "accounting_entity_id_str": str(
                    invoice.get("accounting_entity_id") or ""
                ),
                "entity_type": "avpl_supplier_invoice",
                "entity_id": invoice.get("_id"),
                "entity_id_str": str(invoice.get("_id") or ""),
                "actor_user_id": actor["_id"],
                "actor_user_id_str": str(actor["_id"]),
                "actor_role": actor.get("resolved_role") or "",
                "actor_name": actor.get("resolved_name") or "",
                "previous_status": previous_status,
                "new_status": invoice.get("posting_status"),
                "metadata": {
                    "internal_reference": invoice.get("internal_reference") or "",
                    "supplier_invoice_number": invoice.get(
                        "supplier_invoice_number"
                    )
                    or "",
                    "official_purchase_invoice_number": invoice.get(
                        "official_purchase_invoice_number"
                    )
                    or "",
                    "accounting_voucher_number": invoice.get(
                        "accounting_voucher_number"
                    )
                    or "",
                    "grand_total": invoice.get("grand_total") or "0.00",
                },
                "remarks": str(reason or "")[:1500],
                "created_at": now_utc(),
            }
        )
    except Exception:
        mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
            {"_id": invoice.get("_id")},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_action": action,
                    "audit_sync_error_at": now_utc(),
                }
            },
        )


def ensure_purchase_posting_indexes():
    collection = mongo.db[SUPPLIER_INVOICE_COLLECTION]
    try:
        collection.create_index(
            [("official_purchase_invoice_number", ASCENDING)],
            name="avpl_purchase_invoice_number_unique",
            unique=True,
            partialFilterExpression={
                "official_purchase_invoice_number": {"$type": "string"}
            },
        )
        collection.create_index(
            [
                ("accounting_entity_id", ASCENDING),
                ("posting_status", ASCENDING),
                ("invoice_date", DESCENDING),
            ],
            name="avpl_purchase_posting_status_date_idx",
        )
        collection.create_index(
            [("accounting_voucher_id", ASCENDING)],
            name="avpl_purchase_voucher_link_idx",
            sparse=True,
        )
    except OperationFailure as exc:
        raise RuntimeError(
            "Could not initialize the purchase-posting indexes safely."
        ) from exc


def prepare_supplier_invoice_posting(
    invoice_id,
    actor_user_id,
    expected_version,
):
    actor = _get_actor(actor_user_id, PREPARER_ROLES)
    invoice = _get_invoice(invoice_id)
    _assert_invoice_match_ready(invoice)
    ensure_purchase_posting_indexes()

    current_posting_status = invoice.get("posting_status") or POSTING_STATUS_NOT_POSTED
    if current_posting_status == POSTING_STATUS_POSTED:
        return {
            "invoice": serialize_supplier_invoice(invoice),
            "message": "This Supplier Invoice is already posted.",
            "idempotent_replay": True,
        }
    if current_posting_status in {
        POSTING_STATUS_PREPARED,
        POSTING_STATUS_RECOVERY_REQUIRED,
    } and invoice.get("accounting_voucher_id"):
        return {
            "invoice": serialize_supplier_invoice(invoice),
            "message": "The Accounting posting is already prepared for checker review.",
            "idempotent_replay": True,
        }
    if current_posting_status == POSTING_STATUS_POSTING:
        raise RuntimeError(
            "Purchase posting is already in progress. Refresh before retrying."
        )
    if current_posting_status != POSTING_STATUS_NOT_POSTED:
        raise ValueError("This Supplier Invoice cannot be prepared in its current state.")

    version = _expected_version(expected_version)
    if version != int(invoice.get("version") or 1):
        raise RuntimeError(
            "This Supplier Invoice changed. Refresh before preparing the posting."
        )

    financial_year = get_open_financial_year_for_date(
        invoice.get("accounting_entity_id"),
        invoice.get("invoice_date"),
    )
    if not financial_year:
        raise ValueError(
            "No approved open Financial Year covers the Supplier Invoice date."
        )

    lines = _posting_lines(invoice)
    event_id = invoice.get("document_uid") or str(invoice["_id"])
    voucher_result = create_voucher_draft(
        invoice.get("accounting_entity_id"),
        actor["_id"],
        {
            "voucher_type": PURCHASE_VOUCHER_TYPE,
            "financial_year_id": str(financial_year["_id"]),
            "transaction_date": invoice.get("invoice_date"),
            "reference_number": invoice.get("supplier_invoice_number") or "",
            "reference_date": invoice.get("invoice_date"),
            "narration": (
                f"Purchase posting for Supplier Invoice {invoice.get('supplier_invoice_number') or ''} "
                f"against PO {invoice.get('po_number') or ''}. Stock was already received through posted GRN(s)."
            ),
            "business_event_type": "avpl_supplier_invoice_posting",
            "business_event_id": str(event_id),
            "source_collection": SUPPLIER_INVOICE_COLLECTION,
            "source_document_id": str(invoice["_id"]),
            "source_document_number": invoice.get("internal_reference") or "",
            "voucher_role": "purchase_primary",
            "idempotency_key": f"avpl-purchase-voucher:{event_id}",
        },
    )
    voucher = voucher_result["voucher"]

    if int(voucher.get("draft_line_count") or 0) == 0:
        for line in lines:
            add_result = add_voucher_draft_line(
                voucher["id"],
                actor["_id"],
                line,
                voucher["version"],
            )
            voucher = add_result["voucher"]
    else:
        expected_debit = sum(_decimal(row.get("debit_amount")) for row in lines)
        expected_credit = sum(_decimal(row.get("credit_amount")) for row in lines)
        if (
            int(voucher.get("draft_line_count") or 0) != len(lines)
            or _decimal(voucher.get("draft_debit_total")) != expected_debit
            or _decimal(voucher.get("draft_credit_total")) != expected_credit
        ):
            raise RuntimeError(
                "An existing linked voucher draft has different lines. Review it from Accounting before retrying."
            )

    validation_result = validate_voucher_draft(
        voucher["id"],
        actor["_id"],
        voucher["version"],
    )
    voucher = validation_result["voucher"]
    validation = validation_result.get("validation") or {}
    if not validation.get("is_valid"):
        raise RuntimeError(
            "The purchase voucher draft did not pass Accounting validation."
        )

    timestamp = now_utc()
    result = mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
        {
            "_id": invoice["_id"],
            "version": version,
            "posting_status": {"$in": [None, "", POSTING_STATUS_NOT_POSTED]},
            "voucher_posted": {"$ne": True},
        },
        {
            "$set": {
                "posting_status": POSTING_STATUS_PREPARED,
                "accounting_voucher_id": _to_object_id(voucher["id"]),
                "accounting_voucher_id_str": voucher["id"],
                "accounting_voucher_reference": voucher.get("voucher_id") or "",
                "accounting_voucher_number": "",
                "financial_year_id": _to_object_id(voucher.get("financial_year_id")),
                "financial_year_id_str": voucher.get("financial_year_id") or "",
                "financial_year_code": voucher.get("financial_year_code") or "",
                "posting_prepared_by": actor["_id"],
                "posting_prepared_by_name": actor.get("resolved_name") or "",
                "posting_prepared_at": timestamp,
                "posting_error": None,
                "version": version + 1,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "prepare_purchase_posting",
                    actor,
                    previous_status=POSTING_STATUS_NOT_POSTED,
                    new_status=POSTING_STATUS_PREPARED,
                    reason=(
                        f"Validated purchase voucher draft {voucher.get('voucher_id') or ''} prepared for checker posting."
                    ),
                )
            },
        },
    )
    if result.matched_count != 1:
        current = _get_invoice(invoice_id)
        if (
            current.get("posting_status") == POSTING_STATUS_PREPARED
            and current.get("accounting_voucher_id")
        ):
            return {
                "invoice": serialize_supplier_invoice(current),
                "message": "The Accounting posting was already prepared safely.",
                "idempotent_replay": True,
            }
        raise RuntimeError(
            "This Supplier Invoice changed while the posting was being prepared. Refresh and retry."
        )

    updated = _get_invoice(invoice_id)
    _record_audit(
        updated,
        actor,
        "prepare_purchase_posting",
        previous_status=POSTING_STATUS_NOT_POSTED,
        reason="Validated purchase voucher draft prepared for maker-checker posting.",
    )
    return {
        "invoice": serialize_supplier_invoice(updated),
        "message": "Purchase posting prepared successfully. AVPL Admin can now post the official Purchase Invoice and supplier payable.",
    }


def _refresh_purchase_order_posting_summary(purchase_order_id):
    order_id = _to_object_id(purchase_order_id)
    if not order_id:
        return
    posted = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
            {
                "purchase_order_id": order_id,
                "posting_status": POSTING_STATUS_POSTED,
                "status": {"$ne": STATUS_CANCELLED},
            },
            {
                "grand_total": 1,
                "official_purchase_invoice_number": 1,
            },
        )
    )
    posted_total = sum(_decimal(row.get("grand_total")) for row in posted)
    mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order_id},
        {
            "$set": {
                "posted_purchase_invoice_count": len(posted),
                "posted_purchase_invoice_total": _money_string(posted_total),
                "purchase_posting_status": (
                    "posted" if posted else "not_posted"
                ),
                "purchase_posting_summary_updated_at": now_utc(),
                "updated_at": now_utc(),
            },
            "$inc": {"version": 1},
        },
    )


def post_supplier_invoice_purchase(
    invoice_id,
    actor_user_id,
    expected_version,
):
    actor = _get_actor(actor_user_id, POSTER_ROLES)
    invoice = _get_invoice(invoice_id)
    _assert_invoice_match_ready(invoice)
    ensure_purchase_posting_indexes()

    if invoice.get("posting_status") == POSTING_STATUS_POSTED:
        return {
            "invoice": serialize_supplier_invoice(invoice),
            "message": (
                f"Purchase Invoice {invoice.get('official_purchase_invoice_number') or invoice.get('internal_reference')} "
                "was already posted. No duplicate voucher or payable was created."
            ),
            "idempotent_replay": True,
        }

    if invoice.get("posting_status") not in {
        POSTING_STATUS_PREPARED,
        POSTING_STATUS_RECOVERY_REQUIRED,
    }:
        raise ValueError(
            "Accounts must prepare and validate the purchase posting before AVPL Admin can post it."
        )
    if not invoice.get("accounting_voucher_id"):
        raise ValueError("The prepared Accounting voucher link is unavailable.")
    if str(invoice.get("posting_prepared_by") or "") == str(actor["_id"]):
        raise PermissionError(
            "Maker-checker control: the user who prepared the posting cannot perform the final posting."
        )

    version = _expected_version(expected_version)
    if version != int(invoice.get("version") or 1):
        raise RuntimeError(
            "This Supplier Invoice changed. Refresh before final posting."
        )

    lock_token = uuid4().hex
    timestamp = now_utc()
    lock_result = mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
        {
            "_id": invoice["_id"],
            "version": version,
            "posting_status": {
                "$in": [
                    POSTING_STATUS_PREPARED,
                    POSTING_STATUS_RECOVERY_REQUIRED,
                ]
            },
            "voucher_posted": {"$ne": True},
        },
        {
            "$set": {
                "posting_status": POSTING_STATUS_POSTING,
                "posting_lock_token": lock_token,
                "posting_started_by": actor["_id"],
                "posting_started_by_name": actor.get("resolved_name") or "",
                "posting_started_at": timestamp,
                "posting_error": None,
                "updated_at": timestamp,
            }
        },
    )
    if lock_result.matched_count != 1:
        current = _get_invoice(invoice_id)
        if current.get("posting_status") == POSTING_STATUS_POSTED:
            return {
                "invoice": serialize_supplier_invoice(current),
                "message": "The purchase was already posted safely.",
                "idempotent_replay": True,
            }
        raise RuntimeError(
            "Another purchase-posting attempt is already running or the document changed."
        )

    reservation = None
    try:
        voucher = mongo.db[VOUCHER_COLLECTION].find_one(
            {"_id": _to_object_id(invoice.get("accounting_voucher_id"))}
        )
        if not voucher:
            raise ValueError("The prepared Accounting voucher was not found.")

        event_id = invoice.get("document_uid") or str(invoice["_id"])
        reservation = reserve_document_number(
            entity_id=invoice.get("accounting_entity_id"),
            financial_year_id=voucher.get("financial_year_id"),
            document_category=PURCHASE_INVOICE_CATEGORY,
            document_type=PURCHASE_INVOICE_TYPE,
            idempotency_key=f"avpl-purchase-invoice:{event_id}",
            actor_user_id=actor["_id"],
            required_permission=POST_PERMISSION,
            source_collection=SUPPLIER_INVOICE_COLLECTION,
            source_id=invoice["_id"],
            metadata={
                "supplier_invoice_number": invoice.get(
                    "supplier_invoice_number"
                )
                or "",
                "po_number": invoice.get("po_number") or "",
                "internal_reference": invoice.get("internal_reference") or "",
            },
        )

        voucher_result = post_voucher_draft(
            voucher["_id"],
            actor["_id"],
            voucher.get("version"),
        )
        posted_voucher = voucher_result["voucher"]

        reservation = commit_reserved_number(
            reservation_id=reservation["id"],
            actor_user_id=actor["_id"],
            required_permission=POST_PERMISSION,
            source_collection=SUPPLIER_INVOICE_COLLECTION,
            source_id=invoice["_id"],
            source_reference=invoice.get("internal_reference") or "",
        )

        official_number = reservation.get("full_number") or ""
        if not official_number:
            raise RuntimeError(
                "The official Purchase Invoice number could not be resolved."
            )

        grand_total = _decimal(invoice.get("grand_total"))
        final_timestamp = now_utc()
        final_result = mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
            {
                "_id": invoice["_id"],
                "version": version,
                "posting_status": POSTING_STATUS_POSTING,
                "posting_lock_token": lock_token,
            },
            {
                "$set": {
                    "posting_status": POSTING_STATUS_POSTED,
                    "official_purchase_invoice_number": official_number,
                    "purchase_invoice_number_reservation_id": _to_object_id(
                        reservation.get("id")
                    ),
                    "purchase_invoice_number_reservation_id_str": reservation.get(
                        "id"
                    )
                    or "",
                    "accounting_voucher_number": posted_voucher.get(
                        "voucher_number"
                    )
                    or "",
                    "voucher_posted": True,
                    "payable_posted": True,
                    "stock_posted": False,
                    "payment_status": PAYMENT_STATUS_UNPAID,
                    "paid_amount": "0.00",
                    "outstanding_amount": _money_string(grand_total),
                    "payable_status": "open",
                    "posted_by": actor["_id"],
                    "posted_by_name": actor.get("resolved_name") or "",
                    "posted_at": final_timestamp,
                    "posting_completed_at": final_timestamp,
                    "posting_error": None,
                    "posting_lock_token": None,
                    "version": version + 1,
                    "updated_by": actor["_id"],
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": final_timestamp,
                },
                "$push": {
                    "change_history": _history_event(
                        "post_purchase_invoice_and_supplier_payable",
                        actor,
                        previous_status=POSTING_STATUS_PREPARED,
                        new_status=POSTING_STATUS_POSTED,
                        reason=(
                            f"Official Purchase Invoice {official_number} and Accounting voucher "
                            f"{posted_voucher.get('voucher_number') or ''} posted. Stock was not increased again."
                        ),
                    )
                },
            },
        )
        if final_result.matched_count != 1:
            raise RuntimeError(
                "The voucher and official number were posted, but the Supplier Invoice header requires recovery. Retry safely."
            )

        updated = _get_invoice(invoice_id)
        _refresh_purchase_order_posting_summary(updated.get("purchase_order_id"))
        _record_audit(
            updated,
            actor,
            "post_purchase_invoice_and_supplier_payable",
            previous_status=POSTING_STATUS_PREPARED,
            reason=(
                f"Purchase Invoice {official_number} posted with voucher "
                f"{posted_voucher.get('voucher_number') or ''}."
            ),
        )
        return {
            "invoice": serialize_supplier_invoice(updated),
            "message": (
                f"Purchase Invoice {official_number} posted successfully. Supplier payable ₹{updated.get('grand_total') or '0.00'} is now open and unpaid."
            ),
            "idempotent_replay": voucher_result.get("idempotent_replay") is True,
        }

    except Exception as exc:
        voucher = mongo.db[VOUCHER_COLLECTION].find_one(
            {"_id": _to_object_id(invoice.get("accounting_voucher_id"))}
        )
        voucher_is_posted = bool(voucher and voucher.get("status") == "posted")
        reservation_is_committed = bool(
            reservation and reservation.get("status") == "committed"
        )
        recovery_required = voucher_is_posted or reservation_is_committed
        mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
            {
                "_id": invoice["_id"],
                "posting_lock_token": lock_token,
            },
            {
                "$set": {
                    "posting_status": (
                        POSTING_STATUS_RECOVERY_REQUIRED
                        if recovery_required
                        else POSTING_STATUS_PREPARED
                    ),
                    "posting_error": str(exc)[:1500],
                    "posting_lock_token": None,
                    "updated_at": now_utc(),
                }
            },
        )
        raise


def get_purchase_invoice_print_context(
    accounting_entity_id,
    actor_user_id,
    invoice_id,
):
    _get_actor(actor_user_id, ALLOWED_ROLES)
    invoice = _get_invoice(invoice_id)
    if str(invoice.get("accounting_entity_id")) != str(accounting_entity_id):
        raise PermissionError(
            "This Purchase Invoice belongs to another Accounting entity."
        )

    entity = mongo.db.accounting_entities.find_one(
        {"_id": _to_object_id(accounting_entity_id)}
    ) or {}
    supplier_snapshot = mongo.db.ledgers.find_one(
        {
            "_id": _to_object_id(invoice.get("supplier_ledger_id")),
            "accounting_entity_id": _to_object_id(accounting_entity_id),
            "is_party_ledger": True,
        }
    ) or {}

    return {
        "invoice": serialize_supplier_invoice(invoice),
        "entity": {
            "legal_name": entity.get("legal_name")
            or entity.get("name")
            or "AVPL",
            "trade_name": entity.get("trade_name")
            or entity.get("display_name")
            or "UnnatFarm",
            "address_line_1": entity.get("address_line_1")
            or entity.get("address")
            or entity.get("registered_address")
            or "",
            "address_line_2": entity.get("address_line_2") or "",
            "city": entity.get("city") or "",
            "district": entity.get("district") or "",
            "state": entity.get("state") or entity.get("state_name") or "",
            "state_code": entity.get("state_code") or "",
            "postal_code": entity.get("postal_code") or "",
            "gstin": entity.get("gstin") or "",
            "pan": entity.get("pan") or "",
        },
        "supplier": {
            "legal_name": supplier_snapshot.get("legal_name")
            or invoice.get("supplier_name")
            or "Supplier",
            "display_name": supplier_snapshot.get("name")
            or invoice.get("supplier_name")
            or "Supplier",
            "ledger_code": invoice.get("supplier_code") or "",
            "address_line_1": supplier_snapshot.get("address_line_1") or "",
            "address_line_2": supplier_snapshot.get("address_line_2") or "",
            "city": supplier_snapshot.get("city") or "",
            "district": supplier_snapshot.get("district") or "",
            "state_name": supplier_snapshot.get("state_name") or "",
            "state_code": supplier_snapshot.get("state_code") or "",
            "postal_code": supplier_snapshot.get("postal_code") or "",
            "gst_registration_status": supplier_snapshot.get(
                "gst_registration_status"
            )
            or invoice.get("supplier_gst_registration_status")
            or "unregistered",
            "gstin": supplier_snapshot.get("gstin")
            or invoice.get("supplier_gstin")
            or "",
            "pan": supplier_snapshot.get("pan") or "",
            "phone": supplier_snapshot.get("phone") or "",
            "email": supplier_snapshot.get("email") or "",
        },
        "is_posted": invoice.get("posting_status") == POSTING_STATUS_POSTED,
        "document_title": (
            "Purchase Invoice"
            if invoice.get("posting_status") == POSTING_STATUS_POSTED
            else "Supplier Invoice Matching Copy"
        ),
        "generated_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
    }
