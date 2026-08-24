from __future__ import annotations
from app.utils.timezone import business_today

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from bson import ObjectId
from pymongo import DESCENDING

from app.extensions import mongo


ALLOWED_ROLES = {"accounts", "avpl_admin", "super_admin"}

PURCHASE_ORDER_COLLECTION = "avpl_purchase_orders"
GOODS_RECEIPT_COLLECTION = "avpl_goods_receipts"
SUPPLIER_INVOICE_COLLECTION = "avpl_supplier_invoices"
UFC_ORDER_COLLECTION = "avpl_ufc_orders"
UFC_SALE_COLLECTION = "avpl_ufc_sales"
UFC_INVOICE_COLLECTION = "avpl_sales_invoices"
PAYMENT_COLLECTION = "payments"

MONEY_QUANTUM = Decimal("0.01")

PO_STATUS_LABELS = {
    "draft": "Draft",
    "pending_approval": "Pending Approval",
    "returned_for_correction": "Returned for Correction",
    "approved": "Approved",
    "partially_received": "Partially Received",
    "fully_received": "Fully Received",
    "cancelled": "Cancelled",
}

UFC_ORDER_STATUS_LABELS = {
    "requested": "Requested",
    "approved": "Approved",
    "rejected": "Rejected",
    "cancelled": "Cancelled",
    "dispatched": "Dispatched",
    "received": "Received",
}

