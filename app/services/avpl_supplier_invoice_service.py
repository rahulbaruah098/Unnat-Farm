from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.avpl_goods_receipt_service import (
    GOODS_RECEIPT_COLLECTION,
    STATUS_POSTED as GRN_STATUS_POSTED,
)
from app.services.avpl_purchase_order_service import (
    AVPL_ENTITY_CODE,
    PURCHASE_ORDER_COLLECTION,
    STATUS_APPROVED as PO_STATUS_APPROVED,
    STATUS_FULLY_RECEIVED as PO_STATUS_FULLY_RECEIVED,
    STATUS_PARTIALLY_RECEIVED as PO_STATUS_PARTIALLY_RECEIVED,
    serialize_purchase_order,
)
from app.utils.helpers import now_utc


SUPPLIER_INVOICE_COLLECTION = "avpl_supplier_invoices"

STATUS_MATCHED = "matched"
STATUS_MATCHED_WITH_WARNINGS = "matched_with_warnings"
STATUS_MISMATCH = "mismatch"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_MATCHED: "Matched",
    STATUS_MATCHED_WITH_WARNINGS: "Matched with Warnings",
    STATUS_MISMATCH: "Mismatch",
    STATUS_CANCELLED: "Cancelled",
}

ALLOWED_ROLES = {"accounts", "avpl_admin", "super_admin"}
ELIGIBLE_PO_STATUSES = {
    PO_STATUS_APPROVED,
    PO_STATUS_PARTIALLY_RECEIVED,
    PO_STATUS_FULLY_RECEIVED,
}

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.0001")
RATE_QUANTUM = Decimal("0.001")


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
        if (
            same_keys
            and bool(metadata.get("unique", False)) == required_unique
            and metadata.get("partialFilterExpression") == required_partial
        ):
            return existing_name
        raise RuntimeError(
            f"Conflicting index detected on {collection.name}: "
            f"{existing_name}. No index was dropped automatically."
        )

    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure as exc:
        raise RuntimeError(
            f"Could not create Supplier Invoice index {name}."
        ) from exc


def ensure_supplier_invoice_indexes():
    collection = mongo.db[SUPPLIER_INVOICE_COLLECTION]
    _ensure_exact_index(
        collection,
        [("internal_reference", ASCENDING)],
        name="avpl_supplier_invoice_internal_reference_unique",
        unique=True,
    )
    _ensure_exact_index(
        collection,
        [("document_uid", ASCENDING)],
        name="avpl_supplier_invoice_uid_unique",
        unique=True,
    )
    _ensure_exact_index(
        collection,
        [("supplier_invoice_identity_key", ASCENDING)],
        name="avpl_supplier_invoice_supplier_number_unique",
        unique=True,
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("invoice_date", DESCENDING),
        ],
        name="avpl_supplier_invoice_entity_status_date_idx",
    )
    _ensure_exact_index(
        collection,
        [("purchase_order_id", ASCENDING), ("invoice_date", DESCENDING)],
        name="avpl_supplier_invoice_po_date_idx",
    )


