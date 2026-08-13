from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
import re

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.utils.helpers import now_utc


PAYMENT_COLLECTION = "payments"
RECEIPT_COLLECTION = "payment_receipts"
AUDIT_COLLECTION = "payment_audit"
ACCOUNTING_EVENT_COLLECTION = "accounting_payment_events"

SUPPLIER_INVOICE_COLLECTION = "avpl_supplier_invoices"
AVPL_UFC_INVOICE_COLLECTION = "avpl_sales_invoices"
AVPL_UFC_SALE_COLLECTION = "avpl_ufc_sales"
AVPL_RECEIVABLE_COLLECTION = "avpl_receivables"
UFC_PAYABLE_COLLECTION = "ufc_payables"
UFC_PURCHASE_COLLECTION = "ufc_purchase_entries"
AVPL_UFC_ORDER_COLLECTION = "avpl_ufc_orders"

UFC_FARMER_INVOICE_COLLECTION = "ufc_farmer_sales_invoices"
UFC_FARMER_SALE_COLLECTION = "ufc_farmer_sales"
UFC_FARMER_RECEIVABLE_COLLECTION = "ufc_farmer_receivables"
FARMER_PAYABLE_COLLECTION = "farmer_payables"
FARMER_PURCHASE_COLLECTION = "farmer_purchase_entries"
UFC_FARMER_ORDER_COLLECTION = "ufc_farmer_orders"

MONEY_QUANTUM = Decimal("0.01")
PAYMENT_MODES = {
    "cash": "Cash",
    "upi": "UPI",
    "bank_transfer": "Bank Transfer",
    "cheque": "Cheque",
}
PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
}
SOURCE_LABELS = {
    "supplier_invoice": "Supplier Payment",
    "avpl_ufc_invoice": "UFC Payment to AVPL",
    "ufc_farmer_invoice": "Farmer Payment to UFC",
}


def _decimal(value, default="0"):
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _money(value):
    return f"{_decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}"


def _clean(value, maximum=500):
    return " ".join(str(value or "").split())[:maximum]


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _ensure_indexes():
    definitions = [
        (PAYMENT_COLLECTION, [("payment_number", ASCENDING)], {"unique": True, "name": "payment_number_unique"}),
        (PAYMENT_COLLECTION, [("idempotency_key", ASCENDING)], {"unique": True, "name": "payment_idempotency_unique"}),
        (PAYMENT_COLLECTION, [("source_type", ASCENDING), ("invoice_id", ASCENDING), ("created_at", DESCENDING)], {"name": "payment_invoice_history_idx"}),
        (PAYMENT_COLLECTION, [("payer_key", ASCENDING), ("created_at", DESCENDING)], {"name": "payment_payer_idx"}),
        (PAYMENT_COLLECTION, [("payee_key", ASCENDING), ("created_at", DESCENDING)], {"name": "payment_payee_idx"}),
        (RECEIPT_COLLECTION, [("payment_id", ASCENDING)], {"unique": True, "name": "payment_receipt_payment_unique"}),
        (RECEIPT_COLLECTION, [("receipt_number", ASCENDING)], {"unique": True, "name": "payment_receipt_number_unique"}),
        (ACCOUNTING_EVENT_COLLECTION, [("payment_id", ASCENDING), ("event_role", ASCENDING)], {"unique": True, "name": "payment_accounting_event_unique"}),
        (ACCOUNTING_EVENT_COLLECTION, [("accounting_status", ASCENDING), ("created_at", ASCENDING)], {"name": "payment_accounting_status_idx"}),
        (AUDIT_COLLECTION, [("payment_id", ASCENDING), ("created_at", DESCENDING)], {"name": "payment_audit_idx"}),
    ]
    for collection_name, keys, options in definitions:
        try:
            mongo.db[collection_name].create_index(keys, **options)
        except Exception:
            pass


def _get_actor(user_id):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one({"_id": oid})
    if not actor:
        raise ValueError("Authenticated user was not found.")
    if actor.get("active", True) is False or actor.get("is_active", True) is False or str(actor.get("status") or "").lower() == "inactive":
        raise PermissionError("Inactive users cannot record payments.")
    actor["resolved_role"] = str(actor.get("role") or "").strip().lower()
    actor["resolved_name"] = actor.get("name") or actor.get("full_name") or actor.get("username") or actor.get("phone") or actor["resolved_role"].replace("_", " ").title()
    return actor


def _resolve_ufc_uid(actor):
    uid = _clean(actor.get("centre_uid") or actor.get("mapped_centre_uid"), 80)
    if uid:
        return uid
    master = (
        mongo.db.ufc_admin_master.find_one({"linked_user_id": str(actor.get("_id"))})
        or mongo.db.ufc_admin_master.find_one({"linked_user_id": actor.get("_id")})
        or {}
    )
    return _clean(master.get("centre_uid") or master.get("mapped_centre_uid"), 80)


def _safe_next_number(counter_key, prefix, digits=6):
    # pymongo accepts ReturnDocument.AFTER; importing it here keeps this service
    # isolated from older project snapshots where ReturnDocument may differ.
    from pymongo import ReturnDocument
    year = date.today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"{counter_key}:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"{prefix}-{year}-{sequence:0{digits}d}"


