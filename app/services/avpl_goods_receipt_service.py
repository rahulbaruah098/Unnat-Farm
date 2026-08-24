from __future__ import annotations
from app.utils.timezone import business_today

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_product_tracking_service import (
    get_product_tracking_profile_for_posting,
    validate_product_tracking_for_posting,
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
from app.services.workflow_policy_service import workflow_is_streamlined


GOODS_RECEIPT_COLLECTION = "avpl_goods_receipts"
SUPPLIER_INVOICE_COLLECTION = "avpl_supplier_invoices"
INVENTORY_LOT_COLLECTION = "avpl_inventory_lots"
STOCK_MOVEMENT_COLLECTION = "avpl_stock_movements"
LOCK_COLLECTION = "avpl_procurement_locks"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_POSTED = "posted"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending Approval",
    STATUS_RETURNED: "Returned for Correction",
    STATUS_POSTED: "Posted",
    STATUS_CANCELLED: "Cancelled",
}

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
ALLOWED_ROLES = {"accounts", "avpl_admin", "super_admin"}
CHECKER_ROLES = {"avpl_admin", "super_admin"}
POSTABLE_PO_STATUSES = {PO_STATUS_APPROVED, PO_STATUS_PARTIALLY_RECEIVED}

QTY_QUANTUM = Decimal("0.0001")


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
            f"Could not create Goods Receipt index {name}."
        ) from exc


