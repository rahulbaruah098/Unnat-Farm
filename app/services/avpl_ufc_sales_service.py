from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.extensions import mongo
from app.services.accounting_configuration_service import INDIA_STATE_CODES
from app.services.ufc_profile_service import is_valid_gstin, parse_bool as parse_profile_bool
from app.services.location_service import STATE_CODES as LOCATION_STATE_CODES
from app.services.accounting_financial_year_service import get_open_financial_year_for_date
from app.services.accounting_number_series_service import (
    commit_reserved_number,
    reserve_document_number,
)
from app.services.accounting_product_mapping_service import (
    get_product_accounting_mapping_for_posting,
)
from app.utils.helpers import now_utc


ORDER_COLLECTION = "avpl_ufc_orders"
SALE_COLLECTION = "avpl_ufc_sales"
INVOICE_COLLECTION = "avpl_sales_invoices"
RECEIVABLE_COLLECTION = "avpl_receivables"
UFC_PAYABLE_COLLECTION = "ufc_payables"
UFC_PURCHASE_COLLECTION = "ufc_purchase_entries"
GOODS_RECEIPT_COLLECTION = "avpl_goods_receipts"

AVPL_VIEW_ROLES = {"super_admin", "avpl_admin", "accounts"}
AVPL_ISSUE_ROLES = {"super_admin", "avpl_admin"}
INVOICE_NUMBER_PERMISSION = "accounting.voucher.post"
MONEY_QUANTUM = Decimal("0.01")
QTY_QUANTUM = Decimal("0.0001")

SALE_STATUS_LABELS = {
    "invoiced": "Invoiced",
    "received": "UFC Received",
    "cancelled": "Cancelled",
}

PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
}


def _to_object_id(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


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


def _qty(value):
    number = _decimal(value).quantize(QTY_QUANTUM)
    return f"{number:f}".rstrip("0").rstrip(".") or "0"


def _clean(value, maximum=1000):
    return " ".join(str(value or "").split())[:maximum]


def _date_iso(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return date.today().isoformat()


def _get_actor(user_id, allowed_roles=None):
    oid = _to_object_id(user_id)
    if not oid:
        raise ValueError("Invalid authenticated user.")
    actor = mongo.db.users.find_one({"_id": oid})
    if not actor:
        raise ValueError("Authenticated user was not found.")
    role = str(actor.get("role") or "").strip().lower()
    if allowed_roles and role not in allowed_roles:
        raise PermissionError("You are not authorized for this AVPL sales action.")
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or str(actor.get("status") or "").lower() == "inactive"
    ):
        raise PermissionError("Inactive users cannot perform this action.")
    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )
    return actor


def _active_avpl_entity():
    entity = mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    })
    if not entity:
        raise RuntimeError("The active AVPL Accounting entity is unavailable.")
    return entity


def _ensure_indexes():
    definitions = [
        (SALE_COLLECTION, [("sale_number", ASCENDING)], {"unique": True, "name": "avpl_ufc_sale_number_unique"}),
        (SALE_COLLECTION, [("avpl_ufc_order_id", ASCENDING)], {"unique": True, "name": "avpl_ufc_sale_order_unique"}),
        (SALE_COLLECTION, [("sale_date", DESCENDING), ("centre_uid", ASCENDING)], {"name": "avpl_ufc_sales_date_centre_idx"}),
        (INVOICE_COLLECTION, [("invoice_number", ASCENDING)], {"unique": True, "name": "avpl_sales_invoice_number_unique", "partialFilterExpression": {"invoice_number": {"$exists": True, "$gt": ""}}}),
        (INVOICE_COLLECTION, [("avpl_ufc_order_id", ASCENDING)], {"unique": True, "name": "avpl_sales_invoice_order_unique"}),
        (RECEIVABLE_COLLECTION, [("avpl_ufc_order_id", ASCENDING)], {"unique": True, "name": "avpl_receivable_order_unique"}),
        (RECEIVABLE_COLLECTION, [("payment_status", ASCENDING), ("due_date", ASCENDING)], {"name": "avpl_receivable_payment_due_idx"}),
        (UFC_PAYABLE_COLLECTION, [("avpl_ufc_order_id", ASCENDING)], {"unique": True, "name": "ufc_payable_order_unique"}),
        (UFC_PAYABLE_COLLECTION, [("centre_uid", ASCENDING), ("payment_status", ASCENDING)], {"name": "ufc_payable_centre_payment_idx"}),
    ]
    for name, keys, options in definitions:
        try:
            mongo.db[name].create_index(keys, **options)
        except Exception:
            # Local/restricted Mongo deployments must not make the operational flow unavailable.
            pass