PAYMENT_STATUS_LABELS = {
    "unpaid": "Unpaid",
    "partially_paid": "Partially Paid",
    "paid": "Paid",
    "reversed": "Reversed",
    "not_posted": "Not Posted",
    "not_invoiced": "Not Invoiced",
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


def _date_value(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except (TypeError, ValueError):
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return None


def _date_display(value):
    resolved = _date_value(value)
    if resolved:
        return resolved.strftime("%d %b %Y")
    return str(value or "")[:10]


def _date_iso(value):
    resolved = _date_value(value)
    if resolved:
        return resolved.strftime("%Y-%m-%d")
    return str(value or "")[:10]


def _safe_text(value):
    return " ".join(str(value or "").split())


def _get_actor(actor_user_id):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_id},
        {"role": 1, "active": 1, "is_active": 1, "status": 1},
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")

    role = str(actor.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        raise PermissionError("You are not authorized to view AVPL Accounts operations.")
    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or str(actor.get("status") or "").lower() == "inactive"
    ):
        raise PermissionError("Inactive users cannot view AVPL Accounts operations.")

    actor["resolved_role"] = role
    return actor


def _active_avpl_entity():
    """Return the active AVPL accounting entity, if setup has reached that step.

    Accounts pages are read/reporting surfaces.  A fresh database must not turn
    those pages into HTTP 500s before AVPL Accounting setup is completed.
    Transaction-writing services still enforce their own strict entity checks.
    """
    return mongo.db.accounting_entities.find_one(
        {
            "entity_code": "AVPL",
            "entity_type": "avpl",
            "status": "active",
            "accounting_enabled": {"$ne": False},
            "is_deleted": {"$ne": True},
        }
    )


def _empty_financial_summary():
    return {
        "supplier_purchase_value": "0.00",
        "supplier_paid": "0.00",
        "supplier_outstanding": "0.00",
        "supplier_invoice_count": 0,
        "ufc_sales_value": "0.00",
        "ufc_received": "0.00",
        "ufc_receivable": "0.00",
        "ufc_invoice_count": 0,
        "cost_of_goods_sold": "0.00",
        "gross_margin": "0.00",
        "gross_margin_percent": "0.00",
        "sale_count": 0,
    }


def _setup_message():
    return "Complete the AVPL Accounting entity setup to start financial reporting."


def _invoice_number(source_type, invoice):
    if not invoice:
        return ""
    if source_type == "supplier_invoice":
        return (
            invoice.get("official_purchase_invoice_number")
            or invoice.get("supplier_invoice_number")
            or invoice.get("internal_reference")
            or ""
        )
    return invoice.get("invoice_number") or invoice.get("document_number") or ""


def _invoice_financials(invoice):
    invoice = invoice or {}
    total = _decimal(
        invoice.get("grand_total")
        or invoice.get("total_amount")
        or invoice.get("invoice_total")
    )
    paid_raw = (
        invoice.get("amount_paid")
        if invoice.get("amount_paid") is not None
        else invoice.get("paid_amount")
    )
    paid = max(_decimal(paid_raw), Decimal("0"))
    if paid > total:
        paid = total

    outstanding_raw = invoice.get("outstanding_amount")
    outstanding = (
        max(_decimal(outstanding_raw), Decimal("0"))
        if outstanding_raw not in (None, "")
        else max(total - paid, Decimal("0"))
    )
    if outstanding > max(total - paid, Decimal("0")):
        outstanding = max(total - paid, Decimal("0"))

    if total <= Decimal("0.004") or outstanding <= Decimal("0.004"):
        status = "paid"
    elif paid > Decimal("0.004"):
        status = "partially_paid"
    else:
        status = "unpaid"

    due_date = _date_value(invoice.get("due_date"))
    is_overdue = bool(due_date and outstanding > Decimal("0.004") and due_date.date() < business_today())

    return {
        "total": total,
        "paid": paid,
        "outstanding": outstanding,
        "total_display": _money(total),
        "paid_display": _money(paid),
        "outstanding_display": _money(outstanding),
        "status": status,
        "status_label": PAYMENT_STATUS_LABELS.get(status, status.replace("_", " ").title()),
        "due_date": _date_iso(invoice.get("due_date")),
        "is_overdue": is_overdue,
    }


def _posted_supplier_invoice_query(entity):
    return {
        "accounting_entity_id": entity["_id"],
        "payable_posted": True,
        "posting_status": "posted",
    }


def _issued_ufc_invoice_query(entity):
    return {
        "accounting_entity_id": entity["_id"],
        "status": "issued",
    }


def _financial_summary(entity):
    supplier_purchase = Decimal("0")
    supplier_paid = Decimal("0")
    supplier_outstanding = Decimal("0")
    supplier_invoice_count = 0

    for invoice in mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
        _posted_supplier_invoice_query(entity),
        {"grand_total": 1, "amount_paid": 1, "paid_amount": 1, "outstanding_amount": 1},
    ):
        values = _invoice_financials(invoice)
        supplier_purchase += values["total"]
        supplier_paid += values["paid"]
        supplier_outstanding += values["outstanding"]
        supplier_invoice_count += 1

    ufc_sales = Decimal("0")
    ufc_received = Decimal("0")
    ufc_receivable = Decimal("0")
    ufc_invoice_count = 0
    for invoice in mongo.db[UFC_INVOICE_COLLECTION].find(
        _issued_ufc_invoice_query(entity),
        {"grand_total": 1, "amount_paid": 1, "paid_amount": 1, "outstanding_amount": 1},
    ):
        values = _invoice_financials(invoice)
        ufc_sales += values["total"]
        ufc_received += values["paid"]
        ufc_receivable += values["outstanding"]
        ufc_invoice_count += 1

    taxable_sales = Decimal("0")
    cogs = Decimal("0")
    gross_margin = Decimal("0")
    sale_count = 0
    for sale in mongo.db[UFC_SALE_COLLECTION].find(
        {"accounting_entity_id": entity["_id"]},
        {"taxable_value": 1, "estimated_cogs": 1, "gross_margin_amount": 1},
    ):
        taxable_sales += _decimal(sale.get("taxable_value"))
        cogs += _decimal(sale.get("estimated_cogs"))
        gross_margin += _decimal(sale.get("gross_margin_amount"))
        sale_count += 1

    margin_percent = (
        gross_margin * Decimal("100") / taxable_sales
        if taxable_sales > Decimal("0")
        else Decimal("0")
    )

    return {
        "supplier_purchase_value": _money(supplier_purchase),
        "supplier_paid": _money(supplier_paid),
        "supplier_outstanding": _money(supplier_outstanding),
        "supplier_invoice_count": supplier_invoice_count,
        "ufc_sales_value": _money(ufc_sales),
        "ufc_received": _money(ufc_received),
        "ufc_receivable": _money(ufc_receivable),
        "ufc_invoice_count": ufc_invoice_count,
        "cost_of_goods_sold": _money(cogs),
        "gross_margin": _money(gross_margin),
        "gross_margin_percent": f"{margin_percent.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}",
        "sale_count": sale_count,
    }