def ensure_goods_receipt_indexes():
    receipt_collection = mongo.db[GOODS_RECEIPT_COLLECTION]
    _ensure_exact_index(
        receipt_collection,
        [("grn_number", ASCENDING)],
        name="avpl_goods_receipt_number_unique",
        unique=True,
    )
    _ensure_exact_index(
        receipt_collection,
        [("document_uid", ASCENDING)],
        name="avpl_goods_receipt_uid_unique",
        unique=True,
    )
    _ensure_exact_index(
        receipt_collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("receipt_date", DESCENDING),
        ],
        name="avpl_goods_receipt_entity_status_date_idx",
    )
    _ensure_exact_index(
        receipt_collection,
        [("purchase_order_id", ASCENDING), ("receipt_date", DESCENDING)],
        name="avpl_goods_receipt_po_date_idx",
    )
    _ensure_exact_index(
        receipt_collection,
        [("supplier_delivery_note_key", ASCENDING)],
        name="avpl_goods_receipt_delivery_note_unique",
        unique=True,
        partialFilterExpression={
            "supplier_delivery_note_key": {"$type": "string"}
        },
    )

    _ensure_exact_index(
        mongo.db[INVENTORY_LOT_COLLECTION],
        [("lot_key", ASCENDING)],
        name="avpl_inventory_lot_key_unique",
        unique=True,
    )
    _ensure_exact_index(
        mongo.db[INVENTORY_LOT_COLLECTION],
        [
            ("accounting_entity_id", ASCENDING),
            ("source_product_id", ASCENDING),
            ("warehouse_code", ASCENDING),
            ("expiry_date", ASCENDING),
        ],
        name="avpl_inventory_lot_product_warehouse_expiry_idx",
    )
    _ensure_exact_index(
        mongo.db[STOCK_MOVEMENT_COLLECTION],
        [("source_posting_key", ASCENDING)],
        name="avpl_stock_movement_source_unique",
        unique=True,
    )
    _ensure_exact_index(
        mongo.db[STOCK_MOVEMENT_COLLECTION],
        [
            ("accounting_entity_id", ASCENDING),
            ("source_product_id", ASCENDING),
            ("movement_date", DESCENDING),
        ],
        name="avpl_stock_movement_product_date_idx",
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
            "You are not authorized to manage AVPL Goods Receipts."
        )
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError(
            "Inactive users cannot manage AVPL Goods Receipts."
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


def _decimal(value, label, minimum="0", maximum="999999999.9999"):
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
    return number.quantize(QTY_QUANTUM, rounding=ROUND_HALF_UP)


def _quantity_string(value):
    return format(
        Decimal(str(value or 0)).quantize(
            QTY_QUANTUM,
            rounding=ROUND_HALF_UP,
        ),
        "f",
    )


def _parse_items(raw_items):
    if isinstance(raw_items, str):
        try:
            items = json.loads(raw_items or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Goods Receipt product lines are invalid.") from exc
    else:
        items = raw_items

    if not isinstance(items, list) or not items:
        raise ValueError("Record at least one received product line.")
    if len(items) > 100:
        raise ValueError(
            "A Goods Receipt cannot contain more than 100 product lines."
        )
    return items


def _next_grn_number(receipt_date):
    year = receipt_date.year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_goods_receipt:{year}"},
        {
            "$inc": {"sequence": 1},
            "$setOnInsert": {
                "counter_type": "avpl_goods_receipt",
                "year": year,
                "created_at": now_utc(),
            },
            "$set": {"updated_at": now_utc()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"AVPL-GRN-{year}-{sequence:05d}"


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
    receipt,
    actor,
    action,
    previous_status=None,
    reason="",
    changed_fields=None,
):
    audit = {
        "module": "avpl_procurement",
        "submodule": "goods_receipt",
        "action": action,
        "accounting_entity_id": receipt.get("accounting_entity_id"),
        "accounting_entity_id_str": str(
            receipt.get("accounting_entity_id") or ""
        ),
        "entity_type": "avpl_goods_receipt",
        "entity_id": receipt.get("_id"),
        "entity_id_str": str(receipt.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or "",
        "previous_status": previous_status,
        "new_status": receipt.get("status"),
        "metadata": {
            "grn_number": receipt.get("grn_number"),
            "po_number": receipt.get("po_number"),
            "supplier_name": receipt.get("supplier_name"),
            "accepted_quantity": receipt.get("accepted_quantity_total"),
            "rejected_quantity": receipt.get("rejected_quantity_total"),
            "damaged_quantity": receipt.get("damaged_quantity_total"),
            "version": int(receipt.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": str(reason or "")[:1500],
        "created_at": now_utc(),
    }
    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
    except Exception as exc:
        mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
            {"_id": receipt.get("_id")},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_error": str(exc)[:500],
                    "audit_sync_marked_at": now_utc(),
                }
            },
        )


@contextmanager
def _posting_lock(receipt_id):
    lock_key = f"goods-receipt-post:{receipt_id}"
    token = uuid4().hex
    timestamp = now_utc()
    document = {
        "_id": lock_key,
        "lock_token": token,
        "created_at": timestamp,
        "expires_at": timestamp + timedelta(minutes=5),
    }

    try:
        mongo.db[LOCK_COLLECTION].insert_one(document)
    except DuplicateKeyError:
        existing = mongo.db[LOCK_COLLECTION].find_one({"_id": lock_key})
        if existing and existing.get("expires_at") and existing["expires_at"] <= timestamp:
            mongo.db[LOCK_COLLECTION].delete_one(
                {"_id": lock_key, "expires_at": existing["expires_at"]}
            )
            try:
                mongo.db[LOCK_COLLECTION].insert_one(document)
            except DuplicateKeyError as exc:
                raise RuntimeError(
                    "This Goods Receipt is already being posted. Refresh shortly."
                ) from exc
        else:
            raise RuntimeError(
                "This Goods Receipt is already being posted. Refresh shortly."
            )

    try:
        yield
    finally:
        mongo.db[LOCK_COLLECTION].delete_one(
            {"_id": lock_key, "lock_token": token}
        )


def _get_purchase_order(entity_id, purchase_order_id):
    order_id = _to_object_id(purchase_order_id)
    if not order_id:
        raise ValueError("Select a valid Purchase Order.")
    order = mongo.db[PURCHASE_ORDER_COLLECTION].find_one(
        {
            "_id": order_id,
            "accounting_entity_id": entity_id,
            "status": {
                "$in": [
                    PO_STATUS_APPROVED,
                    PO_STATUS_PARTIALLY_RECEIVED,
                    PO_STATUS_FULLY_RECEIVED,
                ]
            },
        }
    )
    if not order:
        raise ValueError(
            "The selected Purchase Order is not approved or available."
        )
    return order


def _posted_receipt_totals(purchase_order_id, exclude_receipt_id=None):
    query = {
        "purchase_order_id": purchase_order_id,
        "status": STATUS_POSTED,
        "stock_posted": True,
    }
    if exclude_receipt_id:
        query["_id"] = {"$ne": exclude_receipt_id}

    totals = {}
    for receipt in mongo.db[GOODS_RECEIPT_COLLECTION].find(
        query,
        {"items": 1},
    ):
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
    return totals


def _build_receipt_payload(entity, actor, raw_payload, existing=None):
    order = _get_purchase_order(
        entity["_id"],
        raw_payload.get("purchase_order_id"),
    )
    if order.get("status") == PO_STATUS_FULLY_RECEIVED:
        raise ValueError(
            "This Purchase Order is already fully received."
        )

    receipt_date = _parse_date(
        raw_payload.get("receipt_date"),
        "Receipt date",
    )
    order_date = order.get("order_date")
    if isinstance(order_date, datetime) and receipt_date.date() < order_date.date():
        raise ValueError(
            "Receipt date cannot be before the Purchase Order date."
        )

    warehouse_code = _clean_text(
        raw_payload.get("warehouse_code") or "AVPL-MAIN",
        "Warehouse code",
        maximum=40,
        required=True,
    ).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_\-]{1,39}", warehouse_code):
        raise ValueError(
            "Warehouse code may contain letters, numbers, underscore or hyphen."
        )
    warehouse_name = _clean_text(
        raw_payload.get("warehouse_name") or "AVPL Main Warehouse",
        "Warehouse name",
        maximum=120,
        required=True,
    )
    warehouse_bin = _clean_text(
        raw_payload.get("warehouse_bin"),
        "Warehouse bin",
        maximum=80,
    )

    raw_items = _parse_items(
        raw_payload.get("items") or raw_payload.get("items_json")
    )
    existing_id = existing.get("_id") if existing else None
    prior_totals = _posted_receipt_totals(
        order["_id"],
        exclude_receipt_id=existing_id,
    )
    order_items = {
        int(item.get("line_no") or index): item
        for index, item in enumerate(order.get("items") or [], start=1)
    }

    line_items = []
    seen_lines = set()
    total_received = Decimal("0")
    total_accepted = Decimal("0")
    total_rejected = Decimal("0")
    total_damaged = Decimal("0")

    for source_index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Goods Receipt line {source_index} is invalid.")
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
                "in a Goods Receipt."
            )
        seen_lines.add(po_line_no)

        po_item = order_items[po_line_no]
        received = _decimal(
            raw_item.get("received_quantity"),
            f"Received quantity on line {source_index}",
        )
        accepted = _decimal(
            raw_item.get("accepted_quantity"),
            f"Accepted quantity on line {source_index}",
        )
        rejected = _decimal(
            raw_item.get("rejected_quantity"),
            f"Rejected quantity on line {source_index}",
        )
        damaged = _decimal(
            raw_item.get("damaged_quantity"),
            f"Damaged quantity on line {source_index}",
        )

        if received <= 0:
            continue
        if accepted + rejected + damaged != received:
            raise ValueError(
                f"Line {source_index}: received quantity must equal "
                "accepted + rejected + damaged quantities."
            )

        ordered = Decimal(str(po_item.get("quantity") or 0))
        previous = prior_totals.get(
            po_line_no,
            {
                "received": Decimal("0"),
                "accepted": Decimal("0"),
                "rejected": Decimal("0"),
                "damaged": Decimal("0"),
            },
        )
        remaining_acceptance = max(
            ordered - previous["accepted"],
            Decimal("0"),
        )
        if accepted > remaining_acceptance:
            raise ValueError(
                f"Line {source_index}: accepted quantity cannot exceed "
                f"the remaining PO quantity of "
                f"{_quantity_string(remaining_acceptance)}."
            )

        product_id = _to_object_id(po_item.get("source_product_id"))
        mapping_id = _to_object_id(po_item.get("accounting_mapping_id"))
        if not product_id:
            raise ValueError(
                f"Line {source_index}: the linked product is invalid."
            )

        barcode = re.sub(
            r"\s+",
            "",
            str(raw_item.get("barcode") or ""),
        ).upper()
        batch_number = _clean_text(
            raw_item.get("batch_number"),
            "Batch number",
            maximum=80,
        ).upper()
        manufacturing_date = raw_item.get("manufacturing_date") or ""
        expiry_date = raw_item.get("expiry_date") or ""
        tracking_profile = get_product_tracking_profile_for_posting(
            entity["_id"],
            product_mapping_id=mapping_id,
            source_product_id=product_id,
            required=False,
        )
        tracking_result = None

        if accepted > 0 and tracking_profile:
            tracking_result = validate_product_tracking_for_posting(
                tracking_profile,
                transaction_date=receipt_date.date(),
                movement_type="receipt",
                scanned_barcode=barcode,
                batch_number=batch_number,
                manufacturing_date=manufacturing_date or None,
                expiry_date=expiry_date or None,
            )
            barcode = tracking_result.get("scanned_barcode") or barcode
            batch_number = tracking_result.get("batch_number") or batch_number
            manufacturing_date = (
                tracking_result.get("manufacturing_date")
                or manufacturing_date
            )
            expiry_date = tracking_result.get("expiry_date") or expiry_date
        else:
            manufacturing_day = _parse_date(
                manufacturing_date,
                "Manufacturing date",
                required=False,
            )
            expiry_day = _parse_date(
                expiry_date,
                "Expiry date",
                required=False,
            )
            if batch_number and not re.fullmatch(
                r"[A-Z0-9][A-Z0-9._/\-]{0,79}",
                batch_number,
            ):
                raise ValueError(
                    "Batch number may contain letters, numbers, period, "
                    "underscore, slash or hyphen."
                )
            if manufacturing_day and manufacturing_day > receipt_date:
                raise ValueError(
                    "Manufacturing date cannot be after the receipt date."
                )
            if manufacturing_day and expiry_day and expiry_day < manufacturing_day:
                raise ValueError(
                    "Expiry date cannot be before the manufacturing date."
                )
            if accepted > 0 and expiry_day and expiry_day < receipt_date:
                raise ValueError(
                    "Expired stock cannot be accepted into inventory."
                )
            manufacturing_date = (
                manufacturing_day.strftime("%Y-%m-%d")
                if manufacturing_day
                else ""
            )
            expiry_date = (
                expiry_day.strftime("%Y-%m-%d")
                if expiry_day
                else ""
            )

        pending_after = max(
            remaining_acceptance - accepted,
            Decimal("0"),
        )
        line_items.append(
            {
                "line_no": len(line_items) + 1,
                "po_line_no": po_line_no,
                "source_product_id": product_id,
                "source_product_id_str": str(product_id),
                "product_code": po_item.get("product_code") or "",
                "product_name": po_item.get("product_name") or "Product",
                "product_role": po_item.get("product_role") or "",
                "hsn_code": po_item.get("hsn_code") or "",
                "taxability_code": po_item.get("taxability_code") or "",
                "accounting_mapping_id": mapping_id,
                "accounting_mapping_code": po_item.get(
                    "accounting_mapping_code"
                ) or "",
                "unit_code": po_item.get("unit_code") or "",
                "unit_name": po_item.get("unit_name") or "",
                "ordered_quantity": _quantity_string(ordered),
                "previous_received_quantity": _quantity_string(
                    previous["received"]
                ),
                "previous_accepted_quantity": _quantity_string(
                    previous["accepted"]
                ),
                "remaining_acceptance_before": _quantity_string(
                    remaining_acceptance
                ),
                "received_quantity": _quantity_string(received),
                "accepted_quantity": _quantity_string(accepted),
                "rejected_quantity": _quantity_string(rejected),
                "damaged_quantity": _quantity_string(damaged),
                "pending_quantity_after": _quantity_string(pending_after),
                "barcode": barcode,
                "batch_number": batch_number,
                "manufacturing_date": manufacturing_date,
                "expiry_date": expiry_date,
                "warehouse_code": warehouse_code,
                "warehouse_name": warehouse_name,
                "warehouse_bin": warehouse_bin,
                "tracking_profile_id": (
                    tracking_profile.get("_id") if tracking_profile else None
                ),
                "tracking_profile_code": (
                    tracking_profile.get("profile_code") if tracking_profile else ""
                ),
                "tracking_controls_applied": (
                    tracking_result.get("controls_applied")
                    if tracking_result
                    else []
                ),
                "tracking_validation_passed": bool(
                    tracking_result or accepted <= 0 or not tracking_profile
                ),
                "stock_posted": False,
            }
        )

        total_received += received
        total_accepted += accepted
        total_rejected += rejected
        total_damaged += damaged

    if not line_items:
        raise ValueError(
            "Enter a received quantity greater than zero for at least one line."
        )

    supplier_delivery_note = _clean_text(
        raw_payload.get("supplier_delivery_note"),
        "Supplier delivery note",
        maximum=120,
    )
    supplier_delivery_note_key = None
    if supplier_delivery_note:
        normalized_note = re.sub(
            r"[^A-Z0-9]",
            "",
            supplier_delivery_note.upper(),
        )
        supplier_delivery_note_key = (
            f"{entity['_id']}:{order.get('supplier_ledger_id')}:"
            f"{normalized_note}"
        )

    # A supplier may send its GST/tax invoice together with the physical goods.
    # Capture the reference at GRN time without making stock posting depend on
    # accounting finalization. The uploaded file itself is stored by the route
    # after the GRN exists and linked back to this record.
    supplier_invoice_number_capture = _clean_text(
        raw_payload.get("supplier_invoice_number_capture"),
        "Supplier invoice number",
        maximum=80,
    )
    supplier_invoice_date_capture = _parse_date(
        raw_payload.get("supplier_invoice_date_capture"),
        "Supplier invoice date",
        required=False,
    )
    supplier_invoice_received = str(
        raw_payload.get("supplier_invoice_received") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if supplier_invoice_number_capture or supplier_invoice_date_capture:
        supplier_invoice_received = True
    if supplier_invoice_received and not supplier_invoice_number_capture:
        raise ValueError(
            "Enter the supplier invoice number when an invoice was received with the goods."
        )
    if supplier_invoice_received and not supplier_invoice_date_capture:
        raise ValueError(
            "Enter the supplier invoice date when an invoice was received with the goods."
        )
    if (
        supplier_invoice_date_capture
        and supplier_invoice_date_capture.date() > business_today()
    ):
        raise ValueError("Supplier invoice date cannot be in the future.")

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
        "receipt_date": receipt_date,
        "supplier_delivery_note": supplier_delivery_note,
        "supplier_delivery_note_key": supplier_delivery_note_key,
        "supplier_invoice_received": supplier_invoice_received,
        "supplier_invoice_number_capture": supplier_invoice_number_capture,
        "supplier_invoice_date_capture": supplier_invoice_date_capture,
        "transporter_name": _clean_text(
            raw_payload.get("transporter_name"),
            "Transporter name",
            maximum=120,
        ),
        "vehicle_number": _clean_text(
            raw_payload.get("vehicle_number"),
            "Vehicle number",
            maximum=30,
        ).upper(),
        "warehouse_code": warehouse_code,
        "warehouse_name": warehouse_name,
        "warehouse_bin": warehouse_bin,
        "inspection_note": _clean_multiline(
            raw_payload.get("inspection_note"),
            "Inspection note",
            maximum=1500,
        ),
        "remarks": _clean_multiline(
            raw_payload.get("remarks"),
            "Remarks",
            maximum=1500,
        ),
        "items": line_items,
        "item_count": len(line_items),
        "received_quantity_total": _quantity_string(total_received),
        "accepted_quantity_total": _quantity_string(total_accepted),
        "rejected_quantity_total": _quantity_string(total_rejected),
        "damaged_quantity_total": _quantity_string(total_damaged),
    }


def _get_receipt_document(receipt_id):
    object_id = _to_object_id(receipt_id)
    if not object_id:
        raise ValueError("Invalid Goods Receipt reference.")
    receipt = mongo.db[GOODS_RECEIPT_COLLECTION].find_one({"_id": object_id})
    if not receipt:
        raise ValueError("Goods Receipt was not found.")
    return receipt


def _expected_version(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Invalid Goods Receipt version. Refresh and try again."
        ) from exc
    if parsed < 1:
        raise ValueError(
            "Invalid Goods Receipt version. Refresh and try again."
        )
    return parsed


def _date_string(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    return str(value or "")


def serialize_goods_receipt(receipt):
    if not receipt:
        return None

    items = []
    for raw_item in receipt.get("items") or []:
        item = dict(raw_item)
        item["source_product_id_str"] = str(
            item.get("source_product_id_str")
            or item.get("source_product_id")
            or ""
        )
        item["accounting_mapping_id_str"] = str(
            item.get("accounting_mapping_id") or ""
        )
        item["tracking_profile_id_str"] = str(
            item.get("tracking_profile_id") or ""
        )
        item.pop("source_product_id", None)
        item.pop("accounting_mapping_id", None)
        item.pop("tracking_profile_id", None)
        item["manufacturing_date"] = _date_string(
            item.get("manufacturing_date")
        )
        item["expiry_date"] = _date_string(item.get("expiry_date"))
        items.append(item)

    return {
        "id": str(receipt.get("_id") or ""),
        "document_uid": receipt.get("document_uid") or "",
        "grn_number": receipt.get("grn_number") or "",
        "accounting_entity_id": str(
            receipt.get("accounting_entity_id") or ""
        ),
        "purchase_order_id": str(
            receipt.get("purchase_order_id") or ""
        ),
        "po_number": receipt.get("po_number") or "",
        "supplier_ledger_id": str(
            receipt.get("supplier_ledger_id") or ""
        ),
        "supplier_code": receipt.get("supplier_code") or "",
        "supplier_name": receipt.get("supplier_name") or "",
        "supplier_gstin": receipt.get("supplier_gstin") or "",
        "supplier_gst_registration_status": receipt.get(
            "supplier_gst_registration_status"
        ) or "",
        "receipt_date": _date_string(receipt.get("receipt_date")),
        "supplier_delivery_note": receipt.get("supplier_delivery_note") or "",
        "supplier_invoice_received": receipt.get("supplier_invoice_received") is True,
        "supplier_invoice_number_capture": receipt.get("supplier_invoice_number_capture") or "",
        "supplier_invoice_date_capture": _date_string(receipt.get("supplier_invoice_date_capture")),
        "supplier_invoice_attachment_document_id": str(receipt.get("supplier_invoice_attachment_document_id") or ""),
        "supplier_invoice_attachment_filename": receipt.get("supplier_invoice_attachment_filename") or "",
        "supplier_invoice_record_id": str(receipt.get("supplier_invoice_record_id") or ""),
        "supplier_invoice_record_number": receipt.get("supplier_invoice_record_number") or "",
        "transporter_name": receipt.get("transporter_name") or "",
        "vehicle_number": receipt.get("vehicle_number") or "",
        "warehouse_code": receipt.get("warehouse_code") or "",
        "warehouse_name": receipt.get("warehouse_name") or "",
        "warehouse_bin": receipt.get("warehouse_bin") or "",
        "inspection_note": receipt.get("inspection_note") or "",
        "remarks": receipt.get("remarks") or "",
        "items": items,
        "item_count": int(receipt.get("item_count") or len(items)),
        "received_quantity_total": receipt.get(
            "received_quantity_total"
        ) or "0.0000",
        "accepted_quantity_total": receipt.get(
            "accepted_quantity_total"
        ) or "0.0000",
        "rejected_quantity_total": receipt.get(
            "rejected_quantity_total"
        ) or "0.0000",
        "damaged_quantity_total": receipt.get(
            "damaged_quantity_total"
        ) or "0.0000",
        "status": receipt.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(
            receipt.get("status"),
            str(receipt.get("status") or "").replace("_", " ").title(),
        ),
        "stock_posted": receipt.get("stock_posted") is True,
        "version": int(receipt.get("version") or 1),
        "created_by": str(receipt.get("created_by") or ""),
        "created_by_name": receipt.get("created_by_name") or "",
        "created_at": receipt.get("created_at"),
        "updated_by_name": receipt.get("updated_by_name") or "",
        "updated_at": receipt.get("updated_at"),
        "submitted_by_name": receipt.get("submitted_by_name") or "",
        "submitted_at": receipt.get("submitted_at"),
        "posted_by_name": receipt.get("posted_by_name") or "",
        "posted_at": receipt.get("posted_at"),
        "posting_note": receipt.get("posting_note") or "",
        "return_reason": receipt.get("return_reason") or "",
        "cancel_reason": receipt.get("cancel_reason") or "",
        "posting_error": receipt.get("posting_error") or "",
        "change_history": receipt.get("change_history") or [],
    }


def _po_receipt_catalog_row(order):
    posted_totals = _posted_receipt_totals(order["_id"])
    items = []
    total_pending = Decimal("0")
    total_accepted = Decimal("0")
    for index, raw_item in enumerate(order.get("items") or [], start=1):
        line_no = int(raw_item.get("line_no") or index)
        ordered = Decimal(str(raw_item.get("quantity") or 0))
        totals = posted_totals.get(
            line_no,
            {
                "received": Decimal("0"),
                "accepted": Decimal("0"),
                "rejected": Decimal("0"),
                "damaged": Decimal("0"),
            },
        )
        pending = max(ordered - totals["accepted"], Decimal("0"))
        total_pending += pending
        total_accepted += totals["accepted"]
        items.append(
            {
                "po_line_no": line_no,
                "product_id": str(raw_item.get("source_product_id") or ""),
                "product_code": raw_item.get("product_code") or "",
                "product_name": raw_item.get("product_name") or "Product",
                "hsn_code": raw_item.get("hsn_code") or "",
                "unit_code": raw_item.get("unit_code") or "",
                "ordered_quantity": _quantity_string(ordered),
                "received_quantity": _quantity_string(totals["received"]),
                "accepted_quantity": _quantity_string(totals["accepted"]),
                "rejected_quantity": _quantity_string(totals["rejected"]),
                "damaged_quantity": _quantity_string(totals["damaged"]),
                "pending_quantity": _quantity_string(pending),
                "accounting_mapping_id": str(
                    raw_item.get("accounting_mapping_id") or ""
                ),
            }
        )
    return {
        "id": str(order["_id"]),
        "po_number": order.get("po_number") or "",
        "supplier_name": order.get("supplier_name") or "",
        "supplier_code": order.get("supplier_code") or "",
        "status": order.get("status") or "",
        "status_display": str(order.get("status") or "").replace("_", " ").title(),
        "order_date": _date_string(order.get("order_date")),
        "expected_delivery_date": _date_string(
            order.get("expected_delivery_date")
        ),
        "total_pending_quantity": _quantity_string(total_pending),
        "total_accepted_quantity": _quantity_string(total_accepted),
        "items": items,
    }


def get_goods_receipt_form_catalog(
    accounting_entity_id,
    actor_user_id,
    purchase_order_id=None,
):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_goods_receipt_indexes()

    query = {
        "accounting_entity_id": entity["_id"],
        "status": {"$in": list(POSTABLE_PO_STATUSES)},
    }
    selected_id = _to_object_id(purchase_order_id)
    if selected_id:
        query["_id"] = selected_id

    orders = list(
        mongo.db[PURCHASE_ORDER_COLLECTION]
        .find(query)
        .sort([("order_date", DESCENDING), ("created_at", DESCENDING)])
    )
    catalog_orders = []
    for order in orders:
        row = _po_receipt_catalog_row(order)
        if Decimal(row["total_pending_quantity"]) > 0:
            catalog_orders.append(row)

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
        "purchase_orders": catalog_orders,
        "selected_purchase_order_id": str(selected_id or ""),
        "today": business_today().strftime("%Y-%m-%d"),
        "warehouse_defaults": {
            "warehouse_code": "AVPL-MAIN",
            "warehouse_name": "AVPL Main Warehouse",
            "warehouse_bin": "",
        },
        "status_labels": dict(STATUS_LABELS),
    }



def _enrich_receipt_invoice_actions(entity_id, serialized_rows):
    """Attach safe Purchase Invoice actions to Goods Receipt list rows.

    A posted GRN must not automatically offer "Record Purchase Invoice".
    The action depends on the actual active Purchase Invoice records and the
    remaining accepted-but-uninvoiced quantity for the PO.

    Existing/legacy invoices are associated to a GRN through either:
      1) the explicit source_grn_id link added by the refinement, or
      2) posted_grn_snapshots captured on the Purchase Invoice.

    This keeps older Purchase Invoices navigable without rewriting them.
    """
    if not serialized_rows:
        return serialized_rows

    receipt_ids = {
        str(row.get("id") or "")
        for row in serialized_rows
        if row.get("id")
    }
    po_ids = {
        _to_object_id(row.get("purchase_order_id"))
        for row in serialized_rows
        if _to_object_id(row.get("purchase_order_id"))
    }

    # Active Purchase Invoices only. Cancelled invoices must not block a
    # replacement invoice or appear as the current document.
    invoice_query = {
        "accounting_entity_id": entity_id,
        "status": {"$ne": "cancelled"},
    }
    if po_ids:
        invoice_query["purchase_order_id"] = {"$in": list(po_ids)}

    invoices = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION]
        .find(invoice_query)
        .sort([("invoice_date", DESCENDING), ("created_at", DESCENDING)])
    )

    # Map every GRN referenced by an invoice to that Purchase Invoice.
    # Explicit source_grn_id wins naturally because the invoices are newest first.
    invoice_by_grn = {}
    for invoice in invoices:
        invoice_id = str(invoice.get("_id") or "")
        invoice_number = (
            invoice.get("official_purchase_invoice_number")
            or invoice.get("internal_reference")
            or invoice.get("supplier_invoice_number")
            or ""
        )
        refs = set()

        source_grn_id = str(invoice.get("source_grn_id") or "")
        if source_grn_id:
            refs.add(source_grn_id)

        for snapshot in invoice.get("posted_grn_snapshots") or []:
            snapshot_id = str((snapshot or {}).get("id") or "")
            if snapshot_id:
                refs.add(snapshot_id)

        for ref in refs:
            if ref in receipt_ids and ref not in invoice_by_grn:
                invoice_by_grn[ref] = {
                    "id": invoice_id,
                    "number": invoice_number,
                }

    # Compute actual remaining quantity per PO from posted GRNs minus active
    # Purchase Invoice quantities. This mirrors the Purchase Invoice form's
    # eligibility rule, so the two screens cannot contradict each other.
    posted_accepted_by_po_line = {}
    if po_ids:
        for grn in mongo.db[GOODS_RECEIPT_COLLECTION].find(
            {
                "accounting_entity_id": entity_id,
                "purchase_order_id": {"$in": list(po_ids)},
                "status": STATUS_POSTED,
                "stock_posted": True,
            },
            {"purchase_order_id": 1, "items": 1},
        ):
            po_key = str(grn.get("purchase_order_id") or "")
            bucket = posted_accepted_by_po_line.setdefault(po_key, {})
            for index, item in enumerate(grn.get("items") or [], start=1):
                line_no = int(item.get("po_line_no") or item.get("line_no") or index)
                accepted = Decimal(str(item.get("accepted_quantity") or 0))
                bucket[line_no] = bucket.get(line_no, Decimal("0")) + accepted

    invoiced_by_po_line = {}
    for invoice in invoices:
        po_key = str(invoice.get("purchase_order_id") or "")
        bucket = invoiced_by_po_line.setdefault(po_key, {})
        for index, item in enumerate(invoice.get("items") or [], start=1):
            line_no = int(item.get("po_line_no") or item.get("line_no") or index)
            qty = Decimal(
                str(
                    item.get("invoice_quantity")
                    or item.get("quantity")
                    or 0
                )
            )
            bucket[line_no] = bucket.get(line_no, Decimal("0")) + qty

    po_has_remaining = {}
    for po_key, accepted_lines in posted_accepted_by_po_line.items():
        invoice_lines = invoiced_by_po_line.get(po_key, {})
        remaining = sum(
            (
                max(
                    accepted_qty - invoice_lines.get(line_no, Decimal("0")),
                    Decimal("0"),
                )
                for line_no, accepted_qty in accepted_lines.items()
            ),
            Decimal("0"),
        )
        po_has_remaining[po_key] = remaining > Decimal("0.0000")

    for row in serialized_rows:
        receipt_id = str(row.get("id") or "")
        po_key = str(row.get("purchase_order_id") or "")
        linked = invoice_by_grn.get(receipt_id)

        if linked:
            row["purchase_invoice_id"] = linked["id"]
            row["purchase_invoice_number"] = linked["number"]
            row["purchase_invoice_action"] = "view"
        elif (
            row.get("status") == STATUS_POSTED
            and row.get("stock_posted") is True
            and po_has_remaining.get(po_key, False)
        ):
            row["purchase_invoice_id"] = ""
            row["purchase_invoice_number"] = ""
            row["purchase_invoice_action"] = "record"
        else:
            row["purchase_invoice_id"] = ""
            row["purchase_invoice_number"] = ""
            row["purchase_invoice_action"] = "none"

    return serialized_rows