def _next_sale_number():
    year = date.today().year
    counter = mongo.db.system_counters.find_one_and_update(
        {"_id": f"avpl_ufc_sale:{year}"},
        {"$inc": {"sequence": 1}, "$setOnInsert": {"created_at": now_utc()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    sequence = int((counter or {}).get("sequence") or 1)
    return f"AVPL-SALE-{year}-{sequence:06d}"


def _resolve_gst_state(state_name="", state_code="", gstin=""):
    """Return a normalized Indian GST state name/code pair.

    UFC operational masters historically store either the full state name
    ("Assam"), the two-letter MIS code ("AS") or, for GST-aware records, the
    numeric GST code ("18").  Sales tax determination must compare numeric GST
    codes only; comparing "AS" with AVPL's "18" would incorrectly create IGST.
    """
    name = str(state_name or "").strip()
    code = str(state_code or "").strip().upper()
    gstin_text = str(gstin or "").strip().upper()

    if len(gstin_text) >= 2 and gstin_text[:2].isdigit():
        numeric = gstin_text[:2]
        if not name:
            name = next((n for n, c in INDIA_STATE_CODES.items() if c == numeric), "")
        return name, numeric

    if name in INDIA_STATE_CODES:
        return name, INDIA_STATE_CODES[name]

    # Support historic two-letter location codes such as AS, AR, ML, etc.
    if code and not code.isdigit():
        matched_name = next((n for n, c in LOCATION_STATE_CODES.items() if str(c).upper() == code), "")
        if matched_name:
            return matched_name, INDIA_STATE_CODES.get(matched_name, "")

    if code.isdigit():
        numeric = code.zfill(2)
        if not name:
            name = next((n for n, c in INDIA_STATE_CODES.items() if c == numeric), "")
        return name, numeric

    return name, ""


def _buyer_snapshot(order):
    centre_uid = str(order.get("centre_uid") or "").strip()
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or {}
    linked_user_id = centre.get("linked_user_id")
    linked_user = None
    if linked_user_id:
        linked_user = mongo.db.users.find_one({"_id": _to_object_id(linked_user_id)}) or mongo.db.users.find_one({"_id": linked_user_id})
    linked_user = linked_user or {}

    raw_gstin = str(centre.get("gst_number") or centre.get("gstin") or "").strip().upper()
    gstin_valid = is_valid_gstin(raw_gstin)
    gstin = raw_gstin if gstin_valid else ""
    explicit_registered = parse_profile_bool(
        centre.get("gst_registered") if centre.get("gst_registered") is not None else centre.get("is_gst_registered"),
        default=None,
    )
    is_registered = bool(gstin_valid and explicit_registered is not False)

    raw_state_name = str(centre.get("state") or linked_user.get("state") or "").strip()
    state_name, state_code = _resolve_gst_state(
        raw_state_name,
        centre.get("state_code") or linked_user.get("state_code") or "",
        gstin,
    )

    return {
        "centre_uid": centre_uid,
        "legal_name": (
            centre.get("name_of_enterprise")
            or centre.get("enterprise_name")
            or centre.get("centre_name")
            or order.get("centre_name")
            or centre_uid
        ),
        "owner_name": centre.get("name_of_owner") or centre.get("name") or "",
        "gstin": gstin,
        "pan": str(centre.get("pan_number") or centre.get("pan") or "").strip().upper(),
        "gst_registration_status": "registered_regular" if is_registered else "unregistered",
        "gst_configuration_warning": (
            "Invalid Centre GSTIN ignored for tax-state resolution. Correct the UFC Centre profile."
            if raw_gstin and not gstin_valid else ""
        ),
        "state": state_name,
        "state_code": state_code,
        "district": centre.get("district") or linked_user.get("district") or "",
        "block": centre.get("block") or linked_user.get("block") or "",
        "village": centre.get("village") or linked_user.get("village") or "",
        "address": centre.get("address") or linked_user.get("address") or "",
        "phone": centre.get("contact_no") or linked_user.get("phone") or "",
        "email": centre.get("email") or linked_user.get("email") or "",
    }


def _seller_snapshot(entity):
    return {
        "entity_code": entity.get("entity_code") or "AVPL",
        "legal_name": entity.get("legal_name") or entity.get("display_name") or "AVPL",
        "display_name": entity.get("display_name") or entity.get("legal_name") or "AVPL",
        "trade_name": entity.get("trade_name") or "",
        "gstin": entity.get("gstin") or "",
        "pan": entity.get("pan") or "",
        "gst_registration_status": entity.get("gst_registration_status") or "",
        "address_line_1": entity.get("address_line_1") or "",
        "address_line_2": entity.get("address_line_2") or "",
        "city": entity.get("city") or "",
        "district": entity.get("district") or "",
        "state": entity.get("state") or "",
        "state_code": str(entity.get("state_code") or ""),
        "postal_code": entity.get("postal_code") or "",
    }


def _purchase_wac(entity_id, product_id):
    quantity_total = Decimal("0")
    cost_total = Decimal("0")
    receipts = mongo.db[GOODS_RECEIPT_COLLECTION].find({
        "accounting_entity_id": entity_id,
        "status": "posted",
        "stock_posted": True,
        "items.source_product_id": product_id,
    }, {"items": 1, "purchase_order_snapshot.items": 1})
    for receipt in receipts:
        po_items = {
            int(item.get("line_no") or index): item
            for index, item in enumerate((receipt.get("purchase_order_snapshot") or {}).get("items") or [], start=1)
        }
        for index, item in enumerate(receipt.get("items") or [], start=1):
            if str(item.get("source_product_id") or "") != str(product_id):
                continue
            quantity = _decimal(item.get("accepted_quantity"))
            if quantity <= 0:
                continue
            po_item = po_items.get(int(item.get("po_line_no") or index)) or {}
            rate = _decimal(po_item.get("rate"))
            discount = _decimal(po_item.get("discount_percent"))
            net_rate = rate * (Decimal("1") - discount / Decimal("100"))
            quantity_total += quantity
            cost_total += quantity * net_rate
    return (cost_total / quantity_total) if quantity_total > 0 else Decimal("0")


def _financial_snapshot(order, entity, buyer):
    transaction_date = _date_iso(order.get("dispatched_at") or date.today())
    mapping = get_product_accounting_mapping_for_posting(
        entity["_id"],
        order.get("source_product_id"),
        transaction_date=transaction_date,
        operation="sales",
    )

    quantity = _decimal(order.get("dispatched_quantity") or order.get("approved_quantity"))
    unit_price = _decimal(order.get("unit_price"))
    taxable_value = (quantity * unit_price).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    taxability = str((mapping.get("hsn") or {}).get("taxability_code") or "").upper()
    effective_rate = mapping.get("effective_gst_rate") or {}
    gst_rate = _decimal(effective_rate.get("total_rate")) if taxability == "TAXABLE" else Decimal("0")

    seller_state_name, seller_state_code = _resolve_gst_state(
        entity.get("state") or "",
        entity.get("state_code") or "",
        entity.get("gstin") or "",
    )
    buyer_state_name, buyer_state_code = _resolve_gst_state(
        buyer.get("state") or "",
        buyer.get("state_code") or "",
        buyer.get("gstin") or "",
    )
    if gst_rate > 0 and not seller_state_code:
        raise ValueError("AVPL Accounting entity State Code is required before issuing a GST sales invoice.")
    if gst_rate > 0 and not buyer_state_code:
        raise ValueError("The UFC Centre State is required before issuing a GST sales invoice.")

    tax_amount = (taxable_value * gst_rate / Decimal("100")).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    cgst = sgst = igst = Decimal("0")
    supply_type = "non_taxable"
    if tax_amount > 0:
        if seller_state_code == buyer_state_code:
            cgst = (tax_amount / Decimal("2")).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
            sgst = tax_amount - cgst
            supply_type = "intra_state"
        else:
            igst = tax_amount
            supply_type = "inter_state"

    grand_total = taxable_value + cgst + sgst + igst
    wac = _purchase_wac(entity["_id"], order.get("source_product_id"))
    estimated_cogs = (quantity * wac).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    gross_margin = taxable_value - estimated_cogs
    gross_margin_percent = (
        gross_margin * Decimal("100") / taxable_value
        if taxable_value > 0
        else Decimal("0")
    )

    ledgers = mapping.get("ledgers") or {}
    return {
        "transaction_date": transaction_date,
        "quantity": quantity,
        "unit_price": unit_price,
        "taxable_value": taxable_value,
        "taxability_code": taxability or "NON_GST",
        "hsn_code": (mapping.get("hsn") or {}).get("hsn_code") or "",
        "gst_rate": gst_rate,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "grand_total": grand_total,
        "supply_type": supply_type,
        "place_of_supply_state": buyer_state_name or seller_state_name or buyer.get("state") or entity.get("state") or "",
        "place_of_supply_state_code": buyer_state_code or seller_state_code,
        "base_unit_code": (mapping.get("base_unit") or {}).get("unit_code") or order.get("unit_code") or "Unit",
        "sales_ledger_id": _to_object_id((ledgers.get("sales") or {}).get("_id") or (ledgers.get("sales") or {}).get("id")),
        "sales_ledger_name": (ledgers.get("sales") or {}).get("name") or "Sales",
        "inventory_ledger_id": _to_object_id((ledgers.get("inventory") or {}).get("_id") or (ledgers.get("inventory") or {}).get("id")),
        "inventory_ledger_name": (ledgers.get("inventory") or {}).get("name") or "Stock-in-Hand",
        "estimated_unit_cost": wac,
        "estimated_cogs": estimated_cogs,
        "gross_margin": gross_margin,
        "gross_margin_percent": gross_margin_percent,
    }


def _serialize_invoice(row):
    if not row:
        return None
    result = dict(row)
    result["id"] = str(row.get("_id") or "")
    for field in (
        "subtotal", "taxable_value", "cgst_amount", "sgst_amount", "igst_amount",
        "gst_amount", "grand_total", "amount_paid", "outstanding_amount",
        "estimated_unit_cost", "estimated_cogs", "gross_margin_amount",
    ):
        result[f"{field}_display"] = _money(row.get(field))
    result["quantity_display"] = _qty(row.get("quantity"))
    result["unit_price_display"] = _money(row.get("unit_price"))
    result["gst_rate_display"] = _qty(row.get("gst_rate"))
    result["payment_status_label"] = PAYMENT_STATUS_LABELS.get(row.get("payment_status"), str(row.get("payment_status") or "").replace("_", " ").title())
    return result


def _serialize_sale(row):
    if not row:
        return None
    result = dict(row)
    result["id"] = str(row.get("_id") or "")
    result["quantity_display"] = _qty(row.get("quantity"))
    result["unit_price_display"] = _money(row.get("unit_price"))
    result["taxable_value_display"] = _money(row.get("taxable_value"))
    result["grand_total_display"] = _money(row.get("grand_total"))
    result["amount_paid_display"] = _money(row.get("amount_paid") if row.get("amount_paid") is not None else row.get("paid_amount"))
    result["outstanding_amount_display"] = _money(row.get("outstanding_amount"))
    result["estimated_cogs_display"] = _money(row.get("estimated_cogs"))
    result["gross_margin_display"] = _money(row.get("gross_margin_amount"))
    result["gross_margin_percent_display"] = _qty(row.get("gross_margin_percent"))
    result["status_label"] = SALE_STATUS_LABELS.get(row.get("status"), str(row.get("status") or "").replace("_", " ").title())
    result["payment_status_label"] = PAYMENT_STATUS_LABELS.get(row.get("payment_status"), str(row.get("payment_status") or "").replace("_", " ").title())
    return result


def _upsert_sale(order, actor, financial):
    timestamp = now_utc()
    existing = mongo.db[SALE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    if existing:
        return existing
    document = {
        "document_uid": uuid4().hex,
        "sale_number": _next_sale_number(),
        "accounting_entity_id": order.get("accounting_entity_id"),
        "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
        "avpl_ufc_order_id": order["_id"],
        "avpl_ufc_order_id_str": str(order["_id"]),
        "avpl_order_number": order.get("order_number") or "",
        "centre_uid": order.get("centre_uid") or "",
        "centre_name": order.get("centre_name") or order.get("centre_uid") or "UFC",
        "source_product_id": order.get("source_product_id"),
        "source_product_id_str": str(order.get("source_product_id") or ""),
        "product_name": order.get("product_name") or "Product",
        "product_code": order.get("product_code") or "",
        "category": order.get("category") or "",
        "product_role": order.get("product_role") or "",
        "quantity": float(financial["quantity"]),
        "unit_code": financial["base_unit_code"],
        "unit_price": float(financial["unit_price"]),
        "taxable_value": float(financial["taxable_value"]),
        "gst_rate": float(financial["gst_rate"]),
        "cgst_amount": float(financial["cgst"]),
        "sgst_amount": float(financial["sgst"]),
        "igst_amount": float(financial["igst"]),
        "grand_total": float(financial["grand_total"]),
        "estimated_unit_cost": float(financial["estimated_unit_cost"]),
        "estimated_cogs": float(financial["estimated_cogs"]),
        "gross_margin_amount": float(financial["gross_margin"]),
        "gross_margin_percent": float(financial["gross_margin_percent"]),
        "sale_date": order.get("dispatched_at") or timestamp,
        "dispatch_date": order.get("dispatched_at") or timestamp,
        "status": "received" if order.get("status") == "received" else "invoiced",
        "payment_status": "unpaid",
        "amount_paid": 0.0,
        "outstanding_amount": float(financial["grand_total"]),
        "accounting_status": "ready_for_posting",
        "accounting_note": "Payment settlements create controlled Accounting handoff events; final voucher posting remains under the existing maker-checker controls. Stock already moved at physical dispatch.",
        "created_by": actor["_id"],
        "created_by_name": actor.get("resolved_name") or "",
        "created_at": timestamp,
        "updated_at": timestamp,
        "history": [{
            "action": "create_avpl_sale_from_dispatch",
            "actor_user_id": actor["_id"],
            "actor_name": actor.get("resolved_name") or "",
            "at": timestamp,
            "note": f"Automatic AVPL sale entry created from dispatched UFC order {order.get('order_number') or ''}.",
        }],
    }
    try:
        result = mongo.db[SALE_COLLECTION].insert_one(document)
        document["_id"] = result.inserted_id
        return document
    except Exception:
        existing = mongo.db[SALE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
        if existing:
            return existing
        raise


def _upsert_invoice(order, sale, actor, entity, buyer, financial, credit_period_days=0):
    timestamp = now_utc()
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
    if not invoice:
        invoice_date = financial["transaction_date"]
        due_date = (
            datetime.strptime(invoice_date, "%Y-%m-%d").date()
            + timedelta(days=max(int(credit_period_days or 0), 0))
        ).isoformat()
        seller = _seller_snapshot(entity)
        document = {
            "document_uid": uuid4().hex,
            "invoice_number": "",
            "numbering_status": "pending",
            "accounting_entity_id": entity["_id"],
            "accounting_entity_id_str": str(entity["_id"]),
            "avpl_ufc_sale_id": sale["_id"],
            "avpl_ufc_sale_id_str": str(sale["_id"]),
            "sale_number": sale.get("sale_number") or "",
            "avpl_ufc_order_id": order["_id"],
            "avpl_ufc_order_id_str": str(order["_id"]),
            "avpl_order_number": order.get("order_number") or "",
            "invoice_date": invoice_date,
            "due_date": due_date,
            "credit_period_days": max(int(credit_period_days or 0), 0),
            "payment_term": order.get("payment_term") or "credit",
            "payment_term_label": {
                "cod": "Pay on Delivery",
                "credit": "Credit / Pay Later",
                "prepaid_online": "Prepaid / Online (Coming Soon)",
            }.get(order.get("payment_term") or "credit", "Credit / Pay Later"),
            "seller": seller,
            "buyer": buyer,
            "place_of_supply_state": financial["place_of_supply_state"],
            "place_of_supply_state_code": financial["place_of_supply_state_code"],
            "supply_type": financial["supply_type"],
            "source_product_id": order.get("source_product_id"),
            "source_product_id_str": str(order.get("source_product_id") or ""),
            "product_name": order.get("product_name") or "Product",
            "product_code": order.get("product_code") or "",
            "category": order.get("category") or "",
            "product_role": order.get("product_role") or "",
            "hsn_code": financial["hsn_code"],
            "taxability_code": financial["taxability_code"],
            "quantity": float(financial["quantity"]),
            "unit_code": financial["base_unit_code"],
            "unit_price": float(financial["unit_price"]),
            "discount_amount": 0.0,
            "subtotal": float(financial["taxable_value"]),
            "taxable_value": float(financial["taxable_value"]),
            "gst_rate": float(financial["gst_rate"]),
            "cgst_amount": float(financial["cgst"]),
            "sgst_amount": float(financial["sgst"]),
            "igst_amount": float(financial["igst"]),
            "gst_amount": float(financial["cgst"] + financial["sgst"] + financial["igst"]),
            "freight_amount": 0.0,
            "other_charges": 0.0,
            "round_off": 0.0,
            "grand_total": float(financial["grand_total"]),
            "amount_paid": 0.0,
            "outstanding_amount": float(financial["grand_total"]),
            "payment_status": "unpaid",
            "sales_ledger_id": financial["sales_ledger_id"],
            "sales_ledger_name": financial["sales_ledger_name"],
            "inventory_ledger_id": financial["inventory_ledger_id"],
            "inventory_ledger_name": financial["inventory_ledger_name"],
            "estimated_unit_cost": float(financial["estimated_unit_cost"]),
            "estimated_cogs": float(financial["estimated_cogs"]),
            "gross_margin_amount": float(financial["gross_margin"]),
            "gross_margin_percent": float(financial["gross_margin_percent"]),
            "dispatch": {
                "quantity": float(financial["quantity"]),
                "transporter_name": order.get("transporter_name") or "",
                "vehicle_number": order.get("vehicle_number") or "",
                "dispatch_note": order.get("dispatch_note") or "",
                "dispatched_at": order.get("dispatched_at"),
                "allocations": order.get("dispatch_allocations") or [],
            },
            "status": "issued",
            "accounting_status": "ready_for_posting",
            "accounting_readiness": {
                "product_mapping_ready": True,
                "sales_ledger_ready": bool(financial["sales_ledger_id"]),
                "inventory_ledger_ready": bool(financial["inventory_ledger_id"]),
                "financial_year_ready": True,
                "note": "Payment settlement creates a controlled Accounting event. Final voucher posting remains subject to the existing Accounting maker-checker controls.",
            },
            "created_by": actor["_id"],
            "created_by_name": actor.get("resolved_name") or "",
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        try:
            result = mongo.db[INVOICE_COLLECTION].insert_one(document)
            document["_id"] = result.inserted_id
            invoice = document
        except Exception:
            invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})
            if not invoice:
                raise

    if invoice.get("invoice_number"):
        return invoice

    financial_year = get_open_financial_year_for_date(entity["_id"], invoice.get("invoice_date"))
    if not financial_year:
        mongo.db[INVOICE_COLLECTION].update_one(
            {"_id": invoice["_id"]},
            {"$set": {"numbering_status": "recovery_required", "numbering_error": "No approved open Financial Year covers the invoice date.", "updated_at": now_utc()}},
        )
        raise ValueError("No approved open Financial Year covers this Sales Invoice date.")

    reservation = reserve_document_number(
        entity_id=entity["_id"],
        financial_year_id=financial_year["_id"],
        document_category="invoice",
        document_type="sales_invoice",
        idempotency_key=f"avpl-ufc-sales-invoice:{order['_id']}",
        actor_user_id=actor["_id"],
        required_permission=INVOICE_NUMBER_PERMISSION,
        source_collection=INVOICE_COLLECTION,
        source_id=invoice["_id"],
        metadata={
            "avpl_order_number": order.get("order_number") or "",
            "sale_number": sale.get("sale_number") or "",
            "centre_uid": order.get("centre_uid") or "",
        },
    )
    reservation = commit_reserved_number(
        reservation_id=reservation["id"],
        actor_user_id=actor["_id"],
        required_permission=INVOICE_NUMBER_PERMISSION,
        source_collection=INVOICE_COLLECTION,
        source_id=invoice["_id"],
        source_reference=sale.get("sale_number") or order.get("order_number") or "",
    )
    invoice_number = reservation.get("full_number") or ""
    if not invoice_number:
        raise RuntimeError("The official Sales Invoice number could not be resolved.")

    mongo.db[INVOICE_COLLECTION].update_one(
        {"_id": invoice["_id"], "invoice_number": {"$in": [None, ""]}},
        {"$set": {
            "invoice_number": invoice_number,
            "numbering_status": "committed",
            "number_reservation_id": _to_object_id(reservation.get("id")),
            "number_reservation_id_str": reservation.get("id") or "",
            "financial_year_id": _to_object_id(reservation.get("financial_year_id")),
            "financial_year_id_str": reservation.get("financial_year_id") or "",
            "financial_year_code": reservation.get("financial_year_code") or "",
            "numbering_error": None,
            "updated_at": now_utc(),
        }},
    )
    return mongo.db[INVOICE_COLLECTION].find_one({"_id": invoice["_id"]})


def _upsert_receivable(order, sale, invoice):
    timestamp = now_utc()
    total = float(_decimal(invoice.get("grand_total")))
    document = {
        "accounting_entity_id": order.get("accounting_entity_id"),
        "accounting_entity_id_str": str(order.get("accounting_entity_id") or ""),
        "avpl_ufc_order_id": order["_id"],
        "avpl_ufc_order_id_str": str(order["_id"]),
        "avpl_ufc_sale_id": sale["_id"],
        "avpl_ufc_sale_id_str": str(sale["_id"]),
        "sales_invoice_id": invoice["_id"],
        "sales_invoice_id_str": str(invoice["_id"]),
        "invoice_number": invoice.get("invoice_number") or "",
        "centre_uid": order.get("centre_uid") or "",
        "centre_name": order.get("centre_name") or order.get("centre_uid") or "UFC",
        "invoice_date": invoice.get("invoice_date"),
        "due_date": invoice.get("due_date"),
        "total_amount": total,
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "payment_status": invoice.get("payment_status") or "unpaid",
        "status": "open" if _decimal(invoice.get("outstanding_amount"), str(total)) > 0 else "closed",
        "accounting_status": "ready_for_posting",
        "source": "automatic_avpl_ufc_sale",
        "updated_at": timestamp,
    }
    mongo.db[RECEIVABLE_COLLECTION].update_one(
        {"avpl_ufc_order_id": order["_id"]},
        {"$set": document, "$setOnInsert": {"created_at": timestamp}},
        upsert=True,
    )
    return mongo.db[RECEIVABLE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})


def _upsert_ufc_payable(order, sale, invoice):
    timestamp = now_utc()
    total = float(_decimal(invoice.get("grand_total")))
    received = order.get("status") == "received" or order.get("ufc_stock_posted") is True
    document = {
        "centre_uid": order.get("centre_uid") or "",
        "centre_name": order.get("centre_name") or order.get("centre_uid") or "UFC",
        "avpl_ufc_order_id": order["_id"],
        "avpl_ufc_order_id_str": str(order["_id"]),
        "avpl_ufc_sale_id": sale["_id"],
        "avpl_ufc_sale_id_str": str(sale["_id"]),
        "sales_invoice_id": invoice["_id"],
        "sales_invoice_id_str": str(invoice["_id"]),
        "invoice_number": invoice.get("invoice_number") or "",
        "seller_type": "avpl",
        "seller_name": (invoice.get("seller") or {}).get("display_name") or "AVPL",
        "invoice_date": invoice.get("invoice_date"),
        "due_date": invoice.get("due_date"),
        "total_amount": total,
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "payment_status": invoice.get("payment_status") or "unpaid",
        "status": "open" if received else "pending_receipt",
        "accounting_status": "not_posted",
        "source": "automatic_avpl_ufc_sale",
        "updated_at": timestamp,
    }
    mongo.db[UFC_PAYABLE_COLLECTION].update_one(
        {"avpl_ufc_order_id": order["_id"]},
        {"$set": document, "$setOnInsert": {"created_at": timestamp}},
        upsert=True,
    )
    return mongo.db[UFC_PAYABLE_COLLECTION].find_one({"avpl_ufc_order_id": order["_id"]})


def link_ufc_purchase_financials(order_id):
    """Link an existing automatic UFC Purchase Entry to the AVPL invoice/payable.

    Safe to call repeatedly and safe when Stage 5 has not generated the invoice yet.
    """
    oid = _to_object_id(order_id)
    if not oid:
        return None
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_order_id": oid})
    sale = mongo.db[SALE_COLLECTION].find_one({"avpl_ufc_order_id": oid})
    if not order or not invoice or not sale:
        return None

    payable = _upsert_ufc_payable(order, sale, invoice)
    total = float(_decimal(invoice.get("grand_total")))
    update = {
        "avpl_sale_id": sale["_id"],
        "avpl_sale_id_str": str(sale["_id"]),
        "avpl_sale_number": sale.get("sale_number") or "",
        "avpl_sales_invoice_id": invoice["_id"],
        "avpl_sales_invoice_id_str": str(invoice["_id"]),
        "avpl_sales_invoice_number": invoice.get("invoice_number") or "",
        "invoice_date": invoice.get("invoice_date"),
        "due_date": invoice.get("due_date"),
        "hsn_code": invoice.get("hsn_code") or "",
        "taxability_code": invoice.get("taxability_code") or "",
        "taxable_value": float(_decimal(invoice.get("taxable_value"))),
        "gst_rate": float(_decimal(invoice.get("gst_rate"))),
        "cgst_amount": float(_decimal(invoice.get("cgst_amount"))),
        "sgst_amount": float(_decimal(invoice.get("sgst_amount"))),
        "igst_amount": float(_decimal(invoice.get("igst_amount"))),
        "gst_amount": float(_decimal(invoice.get("gst_amount"))),
        "total_amount": total,
        "total_amount_display": _money(total),
        "payment_status": invoice.get("payment_status") or "unpaid",
        "amount_paid": float(_decimal(invoice.get("amount_paid"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"), str(total))),
        "ufc_payable_id": payable.get("_id") if payable else None,
        "ufc_payable_id_str": str((payable or {}).get("_id") or ""),
        "accounting_status": "not_posted",
        "financial_link_status": "linked",
        "updated_at": now_utc(),
    }
    mongo.db[UFC_PURCHASE_COLLECTION].update_one({"avpl_ufc_order_id": oid}, {"$set": update})

    # Keep the seller-side status aligned with the physical buyer receipt.
    # Stage 5 invoices are created at dispatch, but the sale should visibly move
    # from Invoiced to UFC Received when Stage 4 receipt is confirmed.
    if order.get("status") == "received" or order.get("ufc_stock_posted") is True:
        mongo.db[SALE_COLLECTION].update_one(
            {"_id": sale["_id"]},
            {"$set": {"status": "received", "updated_at": now_utc()}},
        )

    mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {
        "avpl_sale_id": sale["_id"],
        "avpl_sale_number": sale.get("sale_number") or "",
        "avpl_sales_invoice_id": invoice["_id"],
        "avpl_sales_invoice_number": invoice.get("invoice_number") or "",
        "invoice_grand_total": total,
        "financial_sync_status": "complete",
        "updated_at": now_utc(),
    }})
    return mongo.db[UFC_PURCHASE_COLLECTION].find_one({"avpl_ufc_order_id": oid})


def ensure_sales_documents_for_order(actor_user_id, order_id):
    """Create/repair the seller-side documents for one physically dispatched order.

    This is intentionally idempotent. The same Stage 4 order can never create a
    second AVPL sale, invoice, receivable or UFC payable.
    """
    _ensure_indexes()
    actor = _get_actor(actor_user_id, AVPL_ISSUE_ROLES)
    oid = _to_object_id(order_id)
    if not oid:
        raise ValueError("Invalid UFC order.")
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": oid})
    if not order:
        raise ValueError("UFC order was not found.")
    if order.get("status") not in {"dispatched", "received"} or order.get("stock_dispatched") is not True:
        raise ValueError("AVPL Sales documents are generated only after physical dispatch.")

    entity = _active_avpl_entity()
    buyer = _buyer_snapshot(order)
    financial = _financial_snapshot(order, entity, buyer)
    credit_days = int(order.get("credit_period_days") or 0)

    sale = _upsert_sale(order, actor, financial)
    invoice = _upsert_invoice(order, sale, actor, entity, buyer, financial, credit_period_days=credit_days)
    receivable = _upsert_receivable(order, sale, invoice)
    payable = _upsert_ufc_payable(order, sale, invoice)

    mongo.db[SALE_COLLECTION].update_one({"_id": sale["_id"]}, {"$set": {
        "sales_invoice_id": invoice["_id"],
        "sales_invoice_id_str": str(invoice["_id"]),
        "invoice_number": invoice.get("invoice_number") or "",
        "invoice_date": invoice.get("invoice_date"),
        "due_date": invoice.get("due_date"),
        "grand_total": float(_decimal(invoice.get("grand_total"))),
        "outstanding_amount": float(_decimal(invoice.get("outstanding_amount"))),
        "receivable_id": receivable.get("_id") if receivable else None,
        "receivable_id_str": str((receivable or {}).get("_id") or ""),
        "status": "received" if order.get("status") == "received" else "invoiced",
        "updated_at": now_utc(),
    }})

    mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {
        "avpl_sale_id": sale["_id"],
        "avpl_sale_number": sale.get("sale_number") or "",
        "avpl_sales_invoice_id": invoice["_id"],
        "avpl_sales_invoice_number": invoice.get("invoice_number") or "",
        "invoice_grand_total": float(_decimal(invoice.get("grand_total"))),
        "financial_sync_status": "complete",
        "financial_sync_error": None,
        "updated_at": now_utc(),
    }})

    linked_purchase = link_ufc_purchase_financials(oid)
    return {
        "sale": _serialize_sale(mongo.db[SALE_COLLECTION].find_one({"_id": sale["_id"]})),
        "invoice": _serialize_invoice(mongo.db[INVOICE_COLLECTION].find_one({"_id": invoice["_id"]})),
        "receivable": receivable,
        "ufc_payable": payable,
        "ufc_purchase": linked_purchase,
        "message": "AVPL Sale Entry, Sales Invoice, receivable and linked UFC payable were created successfully.",
    }


def mark_sales_sync_error(order_id, error_message):
    oid = _to_object_id(order_id)
    if oid:
        mongo.db[ORDER_COLLECTION].update_one({"_id": oid}, {"$set": {
            "financial_sync_status": "recovery_required",
            "financial_sync_error": _clean(error_message, 800),
            "updated_at": now_utc(),
        }})


def bulk_sync_existing_orders(actor_user_id, limit=100):
    actor = _get_actor(actor_user_id, AVPL_ISSUE_ROLES)
    _ensure_indexes()
    limit = min(max(int(limit or 100), 1), 500)
    rows = list(mongo.db[ORDER_COLLECTION].find({
        "status": {"$in": ["dispatched", "received"]},
        "stock_dispatched": True,
        "$or": [
            {"avpl_sales_invoice_id": {"$exists": False}},
            {"avpl_sales_invoice_id": None},
            {"financial_sync_status": {"$ne": "complete"}},
        ],
    }).sort("dispatched_at", ASCENDING).limit(limit))
    result = {"checked": len(rows), "synced": 0, "failed": 0, "errors": []}
    for order in rows:
        try:
            ensure_sales_documents_for_order(actor["_id"], order["_id"])
            result["synced"] += 1
        except Exception as exc:
            result["failed"] += 1
            mark_sales_sync_error(order["_id"], str(exc))
            result["errors"].append(f"{order.get('order_number') or order['_id']}: {exc}")
    return result


def get_avpl_sales_overview(actor_user_id, *, search="", payment_status="all", page=1, per_page=30):
    _ensure_indexes()
    _get_actor(actor_user_id, AVPL_VIEW_ROLES)
    entity = _active_avpl_entity()
    query = {"accounting_entity_id": entity["_id"]}
    payment = str(payment_status or "all").strip().lower()
    if payment in PAYMENT_STATUS_LABELS:
        query["payment_status"] = payment
    text = _clean(search, 120)
    if text:
        import re
        escaped = re.escape(text)
        query["$or"] = [
            {"sale_number": {"$regex": escaped, "$options": "i"}},
            {"invoice_number": {"$regex": escaped, "$options": "i"}},
            {"avpl_order_number": {"$regex": escaped, "$options": "i"}},
            {"centre_uid": {"$regex": escaped, "$options": "i"}},
            {"centre_name": {"$regex": escaped, "$options": "i"}},
            {"product_name": {"$regex": escaped, "$options": "i"}},
        ]
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 30), 10), 100)
    total = mongo.db[SALE_COLLECTION].count_documents(query)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    rows = [
        _serialize_sale(row)
        for row in mongo.db[SALE_COLLECTION].find(query)
        .sort([("sale_date", DESCENDING), ("_id", DESCENDING)])
        .skip((page - 1) * per_page)
        .limit(per_page)
    ]
    total_sales = Decimal("0")
    total_outstanding = Decimal("0")
    for row in mongo.db[SALE_COLLECTION].find({"accounting_entity_id": entity["_id"]}, {"grand_total": 1, "outstanding_amount": 1}):
        total_sales += _decimal(row.get("grand_total"))
        total_outstanding += _decimal(row.get("outstanding_amount"))
    return {
        "rows": rows,
        "query": search or "",
        "selected_payment": payment if payment in PAYMENT_STATUS_LABELS else "all",
        "payment_statuses": PAYMENT_STATUS_LABELS,
        "summary": {
            "sale_count": mongo.db[SALE_COLLECTION].count_documents({"accounting_entity_id": entity["_id"]}),
            "invoice_count": mongo.db[INVOICE_COLLECTION].count_documents({"accounting_entity_id": entity["_id"], "status": "issued"}),
            "total_sales": _money(total_sales),
            "outstanding": _money(total_outstanding),
        },
        "pagination": {
            "page": page,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    }


def get_avpl_sale(actor_user_id, sale_id):
    _get_actor(actor_user_id, AVPL_VIEW_ROLES)
    oid = _to_object_id(sale_id)
    if not oid:
        raise ValueError("Invalid AVPL sale.")
    sale = mongo.db[SALE_COLLECTION].find_one({"_id": oid})
    if not sale:
        raise ValueError("AVPL sale was not found.")
    result = _serialize_sale(sale)
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"avpl_ufc_sale_id": oid})
    result["invoice"] = _serialize_invoice(invoice) if invoice else None
    result["receivable"] = mongo.db[RECEIVABLE_COLLECTION].find_one({"avpl_ufc_sale_id": oid})
    result["ufc_payable"] = mongo.db[UFC_PAYABLE_COLLECTION].find_one({"avpl_ufc_sale_id": oid})
    result["order"] = mongo.db[ORDER_COLLECTION].find_one({"_id": sale.get("avpl_ufc_order_id")})
    return result