def _get_actor(actor_user_id):
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
    if role not in ALLOWED_ROLES:
        raise PermissionError(
            "You are not authorized to manage AVPL Supplier Invoices."
        )
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError(
            "Inactive users cannot manage AVPL Supplier Invoices."
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


def _active_avpl_entity(accounting_entity_id=None):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    if accounting_entity_id:
        entity_id = _to_object_id(accounting_entity_id)
        if not entity_id:
            raise ValueError("Invalid Accounting entity.")
        query["_id"] = entity_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise RuntimeError(
            "The active AVPL Accounting entity is unavailable."
        )
    return entity


def _clean_text(value, label, maximum=500, required=False):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _clean_multiline(value, label, maximum=1500):
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _parse_date(value, label, required=True):
    if isinstance(value, datetime):
        return datetime.combine(value.date(), time.min)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def _decimal(
    value,
    label,
    *,
    minimum="0",
    maximum="999999999999.99",
    quantum=MONEY_QUANTUM,
):
    try:
        number = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be finite.")
    if number < Decimal(minimum):
        raise ValueError(f"{label} cannot be below {minimum}.")
    if number > Decimal(maximum):
        raise ValueError(f"{label} exceeds the supported limit.")
    return number.quantize(quantum, rounding=ROUND_HALF_UP)


def _money(value):
    return _decimal(value, "Amount", quantum=MONEY_QUANTUM)


def _decimal_string(value, quantum=MONEY_QUANTUM):
    return format(
        Decimal(str(value or 0)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        ),
        "f",
    )


def _quantity_string(value):
    return _decimal_string(value, QTY_QUANTUM)


def _rate_string(value):
    return _decimal_string(value, RATE_QUANTUM).rstrip("0").rstrip(".") or "0"


def _parse_items(raw_items):
    if isinstance(raw_items, str):
        try:
            items = json.loads(raw_items or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Supplier Invoice product lines are invalid.") from exc
    else:
        items = raw_items
    if not isinstance(items, list) or not items:
        raise ValueError("Record at least one Supplier Invoice product line.")
    if len(items) > 100:
        raise ValueError(
            "A Supplier Invoice cannot contain more than 100 product lines."
        )
    return items


def _next_internal_reference(invoice_date):
    year = invoice_date.year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_supplier_invoice:{year}"},
        {
            "$inc": {"sequence": 1},
            "$setOnInsert": {
                "counter_type": "avpl_supplier_invoice",
                "year": year,
                "created_at": now_utc(),
            },
            "$set": {"updated_at": now_utc()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"AVPL-SINV-{year}-{sequence:05d}"


def _normalize_invoice_number(value):
    original = _clean_text(
        value,
        "Supplier invoice number",
        maximum=80,
        required=True,
    )
    normalized = re.sub(r"[^A-Z0-9]", "", original.upper())
    if len(normalized) < 2:
        raise ValueError(
            "Supplier invoice number must contain at least two letters or numbers."
        )
    return original, normalized


def _history_event(
    action,
    actor,
    previous_status=None,
    new_status=None,
    reason="",
    changed_fields=None,
):
    return {
        "action": action,
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or "",
        "previous_status": previous_status,
        "new_status": new_status,
        "reason": str(reason or "")[:1000],
        "changed_fields": sorted(set(changed_fields or [])),
        "at": now_utc(),
    }


def _record_audit(
    invoice,
    actor,
    action,
    previous_status=None,
    reason="",
    changed_fields=None,
):
    audit = {
        "module": "avpl_procurement",
        "submodule": "supplier_invoice_three_way_match",
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
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or "",
        "previous_status": previous_status,
        "new_status": invoice.get("status"),
        "metadata": {
            "internal_reference": invoice.get("internal_reference"),
            "supplier_invoice_number": invoice.get(
                "supplier_invoice_number"
            ),
            "po_number": invoice.get("po_number"),
            "supplier_name": invoice.get("supplier_name"),
            "blocking_mismatch_count": invoice.get(
                "blocking_mismatch_count"
            ),
            "warning_count": invoice.get("warning_count"),
            "grand_total": invoice.get("grand_total"),
            "version": int(invoice.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": str(reason or "")[:1500],
        "created_at": now_utc(),
    }
    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
    except Exception as exc:
        mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
            {"_id": invoice.get("_id")},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_error": str(exc)[:500],
                    "audit_sync_marked_at": now_utc(),
                }
            },
        )


def _get_purchase_order(entity_id, purchase_order_id):
    order_id = _to_object_id(purchase_order_id)
    if not order_id:
        raise ValueError("Select a valid Purchase Order.")
    order = mongo.db[PURCHASE_ORDER_COLLECTION].find_one(
        {
            "_id": order_id,
            "accounting_entity_id": entity_id,
            "status": {"$in": list(ELIGIBLE_PO_STATUSES)},
        }
    )
    if not order:
        raise ValueError(
            "The selected Purchase Order is not approved or available for invoice matching."
        )
    return order


def _posted_grn_context(purchase_order_id):
    rows = list(
        mongo.db[GOODS_RECEIPT_COLLECTION]
        .find(
            {
                "purchase_order_id": purchase_order_id,
                "status": GRN_STATUS_POSTED,
                "stock_posted": True,
            }
        )
        .sort([("receipt_date", ASCENDING), ("created_at", ASCENDING)])
    )
    totals = {}
    for receipt in rows:
        for item in receipt.get("items") or []:
            line_no = int(item.get("po_line_no") or 0)
            if not line_no:
                continue
            bucket = totals.setdefault(
                line_no,
                {
                    "received": Decimal("0"),
                    "accepted": Decimal("0"),
                    "rejected": Decimal("0"),
                    "damaged": Decimal("0"),
                },
            )
            bucket["received"] += Decimal(
                str(item.get("received_quantity") or 0)
            )
            bucket["accepted"] += Decimal(
                str(item.get("accepted_quantity") or 0)
            )
            bucket["rejected"] += Decimal(
                str(item.get("rejected_quantity") or 0)
            )
            bucket["damaged"] += Decimal(
                str(item.get("damaged_quantity") or 0)
            )
    return rows, totals


def _already_invoiced_totals(purchase_order_id, exclude_invoice_id=None):
    query = {
        "purchase_order_id": purchase_order_id,
        "status": {"$ne": STATUS_CANCELLED},
        "posting_status": {"$ne": "reversed"},
    }
    if exclude_invoice_id:
        query["_id"] = {"$ne": exclude_invoice_id}

    totals = {}
    for invoice in mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
        query,
        {"items": 1},
    ):
        for item in invoice.get("items") or []:
            line_no = int(item.get("po_line_no") or 0)
            if not line_no:
                continue
            totals[line_no] = totals.get(line_no, Decimal("0")) + Decimal(
                str(item.get("invoice_quantity") or 0)
            )
    return totals


def _expected_gst_rate(po_item):
    if po_item.get("gst_treatment_code") in {
        "UNREGISTERED_SUPPLIER_NO_GST",
        "COMPOSITION_SUPPLIER_NO_ITC",
        "EXEMPT_SUPPLIER_NO_GST",
    }:
        return Decimal("0")
    igst = Decimal(str(po_item.get("igst_rate") or 0))
    if igst > 0:
        return igst
    return Decimal(str(po_item.get("cgst_rate") or 0)) + Decimal(
        str(po_item.get("sgst_rate") or 0)
    )


def _mismatch(
    code,
    message,
    *,
    severity="blocking",
    line_no=None,
    expected="",
    actual="",
):
    return {
        "code": code,
        "message": message,
        "severity": severity,
        "line_no": line_no,
        "expected": str(expected or ""),
        "actual": str(actual or ""),
    }


def _build_invoice_payload(entity, actor, raw_payload, existing=None):
    order = _get_purchase_order(
        entity["_id"],
        raw_payload.get("purchase_order_id"),
    )
    invoice_date = _parse_date(
        raw_payload.get("invoice_date"),
        "Supplier invoice date",
    )
    order_date = order.get("order_date")

    supplier_invoice_number, normalized_number = _normalize_invoice_number(
        raw_payload.get("supplier_invoice_number")
    )
    identity_key = (
        f"{entity['_id']}:{order.get('supplier_ledger_id')}:"
        f"{normalized_number}"
    )

    supplier_snapshot = order.get("supplier_snapshot") or {}
    credit_days = int(supplier_snapshot.get("credit_period_days") or 0)
    due_date = _parse_date(
        raw_payload.get("due_date"),
        "Due date",
        required=False,
    )
    if not due_date:
        due_date = invoice_date + timedelta(days=credit_days)
    if due_date < invoice_date:
        raise ValueError("Due date cannot be before the invoice date.")

    posted_grns, accepted_totals = _posted_grn_context(order["_id"])
    existing_id = existing.get("_id") if existing else None
    already_invoiced = _already_invoiced_totals(
        order["_id"],
        exclude_invoice_id=existing_id,
    )
    order_items = {
        int(item.get("line_no") or index): item
        for index, item in enumerate(order.get("items") or [], start=1)
    }

    raw_items = _parse_items(
        raw_payload.get("items") or raw_payload.get("items_json")
    )
    items = []
    mismatches = []
    seen_lines = set()
    subtotal = Decimal("0")
    discount_total = Decimal("0")
    taxable_total = Decimal("0")
    cgst_total = Decimal("0")
    sgst_total = Decimal("0")
    igst_total = Decimal("0")
    tax_total = Decimal("0")

    if not posted_grns:
        mismatches.append(
            _mismatch(
                "NO_POSTED_GRN",
                "No posted Goods Receipt exists for this Purchase Order.",
                expected="At least one posted GRN",
                actual="None",
            )
        )

    if (
        isinstance(order_date, datetime)
        and invoice_date.date() < order_date.date()
    ):
        mismatches.append(
            _mismatch(
                "INVOICE_DATE_BEFORE_PO",
                "Supplier invoice date is before the Purchase Order date.",
                severity="warning",
                expected=order_date.strftime("%Y-%m-%d"),
                actual=invoice_date.strftime("%Y-%m-%d"),
            )
        )
    if invoice_date.date() > date.today():
        mismatches.append(
            _mismatch(
                "FUTURE_INVOICE_DATE",
                "Supplier invoice date is in the future.",
                severity="warning",
                expected="Today or earlier",
                actual=invoice_date.strftime("%Y-%m-%d"),
            )
        )

    same_state = str(order.get("supplier_state_code") or "") == str(
        entity.get("state_code") or ""
    )

    for source_index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(
                f"Supplier Invoice line {source_index} is invalid."
            )
        try:
            po_line_no = int(raw_item.get("po_line_no") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Select a valid Purchase Order line on row {source_index}."
            ) from exc
        if po_line_no not in order_items:
            raise ValueError(
                f"Purchase Order line {po_line_no} is not available."
            )
        if po_line_no in seen_lines:
            raise ValueError(
                "The same Purchase Order line cannot appear more than once "
                "in a Supplier Invoice."
            )
        seen_lines.add(po_line_no)

        po_item = order_items[po_line_no]
        quantity = _decimal(
            raw_item.get("invoice_quantity"),
            f"Invoice quantity on line {source_index}",
            minimum="0.0001",
            maximum="999999999.9999",
            quantum=QTY_QUANTUM,
        )
        rate = _decimal(
            raw_item.get("rate"),
            f"Rate on line {source_index}",
            minimum="0.01",
            quantum=MONEY_QUANTUM,
        )
        discount_percent = _decimal(
            raw_item.get("discount_percent") or "0",
            f"Discount on line {source_index}",
            minimum="0",
            maximum="100",
            quantum=RATE_QUANTUM,
        )
        hsn_code = _clean_text(
            raw_item.get("hsn_code"),
            f"HSN on line {source_index}",
            maximum=8,
            required=True,
        )
        if not re.fullmatch(r"\d{4}|\d{6}|\d{8}", hsn_code):
            raise ValueError(
                f"HSN on line {source_index} must contain 4, 6 or 8 digits."
            )
        gst_rate = _decimal(
            raw_item.get("gst_rate") or "0",
            f"GST rate on line {source_index}",
            minimum="0",
            maximum="100",
            quantum=RATE_QUANTUM,
        )

        receipt_totals = accepted_totals.get(
            po_line_no,
            {
                "received": Decimal("0"),
                "accepted": Decimal("0"),
                "rejected": Decimal("0"),
                "damaged": Decimal("0"),
            },
        )
        accepted_quantity = receipt_totals["accepted"]
        invoiced_before = already_invoiced.get(po_line_no, Decimal("0"))
        available_to_invoice = max(
            accepted_quantity - invoiced_before,
            Decimal("0"),
        )
        po_rate = Decimal(str(po_item.get("rate") or 0))
        po_discount = Decimal(
            str(po_item.get("discount_percent") or 0)
        )
        expected_hsn = str(po_item.get("hsn_code") or "")
        expected_gst = _expected_gst_rate(po_item)

        if accepted_quantity <= 0:
            mismatches.append(
                _mismatch(
                    "NO_ACCEPTED_QUANTITY",
                    f"No accepted GRN quantity exists for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected="Accepted quantity above 0",
                    actual=_quantity_string(accepted_quantity),
                )
            )
        if quantity > available_to_invoice:
            mismatches.append(
                _mismatch(
                    "INVOICE_QTY_EXCEEDS_ACCEPTED",
                    f"Invoice quantity exceeds the uninvoiced accepted GRN quantity for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected=_quantity_string(available_to_invoice),
                    actual=_quantity_string(quantity),
                )
            )
        if abs(rate - po_rate) > Decimal("0.01"):
            mismatches.append(
                _mismatch(
                    "RATE_MISMATCH",
                    f"Supplier invoice rate does not match the approved PO rate for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected=_decimal_string(po_rate),
                    actual=_decimal_string(rate),
                )
            )
        if abs(discount_percent - po_discount) > Decimal("0.001"):
            mismatches.append(
                _mismatch(
                    "DISCOUNT_MISMATCH",
                    f"Supplier invoice discount differs from the approved PO discount for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected=_rate_string(po_discount),
                    actual=_rate_string(discount_percent),
                )
            )
        if hsn_code != expected_hsn:
            mismatches.append(
                _mismatch(
                    "HSN_MISMATCH",
                    f"Supplier invoice HSN differs from the Product/PO HSN for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected=expected_hsn,
                    actual=hsn_code,
                )
            )
        if abs(gst_rate - expected_gst) > Decimal("0.001"):
            mismatches.append(
                _mismatch(
                    "GST_RATE_MISMATCH",
                    f"Supplier invoice GST rate differs from the PO tax snapshot for {po_item.get('product_name') or 'this product'}.",
                    line_no=po_line_no,
                    expected=_rate_string(expected_gst),
                    actual=_rate_string(gst_rate),
                )
            )

        line_subtotal = _money(quantity * rate)
        line_discount = _money(
            line_subtotal * discount_percent / Decimal("100")
        )
        taxable_value = _money(line_subtotal - line_discount)
        line_tax = _money(taxable_value * gst_rate / Decimal("100"))
        if gst_rate > 0 and same_state:
            cgst_amount = _money(line_tax / Decimal("2"))
            sgst_amount = line_tax - cgst_amount
            igst_amount = Decimal("0.00")
            cgst_rate = gst_rate / Decimal("2")
            sgst_rate = gst_rate - cgst_rate
            igst_rate = Decimal("0")
        elif gst_rate > 0:
            cgst_amount = Decimal("0.00")
            sgst_amount = Decimal("0.00")
            igst_amount = line_tax
            cgst_rate = Decimal("0")
            sgst_rate = Decimal("0")
            igst_rate = gst_rate
        else:
            cgst_amount = Decimal("0.00")
            sgst_amount = Decimal("0.00")
            igst_amount = Decimal("0.00")
            cgst_rate = Decimal("0")
            sgst_rate = Decimal("0")
            igst_rate = Decimal("0")

        line_total = _money(taxable_value + line_tax)
        items.append(
            {
                "line_no": len(items) + 1,
                "po_line_no": po_line_no,
                "source_product_id": po_item.get("source_product_id"),
                "source_product_id_str": str(
                    po_item.get("source_product_id") or ""
                ),
                "product_code": po_item.get("product_code") or "",
                "product_name": po_item.get("product_name") or "Product",
                "unit_code": po_item.get("unit_code") or "",
                "ordered_quantity": po_item.get("quantity") or "0.0000",
                "accepted_grn_quantity": _quantity_string(
                    accepted_quantity
                ),
                "previously_invoiced_quantity": _quantity_string(
                    invoiced_before
                ),
                "available_to_invoice_quantity": _quantity_string(
                    available_to_invoice
                ),
                "invoice_quantity": _quantity_string(quantity),
                "po_rate": _decimal_string(po_rate),
                "rate": _decimal_string(rate),
                "line_subtotal": _decimal_string(line_subtotal),
                "po_discount_percent": _rate_string(po_discount),
                "discount_percent": _rate_string(discount_percent),
                "discount_amount": _decimal_string(line_discount),
                "po_hsn_code": expected_hsn,
                "hsn_code": hsn_code,
                "taxability_code": po_item.get("taxability_code") or "",
                "po_gst_rate": _rate_string(expected_gst),
                "gst_rate": _rate_string(gst_rate),
                "taxable_value": _decimal_string(taxable_value),
                "cgst_rate": _rate_string(cgst_rate),
                "sgst_rate": _rate_string(sgst_rate),
                "igst_rate": _rate_string(igst_rate),
                "cgst_amount": _decimal_string(cgst_amount),
                "sgst_amount": _decimal_string(sgst_amount),
                "igst_amount": _decimal_string(igst_amount),
                "tax_amount": _decimal_string(line_tax),
                "line_total": _decimal_string(line_total),
            }
        )

        subtotal += line_subtotal
        discount_total += line_discount
        taxable_total += taxable_value
        cgst_total += cgst_amount
        sgst_total += sgst_amount
        igst_total += igst_amount
        tax_total += line_tax

    if not items:
        raise ValueError(
            "Enter an invoice quantity greater than zero for at least one line."
        )

    freight_amount = _decimal(
        raw_payload.get("freight_amount") or "0",
        "Freight amount",
    )
    other_charges = _decimal(
        raw_payload.get("other_charges") or "0",
        "Other charges",
    )
    round_off = _decimal(
        raw_payload.get("round_off") or "0",
        "Round off",
        minimum="-999999.99",
        maximum="999999.99",
    )

    subtotal = _money(subtotal)
    discount_total = _money(discount_total)
    taxable_total = _money(taxable_total)
    cgst_total = _money(cgst_total)
    sgst_total = _money(sgst_total)
    igst_total = _money(igst_total)
    tax_total = _money(tax_total)
    computed_total = _money(
        taxable_total
        + tax_total
        + freight_amount
        + other_charges
        + round_off
    )
    declared_total = _decimal(
        raw_payload.get("declared_total"),
        "Supplier invoice declared total",
        minimum="0.01",
    )
    if abs(declared_total - computed_total) > Decimal("0.01"):
        mismatches.append(
            _mismatch(
                "DECLARED_TOTAL_MISMATCH",
                "Supplier invoice declared total does not match the calculated invoice total.",
                expected=_decimal_string(computed_total),
                actual=_decimal_string(declared_total),
            )
        )

    blocking_count = sum(
        1 for row in mismatches if row.get("severity") == "blocking"
    )
    warning_count = sum(
        1 for row in mismatches if row.get("severity") == "warning"
    )
    if blocking_count:
        status = STATUS_MISMATCH
    elif warning_count:
        status = STATUS_MATCHED_WITH_WARNINGS
    else:
        status = STATUS_MATCHED

    grn_snapshots = [
        {
            "id": str(row.get("_id") or ""),
            "grn_number": row.get("grn_number") or "",
            "receipt_date": (
                row.get("receipt_date").strftime("%Y-%m-%d")
                if isinstance(row.get("receipt_date"), datetime)
                else ""
            ),
            "accepted_quantity_total": row.get(
                "accepted_quantity_total"
            ) or "0.0000",
            "warehouse_code": row.get("warehouse_code") or "",
            "warehouse_name": row.get("warehouse_name") or "",
            "warehouse_bin": row.get("warehouse_bin") or "",
        }
        for row in posted_grns
    ]

    return {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "purchase_order_id": order["_id"],
        "purchase_order_id_str": str(order["_id"]),
        "po_number": order.get("po_number") or "",
        "purchase_order_snapshot": serialize_purchase_order(order),
        "supplier_ledger_id": order.get("supplier_ledger_id"),
        "supplier_ledger_id_str": str(
            order.get("supplier_ledger_id") or ""
        ),
        "supplier_code": order.get("supplier_code") or "",
        "supplier_name": order.get("supplier_name") or "",
        "supplier_gstin": order.get("supplier_gstin") or "",
        "supplier_gst_registration_status": order.get(
            "supplier_gst_registration_status"
        ) or "",
        "supplier_invoice_number": supplier_invoice_number,
        "supplier_invoice_number_normalized": normalized_number,
        "supplier_invoice_identity_key": identity_key,
        "invoice_date": invoice_date,
        "due_date": due_date,
        "document_reference": _clean_text(
            raw_payload.get("document_reference"),
            "Document reference",
            maximum=240,
        ),
        "payment_terms": _clean_text(
            raw_payload.get("payment_terms")
            or order.get("payment_terms"),
            "Payment terms",
            maximum=240,
        ),
        "reverse_charge_declared": str(
            raw_payload.get("reverse_charge_declared") or ""
        ).lower()
        in {"1", "true", "yes", "on"},
        "remarks": _clean_multiline(
            raw_payload.get("remarks"),
            "Remarks",
            maximum=1500,
        ),
        "posted_grn_snapshots": grn_snapshots,
        "posted_grn_count": len(grn_snapshots),
        "items": items,
        "item_count": len(items),
        "subtotal": _decimal_string(subtotal),
        "discount_total": _decimal_string(discount_total),
        "taxable_total": _decimal_string(taxable_total),
        "cgst_total": _decimal_string(cgst_total),
        "sgst_total": _decimal_string(sgst_total),
        "igst_total": _decimal_string(igst_total),
        "tax_total": _decimal_string(tax_total),
        "freight_amount": _decimal_string(freight_amount),
        "other_charges": _decimal_string(other_charges),
        "round_off": _decimal_string(round_off),
        "computed_total": _decimal_string(computed_total),
        "declared_total": _decimal_string(declared_total),
        "grand_total": _decimal_string(declared_total),
        "currency": "INR",
        "match_results": mismatches,
        "blocking_mismatch_count": blocking_count,
        "warning_count": warning_count,
        "match_status": status,
        "status": status,
        "posting_status": "not_posted",
        "voucher_posted": False,
        "payable_posted": False,
        "stock_posted": False,
    }


def _get_invoice_document(invoice_id):
    object_id = _to_object_id(invoice_id)
    if not object_id:
        raise ValueError("Invalid Supplier Invoice reference.")
    invoice = mongo.db[SUPPLIER_INVOICE_COLLECTION].find_one(
        {"_id": object_id}
    )
    if not invoice:
        raise ValueError("Supplier Invoice was not found.")
    return invoice


def _expected_version(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Invalid Supplier Invoice version. Refresh and try again."
        ) from exc
    if parsed < 1:
        raise ValueError(
            "Invalid Supplier Invoice version. Refresh and try again."
        )
    return parsed


def _date_string(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value or "")


def serialize_supplier_invoice(invoice):
    if not invoice:
        return None
    items = []
    for raw_item in invoice.get("items") or []:
        item = dict(raw_item)
        item["source_product_id_str"] = str(
            item.get("source_product_id_str")
            or item.get("source_product_id")
            or ""
        )
        item.pop("source_product_id", None)
        items.append(item)

    return {
        "id": str(invoice.get("_id") or ""),
        "document_uid": invoice.get("document_uid") or "",
        "internal_reference": invoice.get("internal_reference") or "",
        "accounting_entity_id": str(
            invoice.get("accounting_entity_id") or ""
        ),
        "purchase_order_id": str(
            invoice.get("purchase_order_id") or ""
        ),
        "po_number": invoice.get("po_number") or "",
        "supplier_ledger_id": str(
            invoice.get("supplier_ledger_id") or ""
        ),
        "supplier_code": invoice.get("supplier_code") or "",
        "supplier_name": invoice.get("supplier_name") or "",
        "supplier_gstin": invoice.get("supplier_gstin") or "",
        "supplier_gst_registration_status": invoice.get(
            "supplier_gst_registration_status"
        ) or "",
        "supplier_invoice_number": invoice.get(
            "supplier_invoice_number"
        ) or "",
        "invoice_date": _date_string(invoice.get("invoice_date")),
        "due_date": _date_string(invoice.get("due_date")),
        "document_reference": invoice.get("document_reference") or "",
        "payment_terms": invoice.get("payment_terms") or "",
        "reverse_charge_declared": invoice.get(
            "reverse_charge_declared"
        ) is True,
        "remarks": invoice.get("remarks") or "",
        "posted_grn_snapshots": invoice.get("posted_grn_snapshots") or [],
        "posted_grn_count": int(invoice.get("posted_grn_count") or 0),
        "items": items,
        "item_count": int(invoice.get("item_count") or len(items)),
        "subtotal": invoice.get("subtotal") or "0.00",
        "discount_total": invoice.get("discount_total") or "0.00",
        "taxable_total": invoice.get("taxable_total") or "0.00",
        "cgst_total": invoice.get("cgst_total") or "0.00",
        "sgst_total": invoice.get("sgst_total") or "0.00",
        "igst_total": invoice.get("igst_total") or "0.00",
        "tax_total": invoice.get("tax_total") or "0.00",
        "freight_amount": invoice.get("freight_amount") or "0.00",
        "other_charges": invoice.get("other_charges") or "0.00",
        "round_off": invoice.get("round_off") or "0.00",
        "computed_total": invoice.get("computed_total") or "0.00",
        "declared_total": invoice.get("declared_total") or "0.00",
        "grand_total": invoice.get("grand_total") or "0.00",
        "currency": invoice.get("currency") or "INR",
        "match_results": invoice.get("match_results") or [],
        "blocking_mismatch_count": int(
            invoice.get("blocking_mismatch_count") or 0
        ),
        "warning_count": int(invoice.get("warning_count") or 0),
        "match_status": invoice.get("match_status") or invoice.get("status") or "",
        "status": invoice.get("status") or STATUS_MISMATCH,
        "status_display": STATUS_LABELS.get(
            invoice.get("status"),
            str(invoice.get("status") or "").replace("_", " ").title(),
        ),
        "posting_status": invoice.get("posting_status") or "not_posted",
        "posting_status_display": {
            "not_posted": "Not Prepared",
            "prepared": "Prepared for Posting",
            "posting": "Posting in Progress",
            "posted": "Posted",
            "recovery_required": "Recovery Required",
        }.get(
            invoice.get("posting_status") or "not_posted",
            str(invoice.get("posting_status") or "not_posted")
            .replace("_", " ")
            .title(),
        ),
        "official_purchase_invoice_number": invoice.get(
            "official_purchase_invoice_number"
        ) or "",
        "purchase_invoice_number_reservation_id": str(
            invoice.get("purchase_invoice_number_reservation_id") or ""
        ),
        "accounting_voucher_id": str(
            invoice.get("accounting_voucher_id") or ""
        ),
        "accounting_voucher_reference": invoice.get(
            "accounting_voucher_reference"
        ) or "",
        "accounting_voucher_number": invoice.get(
            "accounting_voucher_number"
        ) or "",
        "financial_year_id": str(invoice.get("financial_year_id") or ""),
        "financial_year_code": invoice.get("financial_year_code") or "",
        "voucher_posted": invoice.get("voucher_posted") is True,
        "payable_posted": invoice.get("payable_posted") is True,
        "stock_posted": invoice.get("stock_posted") is True,
        "payment_status": invoice.get("payment_status") or "unpaid",
        "payment_status_display": {
            "unpaid": "Unpaid",
            "partially_paid": "Partially Paid",
            "paid": "Paid",
        }.get(
            invoice.get("payment_status") or "unpaid",
            str(invoice.get("payment_status") or "unpaid")
            .replace("_", " ")
            .title(),
        ),
        "paid_amount": invoice.get("paid_amount") or "0.00",
        "outstanding_amount": (
            invoice.get("outstanding_amount")
            or invoice.get("grand_total")
            or "0.00"
        )
        if invoice.get("payable_posted") is True
        else "0.00",
        "payable_status": invoice.get("payable_status") or (
            "open" if invoice.get("payable_posted") is True else "not_posted"
        ),
        "posting_prepared_by": str(
            invoice.get("posting_prepared_by") or ""
        ),
        "posting_prepared_by_name": invoice.get(
            "posting_prepared_by_name"
        ) or "",
        "posting_prepared_at": invoice.get("posting_prepared_at"),
        "posted_by": str(invoice.get("posted_by") or ""),
        "posted_by_name": invoice.get("posted_by_name") or "",
        "posted_at": invoice.get("posted_at"),
        "posting_error": invoice.get("posting_error") or "",
        "version": int(invoice.get("version") or 1),
        "created_by": str(invoice.get("created_by") or ""),
        "created_by_name": invoice.get("created_by_name") or "",
        "created_at": invoice.get("created_at"),
        "updated_by_name": invoice.get("updated_by_name") or "",
        "updated_at": invoice.get("updated_at"),
        "cancel_reason": invoice.get("cancel_reason") or "",
        "change_history": invoice.get("change_history") or [],
    }


def _purchase_order_invoice_catalog_row(order):
    grns, receipt_totals = _posted_grn_context(order["_id"])
    invoiced_totals = _already_invoiced_totals(order["_id"])
    items = []
    total_accepted = Decimal("0")
    total_available = Decimal("0")

    for index, po_item in enumerate(order.get("items") or [], start=1):
        line_no = int(po_item.get("line_no") or index)
        accepted = receipt_totals.get(
            line_no,
            {"accepted": Decimal("0")},
        )["accepted"]
        invoiced = invoiced_totals.get(line_no, Decimal("0"))
        available = max(accepted - invoiced, Decimal("0"))
        expected_gst = _expected_gst_rate(po_item)
        total_accepted += accepted
        total_available += available
        items.append(
            {
                "po_line_no": line_no,
                "product_id": str(po_item.get("source_product_id") or ""),
                "product_code": po_item.get("product_code") or "",
                "product_name": po_item.get("product_name") or "Product",
                "unit_code": po_item.get("unit_code") or "",
                "ordered_quantity": po_item.get("quantity") or "0.0000",
                "accepted_quantity": _quantity_string(accepted),
                "previously_invoiced_quantity": _quantity_string(invoiced),
                "available_to_invoice_quantity": _quantity_string(available),
                "rate": po_item.get("rate") or "0.00",
                "discount_percent": po_item.get("discount_percent") or "0",
                "hsn_code": po_item.get("hsn_code") or "",
                "gst_rate": _rate_string(expected_gst),
                "taxability_code": po_item.get("taxability_code") or "",
            }
        )

    supplier_snapshot = order.get("supplier_snapshot") or {}
    return {
        "id": str(order["_id"]),
        "po_number": order.get("po_number") or "",
        "supplier_name": order.get("supplier_name") or "",
        "supplier_code": order.get("supplier_code") or "",
        "supplier_gstin": order.get("supplier_gstin") or "",
        "supplier_gst_registration_status": order.get(
            "supplier_gst_registration_status"
        ) or "",
        "supplier_state_code": order.get("supplier_state_code") or "",
        "status": order.get("status") or "",
        "status_display": str(order.get("status") or "").replace("_", " ").title(),
        "order_date": _date_string(order.get("order_date")),
        "payment_terms": order.get("payment_terms") or "",
        "credit_period_days": int(
            supplier_snapshot.get("credit_period_days") or 0
        ),
        "posted_grn_count": len(grns),
        "total_accepted_quantity": _quantity_string(total_accepted),
        "total_available_to_invoice_quantity": _quantity_string(total_available),
        "items": items,
    }


def get_supplier_invoice_form_catalog(
    accounting_entity_id,
    actor_user_id,
    purchase_order_id=None,
):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_supplier_invoice_indexes()

    query = {
        "accounting_entity_id": entity["_id"],
        "status": {"$in": list(ELIGIBLE_PO_STATUSES)},
    }
    selected_id = _to_object_id(purchase_order_id)
    if selected_id:
        query["_id"] = selected_id

    orders = list(
        mongo.db[PURCHASE_ORDER_COLLECTION]
        .find(query)
        .sort([("order_date", DESCENDING), ("created_at", DESCENDING)])
    )
    return {
        "entity": {
            "id": str(entity["_id"]),
            "name": entity.get("trade_name")
            or entity.get("display_name")
            or entity.get("legal_name")
            or "AVPL",
            "state_name": entity.get("state_name")
            or entity.get("state")
            or "",
            "state_code": entity.get("state_code") or "",
        },
        "purchase_orders": [
            _purchase_order_invoice_catalog_row(order) for order in orders
        ],
        "selected_purchase_order_id": str(selected_id or ""),
        "today": date.today().strftime("%Y-%m-%d"),
        "status_labels": dict(STATUS_LABELS),
    }


def get_supplier_invoice_overview(
    accounting_entity_id,
    actor_user_id,
    status=None,
    query_text="",
):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_supplier_invoice_indexes()

    query = {"accounting_entity_id": entity["_id"]}
    if status and status in STATUS_LABELS:
        query["status"] = status

    rows = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION]
        .find(query)
        .sort([("invoice_date", DESCENDING), ("created_at", DESCENDING)])
    )
    text = str(query_text or "").strip().lower()
    if text:
        rows = [
            row
            for row in rows
            if text in str(row.get("internal_reference") or "").lower()
            or text in str(
                row.get("official_purchase_invoice_number") or ""
            ).lower()
            or text
            in str(row.get("supplier_invoice_number") or "").lower()
            or text in str(row.get("po_number") or "").lower()
            or text in str(row.get("supplier_name") or "").lower()
            or any(
                text in str(item.get("product_name") or "").lower()
                for item in row.get("items") or []
            )
        ]

    counts = {key: 0 for key in STATUS_LABELS}
    for row in mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
        {"accounting_entity_id": entity["_id"]},
        {"status": 1},
    ):
        key = row.get("status") or STATUS_MISMATCH
        counts[key] = counts.get(key, 0) + 1

    return {
        "rows": [serialize_supplier_invoice(row) for row in rows],
        "counts": counts,
        "status_labels": dict(STATUS_LABELS),
        "selected_status": status or "",
        "query": query_text or "",
    }