def get_goods_receipt_overview(
    accounting_entity_id,
    actor_user_id,
    status=None,
    query_text="",
):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_goods_receipt_indexes()

    query = {"accounting_entity_id": entity["_id"]}
    if status and status in STATUS_LABELS:
        query["status"] = status

    rows = list(
        mongo.db[GOODS_RECEIPT_COLLECTION]
        .find(query)
        .sort([("receipt_date", DESCENDING), ("created_at", DESCENDING)])
    )
    text = str(query_text or "").strip().lower()
    if text:
        rows = [
            row
            for row in rows
            if text in str(row.get("grn_number") or "").lower()
            or text in str(row.get("po_number") or "").lower()
            or text in str(row.get("supplier_name") or "").lower()
            or text
            in str(row.get("supplier_delivery_note") or "").lower()
            or any(
                text in str(item.get("product_name") or "").lower()
                for item in row.get("items") or []
            )
        ]

    counts = {key: 0 for key in STATUS_LABELS}
    for row in mongo.db[GOODS_RECEIPT_COLLECTION].find(
        {"accounting_entity_id": entity["_id"]},
        {"status": 1},
    ):
        key = row.get("status") or STATUS_DRAFT
        counts[key] = counts.get(key, 0) + 1

    serialized_rows = [serialize_goods_receipt(row) for row in rows]
    serialized_rows = _enrich_receipt_invoice_actions(
        entity["_id"],
        serialized_rows,
    )

    return {
        "rows": serialized_rows,
        "counts": counts,
        "status_labels": dict(STATUS_LABELS),
        "selected_status": status or "",
        "query": query_text or "",
    }