def get_sales_invoice(invoice_id, *, actor_user_id=None, centre_uid=None):
    oid = _to_object_id(invoice_id)
    if not oid:
        raise ValueError("Invalid Sales Invoice.")
    invoice = mongo.db[INVOICE_COLLECTION].find_one({"_id": oid})
    if not invoice:
        raise ValueError("Sales Invoice was not found.")
    if actor_user_id:
        actor = _get_actor(actor_user_id)
        role = actor.get("resolved_role")
        if role in AVPL_VIEW_ROLES:
            pass
        elif role == "ufc_admin":
            expected = str(centre_uid or "").strip()
            actual = str((invoice.get("buyer") or {}).get("centre_uid") or "").strip()
            if not expected or expected != actual:
                raise PermissionError("This Sales Invoice does not belong to your UFC Centre.")
        else:
            raise PermissionError("You are not authorized to view this Sales Invoice.")
    return _serialize_invoice(invoice)


def get_sales_invoice_print_context(invoice_id, *, actor_user_id=None, centre_uid=None):
    invoice = get_sales_invoice(invoice_id, actor_user_id=actor_user_id, centre_uid=centre_uid)
    order = mongo.db[ORDER_COLLECTION].find_one({"_id": _to_object_id(invoice.get("avpl_ufc_order_id"))}) or {}
    return {
        "invoice": invoice,
        "order": order,
        "seller": invoice.get("seller") or {},
        "buyer": invoice.get("buyer") or {},
    }