def _source_invoice(source_type, invoice_id):
    oid = _to_object_id(invoice_id)
    if not oid:
        raise ValueError("Invalid invoice reference.")
    collection_name = {
        "supplier_invoice": SUPPLIER_INVOICE_COLLECTION,
        "avpl_ufc_invoice": AVPL_UFC_INVOICE_COLLECTION,
        "ufc_farmer_invoice": UFC_FARMER_INVOICE_COLLECTION,
    }.get(str(source_type or "").strip().lower())
    if not collection_name:
        raise ValueError("Unsupported payment source.")
    invoice = mongo.db[collection_name].find_one({"_id": oid})
    if not invoice:
        raise ValueError("Invoice was not found.")
    return collection_name, invoice


def _assert_access(actor, source_type, invoice, *, write=False):
    role = actor.get("resolved_role")
    if source_type == "supplier_invoice":
        if role not in {"super_admin", "avpl_admin", "accounts"}:
            raise PermissionError("Only AVPL authorized users can manage Supplier payments.")
        if write and invoice.get("payable_posted") is not True:
            raise ValueError("Post the Supplier Invoice and payable before recording a payment.")
        return

    if source_type == "avpl_ufc_invoice":
        if role in {"super_admin", "avpl_admin", "accounts"}:
            return
        if role != "ufc_admin":
            raise PermissionError("You are not authorized for this UFC payment.")
        centre_uid = _resolve_ufc_uid(actor)
        if not centre_uid or str(invoice.get("centre_uid") or "") != centre_uid:
            raise PermissionError("This invoice does not belong to your UFC Centre.")
        return

    if source_type == "ufc_farmer_invoice":
        if role == "ufc_admin":
            centre_uid = _resolve_ufc_uid(actor)
            if not centre_uid or str(invoice.get("centre_uid") or "") != centre_uid:
                raise PermissionError("This invoice does not belong to your UFC Centre.")
            return
        if not write and role == "farmer":
            buyer = invoice.get("buyer") or {}
            if str(buyer.get("farmer_user_id") or buyer.get("farmer_user_id_str") or "") != str(actor.get("_id")):
                # fallback through order link for older Stage 7 documents
                order = mongo.db[UFC_FARMER_ORDER_COLLECTION].find_one({"_id": invoice.get("ufc_farmer_order_id")}) or {}
                if str(order.get("farmer_user_id") or "") != str(actor.get("_id")):
                    raise PermissionError("This invoice does not belong to you.")
            return
        raise PermissionError("Manual Farmer payment collection must be recorded by the UFC Centre.")


def _invoice_number(source_type, invoice):
    if source_type == "supplier_invoice":
        return invoice.get("official_purchase_invoice_number") or invoice.get("supplier_invoice_number") or invoice.get("internal_reference") or str(invoice.get("_id"))
    if source_type == "avpl_ufc_invoice":
        return invoice.get("invoice_number") or invoice.get("document_number") or str(invoice.get("_id"))
    return invoice.get("document_number") or invoice.get("invoice_number") or str(invoice.get("_id"))


def _invoice_total(invoice):
    return _decimal(invoice.get("grand_total") or invoice.get("total_amount") or invoice.get("invoice_total"))


def _current_paid_outstanding(invoice):
    total = _invoice_total(invoice)
    paid = max(_decimal(invoice.get("amount_paid") if invoice.get("amount_paid") is not None else invoice.get("paid_amount")), Decimal("0"))
    outstanding_raw = invoice.get("outstanding_amount")
    outstanding = max(_decimal(outstanding_raw, str(total - paid)) if outstanding_raw not in (None, "") else total - paid, Decimal("0"))
    # Older records may have stale payment fields. Never let paid + outstanding exceed the document total.
    if paid > total:
        paid = total
    if outstanding > total - paid:
        outstanding = max(total - paid, Decimal("0"))
    return total, paid, outstanding


def _status_for(total, paid, outstanding):
    if total <= 0 or outstanding <= Decimal("0.004"):
        return "paid"
    if paid > Decimal("0.004"):
        return "partially_paid"
    return "unpaid"


def _party_snapshot(source_type, invoice):
    if source_type == "supplier_invoice":
        return {
            "payer_key": "AVPL",
            "payer_name": "AVPL",
            "payee_key": str(invoice.get("supplier_ledger_id") or invoice.get("supplier_id") or invoice.get("supplier_name") or "SUPPLIER"),
            "payee_name": invoice.get("supplier_name") or "Supplier",
        }
    if source_type == "avpl_ufc_invoice":
        return {
            "payer_key": str(invoice.get("centre_uid") or "UFC"),
            "payer_name": invoice.get("centre_name") or invoice.get("buyer", {}).get("legal_name") or invoice.get("centre_uid") or "UFC",
            "payee_key": "AVPL",
            "payee_name": (invoice.get("seller") or {}).get("legal_name") or "AVPL",
        }
    buyer = invoice.get("buyer") or {}
    seller = invoice.get("seller") or {}
    return {
        "payer_key": str(buyer.get("farmer_user_id") or buyer.get("farmer_user_id_str") or invoice.get("farmer_user_id") or "FARMER"),
        "payer_name": buyer.get("name") or invoice.get("farmer_name") or "Farmer",
        "payee_key": str(invoice.get("centre_uid") or seller.get("centre_uid") or "UFC"),
        "payee_name": seller.get("legal_name") or invoice.get("centre_name") or invoice.get("centre_uid") or "UFC",
    }