def _matches(text, *values):
    needle = str(text or "").strip().lower()
    if not needle:
        return True
    return any(needle in str(value or "").lower() for value in values)


def _payment_invoice_maps(payments):
    supplier_ids = []
    ufc_ids = []
    for payment in payments:
        invoice_id = _to_object_id(payment.get("invoice_id"))
        if not invoice_id:
            continue
        if payment.get("source_type") == "supplier_invoice":
            supplier_ids.append(invoice_id)
        elif payment.get("source_type") == "avpl_ufc_invoice":
            ufc_ids.append(invoice_id)

    supplier_map = {
        str(row["_id"]): row
        for row in mongo.db[SUPPLIER_INVOICE_COLLECTION].find({"_id": {"$in": supplier_ids}})
    } if supplier_ids else {}
    ufc_map = {
        str(row["_id"]): row
        for row in mongo.db[UFC_INVOICE_COLLECTION].find({"_id": {"$in": ufc_ids}})
    } if ufc_ids else {}
    return supplier_map, ufc_map


def get_accounts_transaction_overview(actor_user_id, *, segment="all", query_text="", limit=500):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity()

    selected_segment = str(segment or "all").strip().lower()
    if not entity:
        if selected_segment not in {"all", "supplier", "ufc"}:
            selected_segment = "all"
        return {
            "rows": [],
            "selected_segment": selected_segment,
            "query": query_text or "",
            "summary": {
                "supplier_paid": "0.00",
                "ufc_received": "0.00",
                "supplier_outstanding": "0.00",
                "ufc_receivable": "0.00",
            },
            "counts": {"all": 0, "supplier": 0, "ufc": 0},
            "setup_required": True,
            "setup_message": _setup_message(),
        }

    selected_segment = str(segment or "all").strip().lower()
    if selected_segment not in {"all", "supplier", "ufc"}:
        selected_segment = "all"

    source_types = ["supplier_invoice", "avpl_ufc_invoice"]
    if selected_segment == "supplier":
        source_types = ["supplier_invoice"]
    elif selected_segment == "ufc":
        source_types = ["avpl_ufc_invoice"]

    payments = list(
        mongo.db[PAYMENT_COLLECTION]
        .find({
            "source_type": {"$in": source_types},
            "status": {"$in": ["completed", "reversed"]},
        })
        .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
        .limit(max(int(limit or 500), 1))
    )
    supplier_map, ufc_map = _payment_invoice_maps(payments)

    rows = []
    for payment in payments:
        source_type = str(payment.get("source_type") or "")
        invoice_id = _to_object_id(payment.get("invoice_id"))
        invoice_key = str(invoice_id or payment.get("invoice_id") or "")
        invoice = supplier_map.get(invoice_key) if source_type == "supplier_invoice" else ufc_map.get(invoice_key)
        if not invoice or str(invoice.get("accounting_entity_id") or "") != str(entity.get("_id") or ""):
            # Payments are only displayed when their source invoice belongs to
            # the active AVPL entity. This keeps the Accounts read-model scoped.
            continue
        financials = _invoice_financials(invoice)

        if source_type == "supplier_invoice":
            party_name = invoice.get("supplier_name") or payment.get("payee_name") or "Supplier"
            order_number = invoice.get("po_number") or ""
            order_id = str(invoice.get("purchase_order_id") or "")
            direction = "out"
            direction_label = "Money Out"
            transaction_group = "Supplier Transaction"
        else:
            buyer = invoice.get("buyer") or {}
            party_name = (
                invoice.get("centre_name")
                or buyer.get("legal_name")
                or buyer.get("display_name")
                or invoice.get("centre_uid")
                or payment.get("payer_name")
                or "UFC"
            )
            order_number = invoice.get("avpl_order_number") or ""
            order_id = str(invoice.get("avpl_ufc_order_id") or "")
            direction = "in"
            direction_label = "Money In"
            transaction_group = "UFC Transaction"

        row = {
            "id": str(payment.get("_id") or ""),
            "date": _date_display(payment.get("payment_date") or payment.get("created_at")),
            "sort_date": _date_value(payment.get("created_at") or payment.get("payment_date")) or datetime.min,
            "payment_number": payment.get("payment_number") or "",
            "source_type": source_type,
            "transaction_group": transaction_group,
            "direction": direction,
            "direction_label": direction_label,
            "party_name": party_name,
            "centre_uid": invoice.get("centre_uid") or "",
            "order_number": order_number,
            "order_id": order_id,
            "invoice_number": payment.get("invoice_number") or _invoice_number(source_type, invoice),
            "invoice_id": invoice_key,
            "invoice_total": financials["total_display"],
            "amount": _money(payment.get("amount")),
            "payment_mode": str(payment.get("payment_mode") or "").replace("_", " ").title(),
            "reference": payment.get("reference") or "",
            "status": payment.get("status") or "completed",
            "status_label": "Reversed" if payment.get("status") == "reversed" else "Completed",
            "settlement_status": financials["status"],
            "settlement_status_label": financials["status_label"],
            "outstanding": financials["outstanding_display"],
        }

        if _matches(
            query_text,
            row["payment_number"],
            row["party_name"],
            row["centre_uid"],
            row["order_number"],
            row["invoice_number"],
            row["amount"],
            row["payment_mode"],
            row["reference"],
            row["status_label"],
            row["settlement_status_label"],
        ):
            rows.append(row)

    summary = _financial_summary(entity)
    supplier_invoice_ids = [
        row["_id"]
        for row in mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
            {"accounting_entity_id": entity["_id"]},
            {"_id": 1},
        )
    ]
    ufc_invoice_ids = [
        row["_id"]
        for row in mongo.db[UFC_INVOICE_COLLECTION].find(
            {"accounting_entity_id": entity["_id"]},
            {"_id": 1},
        )
    ]
    supplier_payment_count = (
        mongo.db[PAYMENT_COLLECTION].count_documents({
            "source_type": "supplier_invoice",
            "invoice_id": {"$in": supplier_invoice_ids},
            "status": {"$in": ["completed", "reversed"]},
        })
        if supplier_invoice_ids else 0
    )
    ufc_payment_count = (
        mongo.db[PAYMENT_COLLECTION].count_documents({
            "source_type": "avpl_ufc_invoice",
            "invoice_id": {"$in": ufc_invoice_ids},
            "status": {"$in": ["completed", "reversed"]},
        })
        if ufc_invoice_ids else 0
    )
    return {
        "rows": rows,
        "selected_segment": selected_segment,
        "query": query_text or "",
        "summary": {
            "supplier_paid": summary["supplier_paid"],
            "ufc_received": summary["ufc_received"],
            "supplier_outstanding": summary["supplier_outstanding"],
            "ufc_receivable": summary["ufc_receivable"],
        },
        "counts": {
            "all": supplier_payment_count + ufc_payment_count,
            "supplier": supplier_payment_count,
            "ufc": ufc_payment_count,
        },
    }