def get_supplier_invoice(accounting_entity_id, actor_user_id, invoice_id):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    invoice = _get_invoice_document(invoice_id)
    if str(invoice.get("accounting_entity_id")) != str(entity["_id"]):
        raise PermissionError(
            "This Supplier Invoice belongs to another Accounting entity."
        )
    return serialize_supplier_invoice(invoice)


def get_supplier_invoices_for_purchase_order(
    accounting_entity_id,
    actor_user_id,
    purchase_order_id,
):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    order_id = _to_object_id(purchase_order_id)
    if not order_id:
        return []
    rows = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "purchase_order_id": order_id,
            }
        )
        .sort([("invoice_date", DESCENDING), ("created_at", DESCENDING)])
    )
    return [serialize_supplier_invoice(row) for row in rows]


def _refresh_purchase_order_invoice_summary(purchase_order_id):
    order = mongo.db[PURCHASE_ORDER_COLLECTION].find_one(
        {"_id": purchase_order_id}
    )
    if not order:
        return

    invoices = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
            {
                "purchase_order_id": order["_id"],
                "status": {"$ne": STATUS_CANCELLED},
            },
            {"status": 1, "items": 1},
        )
    )
    line_totals = {}
    for invoice in invoices:
        for item in invoice.get("items") or []:
            line_no = int(item.get("po_line_no") or 0)
            if not line_no:
                continue
            line_totals[line_no] = line_totals.get(
                line_no,
                Decimal("0"),
            ) + Decimal(str(item.get("invoice_quantity") or 0))

    updated_items = []
    any_invoiced = False
    all_accepted_invoiced = True
    for index, raw_item in enumerate(order.get("items") or [], start=1):
        item = dict(raw_item)
        line_no = int(item.get("line_no") or index)
        accepted = Decimal(str(item.get("accepted_quantity") or 0))
        invoiced = line_totals.get(line_no, Decimal("0"))
        pending = max(accepted - invoiced, Decimal("0"))
        if invoiced > 0:
            any_invoiced = True
        if accepted > 0 and pending > 0:
            all_accepted_invoiced = False
        item.update(
            {
                "invoiced_quantity": _quantity_string(invoiced),
                "invoice_pending_quantity": _quantity_string(pending),
            }
        )
        updated_items.append(item)

    statuses = {invoice.get("status") for invoice in invoices}
    if STATUS_MISMATCH in statuses:
        match_status = "mismatch"
    elif any_invoiced and all_accepted_invoiced:
        match_status = (
            "matched_with_warnings"
            if STATUS_MATCHED_WITH_WARNINGS in statuses
            else "matched"
        )
    elif any_invoiced:
        match_status = (
            "partially_invoiced_with_warnings"
            if STATUS_MATCHED_WITH_WARNINGS in statuses
            else "partially_invoiced"
        )
    else:
        match_status = "not_recorded"

    mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"]},
        {
            "$set": {
                "items": updated_items,
                "supplier_invoice_count": len(invoices),
                "invoice_match_status": match_status,
                "invoice_summary_updated_at": now_utc(),
                "updated_at": now_utc(),
            },
            "$inc": {"version": 1},
        },
    )