def _sync_linked_documents(source_type, invoice, paid, outstanding, status, payment_id):
    common = {
        "payment_status": status,
        "amount_paid": float(paid),
        "paid_amount": float(paid),
        "outstanding_amount": float(outstanding),
        "last_payment_id": payment_id,
        "last_payment_at": now_utc(),
        "updated_at": now_utc(),
    }
    if source_type == "supplier_invoice":
        mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one({"_id": invoice["_id"]}, {"$set": {**common, "payable_status": "closed" if status == "paid" else "open"}})
        return

    if source_type == "avpl_ufc_invoice":
        order_id = invoice.get("avpl_ufc_order_id")
        sale_id = invoice.get("avpl_ufc_sale_id")
        if sale_id:
            mongo.db[AVPL_UFC_SALE_COLLECTION].update_one({"_id": sale_id}, {"$set": common})
        if order_id:
            mongo.db[AVPL_RECEIVABLE_COLLECTION].update_one({"avpl_ufc_order_id": order_id}, {"$set": {**common, "status": "closed" if status == "paid" else "open"}})
            mongo.db[UFC_PAYABLE_COLLECTION].update_one({"avpl_ufc_order_id": order_id}, {"$set": {**common, "status": "closed" if status == "paid" else "open"}})
            mongo.db[UFC_PURCHASE_COLLECTION].update_one({"avpl_ufc_order_id": order_id}, {"$set": common})
            mongo.db[AVPL_UFC_ORDER_COLLECTION].update_one({"_id": order_id}, {"$set": common})
        return

    order_id = invoice.get("ufc_farmer_order_id")
    sale_id = invoice.get("ufc_farmer_sale_id")
    purchase_id = invoice.get("farmer_purchase_entry_id")
    if sale_id:
        mongo.db[UFC_FARMER_SALE_COLLECTION].update_one({"_id": sale_id}, {"$set": common})
    if purchase_id:
        mongo.db[FARMER_PURCHASE_COLLECTION].update_one({"_id": purchase_id}, {"$set": common})
    if order_id:
        mongo.db[UFC_FARMER_RECEIVABLE_COLLECTION].update_one({"ufc_farmer_order_id": order_id}, {"$set": {**common, "status": "closed" if status == "paid" else "open"}})
        mongo.db[FARMER_PAYABLE_COLLECTION].update_one({"ufc_farmer_order_id": order_id}, {"$set": {**common, "status": "closed" if status == "paid" else "open"}})
        mongo.db[UFC_FARMER_ORDER_COLLECTION].update_one({"_id": order_id}, {"$set": common})


def _create_accounting_events(payment, invoice):
    source_type = payment.get("source_type")
    amount = float(_decimal(payment.get("amount")))
    base = {
        "payment_id": payment["_id"],
        "payment_id_str": str(payment["_id"]),
        "payment_number": payment.get("payment_number") or "",
        "source_type": source_type,
        "source_invoice_id": invoice.get("_id"),
        "source_invoice_number": payment.get("invoice_number") or "",
        "amount": amount,
        "payment_mode": payment.get("payment_mode") or "",
        "payment_reference": payment.get("reference") or "",
        "accounting_status": "ready_for_posting",
        "created_at": now_utc(),
        "updated_at": now_utc(),
    }
    roles = []
    if source_type == "supplier_invoice":
        roles = [("avpl_payment", "AVPL", "Payment voucher: reduce Supplier payable and credit Cash/Bank/UPI/Cheque settlement ledger.")]
    elif source_type == "avpl_ufc_invoice":
        roles = [
            ("avpl_receipt", "AVPL", "Receipt voucher: debit Cash/Bank/UPI/Cheque settlement ledger and reduce UFC receivable."),
            ("ufc_payment", str(invoice.get("centre_uid") or "UFC"), "UFC payment-side event linked to the same settlement."),
        ]
    else:
        roles = [("ufc_receipt", str(invoice.get("centre_uid") or "UFC"), "UFC receipt-side event linked to Farmer receivable settlement.")]
    for event_role, entity_key, note in roles:
        mongo.db[ACCOUNTING_EVENT_COLLECTION].update_one(
            {"payment_id": payment["_id"], "event_role": event_role},
            {"$setOnInsert": {**base, "event_role": event_role, "entity_key": entity_key, "posting_note": note}},
            upsert=True,
        )


def _audit(payment_id, actor, action, note=""):
    mongo.db[AUDIT_COLLECTION].insert_one({
        "payment_id": payment_id,
        "action": action,
        "actor_user_id": actor.get("_id"),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or "",
        "note": _clean(note, 1000),
        "created_at": now_utc(),
    })


def serialize_payment(payment):
    if not payment:
        return None
    row = dict(payment)
    row["id"] = str(row.get("_id") or "")
    row["invoice_id_str"] = str(row.get("invoice_id") or "")
    row["amount_display"] = _money(row.get("amount"))
    row["invoice_total_display"] = _money(row.get("invoice_total"))
    row["paid_after_display"] = _money(row.get("paid_after"))
    row["outstanding_after_display"] = _money(row.get("outstanding_after"))
    row["payment_mode_label"] = PAYMENT_MODES.get(row.get("payment_mode"), str(row.get("payment_mode") or "").replace("_", " ").title())
    row["source_label"] = SOURCE_LABELS.get(row.get("source_type"), row.get("source_type"))
    row["status_label"] = str(row.get("status") or "completed").replace("_", " ").title()
    return row