def _group_by(rows, key_name):
    grouped = {}
    for row in rows:
        key = str(row.get(key_name) or "")
        if key:
            grouped.setdefault(key, []).append(row)
    return grouped


def _aggregate_invoice_payment_status(invoices):
    posted = [row for row in invoices if row.get("payable_posted") is True and row.get("posting_status") == "posted"]
    if not invoices:
        return "not_invoiced", PAYMENT_STATUS_LABELS["not_invoiced"]
    if not posted:
        return "not_posted", PAYMENT_STATUS_LABELS["not_posted"]

    statuses = [_invoice_financials(row)["status"] for row in posted]
    if statuses and all(status == "paid" for status in statuses):
        return "paid", PAYMENT_STATUS_LABELS["paid"]
    if any(status in {"paid", "partially_paid"} for status in statuses):
        return "partially_paid", PAYMENT_STATUS_LABELS["partially_paid"]
    return "unpaid", PAYMENT_STATUS_LABELS["unpaid"]


def get_accounts_order_overview(actor_user_id, *, segment="supplier", query_text="", limit=500):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity()

    selected_segment = str(segment or "supplier").strip().lower()
    if not entity:
        if selected_segment not in {"supplier", "ufc"}:
            selected_segment = "supplier"
        return {
            "selected_segment": selected_segment,
            "query": query_text or "",
            "supplier_rows": [],
            "ufc_rows": [],
            "summary": {"supplier_order_count": 0, "ufc_order_count": 0},
            "setup_required": True,
            "setup_message": _setup_message(),
        }

    selected_segment = str(segment or "supplier").strip().lower()
    if selected_segment not in {"supplier", "ufc"}:
        selected_segment = "supplier"

    supplier_rows = []
    ufc_rows = []

    if selected_segment == "supplier":
        purchase_orders = list(
            mongo.db[PURCHASE_ORDER_COLLECTION]
            .find({"accounting_entity_id": entity["_id"]})
            .sort([("order_date", DESCENDING), ("created_at", DESCENDING)])
            .limit(max(int(limit or 500), 1))
        )
        po_ids = [row["_id"] for row in purchase_orders]
        receipts = list(
            mongo.db[GOODS_RECEIPT_COLLECTION].find({"purchase_order_id": {"$in": po_ids}})
        ) if po_ids else []
        invoices = list(
            mongo.db[SUPPLIER_INVOICE_COLLECTION].find({"purchase_order_id": {"$in": po_ids}})
        ) if po_ids else []
        receipts_by_po = _group_by(receipts, "purchase_order_id")
        invoices_by_po = _group_by(invoices, "purchase_order_id")

        for order in purchase_orders:
            order_id = str(order.get("_id") or "")
            order_receipts = sorted(
                receipts_by_po.get(order_id, []),
                key=lambda row: _date_value(row.get("receipt_date") or row.get("created_at")) or datetime.min,
                reverse=True,
            )
            order_invoices = sorted(
                invoices_by_po.get(order_id, []),
                key=lambda row: _date_value(row.get("invoice_date") or row.get("created_at")) or datetime.min,
                reverse=True,
            )
            latest_receipt = order_receipts[0] if order_receipts else {}
            latest_invoice = order_invoices[0] if order_invoices else {}
            posted_invoices = [row for row in order_invoices if row.get("payable_posted") is True and row.get("posting_status") == "posted"]

            invoice_total = sum((_invoice_financials(row)["total"] for row in posted_invoices), Decimal("0"))
            paid_total = sum((_invoice_financials(row)["paid"] for row in posted_invoices), Decimal("0"))
            outstanding_total = sum((_invoice_financials(row)["outstanding"] for row in posted_invoices), Decimal("0"))
            payment_status, payment_status_label = _aggregate_invoice_payment_status(order_invoices)

            receipt_status = order.get("receipt_status") or ""
            if order_receipts:
                if order.get("status") == "fully_received" or receipt_status == "fully_received":
                    grn_status_label = "Fully Received"
                elif order.get("status") == "partially_received" or receipt_status == "partially_received":
                    grn_status_label = "Partially Received"
                else:
                    posted_count = sum(1 for row in order_receipts if row.get("status") == "posted")
                    grn_status_label = f"{posted_count or len(order_receipts)} GRN" + ("s" if (posted_count or len(order_receipts)) != 1 else "")
            else:
                grn_status_label = "No GRN"

            invoice_number = _invoice_number("supplier_invoice", latest_invoice)
            if len(order_invoices) > 1 and invoice_number:
                invoice_number = f"{invoice_number} +{len(order_invoices) - 1}"

            row = {
                "id": order_id,
                "date": _date_display(order.get("order_date") or order.get("created_at")),
                "po_number": order.get("po_number") or "",
                "supplier_name": order.get("supplier_name") or "Supplier",
                "item_count": int(order.get("item_count") or len(order.get("items") or [])),
                "order_value": _money(order.get("grand_total")),
                "status": order.get("status") or "draft",
                "status_label": PO_STATUS_LABELS.get(order.get("status"), str(order.get("status") or "draft").replace("_", " ").title()),
                "grn_count": len(order_receipts),
                "grn_status_label": grn_status_label,
                "latest_grn_id": str(latest_receipt.get("_id") or ""),
                "latest_grn_number": latest_receipt.get("grn_number") or "",
                "invoice_count": len(order_invoices),
                "latest_invoice_id": str(latest_invoice.get("_id") or ""),
                "invoice_number": invoice_number,
                "invoice_printable": bool(latest_invoice.get("official_purchase_invoice_number") or latest_invoice.get("posting_status") == "posted"),
                "invoice_total": _money(invoice_total),
                "paid": _money(paid_total),
                "outstanding": _money(outstanding_total),
                "payment_status": payment_status,
                "payment_status_label": payment_status_label,
            }
            if _matches(
                query_text,
                row["po_number"],
                row["supplier_name"],
                row["latest_grn_number"],
                row["invoice_number"],
                row["status_label"],
                row["payment_status_label"],
                *[item.get("product_name") for item in order.get("items") or []],
            ):
                supplier_rows.append(row)

    else:
        orders = list(
            mongo.db[UFC_ORDER_COLLECTION]
            .find({"accounting_entity_id": entity["_id"]})
            .sort([("created_at", DESCENDING), ("_id", DESCENDING)])
            .limit(max(int(limit or 500), 1))
        )
        order_ids = [row["_id"] for row in orders]
        invoices = list(
            mongo.db[UFC_INVOICE_COLLECTION].find({"avpl_ufc_order_id": {"$in": order_ids}})
        ) if order_ids else []
        sales = list(
            mongo.db[UFC_SALE_COLLECTION].find({"avpl_ufc_order_id": {"$in": order_ids}})
        ) if order_ids else []
        invoice_by_order = {str(row.get("avpl_ufc_order_id") or ""): row for row in invoices}
        sale_by_order = {str(row.get("avpl_ufc_order_id") or ""): row for row in sales}

        for order in orders:
            order_id = str(order.get("_id") or "")
            invoice = invoice_by_order.get(order_id, {})
            sale = sale_by_order.get(order_id, {})
            financials = _invoice_financials(invoice) if invoice else {
                "total_display": _money(order.get("invoice_grand_total") or order.get("total_amount")),
                "paid_display": _money(order.get("amount_paid") or order.get("paid_amount")),
                "outstanding_display": _money(order.get("outstanding_amount") or order.get("invoice_grand_total") or order.get("total_amount")),
                "status": order.get("payment_status") or "unpaid",
                "status_label": PAYMENT_STATUS_LABELS.get(order.get("payment_status") or "unpaid", str(order.get("payment_status") or "unpaid").replace("_", " ").title()),
            }
            status = str(order.get("status") or "requested")
            dispatch_label = {
                "dispatched": "Dispatched",
                "received": "Received",
            }.get(status, "Not Dispatched")
            requested_qty = order.get("requested_quantity") or order.get("quantity") or 0
            approved_qty = order.get("approved_quantity") or requested_qty
            display_qty = order.get("dispatched_quantity") or approved_qty
            row = {
                "id": order_id,
                "date": _date_display(order.get("created_at")),
                "order_number": order.get("order_number") or "",
                "centre_uid": order.get("centre_uid") or "",
                "centre_name": order.get("centre_name") or order.get("centre_uid") or "UFC",
                "product_name": order.get("product_name") or "Product",
                "quantity": str(display_qty or 0),
                "unit_code": order.get("unit_code") or "",
                "order_value": financials["total_display"],
                "status": status,
                "status_label": UFC_ORDER_STATUS_LABELS.get(status, status.replace("_", " ").title()),
                "dispatch_status_label": dispatch_label,
                "invoice_id": str(invoice.get("_id") or order.get("avpl_sales_invoice_id") or ""),
                "invoice_number": invoice.get("invoice_number") or order.get("avpl_sales_invoice_number") or "",
                "payment_status": financials["status"],
                "payment_status_label": financials["status_label"],
                "paid": financials["paid_display"],
                "outstanding": financials["outstanding_display"],
                "sale_id": str(sale.get("_id") or order.get("avpl_sale_id") or ""),
            }
            if _matches(
                query_text,
                row["order_number"],
                row["centre_uid"],
                row["centre_name"],
                row["product_name"],
                row["invoice_number"],
                row["status_label"],
                row["payment_status_label"],
            ):
                ufc_rows.append(row)

    return {
        "selected_segment": selected_segment,
        "query": query_text or "",
        "supplier_rows": supplier_rows,
        "ufc_rows": ufc_rows,
        "summary": {
            "supplier_order_count": mongo.db[PURCHASE_ORDER_COLLECTION].count_documents({"accounting_entity_id": entity["_id"]}),
            "ufc_order_count": mongo.db[UFC_ORDER_COLLECTION].count_documents({"accounting_entity_id": entity["_id"]}),
        },
    }