def get_goods_receipt(accounting_entity_id, actor_user_id, receipt_id):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    receipt = _get_receipt_document(receipt_id)
    if str(receipt.get("accounting_entity_id")) != str(entity["_id"]):
        raise PermissionError(
            "This Goods Receipt belongs to another Accounting entity."
        )
    return serialize_goods_receipt(receipt)


def get_goods_receipts_for_purchase_order(
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
        mongo.db[GOODS_RECEIPT_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "purchase_order_id": order_id,
            }
        )
        .sort([("receipt_date", DESCENDING), ("created_at", DESCENDING)])
    )
    return [serialize_goods_receipt(row) for row in rows]


def attach_goods_receipt_supplier_invoice_document(
    receipt_id,
    actor_user_id,
    document_id,
    filename,
    *,
    supplier_invoice_number="",
    supplier_invoice_date="",
):
    """Attach/replace the supplier GST/tax invoice captured with a GRN.

    The supplier invoice reference may be added after the GRN has already been
    posted (for example, when the supplier sends the tax invoice later). This
    action changes document/reference metadata only; it never reposts receipt
    quantities, stock movements, invoice matching, or Accounting entries.
    """
    actor = _get_actor(actor_user_id)
    receipt = _get_receipt_document(receipt_id)
    document_object_id = _to_object_id(document_id)
    clean_filename = _clean_text(
        filename,
        "Supplier invoice attachment",
        maximum=260,
        required=True,
    )
    if not document_object_id:
        raise ValueError("The supplier invoice attachment reference is invalid.")

    clean_invoice_number = _clean_text(
        supplier_invoice_number,
        "Supplier invoice number",
        maximum=80,
    ) or _clean_text(
        receipt.get("supplier_invoice_number_capture"),
        "Supplier invoice number",
        maximum=80,
    )
    parsed_invoice_date = _parse_date(
        supplier_invoice_date,
        "Supplier invoice date",
        required=False,
    ) or receipt.get("supplier_invoice_date_capture")

    if not clean_invoice_number:
        raise ValueError("Enter the supplier invoice number before attaching the supplier bill.")
    if not parsed_invoice_date:
        raise ValueError("Enter the supplier invoice date before attaching the supplier bill.")
    if isinstance(parsed_invoice_date, date) and not isinstance(parsed_invoice_date, datetime):
        parsed_invoice_date = datetime.combine(parsed_invoice_date, time.min)
    if parsed_invoice_date.date() > business_today():
        raise ValueError("Supplier invoice date cannot be in the future.")

    timestamp = now_utc()
    mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
        {"_id": receipt["_id"]},
        {
            "$set": {
                "supplier_invoice_received": True,
                "supplier_invoice_number_capture": clean_invoice_number,
                "supplier_invoice_date_capture": parsed_invoice_date,
                "supplier_invoice_attachment_document_id": document_object_id,
                "supplier_invoice_attachment_document_id_str": str(document_object_id),
                "supplier_invoice_attachment_filename": clean_filename,
                "supplier_invoice_attachment_updated_at": timestamp,
                "supplier_invoice_attachment_updated_by": actor["_id"],
                "supplier_invoice_attachment_updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "attach_supplier_invoice_document",
                    actor,
                    previous_status=receipt.get("status"),
                    new_status=receipt.get("status"),
                    reason=f"Supplier invoice {clean_invoice_number} attached to Goods Receipt.",
                )
            },
        },
    )
    updated = _get_receipt_document(receipt_id)
    _record_audit(
        updated,
        actor,
        "attach_supplier_invoice_document",
        previous_status=receipt.get("status"),
        reason=f"Supplier invoice {clean_invoice_number} attached to Goods Receipt.",
    )
    return {
        "receipt": serialize_goods_receipt(updated),
        "message": "Supplier invoice reference and attachment saved.",
    }