def _payment_matches_request(payment, source_type, invoice, value, mode):
    return bool(
        payment
        and str(payment.get("source_type") or "") == str(source_type or "")
        and str(payment.get("invoice_id") or "") == str(invoice.get("_id") or "")
        and _decimal(payment.get("amount")) == _decimal(value)
        and str(payment.get("payment_mode") or "") == str(mode or "")
    )


def _ensure_payment_receipt(payment, invoice, parties, final_total, final_paid, final_outstanding, final_status):
    existing = mongo.db[RECEIPT_COLLECTION].find_one({"payment_id": payment["_id"]})
    if existing:
        return existing
    receipt_doc = {
        "receipt_number": _safe_next_number("payment_receipt", "PR"),
        "payment_id": payment["_id"],
        "payment_number": payment.get("payment_number") or "",
        "source_type": payment.get("source_type") or "",
        "invoice_id": invoice["_id"],
        "invoice_number": _invoice_number(payment.get("source_type"), invoice),
        "payer_name": parties.get("payer_name") or "",
        "payee_name": parties.get("payee_name") or "",
        "amount": float(_decimal(payment.get("amount"))),
        "payment_mode": payment.get("payment_mode") or "",
        "reference": payment.get("reference") or "",
        "invoice_total": float(final_total),
        "total_paid": float(final_paid),
        "outstanding": float(final_outstanding),
        "payment_status": final_status,
        "created_at": now_utc(),
    }
    mongo.db[RECEIPT_COLLECTION].update_one(
        {"payment_id": payment["_id"]},
        {"$setOnInsert": receipt_doc},
        upsert=True,
    )
    return mongo.db[RECEIPT_COLLECTION].find_one({"payment_id": payment["_id"]}) or receipt_doc


def _ensure_accounting_handoff(payment, invoice):
    try:
        _create_accounting_events(payment, invoice)
        mongo.db[PAYMENT_COLLECTION].update_one(
            {"_id": payment["_id"]},
            {"$set": {"accounting_handoff_status": "ready", "accounting_handoff_error": None, "updated_at": now_utc()}},
        )
        return ""
    except Exception as exc:
        warning = _clean(exc, 500) or "Accounting handoff event could not be prepared."
        mongo.db[PAYMENT_COLLECTION].update_one(
            {"_id": payment["_id"]},
            {"$set": {"accounting_handoff_status": "attention_required", "accounting_handoff_error": warning, "updated_at": now_utc()}},
        )
        return warning