def get_purchase_sales_summary(actor_user_id, *, query_text="", limit=500):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity()
    if not entity:
        return {
            "query": query_text or "",
            "summary": _empty_financial_summary(),
            "supplier_rows": [],
            "ufc_rows": [],
            "setup_required": True,
            "setup_message": _setup_message(),
        }
    summary = _financial_summary(entity)

    supplier_rows = []
    supplier_invoices = list(
        mongo.db[SUPPLIER_INVOICE_COLLECTION]
        .find({"accounting_entity_id": entity["_id"]})
        .sort([("invoice_date", DESCENDING), ("created_at", DESCENDING)])
        .limit(max(int(limit or 500), 1))
    )
    for invoice in supplier_invoices:
        financials = _invoice_financials(invoice)
        posted = invoice.get("payable_posted") is True and invoice.get("posting_status") == "posted"
        payment_status = financials["status"] if posted else "not_posted"
        payment_status_label = financials["status_label"] if posted else PAYMENT_STATUS_LABELS["not_posted"]
        row = {
            "id": str(invoice.get("_id") or ""),
            "date": _date_display(invoice.get("invoice_date") or invoice.get("created_at")),
            "supplier_name": invoice.get("supplier_name") or "Supplier",
            "po_number": invoice.get("po_number") or "",
            "purchase_order_id": str(invoice.get("purchase_order_id") or ""),
            "invoice_number": _invoice_number("supplier_invoice", invoice),
            "purchase_value": financials["total_display"],
            "paid": financials["paid_display"] if posted else "0.00",
            "outstanding": financials["outstanding_display"] if posted else "0.00",
            "payment_status": payment_status,
            "payment_status_label": payment_status_label,
            "posting_status": invoice.get("posting_status") or "not_posted",
            "posting_status_label": str(invoice.get("posting_status") or "not_posted").replace("_", " ").title(),
            "printable": bool(invoice.get("official_purchase_invoice_number") or invoice.get("posting_status") == "posted"),
        }
        if _matches(
            query_text,
            row["supplier_name"],
            row["po_number"],
            row["invoice_number"],
            row["payment_status_label"],
            row["posting_status_label"],
        ):
            supplier_rows.append(row)

    ufc_rows = []
    sales = list(
        mongo.db[UFC_SALE_COLLECTION]
        .find({"accounting_entity_id": entity["_id"]})
        .sort([("sale_date", DESCENDING), ("_id", DESCENDING)])
        .limit(max(int(limit or 500), 1))
    )
    sale_ids = [row["_id"] for row in sales]
    invoices = list(
        mongo.db[UFC_INVOICE_COLLECTION].find({"avpl_ufc_sale_id": {"$in": sale_ids}})
    ) if sale_ids else []
    invoice_by_sale = {str(row.get("avpl_ufc_sale_id") or ""): row for row in invoices}

    for sale in sales:
        sale_id = str(sale.get("_id") or "")
        invoice = invoice_by_sale.get(sale_id, {})
        financials = _invoice_financials(invoice if invoice else sale)
        margin = _decimal(sale.get("gross_margin_amount"))
        taxable = _decimal(sale.get("taxable_value"))
        margin_percent = (
            margin * Decimal("100") / taxable
            if taxable > Decimal("0")
            else Decimal("0")
        )
        row = {
            "id": sale_id,
            "date": _date_display(sale.get("sale_date") or sale.get("created_at")),
            "centre_uid": sale.get("centre_uid") or "",
            "centre_name": sale.get("centre_name") or sale.get("centre_uid") or "UFC",
            "order_number": sale.get("avpl_order_number") or "",
            "order_id": str(sale.get("avpl_ufc_order_id") or ""),
            "invoice_id": str(invoice.get("_id") or ""),
            "invoice_number": invoice.get("invoice_number") or sale.get("invoice_number") or "",
            "product_name": sale.get("product_name") or "Product",
            "sales_value": _money(sale.get("grand_total")),
            "cogs": _money(sale.get("estimated_cogs")),
            "gross_margin": _money(margin),
            "gross_margin_percent": f"{margin_percent.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP):.2f}",
            "received": financials["paid_display"],
            "outstanding": financials["outstanding_display"],
            "payment_status": financials["status"],
            "payment_status_label": financials["status_label"],
        }
        if _matches(
            query_text,
            row["centre_uid"],
            row["centre_name"],
            row["order_number"],
            row["invoice_number"],
            row["product_name"],
            row["payment_status_label"],
        ):
            ufc_rows.append(row)

    return {
        "query": query_text or "",
        "summary": summary,
        "supplier_rows": supplier_rows,
        "ufc_rows": ufc_rows,
    }


