from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.extensions import mongo
from app.services.accounting_gst_determination_service import (
    determine_gst_for_transaction,
)
from app.services.accounting_party_ledger_service import (
    LEDGER_COLLECTION,
    PARTY_ROLE_SUPPLIER,
    get_active_party_ledger_for_posting,
    serialize_party_ledger,
)
from app.services.accounting_product_mapping_service import (
    get_product_accounting_mapping_for_posting,
)
from app.utils.helpers import now_utc
from app.services.workflow_policy_service import workflow_is_streamlined


PURCHASE_ORDER_COLLECTION = "avpl_purchase_orders"
AVPL_ENTITY_CODE = "AVPL"

STATUS_DRAFT = "draft"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_RETURNED = "returned_for_correction"
STATUS_APPROVED = "approved"
STATUS_PARTIALLY_RECEIVED = "partially_received"
STATUS_FULLY_RECEIVED = "fully_received"
STATUS_CANCELLED = "cancelled"

STATUS_LABELS = {
    STATUS_DRAFT: "Draft",
    STATUS_PENDING_APPROVAL: "Pending Approval",
    STATUS_RETURNED: "Returned for Correction",
    STATUS_APPROVED: "Approved",
    STATUS_PARTIALLY_RECEIVED: "Partially Received",
    STATUS_FULLY_RECEIVED: "Fully Received",
    STATUS_CANCELLED: "Cancelled",
}

EDITABLE_STATUSES = {STATUS_DRAFT, STATUS_RETURNED}
APPROVABLE_STATUSES = {STATUS_PENDING_APPROVAL}
RECEIPT_STATUSES = {STATUS_PARTIALLY_RECEIVED, STATUS_FULLY_RECEIVED}
ALLOWED_ROLES = {"accounts", "avpl_admin", "super_admin"}
CHECKER_ROLES = {"avpl_admin", "super_admin"}

MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.0001")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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
            f"Could not create purchase-order index {name}."
        ) from exc


def ensure_purchase_order_indexes():
    collection = mongo.db[PURCHASE_ORDER_COLLECTION]

    _ensure_exact_index(
        collection,
        [("po_number", ASCENDING)],
        name="avpl_purchase_order_number_unique",
        unique=True,
    )
    _ensure_exact_index(
        collection,
        [
            ("accounting_entity_id", ASCENDING),
            ("status", ASCENDING),
            ("order_date", DESCENDING),
        ],
        name="avpl_purchase_order_entity_status_date_idx",
    )
    _ensure_exact_index(
        collection,
        [
            ("supplier_ledger_id", ASCENDING),
            ("order_date", DESCENDING),
        ],
        name="avpl_purchase_order_supplier_date_idx",
    )
    _ensure_exact_index(
        collection,
        [("created_by", ASCENDING), ("updated_at", DESCENDING)],
        name="avpl_purchase_order_creator_updated_idx",
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
            "You are not authorized to manage AVPL purchase orders."
        )

    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError("Inactive users cannot manage purchase orders.")

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
    entity_id = _to_object_id(accounting_entity_id)
    if entity_id:
        query["_id"] = entity_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise ValueError("The active AVPL Accounting entity was not found.")
    return entity


def _clean_text(value, label, maximum=500, required=False):
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ValueError(f"{label} is required.")
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _clean_multiline(value, label, maximum=1500):
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    if len(text) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return text


def _parse_date(value, label, required=True):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())

    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{label} is required.")
        return None

    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc
    return parsed


def _decimal(value, label, *, minimum="0", maximum="999999999999.99", quantum=MONEY_QUANTUM):
    try:
        parsed = Decimal(str(value if value is not None else "0").strip() or "0")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc

    parsed = parsed.quantize(quantum, rounding=ROUND_HALF_UP)
    if parsed < Decimal(minimum) or parsed > Decimal(maximum):
        raise ValueError(f"{label} must be between {minimum} and {maximum}.")
    return parsed