def create_supplier_invoice(
    accounting_entity_id,
    actor_user_id,
    raw_payload,
):
    actor = _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_supplier_invoice_indexes()
    payload = _build_invoice_payload(entity, actor, raw_payload)

    timestamp = now_utc()
    document = {
        **payload,
        "document_uid": uuid4().hex,
        "internal_reference": _next_internal_reference(
            payload["invoice_date"]
        ),
        "version": 1,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "change_history": [
            _history_event(
                "record_supplier_invoice_and_run_three_way_match",
                actor,
                previous_status=None,
                new_status=payload["status"],
                changed_fields=sorted(payload.keys()),
            )
        ],
        "audit_sync_required": False,
    }

    try:
        result = mongo.db[SUPPLIER_INVOICE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "This supplier invoice number is already recorded for the selected supplier."
        ) from exc

    _refresh_purchase_order_invoice_summary(document["purchase_order_id"])
    _record_audit(
        document,
        actor,
        "record_supplier_invoice_and_run_three_way_match",
        previous_status=None,
        reason="Supplier Invoice recorded and three-way match completed.",
        changed_fields=sorted(payload.keys()),
    )
    return {
        "invoice": serialize_supplier_invoice(document),
        "message": (
            "Supplier Invoice recorded. Three-way match passed."
            if document["status"] == STATUS_MATCHED
            else "Supplier Invoice recorded with warnings."
            if document["status"] == STATUS_MATCHED_WITH_WARNINGS
            else "Supplier Invoice recorded with blocking match exceptions."
        ),
    }