def get_accounts_dashboard_overview(actor_user_id):
    _get_actor(actor_user_id)
    entity = _active_avpl_entity()
    if not entity:
        return {
            **_empty_financial_summary(),
            "supplier_order_count": 0,
            "ufc_order_count": 0,
            "transaction_count": 0,
            "setup_required": True,
            "setup_message": _setup_message(),
        }
    summary = _financial_summary(entity)
    supplier_invoice_ids = [
        row["_id"]
        for row in mongo.db[SUPPLIER_INVOICE_COLLECTION].find(
            {"accounting_entity_id": entity["_id"]},
            {"_id": 1},
        )
    ]
    ufc_invoice_ids = [
        row["_id"]
        for row in mongo.db[UFC_INVOICE_COLLECTION].find(
            {"accounting_entity_id": entity["_id"]},
            {"_id": 1},
        )
    ]
    transaction_count = 0
    if supplier_invoice_ids:
        transaction_count += mongo.db[PAYMENT_COLLECTION].count_documents({
            "source_type": "supplier_invoice",
            "invoice_id": {"$in": supplier_invoice_ids},
            "status": {"$in": ["completed", "reversed"]},
        })
    if ufc_invoice_ids:
        transaction_count += mongo.db[PAYMENT_COLLECTION].count_documents({
            "source_type": "avpl_ufc_invoice",
            "invoice_id": {"$in": ufc_invoice_ids},
            "status": {"$in": ["completed", "reversed"]},
        })
    return {
        **summary,
        "supplier_order_count": mongo.db[PURCHASE_ORDER_COLLECTION].count_documents({"accounting_entity_id": entity["_id"]}),
        "ufc_order_count": mongo.db[UFC_ORDER_COLLECTION].count_documents({"accounting_entity_id": entity["_id"]}),
        "transaction_count": transaction_count,
    }