def create_goods_receipt(
    accounting_entity_id,
    actor_user_id,
    raw_payload,
    auto_post=False,
):
    actor = _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_goods_receipt_indexes()
    payload = _build_receipt_payload(entity, actor, raw_payload)

    timestamp = now_utc()
    document = {
        **payload,
        "document_uid": uuid4().hex,
        "grn_number": _next_grn_number(payload["receipt_date"]),
        "status": STATUS_DRAFT,
        "stock_posted": False,
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
                "create_goods_receipt_draft",
                actor,
                previous_status=None,
                new_status=STATUS_DRAFT,
                changed_fields=sorted(payload.keys()),
            )
        ],
        "audit_sync_required": False,
    }
    if not document.get("supplier_delivery_note_key"):
        document.pop("supplier_delivery_note_key", None)

    try:
        result = mongo.db[GOODS_RECEIPT_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "This supplier delivery note is already recorded for the supplier, "
            "or the generated GRN number already exists."
        ) from exc

    _record_audit(
        document,
        actor,
        "create_goods_receipt_draft",
        previous_status=None,
        reason="Goods Receipt draft created.",
        changed_fields=sorted(payload.keys()),
    )

    if auto_post and (
        actor.get("resolved_role") in CHECKER_ROLES
        or workflow_is_streamlined("avpl.goods_receipt")
    ):
        return post_goods_receipt(
            document["_id"],
            actor["_id"],
            expected_version=1,
            posting_note=raw_payload.get("posting_note") or "",
            allow_creator_post=True,
        )

    return {
        "receipt": serialize_goods_receipt(document),
        "message": "Goods Receipt draft created.",
    }