def update_supplier_invoice(
    invoice_id,
    actor_user_id,
    raw_payload,
    expected_version,
):
    actor = _get_actor(actor_user_id)
    invoice = _get_invoice_document(invoice_id)
    if invoice.get("status") == STATUS_CANCELLED:
        raise ValueError("A cancelled Supplier Invoice cannot be edited.")
    if invoice.get("posting_status") != "not_posted":
        raise ValueError(
            "A posted Supplier Invoice cannot be edited from this stage."
        )
    if (
        actor.get("resolved_role") == "accounts"
        and str(invoice.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError(
            "Accounts users can edit only Supplier Invoices they created."
        )

    version = _expected_version(expected_version)
    if version != int(invoice.get("version") or 1):
        raise RuntimeError(
            "This Supplier Invoice changed. Refresh before saving."
        )

    entity = _active_avpl_entity(invoice.get("accounting_entity_id"))
    payload = _build_invoice_payload(
        entity,
        actor,
        raw_payload,
        existing=invoice,
    )
    timestamp = now_utc()
    try:
        result = mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
            {
                "_id": invoice["_id"],
                "version": version,
                "status": {"$ne": STATUS_CANCELLED},
                "posting_status": "not_posted",
            },
            {
                "$set": {
                    **payload,
                    "version": version + 1,
                    "updated_by": actor["_id"],
                    "updated_by_str": str(actor["_id"]),
                    "updated_by_name": actor.get("resolved_name") or "",
                    "updated_at": timestamp,
                    "cancel_reason": None,
                },
                "$push": {
                    "change_history": _history_event(
                        "update_supplier_invoice_and_rerun_match",
                        actor,
                        previous_status=invoice.get("status"),
                        new_status=payload["status"],
                        changed_fields=sorted(payload.keys()),
                    )
                },
            },
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "This supplier invoice number is already recorded for the selected supplier."
        ) from exc

    if result.matched_count != 1:
        raise RuntimeError(
            "This Supplier Invoice changed. Refresh before saving."
        )

    updated = _get_invoice_document(invoice_id)
    _refresh_purchase_order_invoice_summary(updated["purchase_order_id"])
    _record_audit(
        updated,
        actor,
        "update_supplier_invoice_and_rerun_match",
        previous_status=invoice.get("status"),
        reason="Supplier Invoice updated and three-way match rerun.",
        changed_fields=sorted(payload.keys()),
    )
    return {
        "invoice": serialize_supplier_invoice(updated),
        "message": (
            "Supplier Invoice updated. Three-way match passed."
            if updated["status"] == STATUS_MATCHED
            else "Supplier Invoice updated with warnings."
            if updated["status"] == STATUS_MATCHED_WITH_WARNINGS
            else "Supplier Invoice updated with blocking match exceptions."
        ),
    }


def cancel_supplier_invoice(
    invoice_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id)
    invoice = _get_invoice_document(invoice_id)
    if invoice.get("status") == STATUS_CANCELLED:
        raise ValueError("This Supplier Invoice is already cancelled.")
    if invoice.get("posting_status") != "not_posted":
        raise ValueError(
            "A posted Supplier Invoice cannot be cancelled from this stage."
        )
    if (
        actor.get("resolved_role") == "accounts"
        and str(invoice.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError(
            "Accounts users can cancel only Supplier Invoices they created."
        )

    version = _expected_version(expected_version)
    reason_text = _clean_multiline(
        reason,
        "Cancellation reason",
        maximum=1000,
    )
    if not reason_text:
        raise ValueError("Cancellation reason is required.")

    previous_status = invoice.get("status")
    timestamp = now_utc()
    result = mongo.db[SUPPLIER_INVOICE_COLLECTION].update_one(
        {
            "_id": invoice["_id"],
            "version": version,
            "status": previous_status,
            "posting_status": "not_posted",
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
                "match_status": STATUS_CANCELLED,
                "version": version + 1,
                "cancel_reason": reason_text,
                "cancelled_by": actor["_id"],
                "cancelled_by_name": actor.get("resolved_name") or "",
                "cancelled_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "cancel_supplier_invoice",
                    actor,
                    previous_status=previous_status,
                    new_status=STATUS_CANCELLED,
                    reason=reason_text,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError(
            "This Supplier Invoice changed. Refresh before cancelling."
        )

    updated = _get_invoice_document(invoice_id)
    _refresh_purchase_order_invoice_summary(updated["purchase_order_id"])
    _record_audit(
        updated,
        actor,
        "cancel_supplier_invoice",
        previous_status=previous_status,
        reason=reason_text,
    )
    return {
        "invoice": serialize_supplier_invoice(updated),
        "message": "Supplier Invoice cancelled. No stock, payable or voucher was changed.",
    }