def _money(value):
    return Decimal(str(value or 0)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _decimal_string(value):
    return format(_money(value), "f")


def _quantity_string(value):
    return format(Decimal(str(value or 0)).quantize(QTY_QUANTUM, rounding=ROUND_HALF_UP), "f")


def _parse_items(raw_items):
    if isinstance(raw_items, str):
        try:
            items = json.loads(raw_items or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("Purchase-order items are invalid.") from exc
    else:
        items = raw_items

    if not isinstance(items, list) or not items:
        raise ValueError("Add at least one product to the purchase order.")
    if len(items) > 100:
        raise ValueError("A purchase order cannot contain more than 100 product lines.")
    return items


def _next_po_number(order_date):
    year = order_date.year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_purchase_order:{year}"},
        {
            "$inc": {"sequence": 1},
            "$setOnInsert": {
                "counter_type": "avpl_purchase_order",
                "year": year,
                "created_at": now_utc(),
            },
            "$set": {"updated_at": now_utc()},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"AVPL-PO-{year}-{sequence:05d}"


def _history_event(action, actor, previous_status=None, new_status=None, reason="", changed_fields=None):
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


def _record_audit(order, actor, action, previous_status=None, reason="", changed_fields=None):
    audit = {
        "module": "avpl_procurement",
        "submodule": "purchase_order",
        "action": action,
        "accounting_entity_id": order.get("accounting_entity_id"),
        "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
        "entity_type": "avpl_purchase_order",
        "entity_id": order.get("_id"),
        "entity_id_str": str(order.get("_id") or ""),
        "actor_user_id": actor["_id"],
        "actor_user_id_str": str(actor["_id"]),
        "actor_name": actor.get("resolved_name") or "",
        "actor_role": actor.get("resolved_role") or "",
        "previous_status": previous_status,
        "new_status": order.get("status"),
        "metadata": {
            "po_number": order.get("po_number"),
            "supplier_ledger_id": str(order.get("supplier_ledger_id") or ""),
            "supplier_name": order.get("supplier_name"),
            "item_count": len(order.get("items") or []),
            "grand_total": order.get("grand_total"),
            "version": int(order.get("version") or 1),
            "changed_fields": sorted(set(changed_fields or [])),
        },
        "remarks": str(reason or "")[:1500],
        "created_at": now_utc(),
    }
    try:
        mongo.db.accounting_audit_logs.insert_one(audit)
    except Exception as exc:
        mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
            {"_id": order.get("_id")},
            {
                "$set": {
                    "audit_sync_required": True,
                    "audit_sync_error": str(exc)[:500],
                    "audit_sync_marked_at": now_utc(),
                }
            },
        )


# ---------------------------------------------------------------------------
# Calculation and validation
# ---------------------------------------------------------------------------


NON_REGULAR_SUPPLIER_GST_STATUSES = {
    "unregistered",
    "registered_composition",
    "exempt",
}


def _supplier_gst_status(supplier):
    return str(
        supplier.get("gst_registration_status") or ""
    ).strip().lower()


def _purchase_order_gst_preview(
    entity,
    supplier,
    product_id,
    mapping_bundle,
    order_date,
    taxable_value,
):
    """Resolve line tax for a Purchase Order without blocking valid unregistered suppliers.

    A Purchase Order records commercial intent only. When a supplier is
    unregistered, composition-registered or exempt, the supplier cannot pass
    regular input GST through the PO. The PO is therefore allowed with zero
    supplier GST and a compliance flag for later supplier-invoice/RCM review.
    """
    hsn = mapping_bundle.get("hsn") or {}
    taxability_code = str(
        hsn.get("taxability_code") or ""
    ).strip().upper()
    supplier_status = _supplier_gst_status(supplier)

    if (
        taxability_code == "TAXABLE"
        and supplier_status in NON_REGULAR_SUPPLIER_GST_STATUSES
    ):
        supplier_state_code = str(
            supplier.get("state_code") or ""
        ).strip()
        recipient_state_code = str(
            entity.get("state_code") or ""
        ).strip()

        if not supplier_state_code:
            raise ValueError(
                "The selected supplier does not have a valid state code."
            )
        if not recipient_state_code:
            raise ValueError(
                "The AVPL Accounting entity does not have a valid state code."
            )

        supply_type = (
            "intra_state"
            if supplier_state_code == recipient_state_code
            else "inter_state"
        )

        if supplier_status == "unregistered":
            treatment_code = "UNREGISTERED_SUPPLIER_NO_GST"
            treatment_label = "Supplier unregistered — GST not charged"
            compliance_note = (
                "No supplier GST or input tax credit is included in this "
                "Purchase Order. Review reverse-charge applicability, if any, "
                "when the supplier invoice/purchase document is posted."
            )
            rcm_review_required = True
        elif supplier_status == "registered_composition":
            treatment_code = "COMPOSITION_SUPPLIER_NO_ITC"
            treatment_label = "Composition supplier — no input GST credit"
            compliance_note = (
                "The composition supplier cannot pass regular input GST. "
                "Final treatment will be confirmed during supplier-invoice posting."
            )
            rcm_review_required = False
        else:
            treatment_code = "EXEMPT_SUPPLIER_NO_GST"
            treatment_label = "Exempt supplier — GST not charged"
            compliance_note = (
                "No supplier GST or input tax credit is included in this "
                "Purchase Order."
            )
            rcm_review_required = False

        return {
            "supply_type": supply_type,
            "supply_type_label": (
                "Intra-state — supplier GST not charged"
                if supply_type == "intra_state"
                else "Inter-state — supplier GST not charged"
            ),
            "place_of_supply": {
                "state_code": recipient_state_code,
                "state_name": (
                    entity.get("state_name")
                    or entity.get("state")
                    or ""
                ),
                "source": "AVPL recipient state",
            },
            "components": [],
            "total_tax": "0.00",
            "gross_value": _decimal_string(taxable_value),
            "gst_treatment_code": treatment_code,
            "gst_treatment_label": treatment_label,
            "supplier_gst_registration_status": supplier_status,
            "input_tax_credit_eligible": False,
            "reverse_charge_review_required": rcm_review_required,
            "compliance_note": compliance_note,
        }

    preview = determine_gst_for_transaction(
        entity["_id"],
        supplier["_id"],
        product_id,
        "purchase",
        order_date.date(),
        taxable_value,
        place_of_supply_state_code=entity.get("state_code"),
    )

    preview = dict(preview or {})
    is_taxable = taxability_code == "TAXABLE"
    preview.setdefault(
        "gst_treatment_code",
        "FORWARD_CHARGE" if is_taxable else "PRODUCT_NON_TAXABLE",
    )
    preview.setdefault(
        "gst_treatment_label",
        "Supplier GST charged normally"
        if is_taxable
        else f"{taxability_code.replace('_', ' ').title()} — no GST",
    )
    preview.setdefault(
        "supplier_gst_registration_status",
        supplier_status,
    )
    preview.setdefault("input_tax_credit_eligible", is_taxable)
    preview.setdefault("reverse_charge_review_required", False)
    preview.setdefault("compliance_note", "")
    return preview


def _build_order_payload(entity, supplier, raw_payload):
    order_date = _parse_date(raw_payload.get("order_date"), "Order date")
    expected_delivery_date = _parse_date(
        raw_payload.get("expected_delivery_date"),
        "Expected delivery date",
        required=False,
    )
    if expected_delivery_date and expected_delivery_date.date() < order_date.date():
        raise ValueError("Expected delivery date cannot be before the order date.")

    raw_items = _parse_items(raw_payload.get("items") or raw_payload.get("items_json"))
    line_items = []
    seen_products = set()

    subtotal = Decimal("0.00")
    discount_total = Decimal("0.00")
    taxable_total = Decimal("0.00")
    cgst_total = Decimal("0.00")
    sgst_total = Decimal("0.00")
    igst_total = Decimal("0.00")
    tax_total = Decimal("0.00")
    reverse_charge_review_required = False
    non_regular_supplier_tax_lines = 0

    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Line {index} is invalid.")

        product_id = _to_object_id(raw_item.get("product_id"))
        if not product_id:
            raise ValueError(f"Select a valid product on line {index}.")
        if str(product_id) in seen_products:
            raise ValueError("The same product cannot appear more than once in a purchase order.")
        seen_products.add(str(product_id))

        quantity = _decimal(
            raw_item.get("quantity"),
            f"Quantity on line {index}",
            minimum="0.0001",
            maximum="999999999.9999",
            quantum=QTY_QUANTUM,
        )
        rate = _decimal(
            raw_item.get("rate"),
            f"Rate on line {index}",
            minimum="0.01",
        )
        discount_percent = _decimal(
            raw_item.get("discount_percent") or "0",
            f"Discount on line {index}",
            minimum="0",
            maximum="100",
        )

        mapping_bundle = get_product_accounting_mapping_for_posting(
            entity["_id"],
            product_id,
            transaction_date=order_date.date(),
            operation="purchase",
        )

        product = mongo.db.products.find_one(
            {
                "_id": product_id,
                "is_deleted": {"$ne": True},
                "is_active": {"$ne": False},
                "status": {"$ne": "inactive"},
            }
        )
        if not product:
            raise ValueError(f"The product on line {index} is inactive or unavailable.")

        line_subtotal = _money(quantity * rate)
        line_discount = _money(line_subtotal * discount_percent / Decimal("100"))
        taxable_value = _money(line_subtotal - line_discount)
        if taxable_value <= 0:
            raise ValueError(f"Taxable value on line {index} must be greater than zero.")

        gst_preview = _purchase_order_gst_preview(
            entity,
            supplier,
            product_id,
            mapping_bundle,
            order_date,
            taxable_value,
        )

        if gst_preview.get("reverse_charge_review_required"):
            reverse_charge_review_required = True
        if gst_preview.get("gst_treatment_code") in {
            "UNREGISTERED_SUPPLIER_NO_GST",
            "COMPOSITION_SUPPLIER_NO_ITC",
            "EXEMPT_SUPPLIER_NO_GST",
        }:
            non_regular_supplier_tax_lines += 1

        component_amounts = {"CGST": Decimal("0.00"), "SGST": Decimal("0.00"), "IGST": Decimal("0.00")}
        component_rates = {"CGST": "0", "SGST": "0", "IGST": "0"}
        for component in gst_preview.get("components") or []:
            code = str(component.get("code") or "").upper()
            if code in component_amounts:
                component_amounts[code] += _money(component.get("amount"))
                component_rates[code] = str(component.get("rate") or "0")

        line_tax = _money(gst_preview.get("total_tax"))
        line_total = _money(taxable_value + line_tax)
        source = mapping_bundle.get("source_product") or {}
        hsn = mapping_bundle.get("hsn") or {}
        unit = mapping_bundle.get("base_unit") or {}

        line_items.append(
            {
                "line_no": index,
                "source_product_id": product_id,
                "source_product_id_str": str(product_id),
                "product_code": product.get("product_code") or source.get("product_code") or "",
                "product_name": product.get("name") or source.get("name") or "Product",
                "product_role": product.get("product_role") or product.get("type") or "",
                "hsn_code": hsn.get("hsn_code") or "",
                "taxability_code": hsn.get("taxability_code") or "",
                "gst_rate_code": hsn.get("gst_rate_code") or "",
                "base_unit_id": _to_object_id(unit.get("id")),
                "base_unit_id_str": str(unit.get("id") or ""),
                "unit_code": unit.get("unit_code") or "",
                "unit_name": unit.get("name") or "",
                "quantity": _quantity_string(quantity),
                "rate": _decimal_string(rate),
                "line_subtotal": _decimal_string(line_subtotal),
                "discount_percent": _decimal_string(discount_percent),
                "discount_amount": _decimal_string(line_discount),
                "taxable_value": _decimal_string(taxable_value),
                "supply_type": gst_preview.get("supply_type") or "",
                "supply_type_label": gst_preview.get("supply_type_label") or "",
                "place_of_supply_state_code": (gst_preview.get("place_of_supply") or {}).get("state_code") or "",
                "place_of_supply_state_name": (gst_preview.get("place_of_supply") or {}).get("state_name") or "",
                "cgst_rate": component_rates["CGST"],
                "sgst_rate": component_rates["SGST"],
                "igst_rate": component_rates["IGST"],
                "cgst_amount": _decimal_string(component_amounts["CGST"]),
                "sgst_amount": _decimal_string(component_amounts["SGST"]),
                "igst_amount": _decimal_string(component_amounts["IGST"]),
                "tax_amount": _decimal_string(line_tax),
                "gst_treatment_code": gst_preview.get("gst_treatment_code") or "",
                "gst_treatment_label": gst_preview.get("gst_treatment_label") or "",
                "supplier_gst_registration_status": (
                    gst_preview.get("supplier_gst_registration_status") or ""
                ),
                "input_tax_credit_eligible": (
                    gst_preview.get("input_tax_credit_eligible") is True
                ),
                "reverse_charge_review_required": (
                    gst_preview.get("reverse_charge_review_required") is True
                ),
                "tax_compliance_note": gst_preview.get("compliance_note") or "",
                "line_total": _decimal_string(line_total),
                "accounting_mapping_id": _to_object_id((mapping_bundle.get("mapping") or {}).get("id")),
                "accounting_mapping_code": (mapping_bundle.get("mapping") or {}).get("mapping_code") or "",
                "tax_snapshot_locked": True,
                "received_quantity": "0.0000",
                "pending_quantity": _quantity_string(quantity),
                "receipt_status": "not_received",
            }
        )

        subtotal += line_subtotal
        discount_total += line_discount
        taxable_total += taxable_value
        cgst_total += component_amounts["CGST"]
        sgst_total += component_amounts["SGST"]
        igst_total += component_amounts["IGST"]
        tax_total += line_tax

    freight_amount = _decimal(raw_payload.get("freight_amount") or "0", "Freight amount")
    other_charges = _decimal(raw_payload.get("other_charges") or "0", "Other charges")

    subtotal = _money(subtotal)
    discount_total = _money(discount_total)
    taxable_total = _money(taxable_total)
    cgst_total = _money(cgst_total)
    sgst_total = _money(sgst_total)
    igst_total = _money(igst_total)
    tax_total = _money(tax_total)
    grand_total = _money(taxable_total + tax_total + freight_amount + other_charges)

    return {
        "accounting_entity_id": entity["_id"],
        "accounting_entity_id_str": str(entity["_id"]),
        "supplier_ledger_id": supplier["_id"],
        "supplier_ledger_id_str": str(supplier["_id"]),
        "supplier_snapshot": serialize_party_ledger(supplier),
        "supplier_code": supplier.get("ledger_code") or "",
        "supplier_name": supplier.get("name") or supplier.get("legal_name") or "Supplier",
        "supplier_gstin": supplier.get("gstin") or "",
        "supplier_gst_registration_status": _supplier_gst_status(supplier),
        "supplier_gst_registration_status_label": (
            (serialize_party_ledger(supplier) or {}).get(
                "gst_registration_status_label"
            )
            or _supplier_gst_status(supplier).replace("_", " ").title()
        ),
        "supplier_state_code": supplier.get("state_code") or "",
        "supplier_state_name": supplier.get("state_name") or "",
        "order_date": order_date,
        "expected_delivery_date": expected_delivery_date,
        "supplier_reference": _clean_text(raw_payload.get("supplier_reference"), "Supplier reference", 120),
        "payment_terms": _clean_text(raw_payload.get("payment_terms"), "Payment terms", 240),
        "delivery_address": _clean_multiline(raw_payload.get("delivery_address"), "Delivery address", 1000),
        "remarks": _clean_multiline(raw_payload.get("remarks"), "Remarks", 1500),
        "items": line_items,
        "item_count": len(line_items),
        "subtotal": _decimal_string(subtotal),
        "discount_total": _decimal_string(discount_total),
        "taxable_total": _decimal_string(taxable_total),
        "cgst_total": _decimal_string(cgst_total),
        "sgst_total": _decimal_string(sgst_total),
        "igst_total": _decimal_string(igst_total),
        "tax_total": _decimal_string(tax_total),
        "freight_amount": _decimal_string(freight_amount),
        "other_charges": _decimal_string(other_charges),
        "grand_total": _decimal_string(grand_total),
        "currency": "INR",
        "charges_tax_treatment": "excluded_from_line_gst_preview",
        "reverse_charge_review_required": reverse_charge_review_required,
        "non_regular_supplier_tax_lines": non_regular_supplier_tax_lines,
    }


def _get_order_document(order_id):
    object_id = _to_object_id(order_id)
    if not object_id:
        raise ValueError("Invalid purchase-order reference.")

    order = mongo.db[PURCHASE_ORDER_COLLECTION].find_one({"_id": object_id})
    if not order:
        raise ValueError("Purchase order was not found.")
    return order


def _expected_version(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid purchase-order version. Refresh and try again.") from exc
    if parsed < 1:
        raise ValueError("Invalid purchase-order version. Refresh and try again.")
    return parsed


def serialize_purchase_order(order):
    if not order:
        return None

    def date_string(value):
        return value.strftime("%Y-%m-%d") if isinstance(value, datetime) else ""

    serialized_items = []
    for raw_item in order.get("items") or []:
        item = dict(raw_item)
        item["source_product_id_str"] = str(
            item.get("source_product_id_str")
            or item.get("source_product_id")
            or ""
        )
        item["base_unit_id_str"] = str(
            item.get("base_unit_id_str")
            or item.get("base_unit_id")
            or ""
        )
        item["accounting_mapping_id_str"] = str(
            item.get("accounting_mapping_id") or ""
        )
        item.setdefault("received_quantity", "0.0000")
        item.setdefault("accepted_quantity", "0.0000")
        item.setdefault("rejected_quantity", "0.0000")
        item.setdefault("damaged_quantity", "0.0000")
        item.setdefault(
            "pending_quantity",
            item.get("pending_quantity")
            or item.get("quantity")
            or "0.0000",
        )
        item.setdefault("invoiced_quantity", "0.0000")
        item.setdefault(
            "invoice_pending_quantity",
            item.get("accepted_quantity") or "0.0000",
        )
        item.pop("source_product_id", None)
        item.pop("base_unit_id", None)
        item.pop("accounting_mapping_id", None)
        serialized_items.append(item)

    pending_quantity_total = sum(
        (Decimal(str(item.get("pending_quantity") or 0)) for item in serialized_items),
        Decimal("0"),
    )
    invoiced_quantity_total = sum(
        (Decimal(str(item.get("invoiced_quantity") or 0)) for item in serialized_items),
        Decimal("0"),
    )
    invoice_pending_quantity_total = sum(
        (Decimal(str(item.get("invoice_pending_quantity") or 0)) for item in serialized_items),
        Decimal("0"),
    )

    return {
        "id": str(order.get("_id") or ""),
        "document_uid": order.get("document_uid") or "",
        "po_number": order.get("po_number") or "",
        "accounting_entity_id": str(order.get("accounting_entity_id") or ""),
        "supplier_ledger_id": str(order.get("supplier_ledger_id") or ""),
        "supplier_code": order.get("supplier_code") or "",
        "supplier_name": order.get("supplier_name") or "",
        "supplier_gstin": order.get("supplier_gstin") or "",
        "supplier_gst_registration_status": (
            order.get("supplier_gst_registration_status")
            or (order.get("supplier_snapshot") or {}).get(
                "gst_registration_status"
            )
            or ""
        ),
        "supplier_gst_registration_status_label": (
            order.get("supplier_gst_registration_status_label")
            or (order.get("supplier_snapshot") or {}).get(
                "gst_registration_status_label"
            )
            or ""
        ),
        "supplier_state_code": order.get("supplier_state_code") or "",
        "supplier_state_name": order.get("supplier_state_name") or "",
        "order_date": date_string(order.get("order_date")),
        "expected_delivery_date": date_string(order.get("expected_delivery_date")),
        "supplier_reference": order.get("supplier_reference") or "",
        "payment_terms": order.get("payment_terms") or "",
        "delivery_address": order.get("delivery_address") or "",
        "remarks": order.get("remarks") or "",
        "items": serialized_items,
        "item_count": int(order.get("item_count") or len(serialized_items)),
        "subtotal": order.get("subtotal") or "0.00",
        "discount_total": order.get("discount_total") or "0.00",
        "taxable_total": order.get("taxable_total") or "0.00",
        "cgst_total": order.get("cgst_total") or "0.00",
        "sgst_total": order.get("sgst_total") or "0.00",
        "igst_total": order.get("igst_total") or "0.00",
        "tax_total": order.get("tax_total") or "0.00",
        "freight_amount": order.get("freight_amount") or "0.00",
        "other_charges": order.get("other_charges") or "0.00",
        "grand_total": order.get("grand_total") or "0.00",
        "currency": order.get("currency") or "INR",
        "status": order.get("status") or STATUS_DRAFT,
        "status_display": STATUS_LABELS.get(order.get("status"), str(order.get("status") or "").title()),
        "version": int(order.get("version") or 1),
        "created_by": str(order.get("created_by") or ""),
        "created_by_name": order.get("created_by_name") or "",
        "created_at": order.get("created_at"),
        "updated_by_name": order.get("updated_by_name") or "",
        "updated_at": order.get("updated_at"),
        "submitted_by_name": order.get("submitted_by_name") or "",
        "submitted_at": order.get("submitted_at"),
        "approved_by_name": order.get("approved_by_name") or "",
        "approved_at": order.get("approved_at"),
        "return_reason": order.get("return_reason") or "",
        "cancel_reason": order.get("cancel_reason") or "",
        "receipt_status": order.get("receipt_status") or "not_started",
        "stock_posted": order.get("stock_posted") is True,
        "voucher_posted": order.get("voucher_posted") is True,
        "payable_posted": order.get("payable_posted") is True,
        "reverse_charge_review_required": (
            order.get("reverse_charge_review_required") is True
            or any(
                item.get("reverse_charge_review_required") is True
                for item in serialized_items
            )
        ),
        "non_regular_supplier_tax_lines": int(
            order.get("non_regular_supplier_tax_lines")
            or sum(
                1
                for item in serialized_items
                if item.get("gst_treatment_code") in {
                    "UNREGISTERED_SUPPLIER_NO_GST",
                    "COMPOSITION_SUPPLIER_NO_ITC",
                    "EXEMPT_SUPPLIER_NO_GST",
                }
            )
        ),
        "grn_count": int(order.get("grn_count") or 0),
        "supplier_invoice_count": int(
            order.get("supplier_invoice_count") or 0
        ),
        "received_quantity_total": order.get(
            "received_quantity_total"
        ) or "0.0000",
        "accepted_quantity_total": order.get(
            "accepted_quantity_total"
        ) or "0.0000",
        "rejected_quantity_total": order.get(
            "rejected_quantity_total"
        ) or "0.0000",
        "damaged_quantity_total": order.get(
            "damaged_quantity_total"
        ) or "0.0000",
        "pending_quantity_total": _quantity_string(
            pending_quantity_total
        ),
        "invoiced_quantity_total": _quantity_string(
            invoiced_quantity_total
        ),
        "invoice_pending_quantity_total": _quantity_string(
            invoice_pending_quantity_total
        ),
        "invoice_match_status": order.get("invoice_match_status")
        or "not_recorded",
        "invoice_match_status_display": (
            str(order.get("invoice_match_status") or "not_recorded")
            .replace("_", " ")
            .title()
        ),
        "change_history": order.get("change_history") or [],
    }


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def get_purchase_order_form_catalog(accounting_entity_id, actor_user_id):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_purchase_order_indexes()

    supplier_rows = list(
        mongo.db[LEDGER_COLLECTION].find(
            {
                "accounting_entity_id": entity["_id"],
                "is_party_ledger": True,
                "party_role": PARTY_ROLE_SUPPLIER,
                "status": "active",
                "is_active": True,
                "is_deleted": False,
            }
        ).sort("name", ASCENDING)
    )

    mapping_rows = list(
        mongo.db.accounting_product_mappings.find(
            {
                "accounting_entity_id": entity["_id"],
                "status": "active",
                "is_active": True,
                "is_accounting_eligible": True,
                "purchase_enabled": {"$ne": False},
                "is_deleted": {"$ne": True},
            }
        ).sort("source_product_name", ASCENDING)
    )

    products = []
    for mapping in mapping_rows:
        product_id = mapping.get("source_product_id")
        product = mongo.db.products.find_one(
            {
                "_id": product_id,
                "is_deleted": {"$ne": True},
                "is_active": {"$ne": False},
                "status": {"$ne": "inactive"},
            }
        )
        if not product:
            continue

        products.append(
            {
                "id": str(product["_id"]),
                "product_code": product.get("product_code") or "",
                "name": product.get("name") or mapping.get("source_product_name") or "Product",
                "category": product.get("category") or "",
                "product_role": product.get("product_role") or product.get("type") or "",
                "hsn_code": mapping.get("hsn_code") or "",
                "gst_rate_code": mapping.get("gst_rate_code") or "",
                "taxability_code": mapping.get("taxability_code") or "",
                "unit_code": mapping.get("base_unit_code") or mapping.get("base_unit_name") or "",
                "unit_name": mapping.get("base_unit_name") or mapping.get("base_unit_code") or "",
            }
        )

    return {
        "entity": {
            "id": str(entity["_id"]),
            "name": entity.get("name") or entity.get("legal_name") or "AVPL",
            "state_name": entity.get("state_name") or entity.get("state") or "",
            "state_code": entity.get("state_code") or "",
            "address": entity.get("address") or entity.get("registered_address") or "",
        },
        "suppliers": [serialize_party_ledger(row) for row in supplier_rows],
        "products": products,
        "status_labels": dict(STATUS_LABELS),
    }


def get_purchase_order_overview(accounting_entity_id, actor_user_id, status=None, query_text=""):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_purchase_order_indexes()

    query = {"accounting_entity_id": entity["_id"]}
    if status and status in STATUS_LABELS:
        query["status"] = status

    rows = list(
        mongo.db[PURCHASE_ORDER_COLLECTION].find(query).sort(
            [("order_date", DESCENDING), ("created_at", DESCENDING)]
        )
    )

    text = str(query_text or "").strip().lower()
    if text:
        rows = [
            row
            for row in rows
            if text in str(row.get("po_number") or "").lower()
            or text in str(row.get("supplier_name") or "").lower()
            or text in str(row.get("supplier_code") or "").lower()
            or text in str(row.get("supplier_reference") or "").lower()
            or any(text in str(item.get("product_name") or "").lower() for item in row.get("items") or [])
        ]

    serialized = [serialize_purchase_order(row) for row in rows]
    counts = {key: 0 for key in STATUS_LABELS}
    for row in mongo.db[PURCHASE_ORDER_COLLECTION].find(
        {"accounting_entity_id": entity["_id"]}, {"status": 1}
    ):
        key = row.get("status") or STATUS_DRAFT
        counts[key] = counts.get(key, 0) + 1

    return {
        "rows": serialized,
        "counts": counts,
        "status_labels": dict(STATUS_LABELS),
        "selected_status": status or "",
        "query": query_text or "",
        "entity_id": str(entity["_id"]),
    }


def get_purchase_order(accounting_entity_id, actor_user_id, order_id):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    order = _get_order_document(order_id)
    if str(order.get("accounting_entity_id")) != str(entity["_id"]):
        raise PermissionError("This purchase order belongs to another Accounting entity.")
    return serialize_purchase_order(order)


# ---------------------------------------------------------------------------
# Create, edit and workflow
# ---------------------------------------------------------------------------


def create_purchase_order(accounting_entity_id, actor_user_id, raw_payload, auto_approve=False):
    actor = _get_actor(actor_user_id)
    entity = _active_avpl_entity(accounting_entity_id)
    ensure_purchase_order_indexes()

    supplier = get_active_party_ledger_for_posting(
        entity["_id"],
        raw_payload.get("supplier_ledger_id"),
        party_role=PARTY_ROLE_SUPPLIER,
    )
    payload = _build_order_payload(entity, supplier, raw_payload)

    can_auto_approve = bool(auto_approve) and (
        actor.get("resolved_role") in CHECKER_ROLES
        or workflow_is_streamlined("avpl.purchase_order")
    )
    status = STATUS_APPROVED if can_auto_approve else STATUS_DRAFT
    timestamp = now_utc()
    po_number = _next_po_number(payload["order_date"])

    document = {
        **payload,
        "document_uid": uuid4().hex,
        "po_number": po_number,
        "status": status,
        "version": 1,
        "receipt_status": "not_started",
        "grn_count": 0,
        "supplier_invoice_count": 0,
        "invoice_match_status": "not_recorded",
        "received_quantity_total": "0.0000",
        "accepted_quantity_total": "0.0000",
        "rejected_quantity_total": "0.0000",
        "damaged_quantity_total": "0.0000",
        "stock_posted": False,
        "voucher_posted": False,
        "payable_posted": False,
        "created_by": actor["_id"],
        "created_by_str": str(actor["_id"]),
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "approved_by": actor["_id"] if can_auto_approve else None,
        "approved_by_str": str(actor["_id"]) if can_auto_approve else "",
        "approved_by_name": actor.get("resolved_name") if can_auto_approve else "",
        "approved_at": timestamp if can_auto_approve else None,
        "change_history": [
            _history_event(
                "create_and_approve_purchase_order" if can_auto_approve else "create_purchase_order_draft",
                actor,
                previous_status=None,
                new_status=status,
                changed_fields=sorted(payload.keys()),
            )
        ],
        "audit_sync_required": False,
    }

    try:
        result = mongo.db[PURCHASE_ORDER_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
    except DuplicateKeyError as exc:
        raise RuntimeError("A purchase order with the same number already exists. Retry the operation.") from exc

    _record_audit(
        document,
        actor,
        "create_and_approve_purchase_order" if can_auto_approve else "create_purchase_order_draft",
        previous_status=None,
        reason="Purchase order created.",
        changed_fields=sorted(payload.keys()),
    )
    return {
        "order": serialize_purchase_order(document),
        "message": (
            "Purchase order created and approved."
            if can_auto_approve
            else "Purchase-order draft created."
        ),
    }


def update_purchase_order(order_id, actor_user_id, raw_payload, expected_version, auto_approve=False):
    actor = _get_actor(actor_user_id)
    order = _get_order_document(order_id)
    entity = _active_avpl_entity(order.get("accounting_entity_id"))

    if order.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only draft or returned purchase orders can be edited.")

    if (
        actor.get("resolved_role") == "accounts"
        and str(order.get("created_by")) != str(actor["_id"])
    ):
        raise PermissionError("Accounts users can edit only purchase orders they created.")

    version = _expected_version(expected_version)
    if version != int(order.get("version") or 1):
        raise RuntimeError("This purchase order changed. Refresh before saving.")

    supplier = get_active_party_ledger_for_posting(
        entity["_id"],
        raw_payload.get("supplier_ledger_id"),
        party_role=PARTY_ROLE_SUPPLIER,
    )
    payload = _build_order_payload(entity, supplier, raw_payload)

    can_auto_approve = bool(auto_approve) and (
        actor.get("resolved_role") in CHECKER_ROLES
        or workflow_is_streamlined("avpl.purchase_order")
    )
    next_status = STATUS_APPROVED if can_auto_approve else STATUS_DRAFT
    timestamp = now_utc()
    updates = {
        **payload,
        "status": next_status,
        "version": version + 1,
        "updated_by": actor["_id"],
        "updated_by_str": str(actor["_id"]),
        "updated_by_name": actor.get("resolved_name") or "",
        "updated_at": timestamp,
        "return_reason": None,
    }
    if can_auto_approve:
        updates.update(
            {
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_by_name": actor.get("resolved_name") or "",
                "approved_at": timestamp,
            }
        )

    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {
            "_id": order["_id"],
            "version": version,
            "status": {"$in": list(EDITABLE_STATUSES)},
        },
        {
            "$set": updates,
            "$push": {
                "change_history": _history_event(
                    "update_and_approve_purchase_order" if can_auto_approve else "update_purchase_order_draft",
                    actor,
                    previous_status=order.get("status"),
                    new_status=next_status,
                    changed_fields=sorted(payload.keys()),
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh before saving.")

    updated = _get_order_document(order_id)
    _record_audit(
        updated,
        actor,
        "update_and_approve_purchase_order" if can_auto_approve else "update_purchase_order_draft",
        previous_status=order.get("status"),
        reason="Purchase order updated.",
        changed_fields=sorted(payload.keys()),
    )
    return {
        "order": serialize_purchase_order(updated),
        "message": (
            "Purchase order updated and approved."
            if can_auto_approve
            else "Purchase-order draft updated."
        ),
    }


def submit_purchase_order(order_id, actor_user_id, expected_version):
    actor = _get_actor(actor_user_id)
    order = _get_order_document(order_id)
    if order.get("status") not in EDITABLE_STATUSES:
        raise ValueError("Only a draft or returned purchase order can be submitted.")
    if actor.get("resolved_role") == "accounts" and str(order.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Accounts users can submit only purchase orders they created.")

    version = _expected_version(expected_version)
    timestamp = now_utc()
    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "version": version, "status": {"$in": list(EDITABLE_STATUSES)}},
        {
            "$set": {
                "status": STATUS_PENDING_APPROVAL,
                "version": version + 1,
                "submitted_by": actor["_id"],
                "submitted_by_str": str(actor["_id"]),
                "submitted_by_name": actor.get("resolved_name") or "",
                "submitted_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
                "return_reason": None,
            },
            "$push": {
                "change_history": _history_event(
                    "submit_purchase_order",
                    actor,
                    previous_status=order.get("status"),
                    new_status=STATUS_PENDING_APPROVAL,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh and try again.")

    updated = _get_order_document(order_id)
    _record_audit(updated, actor, "submit_purchase_order", order.get("status"), "Submitted for approval.")
    return {"order": serialize_purchase_order(updated), "message": "Purchase order submitted for approval."}


def approve_purchase_order(order_id, actor_user_id, expected_version, approval_note=""):
    actor = _get_actor(actor_user_id)
    if (
        actor.get("resolved_role") not in CHECKER_ROLES
        and not workflow_is_streamlined("avpl.purchase_order")
    ):
        raise PermissionError("You are not authorized to approve purchase orders.")

    order = _get_order_document(order_id)
    if order.get("status") not in APPROVABLE_STATUSES:
        raise ValueError("Only a pending purchase order can be approved.")

    version = _expected_version(expected_version)
    note = _clean_multiline(approval_note, "Approval note", 1000)
    timestamp = now_utc()
    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "version": version, "status": STATUS_PENDING_APPROVAL},
        {
            "$set": {
                "status": STATUS_APPROVED,
                "version": version + 1,
                "approved_by": actor["_id"],
                "approved_by_str": str(actor["_id"]),
                "approved_by_name": actor.get("resolved_name") or "",
                "approved_at": timestamp,
                "approval_note": note,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
                "return_reason": None,
            },
            "$push": {
                "change_history": _history_event(
                    "approve_purchase_order",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_APPROVED,
                    reason=note,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh and try again.")

    updated = _get_order_document(order_id)
    _record_audit(updated, actor, "approve_purchase_order", STATUS_PENDING_APPROVAL, note or "Approved.")
    return {"order": serialize_purchase_order(updated), "message": "Purchase order approved."}


def return_purchase_order(order_id, actor_user_id, expected_version, reason):
    actor = _get_actor(actor_user_id)
    if actor.get("resolved_role") not in CHECKER_ROLES:
        raise PermissionError("Only AVPL Admin can return purchase orders.")

    order = _get_order_document(order_id)
    if order.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending purchase order can be returned.")

    version = _expected_version(expected_version)
    reason_text = _clean_text(reason, "Return reason", 1000, required=True)
    timestamp = now_utc()
    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "version": version, "status": STATUS_PENDING_APPROVAL},
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
                    "return_purchase_order",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_RETURNED,
                    reason=reason_text,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh and try again.")

    updated = _get_order_document(order_id)
    _record_audit(updated, actor, "return_purchase_order", STATUS_PENDING_APPROVAL, reason_text)
    return {"order": serialize_purchase_order(updated), "message": "Purchase order returned for correction."}


def withdraw_purchase_order(order_id, actor_user_id, expected_version, reason=""):
    actor = _get_actor(actor_user_id)
    order = _get_order_document(order_id)
    if order.get("status") != STATUS_PENDING_APPROVAL:
        raise ValueError("Only a pending purchase order can be withdrawn.")
    if str(order.get("created_by")) != str(actor["_id"]) and actor.get("resolved_role") not in CHECKER_ROLES:
        raise PermissionError("Only the original maker can withdraw this purchase order.")

    version = _expected_version(expected_version)
    reason_text = _clean_multiline(reason, "Withdrawal reason", 1000)
    timestamp = now_utc()
    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "version": version, "status": STATUS_PENDING_APPROVAL},
        {
            "$set": {
                "status": STATUS_DRAFT,
                "version": version + 1,
                "withdraw_reason": reason_text,
                "withdrawn_by": actor["_id"],
                "withdrawn_by_name": actor.get("resolved_name") or "",
                "withdrawn_at": timestamp,
                "updated_by": actor["_id"],
                "updated_by_name": actor.get("resolved_name") or "",
                "updated_at": timestamp,
            },
            "$push": {
                "change_history": _history_event(
                    "withdraw_purchase_order",
                    actor,
                    previous_status=STATUS_PENDING_APPROVAL,
                    new_status=STATUS_DRAFT,
                    reason=reason_text,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh and try again.")

    updated = _get_order_document(order_id)
    _record_audit(updated, actor, "withdraw_purchase_order", STATUS_PENDING_APPROVAL, reason_text)
    return {"order": serialize_purchase_order(updated), "message": "Purchase order withdrawn to Draft."}


def cancel_purchase_order(order_id, actor_user_id, expected_version, reason):
    actor = _get_actor(actor_user_id)
    order = _get_order_document(order_id)

    if order.get("status") in RECEIPT_STATUSES:
        raise ValueError("A received purchase order cannot be cancelled from this stage.")
    if order.get("status") == STATUS_CANCELLED:
        raise ValueError("This purchase order is already cancelled.")
    if order.get("stock_posted") or order.get("voucher_posted") or order.get("payable_posted"):
        raise ValueError("A posted purchase order cannot be cancelled.")
    if actor.get("resolved_role") == "accounts" and str(order.get("created_by")) != str(actor["_id"]):
        raise PermissionError("Accounts users can cancel only purchase orders they created.")

    version = _expected_version(expected_version)
    reason_text = _clean_text(reason, "Cancellation reason", 1000, required=True)
    previous_status = order.get("status")
    timestamp = now_utc()
    result = mongo.db[PURCHASE_ORDER_COLLECTION].update_one(
        {"_id": order["_id"], "version": version, "status": previous_status},
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
                    "cancel_purchase_order",
                    actor,
                    previous_status=previous_status,
                    new_status=STATUS_CANCELLED,
                    reason=reason_text,
                )
            },
        },
    )
    if result.matched_count != 1:
        raise RuntimeError("This purchase order changed. Refresh and try again.")

    updated = _get_order_document(order_id)
    _record_audit(updated, actor, "cancel_purchase_order", previous_status, reason_text)
    return {"order": serialize_purchase_order(updated), "message": "Purchase order cancelled."}