def update_goods_receipt(
    receipt_id,
    actor_user_id,
    raw_payload,
    expected_version,
    auto_post=False,
):
    actor = _get_actor(actor_user_id)
    receipt = _get_receipt_document(receipt_id)
    entity = _active_avpl_entity(receipt.get("accounting_entity_id"))

    if receipt.get("status") not in EDITABLE_STATUSES:
        raise ValueError(
            "Only Draft or Returned Goods Receipts can be edited."
        )
    if (
        actor.get("resolved_role") == "accounts"
        and str(receipt.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError(
            "Accounts users can edit only Goods Receipts they created."
        )

    version = _expected_version(expected_version)
    if version != int(receipt.get("version") or 1):
        raise RuntimeError(
            "This Goods Receipt changed. Refresh before saving."
        )

    payload = _build_receipt_payload(
        entity,
        actor,
        raw_payload,
        existing=receipt,
    )
    timestamp = now_utc()
    updates = {
        **payload,
        "status": STATUS_DRAFT,
        "version": version + 1,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "return_reason": None,
        "posting_error": None,
    }
    if not updates.get("supplier_delivery_note_key"):
        updates["supplier_delivery_note_key"] = None

    try:
        result = mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
            {
                "_id": receipt["_id"],
                "version": version,
                "status": {"$in": list(EDITABLE_STATUSES)},
            },
            {
                "$set": updates,
                "$push": {
                    "change_history": _history_event(
                        "update_goods_receipt_draft",
                        actor,
                        previous_status=receipt.get("status"),
                        new_status=STATUS_DRAFT,
                        changed_fields=sorted(payload.keys()),
                    )
                },
            },
        )
    except DuplicateKeyError as exc:
        raise RuntimeError(
            "This supplier delivery note is already recorded."
        ) from exc

    if result.matched_count != 1:
        raise RuntimeError(
            "This Goods Receipt changed. Refresh before saving."
        )

    updated = _get_receipt_document(receipt_id)
    _record_audit(
        updated,
        actor,
        "update_goods_receipt_draft",
        previous_status=receipt.get("status"),
        reason="Goods Receipt draft updated.",
        changed_fields=sorted(payload.keys()),
    )

    if auto_post and (
        actor.get("resolved_role") in CHECKER_ROLES
        or workflow_is_streamlined("avpl.goods_receipt")
    ):
        return post_goods_receipt(
            updated["_id"],
            actor["_id"],
            expected_version=updated["version"],
            posting_note=raw_payload.get("posting_note") or "",
            allow_creator_post=True,
        )

    return {
        "receipt": serialize_goods_receipt(updated),
        "message": "Goods Receipt draft updated.",
    }


def submit_goods_receipt(receipt_id, actor_user_id, expected_version):
    actor = _get_actor(actor_user_id)
    receipt = _get_receipt_document(receipt_id)
    if receipt.get("status") not in EDITABLE_STATUSES:
        raise ValueError(
            "Only a Draft or Returned Goods Receipt can be submitted."
        )
    if (
        actor.get("resolved_role") == "accounts"
        and str(receipt.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError(
            "Accounts users can submit only Goods Receipts they created."
        )

    version = _expected_version(expected_version)
    timestamp = now_utc()
    result = mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
        {
            "_id": receipt["_id"],
            "version": version,
            "status": {"$in": list(EDITABLE_STATUSES)},
        },
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "version": version + 1,
                "submitted_by": actor["_id"],
                "submitted_by_name": actor.get("resolved_name") or "",
                "submitted_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "submit_goods_receipt",
                    actor,
                    previous_status=receipt.get("status"),
                    new_status=STATUS_PENDING_APPROVAL,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError(
            "This Goods Receipt changed. Refresh before submitting."
        )

    updated = _get_receipt_document(receipt_id)
    _record_audit(
        updated,
        actor,
        "submit_goods_receipt",
        previous_status=receipt.get("status"),
        reason="Goods Receipt submitted for posting.",
    )
    return {
        "receipt": serialize_goods_receipt(updated),
        "message": "Goods Receipt submitted for posting.",
    }


def _lot_key(entity_id, item):
    batch = item.get("batch_number") or f"NO-BATCH-{item.get('line_no')}"
    expiry = item.get("expiry_date") or "NO-EXPIRY"
    return ":".join(
        [
            str(entity_id),
            str(item.get("source_product_id")),
            str(item.get("warehouse_code") or "AVPL-MAIN"),
            str(item.get("warehouse_bin") or "NO-BIN"),
            str(batch),
            str(expiry),
        ]
    )


def _apply_product_stock(receipt, item, actor):
    accepted = Decimal(str(item.get("accepted_quantity") or 0))
    if accepted <= 0:
        return False

    source_key = f"GRN:{receipt['_id']}:{item.get('line_no')}"
    product_id = item.get("source_product_id")

    # Legacy operational products may still contain a numeric string in
    # available_quantity. Normalize it safely while keeping the posting
    # idempotent and resilient to a concurrent stock update.
    for _attempt in range(5):
        product = mongo.db.products.find_one(
            {"_id": product_id, "is_deleted": {"$ne": True}},
            {
                "available_quantity": 1,
                "grn_stock_posting_keys": 1,
            },
        )
        if not product:
            raise RuntimeError(
                f"Product {item.get('product_name') or ''} was not found during stock posting."
            )
        if source_key in (product.get("grn_stock_posting_keys") or []):
            return False

        current_raw = product.get("available_quantity", 0)
        try:
            current_quantity = Decimal(str(current_raw or 0))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Current stock for {item.get('product_name') or 'the product'} is not numeric."
            ) from exc
        if not current_quantity.is_finite():
            raise RuntimeError(
                f"Current stock for {item.get('product_name') or 'the product'} is invalid."
            )

        new_quantity = current_quantity + accepted
        timestamp = now_utc()
        quantity_match = (
            {"available_quantity": current_raw}
            if "available_quantity" in product
            else {"available_quantity": {"$exists": False}}
        )
        result = mongo.db.products.update_one(
            {
                "_id": product_id,
                "is_deleted": {"$ne": True},
                "grn_stock_posting_keys": {"$ne": source_key},
                **quantity_match,
            },
            {
                "$set": {
                    "available_quantity": float(new_quantity),
                    "updated_at": timestamp,
                    "last_goods_receipt_at": timestamp,
                    "last_goods_receipt_by": actor["_id"],
                },
                "$addToSet": {"grn_stock_posting_keys": source_key},
                "$push": {
                    "stock_history": {
                        "source": "avpl_goods_receipt",
                        "source_key": source_key,
                        "grn_id": receipt["_id"],
                        "grn_number": receipt.get("grn_number"),
                        "purchase_order_id": receipt.get("purchase_order_id"),
                        "po_number": receipt.get("po_number"),
                        "previous_quantity": float(current_quantity),
                        "quantity_added": float(accepted),
                        "new_quantity": float(new_quantity),
                        "unit_code": item.get("unit_code") or "",
                        "warehouse_code": item.get("warehouse_code") or "",
                        "batch_number": item.get("batch_number") or "",
                        "expiry_date": item.get("expiry_date") or "",
                        "posted_by": actor["_id"],
                        "posted_at": timestamp,
                    }
                },
            },
        )
        if result.matched_count == 1:
            return result.modified_count == 1

    product = mongo.db.products.find_one(
        {"_id": product_id},
        {"grn_stock_posting_keys": 1},
    )
    if product and source_key in (product.get("grn_stock_posting_keys") or []):
        return False
    raise RuntimeError(
        f"Stock changed repeatedly while posting {item.get('product_name') or 'the product'}. Retry the GRN."
    )


def _apply_inventory_lot(receipt, item, actor):
    accepted = Decimal(str(item.get("accepted_quantity") or 0))
    if accepted <= 0:
        return

    source_key = f"GRN:{receipt['_id']}:{item.get('line_no')}"
    lot_key = _lot_key(receipt.get("accounting_entity_id"), item)
    timestamp = now_utc()
    collection = mongo.db[INVENTORY_LOT_COLLECTION]
    lot = collection.find_one({"lot_key": lot_key})

    if lot:
        collection.update_one(
            {
                "_id": lot["_id"],
                "applied_stock_keys": {"$ne": source_key},
            },
            {
                "$inc": {
                    "received_quantity": float(accepted),
                    "available_quantity": float(accepted),
                },
                "$addToSet": {"applied_stock_keys": source_key},
                "$set": {
                    "updated_at": timestamp,
                    "last_receipt_id": receipt["_id"],
                    "last_receipt_number": receipt.get("grn_number"),
                },
            },
        )
        return

    document = {
        "lot_key": lot_key,
        "accounting_entity_id": receipt.get("accounting_entity_id"),
        "accounting_entity_id_str": str(
            receipt.get("accounting_entity_id") or ""
        ),
        "source_product_id": item.get("source_product_id"),
        "source_product_id_str": str(item.get("source_product_id") or ""),
        "product_code": item.get("product_code") or "",
        "product_name": item.get("product_name") or "",
        "unit_code": item.get("unit_code") or "",
        "warehouse_code": item.get("warehouse_code") or "",
        "warehouse_name": item.get("warehouse_name") or "",
        "warehouse_bin": item.get("warehouse_bin") or "",
        "barcode": item.get("barcode") or "",
        "batch_number": item.get("batch_number") or "",
        "manufacturing_date": item.get("manufacturing_date") or "",
        "expiry_date": item.get("expiry_date") or "",
        "received_quantity": float(accepted),
        "available_quantity": float(accepted),
        "reserved_quantity": 0.0,
        "issued_quantity": 0.0,
        "status": "available",
        "applied_stock_keys": [source_key],
        "first_receipt_id": receipt["_id"],
        "first_receipt_number": receipt.get("grn_number"),
        "last_receipt_id": receipt["_id"],
        "last_receipt_number": receipt.get("grn_number"),
        "created_by": actor["_id"],
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        collection.insert_one(document)
    except DuplicateKeyError:
        collection.update_one(
            {
                "lot_key": lot_key,
                "applied_stock_keys": {"$ne": source_key},
            },
            {
                "$inc": {
                    "received_quantity": float(accepted),
                    "available_quantity": float(accepted),
                },
                "$addToSet": {"applied_stock_keys": source_key},
                "$set": {
                    "updated_at": timestamp,
                    "last_receipt_id": receipt["_id"],
                    "last_receipt_number": receipt.get("grn_number"),
                },
            },
        )


def _record_stock_movement(receipt, item, actor):
    accepted = Decimal(str(item.get("accepted_quantity") or 0))
    if accepted <= 0:
        return

    source_key = f"GRN:{receipt['_id']}:{item.get('line_no')}"
    timestamp = now_utc()
    mongo.db[STOCK_MOVEMENT_COLLECTION].update_one(
        {"source_posting_key": source_key},
        {
            "$setOnInsert": {
                "source_posting_key": source_key,
                "movement_uid": uuid4().hex,
                "accounting_entity_id": receipt.get("accounting_entity_id"),
                "accounting_entity_id_str": str(
                    receipt.get("accounting_entity_id") or ""
                ),
                "source_document_type": "goods_receipt",
                "source_document_id": receipt["_id"],
                "source_document_id_str": str(receipt["_id"]),
                "source_document_number": receipt.get("grn_number"),
                "purchase_order_id": receipt.get("purchase_order_id"),
                "po_number": receipt.get("po_number"),
                "source_product_id": item.get("source_product_id"),
                "source_product_id_str": str(
                    item.get("source_product_id") or ""
                ),
                "product_code": item.get("product_code") or "",
                "product_name": item.get("product_name") or "",
                "movement_type": "purchase_receipt",
                "direction": "in",
                "quantity": float(accepted),
                "quantity_display": _quantity_string(accepted),
                "unit_code": item.get("unit_code") or "",
                "warehouse_code": item.get("warehouse_code") or "",
                "warehouse_name": item.get("warehouse_name") or "",
                "warehouse_bin": item.get("warehouse_bin") or "",
                "barcode": item.get("barcode") or "",
                "batch_number": item.get("batch_number") or "",
                "manufacturing_date": item.get("manufacturing_date") or "",
                "expiry_date": item.get("expiry_date") or "",
                "movement_date": receipt.get("receipt_date"),
                "posted_by": actor["_id"],
                "posted_by_name": actor.get("resolved_name") or "",
                "posted_at": timestamp,
                "created_at": timestamp,
            }
        },
        upsert=True,
    )


def _refresh_purchase_order_receipt_summary(purchase_order_id):
    order = mongo.db[PURCHASE_ORDER_COLLECTION].find_one(
        {"_id": purchase_order_id}
    )
    if not order:
        raise RuntimeError(
            "The linked Purchase Order was not found while refreshing receipt status."
        )

    totals = _posted_receipt_totals(order["_id"])
    updated_items = []
    all_fulfilled = True
    any_physical_receipt = False
    any_accepted = False
    accepted_total = Decimal("0")
    received_total = Decimal("0")
    rejected_total = Decimal("0")
    damaged_total = Decimal("0")

    for index, raw_item in enumerate(order.get("items") or [], start=1):
        item = dict(raw_item)
        line_no = int(item.get("line_no") or index)
        ordered = Decimal(str(item.get("quantity") or 0))
        line_totals = totals.get(
            line_no,
            {
                "received": Decimal("0"),
                "accepted": Decimal("0"),
                "rejected": Decimal("0"),
                "damaged": Decimal("0"),
            },
        )
        pending = max(ordered - line_totals["accepted"], Decimal("0"))
        if pending > 0:
            all_fulfilled = False
        if line_totals["received"] > 0:
            any_physical_receipt = True
        if line_totals["accepted"] > 0:
            any_accepted = True

        item.update(
            {
                "received_quantity": _quantity_string(
                    line_totals["received"]
                ),
                "accepted_quantity": _quantity_string(
                    line_totals["accepted"]
                ),
                "rejected_quantity": _quantity_string(
                    line_totals["rejected"]
                ),
                "damaged_quantity": _quantity_string(
                    line_totals["damaged"]
                ),
                "pending_quantity": _quantity_string(pending),
                "receipt_status": (
                    "fully_received"
                    if pending <= 0
                    else "partially_received"
                    if line_totals["received"] > 0
                    else "not_received"
                ),
            }
        )
        updated_items.append(item)
        accepted_total += line_totals["accepted"]
        received_total += line_totals["received"]
        rejected_total += line_totals["rejected"]
        damaged_total += line_totals["damaged"]

    if all_fulfilled and updated_items:
        status = PO_STATUS_FULLY_RECEIVED
        receipt_status = "fully_received"
    elif any_physical_receipt:
        status = PO_STATUS_PARTIALLY_RECEIVED
        receipt_status = "partially_received"
    else:
        status = PO_STATUS_APPROVED
        receipt_status = "not_started"

    grn_count = mongo.db[GOODS_RECEIPT_COLLECTION].count_documents(
        {
            "purchase_order_id": order["_id"],
            "status": STATUS_POSTED,
            "stock_posted": True,
        }
    )
    mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"]},
        {
            "$set": {
                "items": updated_items,
                "status": status,
                "receipt_status": receipt_status,
                "stock_posted": any_accepted,
                "grn_count": grn_count,
                "received_quantity_total": _quantity_string(
                    received_total
                ),
                "accepted_quantity_total": _quantity_string(
                    accepted_total
                ),
                "rejected_quantity_total": _quantity_string(
                    rejected_total
                ),
                "damaged_quantity_total": _quantity_string(
                    damaged_total
                ),
                "receipt_summary_updated_at": now_utc(),
                "updated_at": now_utc(),
            },
            "$inc": {"version": 1},
        },
    )


def post_goods_receipt(
    receipt_id,
    actor_user_id,
    expected_version,
    posting_note="",
    allow_creator_post=False,
):
    actor = _get_actor(actor_user_id)
    if (
        actor.get("resolved_role") not in CHECKER_ROLES
        and not workflow_is_streamlined("avpl.goods_receipt")
    ):
        raise PermissionError(
            "You are not authorized to post a Goods Receipt."
        )

    with _posting_lock(receipt_id):
        receipt = _get_receipt_document(receipt_id)
        if receipt.get("status") == STATUS_POSTED and receipt.get("stock_posted"):
            return {
                "receipt": serialize_goods_receipt(receipt),
                "message": "Goods Receipt is already posted.",
            }
        if receipt.get("status") not in {
            STATUS_DRAFT,
            STATUS_PENDING_APPROVAL,
            STATUS_RETURNED,
        }:
            raise ValueError(
                "Only a Draft, Pending or Returned Goods Receipt can be posted."
            )

        version = _expected_version(expected_version)
        if version != int(receipt.get("version") or 1):
            raise RuntimeError(
                "This Goods Receipt changed. Refresh before posting."
            )
        if (
            receipt.get("status") == STATUS_PENDING_APPROVAL
            and str(receipt.get("created_by")) == str(actor["_id"])
            and not allow_creator_post
        ):
            raise PermissionError(
                "The maker cannot post the same submitted Goods Receipt."
            )

        entity = _active_avpl_entity(receipt.get("accounting_entity_id"))
        rebuilt = _build_receipt_payload(
            entity,
            actor,
            {
                **receipt,
                "purchase_order_id": str(receipt.get("purchase_order_id")),
                "receipt_date": _date_string(receipt.get("receipt_date")),
                "items": receipt.get("items") or [],
            },
            existing=receipt,
        )
        receipt.update(rebuilt)

        try:
            for item in receipt.get("items") or []:
                _apply_product_stock(receipt, item, actor)
                _apply_inventory_lot(receipt, item, actor)
                _record_stock_movement(receipt, item, actor)

            timestamp = now_utc()
            note = _clean_multiline(
                posting_note,
                "Posting note",
                maximum=1000,
            )
            previous_status = receipt.get("status")
            result = mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
                {
                    "_id": receipt["_id"],
                    "version": version,
                    "status": previous_status,
                    "stock_posted": {"$ne": True},
                },
                {
                    "$set": {
                        **rebuilt,
                        "status": STATUS_POSTED,
                        "stock_posted": True,
                        "version": version + 1,
                        "posted_by": actor["_id"],
                        "posted_by_str": str(actor["_id"]),
                        "posted_by_name": actor.get("resolved_name") or "",
                        "posted_at": timestamp,
                        "posting_note": note,
                        "posting_error": None,
                        "updated_by": actor["_id"],
                        "updated_by_name": actor.get("resolved_name") or "",
                        "updated_at": timestamp,
                    },
                    "$push": {
                        "change_history": _history_event(
                            "post_goods_receipt",
                            actor,
                            previous_status=previous_status,
                            new_status=STATUS_POSTED,
                            reason=note,
                        )
                    },
                },
            )
            if result.matched_count != 1:
                current = _get_receipt_document(receipt_id)
                if not (
                    current.get("status") == STATUS_POSTED
                    and current.get("stock_posted") is True
                ):
                    raise RuntimeError(
                        "This Goods Receipt changed during posting. Refresh and verify stock."
                    )

            _refresh_purchase_order_receipt_summary(
                receipt.get("purchase_order_id")
            )
        except Exception as exc:
            mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
                {"_id": receipt["_id"]},
                {
                    "$set": {
                        "posting_error": str(exc)[:1000],
                        "posting_error_at": now_utc(),
                        "updated_at": now_utc(),
                    }
                },
            )
            raise

        updated = _get_receipt_document(receipt_id)
        _record_audit(
            updated,
            actor,
            "post_goods_receipt",
            previous_status=receipt.get("status"),
            reason=posting_note or "Accepted quantities posted to AVPL stock.",
        )
        return {
            "receipt": serialize_goods_receipt(updated),
            "message": (
                "Goods Receipt posted successfully. Only accepted quantities "
                "were added to AVPL stock."
            ),
        }