def record_payment(actor_user_id, source_type, invoice_id, amount, payment_mode, *, reference="", note="", idempotency_key=""):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    source_type = str(source_type or "").strip().lower()
    collection_name, invoice = _source_invoice(source_type, invoice_id)
    _assert_access(actor, source_type, invoice, write=True)

    mode = str(payment_mode or "").strip().lower()
    if mode not in PAYMENT_MODES:
        if mode in {"online", "prepaid", "razorpay"}:
            raise ValueError("Online payment is prepared for future Razorpay integration but is not enabled yet.")
        raise ValueError("Select Cash, UPI, Bank Transfer or Cheque.")
    value = _decimal(amount)
    if value <= 0:
        raise ValueError("Payment amount must be greater than zero.")

    token = _clean(idempotency_key, 160) or f"PAY-{uuid4().hex.upper()}"
    existing = mongo.db[PAYMENT_COLLECTION].find_one({"idempotency_key": token})
    resume_payment = None
    if existing:
        if existing.get("status") == "completed":
            if not _payment_matches_request(existing, source_type, invoice, value, mode):
                raise RuntimeError("This payment request token is already linked to a different settlement. Refresh and try again.")
            accounting_warning = _ensure_accounting_handoff(existing, invoice)
            message = "This payment was already recorded safely."
            if accounting_warning:
                message += " Payment is settled, but the Accounting handoff needs attention."
            return {"payment": serialize_payment(existing), "message": message, "idempotent_replay": True, "accounting_warning": accounting_warning}
        if existing.get("status") == "reversed":
            raise ValueError("This payment request token belongs to a reversed payment. Refresh and try again.")
        if existing.get("status") == "failed":
            raise ValueError("A previous attempt with this payment token failed. Refresh the page and record it again after reviewing the outstanding balance.")
        if existing.get("status") == "processing":
            # Crash/timeout recovery: resume the same payment record instead of
            # attempting to insert a duplicate or asking the operator to guess
            # whether money was already applied.
            if (
                existing.get("source_type") != source_type
                or str(existing.get("invoice_id")) != str(invoice.get("_id"))
                or _decimal(existing.get("amount")) != value
                or str(existing.get("payment_mode") or "") != mode
            ):
                raise RuntimeError("This payment token is already being used for a different settlement. Refresh and try again.")
            resume_payment = existing

    total, paid, outstanding = _current_paid_outstanding(invoice)
    already_applied = bool(resume_payment and resume_payment.get("_id") in (invoice.get("payment_ids") or []))
    if outstanding <= 0 and not already_applied:
        raise ValueError("This invoice is already fully paid.")
    if value > outstanding + Decimal("0.004") and not already_applied:
        raise ValueError(f"Payment cannot exceed the outstanding amount of ₹{_money(outstanding)}.")

    clean_reference = _clean(reference, 160)
    if mode in {"upi", "bank_transfer", "cheque"} and not clean_reference:
        raise ValueError(f"{PAYMENT_MODES[mode]} reference is required for audit and reconciliation.")
    if clean_reference:
        # A bank/UPI/cheque reference normally identifies one real-world transfer.
        # Reusing the exact same reference+amount against the same invoice is
        # treated as a replay rather than creating a second settlement.
        duplicate_reference = mongo.db[PAYMENT_COLLECTION].find_one({
            "source_type": source_type,
            "invoice_id": invoice["_id"],
            "payment_mode": mode,
            "reference": clean_reference,
            "amount": float(value),
            "status": "completed",
        })
        if duplicate_reference:
            return {
                "payment": serialize_payment(duplicate_reference),
                "message": "A payment with this reference and amount is already recorded against this invoice.",
                "idempotent_replay": True,
            }

    parties = _party_snapshot(source_type, invoice)
    timestamp = now_utc()
    if resume_payment:
        payment = resume_payment
    else:
        payment = {
            "payment_number": _safe_next_number("unified_payment", "PAY"),
            "idempotency_key": token,
            "source_type": source_type,
            "source_label": SOURCE_LABELS.get(source_type, source_type),
            "invoice_collection": collection_name,
            "invoice_id": invoice["_id"],
            "invoice_id_str": str(invoice["_id"]),
            "invoice_number": _invoice_number(source_type, invoice),
            "invoice_total": float(total),
            "amount": float(value),
            "payment_mode": mode,
            "reference": clean_reference,
            "note": _clean(note, 1000),
            # Reserved provider fields keep the schema ready for Razorpay/other
            # prepaid gateways without changing the manual settlement model later.
            "payment_provider": None,
            "provider_order_id": None,
            "provider_payment_id": None,
            "provider_signature": None,
            "provider_status": None,
            **parties,
            "status": "processing",
            "recorded_by": actor["_id"],
            "recorded_by_name": actor.get("resolved_name") or "",
            "recorded_by_role": actor.get("resolved_role") or "",
            "payment_date": date.today().isoformat(),
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = mongo.db[PAYMENT_COLLECTION].insert_one(payment)
            payment["_id"] = result.inserted_id
        except DuplicateKeyError:
            existing = mongo.db[PAYMENT_COLLECTION].find_one({"idempotency_key": token})
            if existing and existing.get("status") == "completed":
                if not _payment_matches_request(existing, source_type, invoice, value, mode):
                    raise RuntimeError("This payment request token is already linked to a different settlement. Refresh and try again.")
                accounting_warning = _ensure_accounting_handoff(existing, invoice)
                message = "This payment was already recorded safely."
                if accounting_warning:
                    message += " Payment is settled, but the Accounting handoff needs attention."
                return {"payment": serialize_payment(existing), "message": message, "idempotent_replay": True, "accounting_warning": accounting_warning}
            if existing and existing.get("status") == "processing":
                raise RuntimeError("This payment is already being processed in another request. Wait a moment, then refresh once.")
            raise RuntimeError("This payment request could not be inserted safely. Refresh and try again.")

    # Optimistic, idempotent invoice settlement. The payment ID itself becomes
    # the permanent guard against a repeated POST or recovery retry.
    settled_invoice = None
    for _attempt in range(5):
        latest = mongo.db[collection_name].find_one({"_id": invoice["_id"]})
        if not latest:
            break
        if payment["_id"] in (latest.get("payment_ids") or []):
            settled_invoice = latest
            break
        latest_total, latest_paid, latest_outstanding = _current_paid_outstanding(latest)
        if value > latest_outstanding + Decimal("0.004"):
            mongo.db[PAYMENT_COLLECTION].update_one({"_id": payment["_id"]}, {"$set": {"status": "failed", "failure_reason": "Outstanding changed before settlement.", "updated_at": now_utc()}})
            raise RuntimeError("The outstanding amount changed in another session. Refresh before recording this payment.")
        version = int(latest.get("payment_version") or 0)
        new_paid = min(latest_paid + value, latest_total)
        new_outstanding = max(latest_total - new_paid, Decimal("0"))
        new_status = _status_for(latest_total, new_paid, new_outstanding)
        version_filter = {"payment_version": version}
        if version == 0:
            version_filter = {"$or": [{"payment_version": 0}, {"payment_version": {"$exists": False}}]}
        settle_query = {"_id": latest["_id"], "payment_ids": {"$ne": payment["_id"]}, **version_filter}
        result = mongo.db[collection_name].update_one(
            settle_query,
            {"$set": {
                "amount_paid": float(new_paid),
                "paid_amount": float(new_paid),
                "outstanding_amount": float(new_outstanding),
                "payment_status": new_status,
                "payment_version": version + 1,
                "last_payment_id": payment["_id"],
                "last_payment_at": now_utc(),
                "updated_at": now_utc(),
            }, "$addToSet": {"payment_ids": payment["_id"]}},
        )
        if result.modified_count == 1:
            settled_invoice = mongo.db[collection_name].find_one({"_id": latest["_id"]})
            break
    if not settled_invoice:
        mongo.db[PAYMENT_COLLECTION].update_one({"_id": payment["_id"]}, {"$set": {"status": "failed", "failure_reason": "Invoice settlement concurrency failure.", "updated_at": now_utc()}})
        raise RuntimeError("Payment could not be applied safely because the invoice changed. Refresh and try again.")

    final_total, final_paid, final_outstanding = _current_paid_outstanding(settled_invoice)
    final_status = _status_for(final_total, final_paid, final_outstanding)
    _sync_linked_documents(source_type, settled_invoice, final_paid, final_outstanding, final_status, payment["_id"])

    receipt = _ensure_payment_receipt(
        payment, settled_invoice, parties, final_total, final_paid, final_outstanding, final_status
    )
    receipt_number = receipt.get("receipt_number") or ""

    effective_paid_before = paid
    effective_outstanding_before = outstanding
    if resume_payment and payment["_id"] in (settled_invoice.get("payment_ids") or []):
        effective_paid_before = max(final_paid - value, Decimal("0"))
        effective_outstanding_before = min(final_outstanding + value, final_total)
    mongo.db[PAYMENT_COLLECTION].update_one({"_id": payment["_id"]}, {"$set": {
        "status": "completed",
        "receipt_number": receipt_number,
        "paid_before": float(effective_paid_before),
        "outstanding_before": float(effective_outstanding_before),
        "paid_after": float(final_paid),
        "outstanding_after": float(final_outstanding),
        "invoice_payment_status_after": final_status,
        "completed_at": now_utc(),
        "updated_at": now_utc(),
    }})
    payment = mongo.db[PAYMENT_COLLECTION].find_one({"_id": payment["_id"]})
    accounting_warning = _ensure_accounting_handoff(payment, settled_invoice)
    payment = mongo.db[PAYMENT_COLLECTION].find_one({"_id": payment["_id"]}) or payment
    _audit(payment["_id"], actor, "record_payment", f"₹{_money(value)} recorded by {PAYMENT_MODES.get(mode, mode)} against {_invoice_number(source_type, settled_invoice)}.")
    message = f"Payment ₹{_money(value)} recorded successfully. Outstanding is now ₹{_money(final_outstanding)}."
    if accounting_warning:
        message += " Settlement is complete, but the controlled Accounting handoff needs attention."
    return {
        "payment": serialize_payment(payment),
        "message": message,
        "idempotent_replay": False,
        "accounting_warning": accounting_warning,
    }


def reverse_payment(actor_user_id, payment_id, reason):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    oid = _to_object_id(payment_id)
    if not oid:
        raise ValueError("Invalid payment reference.")
    payment = mongo.db[PAYMENT_COLLECTION].find_one({"_id": oid})
    if not payment:
        raise ValueError("Payment was not found.")
    if payment.get("status") == "reversed":
        return {"payment": serialize_payment(payment), "message": "This payment is already reversed."}
    if payment.get("status") != "completed":
        raise ValueError("Only a completed payment can be reversed.")
    source_type = payment.get("source_type")
    collection_name, invoice = _source_invoice(source_type, payment.get("invoice_id"))
    _assert_access(actor, source_type, invoice, write=True)
    role = actor.get("resolved_role")
    # Reversals are intentionally stricter than recording. Supplier and
    # AVPL↔UFC settlements affect AVPL books, so only AVPL financial roles can
    # reverse them. UFC Admin may reverse only collections made from Farmers.
    if source_type in {"supplier_invoice", "avpl_ufc_invoice"}:
        if role not in {"super_admin", "avpl_admin", "accounts"}:
            raise PermissionError("Only AVPL authorized financial users can reverse this payment.")
    elif source_type == "ufc_farmer_invoice":
        if role not in {"super_admin", "avpl_admin", "accounts", "ufc_admin"}:
            raise PermissionError("You are not authorized to reverse this Farmer payment.")
    else:
        raise PermissionError("You are not authorized to reverse this payment.")
    reason = _clean(reason, 1000)
    if len(reason) < 4:
        raise ValueError("Enter a clear reason for the payment reversal.")

    amount = _decimal(payment.get("amount"))
    latest_total, latest_paid, _latest_outstanding = _current_paid_outstanding(invoice)
    if latest_paid + Decimal("0.004") < amount:
        raise RuntimeError("This payment cannot be reversed because later settlement data is inconsistent. Review the payment history first.")
    new_paid = max(latest_paid - amount, Decimal("0"))
    new_outstanding = max(latest_total - new_paid, Decimal("0"))
    new_status = _status_for(latest_total, new_paid, new_outstanding)
    version = int(invoice.get("payment_version") or 0)
    version_filter = {"payment_version": version}
    if version == 0:
        version_filter = {"$or": [{"payment_version": 0}, {"payment_version": {"$exists": False}}]}
    reversal_query = {"_id": invoice["_id"], "payment_ids": payment["_id"], **version_filter}
    result = mongo.db[collection_name].update_one(
        reversal_query,
        {"$set": {
            "amount_paid": float(new_paid), "paid_amount": float(new_paid), "outstanding_amount": float(new_outstanding),
            "payment_status": new_status, "payment_version": version + 1, "updated_at": now_utc(),
        }, "$pull": {"payment_ids": payment["_id"]}},
    )
    if result.modified_count != 1:
        raise RuntimeError("The invoice changed while reversing this payment. Refresh and try again.")
    updated_invoice = mongo.db[collection_name].find_one({"_id": invoice["_id"]})
    _sync_linked_documents(source_type, updated_invoice, new_paid, new_outstanding, new_status, None)
    mongo.db[PAYMENT_COLLECTION].update_one({"_id": payment["_id"], "status": "completed"}, {"$set": {
        "status": "reversed", "reversal_reason": reason, "reversed_by": actor["_id"], "reversed_by_name": actor.get("resolved_name") or "", "reversed_at": now_utc(), "updated_at": now_utc(),
    }})
    mongo.db[RECEIPT_COLLECTION].update_one(
        {"payment_id": payment["_id"]},
        {"$set": {
            "reversed": True,
            "reversal_reason": reason,
            "reversed_at": now_utc(),
            "reversed_by_name": actor.get("resolved_name") or "",
            "invoice_total_after_reversal": float(latest_total),
            "total_paid_after_reversal": float(new_paid),
            "outstanding_after_reversal": float(new_outstanding),
            "payment_status_after_reversal": new_status,
        }},
    )
    mongo.db[ACCOUNTING_EVENT_COLLECTION].update_many({"payment_id": payment["_id"]}, {"$set": {"accounting_status": "reversal_required", "reversal_reason": reason, "updated_at": now_utc()}})
    _audit(payment["_id"], actor, "reverse_payment", reason)
    return {"payment": serialize_payment(mongo.db[PAYMENT_COLLECTION].find_one({"_id": payment["_id"]})), "message": f"Payment reversed. Outstanding is now ₹{_money(new_outstanding)}."}


def _serialize_invoice_row(source_type, invoice):
    total, paid, outstanding = _current_paid_outstanding(invoice)
    parties = _party_snapshot(source_type, invoice)
    raw_due_date = invoice.get("due_date") or ""
    due_iso = ""
    is_overdue = False
    if raw_due_date:
        try:
            if isinstance(raw_due_date, datetime):
                due_day = raw_due_date.date()
            elif isinstance(raw_due_date, date):
                due_day = raw_due_date
            else:
                due_day = datetime.strptime(str(raw_due_date)[:10], "%Y-%m-%d").date()
            due_iso = due_day.isoformat()
            is_overdue = outstanding > Decimal("0.004") and due_day < date.today()
        except Exception:
            due_iso = str(raw_due_date)[:10]
    row = {
        "source_type": source_type,
        "id": str(invoice.get("_id") or ""),
        "invoice_number": _invoice_number(source_type, invoice),
        "total_display": _money(total),
        "paid_display": _money(paid),
        "outstanding_display": _money(outstanding),
        "payment_status": _status_for(total, paid, outstanding),
        "payment_status_label": PAYMENT_STATUS_LABELS.get(_status_for(total, paid, outstanding), "Unpaid"),
        "payer_name": parties.get("payer_name") or "",
        "payee_name": parties.get("payee_name") or "",
        "payment_token": f"PAYFORM-{uuid4().hex.upper()}",
        "due_date": due_iso,
        "is_overdue": is_overdue,
        "due_status_label": "Overdue" if is_overdue else ("Due" if due_iso and outstanding > Decimal("0.004") else ""),
    }
    if source_type == "supplier_invoice":
        row.update({"party_name": invoice.get("supplier_name") or "Supplier", "reference": invoice.get("po_number") or "", "can_pay": invoice.get("payable_posted") is True and outstanding > 0})
    elif source_type == "avpl_ufc_invoice":
        row.update({"party_name": invoice.get("centre_name") or invoice.get("centre_uid") or "UFC", "reference": invoice.get("avpl_order_number") or "", "centre_uid": invoice.get("centre_uid") or "", "can_pay": outstanding > 0})
    else:
        buyer = invoice.get("buyer") or {}
        row.update({"party_name": buyer.get("name") or invoice.get("farmer_name") or "Farmer", "reference": invoice.get("order_number") or "", "centre_uid": invoice.get("centre_uid") or "", "can_pay": outstanding > 0})
    return row


def _recent_payments(query=None, limit=25):
    query = {**(query or {}), "status": {"$in": ["completed", "reversed"]}}
    return [serialize_payment(row) for row in mongo.db[PAYMENT_COLLECTION].find(query).sort("created_at", DESCENDING).limit(limit)]


def _accounting_event_rows(query=None, limit=50):
    rows = []
    for event in mongo.db[ACCOUNTING_EVENT_COLLECTION].find(query or {}).sort("created_at", DESCENDING).limit(limit):
        row = dict(event)
        row["id"] = str(row.get("_id") or "")
        row["payment_id_str"] = str(row.get("payment_id") or "")
        row["amount_display"] = _money(row.get("amount"))
        row["status_label"] = str(row.get("accounting_status") or "ready_for_posting").replace("_", " ").title()
        rows.append(row)
    return rows


def get_avpl_payment_overview(actor_user_id):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") not in {"super_admin", "avpl_admin", "accounts"}:
        raise PermissionError("Only AVPL authorized users can view this payment workspace.")
    supplier_rows = [_serialize_invoice_row("supplier_invoice", row) for row in mongo.db[SUPPLIER_INVOICE_COLLECTION].find({"payable_posted": True, "posting_status": "posted"}).sort("posted_at", DESCENDING).limit(100)]
    ufc_rows = [_serialize_invoice_row("avpl_ufc_invoice", row) for row in mongo.db[AVPL_UFC_INVOICE_COLLECTION].find({"status": "issued"}).sort("issued_at", DESCENDING).limit(100)]
    recent = _recent_payments({"$or": [{"source_type": "supplier_invoice"}, {"source_type": "avpl_ufc_invoice"}]})
    supplier_due = sum((_decimal(r["outstanding_display"]) for r in supplier_rows), Decimal("0"))
    ufc_due = sum((_decimal(r["outstanding_display"]) for r in ufc_rows), Decimal("0"))
    pending_accounting = mongo.db[ACCOUNTING_EVENT_COLLECTION].count_documents({"entity_key": "AVPL", "accounting_status": {"$in": ["ready_for_posting", "reversal_required"]}})
    return {
        "supplier_payables": supplier_rows,
        "ufc_receivables": ufc_rows,
        "recent_payments": recent,
        "accounting_events": _accounting_event_rows({"entity_key": "AVPL"}),
        "payment_modes": PAYMENT_MODES,
        "summary": {"supplier_due": _money(supplier_due), "ufc_due": _money(ufc_due), "recent_count": len(recent), "accounting_pending": pending_accounting},
    }


def get_ufc_payment_overview(actor_user_id):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") != "ufc_admin":
        raise PermissionError("Only UFC Admin can view the UFC payment workspace.")
    centre_uid = _resolve_ufc_uid(actor)
    if not centre_uid:
        raise ValueError("This UFC Admin is not linked to a Centre UID.")
    avpl_rows = [_serialize_invoice_row("avpl_ufc_invoice", row) for row in mongo.db[AVPL_UFC_INVOICE_COLLECTION].find({"centre_uid": centre_uid, "status": "issued"}).sort("issued_at", DESCENDING).limit(100)]
    farmer_rows = [_serialize_invoice_row("ufc_farmer_invoice", row) for row in mongo.db[UFC_FARMER_INVOICE_COLLECTION].find({"centre_uid": centre_uid, "status": "issued"}).sort("issued_at", DESCENDING).limit(100)]
    recent = _recent_payments({"$or": [{"source_type": "avpl_ufc_invoice", "payer_key": centre_uid}, {"source_type": "ufc_farmer_invoice", "payee_key": centre_uid}]})
    avpl_due = sum((_decimal(r["outstanding_display"]) for r in avpl_rows), Decimal("0"))
    farmer_due = sum((_decimal(r["outstanding_display"]) for r in farmer_rows), Decimal("0"))
    return {
        "centre_uid": centre_uid,
        "avpl_payables": avpl_rows,
        "farmer_receivables": farmer_rows,
        "recent_payments": recent,
        "payment_modes": PAYMENT_MODES,
        "summary": {"avpl_due": _money(avpl_due), "farmer_due": _money(farmer_due), "recent_count": len(recent)},
    }


def get_farmer_payment_overview(actor_user_id):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") != "farmer":
        raise PermissionError("Only Farmers can view My Payments.")
    invoices = list(mongo.db[UFC_FARMER_INVOICE_COLLECTION].find({"$or": [
        {"buyer.farmer_user_id": actor["_id"]},
        {"buyer.farmer_user_id_str": str(actor["_id"])},
    ]}).sort("issued_at", DESCENDING).limit(100))
    rows = []
    for invoice in invoices:
        try:
            _assert_access(actor, "ufc_farmer_invoice", invoice, write=False)
            rows.append(_serialize_invoice_row("ufc_farmer_invoice", invoice))
        except PermissionError:
            continue
    recent = _recent_payments({"source_type": "ufc_farmer_invoice", "payer_key": str(actor["_id"])})
    due = sum((_decimal(r["outstanding_display"]) for r in rows), Decimal("0"))
    return {"payables": rows, "recent_payments": recent, "summary": {"outstanding": _money(due), "recent_count": len(recent)}}


def get_payment_receipt_context(actor_user_id, payment_id):
    _ensure_indexes()
    actor = _get_actor(actor_user_id)
    oid = _to_object_id(payment_id)
    if not oid:
        raise ValueError("Invalid payment reference.")
    payment = mongo.db[PAYMENT_COLLECTION].find_one({"_id": oid})
    if not payment:
        raise ValueError("Payment was not found.")
    collection_name, invoice = _source_invoice(payment.get("source_type"), payment.get("invoice_id"))
    _assert_access(actor, payment.get("source_type"), invoice, write=False)
    receipt = mongo.db[RECEIPT_COLLECTION].find_one({"payment_id": oid}) or {}
    return {
        "payment": serialize_payment(payment),
        "receipt": {
            **receipt,
            "id": str(receipt.get("_id") or ""),
            "amount_display": _money(receipt.get("amount") or payment.get("amount")),
            "invoice_total_display": _money(receipt.get("invoice_total") or payment.get("invoice_total")),
            "total_paid_display": _money(receipt.get("total_paid") or payment.get("paid_after")),
            "outstanding_display": _money(receipt.get("outstanding") or payment.get("outstanding_after")),
            "payment_status_label": PAYMENT_STATUS_LABELS.get(receipt.get("payment_status") or payment.get("invoice_payment_status_after"), "Unpaid"),
            "payment_mode_label": PAYMENT_MODES.get(receipt.get("payment_mode") or payment.get("payment_mode"), ""),
            "reversed": bool(receipt.get("reversed") or payment.get("status") == "reversed"),
            "total_paid_after_reversal_display": _money(receipt.get("total_paid_after_reversal")),
            "outstanding_after_reversal_display": _money(receipt.get("outstanding_after_reversal")),
            "payment_status_after_reversal_label": PAYMENT_STATUS_LABELS.get(receipt.get("payment_status_after_reversal"), str(receipt.get("payment_status_after_reversal") or "").replace("_", " ").title()),
        },
        "invoice": invoice,
        "invoice_collection": collection_name,
    }