def return_goods_receipt(
    receipt_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") not in CHECKER_ROLES:
        raise PermissionError(
            "Only AVPL Admin or Super Admin can return a Goods Receipt."
        )
    receipt = _get_receipt_document(receipt_id)
    if receipt.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError(
            "Only a Pending Goods Receipt can be returned."
        )
    version = _expected_version(expected_version)
    reason_text = _clean_multiline(
        reason,
        "Correction reason",
        maximum=1000,
    )
    if not reason_text:
        raise ValueError("Correction reason is required.")

    timestamp = now_utc()
    result = mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
        {
            "_id": receipt["_id"],
            "version": version,
            "status": STATUS_PENDING_APPROVAL,
        },
        {
            "$set": {
                "status": STATUS_RETURNED,
                "version": version + 1,
                "return_reason": reason_text,
                "returned_by": actor["_id"],
                "returned_by_name": actor.get("resolved_name") or "",
                "returned_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "return_goods_receipt",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_RETURNED,
                    reason=reason_text,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError(
            "This Goods Receipt changed. Refresh before returning."
        )
    updated = _get_receipt_document(receipt_id)
    _record_audit(
        updated,
        actor,
        "return_goods_receipt",
        previous_status=STATUS_PENDING_APPROVAL,
        reason=reason_text,
    )
    return {
        "receipt": serialize_goods_receipt(updated),
        "message": "Goods Receipt returned for correction.",
    }


def cancel_goods_receipt(
    receipt_id,
    actor_user_id,
    expected_version,
    reason,
):
    actor = _get_actor(actor_user_id)
    receipt = _get_receipt_document(receipt_id)
    if receipt.get("status") == STATUS_POSTED or receipt.get("stock_posted"):
        raise ValueError(
            "A posted Goods Receipt cannot be cancelled. Use a controlled "
            "stock adjustment or purchase return in a later stage."
        )
    if receipt.get("status") == STATUS_CANCELLED:
        raise ValueError("This Goods Receipt is already cancelled.")
    if (
        actor.get("resolved_role") == "accounts"
        and str(receipt.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError(
            "Accounts users can cancel only Goods Receipts they created."
        )

    version = _expected_version(expected_version)
    reason_text = _clean_multiline(
        reason,
        "Cancellation reason",
        maximum=1000,
    )
    if not reason_text:
        raise ValueError("Cancellation reason is required.")

    previous_status = receipt.get("status")
    timestamp = now_utc()
    result = mongo.db[GOODS_RECEIPT_COLLECTION].update_one(
        {
            "_id": receipt["_id"],
            "version": version,
            "status": previous_status,
            "stock_posted": {"$ne": True},
        },
        {
            "$set": {
                "status": STATUS_CANCELLED,
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
                    "cancel_goods_receipt",
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
            "This Goods Receipt changed. Refresh before cancelling."
        )
    updated = _get_receipt_document(receipt_id)
    _record_audit(
        updated,
        actor,
        "cancel_goods_receipt",
        previous_status=previous_status,
        reason=reason_text,
    )
    return {
        "receipt": serialize_goods_receipt(updated),
        "message": "Goods Receipt cancelled without changing stock.",
    }
