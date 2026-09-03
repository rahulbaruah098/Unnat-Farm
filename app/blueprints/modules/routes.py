from app.utils.timezone import business_today
import os
import re
import json
from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.utils.security import save_file
from app.services.avpl_ufc_order_service import (
    create_ufc_order_request,
    create_ufc_cart_order_request,
    get_order as get_avpl_ufc_order,
    get_ufc_order_overview,
    get_ufc_purchase_overview,
    get_ufc_stock_overview,
    receive_ufc_order,
)
from app.services.opening_stock_service import (
    correct_opening_stock_entry,
    create_ufc_opening_stock,
    get_ufc_opening_stock_overview,
    void_opening_stock_entry,
)
from app.services.document_service import store_document
from app.services.audit_service import log_action
from app.services.avpl_ufc_sales_service import (
    get_sales_invoice_print_context,
)
from app.services.mitra_activity_service import get_mitra_transactions
from app.services.mitra_commission_service import get_mitra_earning_overview
from app.services.sales_unnatfarm_service import (
    get_finance_leads as get_sales_finance_leads,
    get_sales_activity as get_sales_unnatfarm_activity,
    get_sales_overview as get_sales_unnatfarm_overview,
    update_finance_lead_followup as update_sales_finance_lead_followup,
)
from app.services.avpl_accounts_operations_service import (
    get_accounts_order_overview,
    get_accounts_transaction_overview,
    get_purchase_sales_summary,
)
from app.services.ufc_farmer_marketplace_service import (
    bulk_update_publication as bulk_update_ufc_farmer_publication,
    get_farmer_marketplace,
    get_ufc_marketplace_setup,
    save_product_selling_setup,
    set_farmer_delivery_enabled,
)
from app.services.ufc_farmer_order_service import (
    approve_farmer_order as stage7_approve_farmer_order,
    approve_farmer_cart_order as stage7_approve_farmer_cart_order,
    cancel_farmer_order as stage7_cancel_farmer_order,
    create_farmer_order as stage7_create_farmer_order,
    create_farmer_cart_order as stage7_create_farmer_cart_order,
    deliver_farmer_order as stage7_deliver_farmer_order,
    receive_farmer_order as stage7_receive_farmer_order,
    ensure_delivery_documents as stage7_ensure_delivery_documents,
    get_farmer_order_overview as stage7_get_farmer_order_overview,
    get_farmer_purchase_overview as stage7_get_farmer_purchase_overview,
    get_invoice_print_context as stage7_get_invoice_print_context,
    get_order as stage7_get_order,
    get_ufc_order_overview as stage7_get_ufc_order_overview,
    get_ufc_sales_overview as stage7_get_ufc_sales_overview,
    reject_farmer_order as stage7_reject_farmer_order,
    refresh_ufc_farmer_tax_documents as stage8_refresh_ufc_farmer_tax_documents,
)
from app.services.ufc_profile_service import (
    UFC_DOCUMENTS,
    calculate_age as calculate_ufc_owner_age,
    get_ufc_admin_master,
    get_ufc_profile_documents,
    normalize_gstin,
    normalize_pan,
    normalize_pin,
    parse_bool as parse_profile_bool,
    profile_health,
)
from app.services.payment_service import (
    confirm_reported_payment as stage8_confirm_reported_payment,
    get_farmer_payment_overview,
    get_invoice_payment_context,
    get_payment_receipt_context,
    get_ufc_payment_overview,
    record_payment as stage8_record_payment,
    reject_reported_payment as stage8_reject_reported_payment,
    reverse_payment as stage8_reverse_payment,
)
from app.services.lms_service import (
    get_learner_course_context as lms_get_learner_course_context,
    get_learner_overview as lms_get_learner_overview,
    get_resource_for_learner as lms_get_resource_for_learner,
    mark_lesson_complete as lms_mark_lesson_complete,
)
from app.services.support_service import (
    SUPPORT_EMAIL,
    SUPPORT_NUMBER,
    add_ticket_reply as support_add_ticket_reply,
    create_ticket as support_create_ticket,
    get_support_overview as support_get_overview,
    get_ticket_detail as support_get_ticket_detail,
    update_ticket as support_update_ticket,
    user_ticket_action as support_user_ticket_action,
)

from app.services.pos_service import (
    create_farmer_pos_sale,
    create_ufc_pos_sale,
    get_farmer_pos_context,
    get_pos_sale_context,
    get_ufc_output_stock_overview,
    get_ufc_pos_context,
    void_pos_sale,
)
from app.services.farmer_marketplace_service import (
    approve_order as stage9_market_approve_order,
    cancel_order as stage9_market_cancel_order,
    dispatch_order as stage9_market_dispatch_order,
    confirm_delivery as stage9_market_confirm_delivery,
    get_invoice_print_context as stage9_market_get_invoice_print_context,
    get_listing as stage9_market_get_listing,
    get_listing_form_context as stage9_market_get_listing_form_context,
    get_marketplace as stage9_market_get_marketplace,
    get_my_listings as stage9_market_get_my_listings,
    get_order_detail as stage9_market_get_order_detail,
    get_orders as stage9_market_get_orders,
    get_purchases as stage9_market_get_purchases,
    get_sales as stage9_market_get_sales,
    place_order as stage9_market_place_order,
    place_cart_orders as stage9_market_place_cart_orders,
    receive_order as stage9_market_receive_order,
    reject_order as stage9_market_reject_order,
    repair_financial_documents as stage9_market_repair_financial_documents,
    save_listing as stage9_market_save_listing,
    set_listing_status as stage9_market_set_listing_status,
)
from app.services.farmer_production_service import (
    create_external_purchase as stage9_create_external_purchase,
    create_external_sale as stage9_create_external_sale,
    get_external_purchase_form_context as stage9_get_external_purchase_form_context,
    get_external_purchase_print_context as stage9_get_external_purchase_print_context,
    get_external_purchase_rows as stage9_get_external_purchase_rows,
    get_invoice_print_context as stage9_get_invoice_print_context,
    get_production_overview as stage9_get_production_overview,
    get_sale_detail as stage9_get_sale_detail,
    get_sale_form_context as stage9_get_sale_form_context,
    get_sales_overview as stage9_get_sales_overview,
    get_stock_overview as stage9_get_stock_overview,
    record_expense as stage9_record_expense,
    record_production as stage9_record_production,
    record_stock_adjustment as stage9_record_stock_adjustment,
    record_stock_loss as stage9_record_stock_loss,
    void_external_sale as stage9_void_external_sale,
)
from datetime import datetime
from uuid import uuid4

modules_bp = Blueprint("modules", __name__, url_prefix="/modules")


def _legacy_source_reference(prefix):
    """Create a permanent trace key for legacy operational records.

    Stage 0 does not post these records into Accounting. The reference simply
    makes later migration, reconciliation and duplicate detection reliable.
    """
    clean_prefix = str(prefix or "EVENT").strip().upper().replace(" ", "_")
    return f"LEGACY-{clean_prefix}-{uuid4().hex.upper()}"

def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [json_safe(item) for item in value]

    if isinstance(value, dict):
        return {
            key: json_safe(item)
            for key, item in value.items()
        }

    return value

def wants_json_response():
    return (
        request.headers.get("Accept") == "application/json"
        or request.is_json
        or request.args.get("format") == "json"
    )


def _receipt_lines_from_request(items):
    """Read the shared buyer-side receipt matrix from form or JSON payload."""
    if request.is_json:
        payload=request.get_json(silent=True) or {}; rows=payload.get("receipt_lines") or payload.get("items") or []
        return rows if isinstance(rows,list) else []
    rows=[]
    for index,line in enumerate(items or []):
        if not isinstance(line,dict): continue
        line_id=str(line.get("line_id") or ("legacy" if index==0 else f"line-{index+1}"))
        rows.append({
            "line_id":line_id,
            "physically_received_quantity":request.form.get(f"received_{line_id}",""),
            "accepted_quantity":request.form.get(f"accepted_{line_id}",""),
            "damaged_quantity":request.form.get(f"damaged_{line_id}","0"),
            "rejected_quantity":request.form.get(f"rejected_{line_id}","0"),
        })
    return rows


def json_error(message, status=400):
    return jsonify({
        "ok": False,
        "message": message
    }), status


def _active_avpl_marketplace_entity():
    """Return the active AVPL entity used by Stage 3 marketplace publication."""
    return mongo.db.accounting_entities.find_one({
        "entity_code": "AVPL",
        "entity_type": "avpl",
        "status": "active",
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    })


def _published_avpl_products_for_ufc():
    """Return only AVPL products explicitly published to all active UFCs.

    Stage 3 intentionally ignores legacy `available_centres`. Marketplace
    publication controls visibility; inventory lots control displayed stock.
    No stock mutation occurs here.
    """
    avpl_entity = _active_avpl_marketplace_entity()
    if not avpl_entity:
        return []

    publications = list(
        mongo.db.avpl_marketplace_publications.find(
            {
                "accounting_entity_id": avpl_entity["_id"],
                "status": "published",
                "scope": "all_active_ufc",
            },
            {"source_product_id": 1, "published_at": 1},
        ).sort("published_at", -1)
    )

    published_ids = []
    seen_ids = set()
    for publication in publications:
        product_oid = publication.get("source_product_id")
        if isinstance(product_oid, ObjectId) and product_oid not in seen_ids:
            published_ids.append(product_oid)
            seen_ids.add(product_oid)

    if not published_ids:
        return []

    product_rows = list(
        mongo.db.products.find({
            "_id": {"$in": published_ids},
            "is_deleted": {"$ne": True},
            "is_active": {"$ne": False},
            "status": {"$nin": ["disabled", "deleted"]},
            "unnatfarm_eligible": {"$ne": False},
        })
    )
    product_by_id = {row["_id"]: row for row in product_rows}

    stock_by_product = {}
    for lot in mongo.db.avpl_inventory_lots.find({
        "accounting_entity_id": avpl_entity["_id"],
        "source_product_id": {"$in": published_ids},
        "status": {"$ne": "cancelled"},
    }):
        product_key = str(lot.get("source_product_id") or "")
        if not product_key:
            continue
        row = stock_by_product.setdefault(product_key, {
            "physical": 0.0,
            "reserved": 0.0,
            "damaged": 0.0,
            "blocked": 0.0,
            "expired": 0.0,
        })

        def number(value):
            try:
                return max(float(value or 0), 0.0)
            except (TypeError, ValueError):
                return 0.0

        physical = number(lot.get("available_quantity"))
        row["physical"] += physical
        row["reserved"] += number(lot.get("reserved_quantity"))
        row["damaged"] += number(lot.get("damaged_quantity"))
        row["blocked"] += number(lot.get("blocked_quantity"))

        expiry_value = lot.get("expiry_date")
        if expiry_value:
            try:
                expiry_date = (
                    expiry_value.date()
                    if hasattr(expiry_value, "date")
                    else datetime.strptime(str(expiry_value)[:10], "%Y-%m-%d").date()
                )
                if expiry_date < business_today():
                    row["expired"] += physical
            except Exception:
                pass

    products = []
    for product_id in published_ids:
        product = product_by_id.get(product_id)
        if not product:
            continue
        stock = stock_by_product.get(str(product_id), {})
        physical = max(float(stock.get("physical") or 0), 0.0)
        reserved = max(float(stock.get("reserved") or 0), 0.0)
        damaged = max(float(stock.get("damaged") or 0), 0.0)
        blocked = max(float(stock.get("blocked") or 0), 0.0)
        expired = max(float(stock.get("expired") or 0), 0.0)
        saleable = max(physical - reserved - damaged - blocked - expired, 0.0)
        product["_marketplace_stock"] = {
            "physical_quantity": physical,
            "reserved_quantity": reserved,
            "saleable_quantity": saleable,
        }
        product["_ufc_published"] = True
        products.append(product)
    return products


def _is_avpl_product_published_to_ufc(product_id):
    if isinstance(product_id, ObjectId):
        product_oid = product_id
    else:
        try:
            product_oid = ObjectId(str(product_id))
        except Exception:
            return False
    entity = _active_avpl_marketplace_entity()
    if not entity:
        return False
    return mongo.db.avpl_marketplace_publications.find_one({
        "accounting_entity_id": entity["_id"],
        "source_product_id": product_oid,
        "status": "published",
        "scope": "all_active_ufc",
    }) is not None


def normalize_product_for_app(item):
    item = dict(item or {})

    image_value = (
        item.get("image")
        or item.get("picture")
        or item.get("product_image")
        or item.get("image_url")
        or item.get("image_path")
        or item.get("file_path")
        or item.get("filename")
        or item.get("file_name")
        or item.get("stored_name")
        or item.get("photo")
        or ""
    )

    if not image_value:
        image_name = (
            item.get("image_name")
            or item.get("filename")
            or item.get("file_name")
            or item.get("stored_name")
            or ""
        )

        if image_name:
            doc = mongo.db.documents.find_one({
                "$or": [
                    {"filename": image_name},
                    {"file_name": image_name},
                    {"stored_name": image_name},
                    {"original_name": image_name},
                    {"file_path": {"$regex": image_name, "$options": "i"}},
                    {"path": {"$regex": image_name, "$options": "i"}},
                ]
            })

            if doc:
                image_value = (
                    doc.get("file_path")
                    or doc.get("path")
                    or doc.get("url")
                    or doc.get("filename")
                    or doc.get("file_name")
                    or doc.get("stored_name")
                    or image_name
                )

    if image_value:
        image_value = str(image_value).replace("\\", "/")
        item["image"] = image_value
        item["image_url"] = image_value
        item["picture"] = image_value

    return item

def _get_farmer_orders_for_web(user, q="", page=1, per_page=10):
    user = dict(user or {})

    try:
        page = int(page or 1)
    except Exception:
        page = 1

    try:
        per_page = int(per_page or 10)
    except Exception:
        per_page = 10

    page = max(page, 1)
    per_page = max(min(per_page, 50), 5)

    user_id = str(user.get("_id") or user.get("id") or user.get("user_id") or "").strip()
    phone = str(user.get("phone") or user.get("contact_no") or "").strip()

    farmer = {}

    if user_id:
        try:
            farmer = (
                mongo.db.farmer_master.find_one({"linked_user_id": user_id})
                or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(user_id)})
                or {}
            )
        except Exception:
            farmer = (
                mongo.db.farmer_master.find_one({"linked_user_id": user_id})
                or {}
            )

    if not farmer and phone:
        farmer = mongo.db.farmer_master.find_one({"contact_no": phone}) or {}

    farmer_id = str(farmer.get("_id") or "").strip()
    farmer_phone = str(
        farmer.get("contact_no")
        or farmer.get("phone")
        or phone
        or ""
    ).strip()

    owner_filters = []

    if user_id:
        owner_filters.extend([
            {"user_id": user_id},
            {"farmer_user_id": user_id},
            {"buyer_user_id": user_id},
        ])

    if farmer_id:
        owner_filters.append({"farmer_id": farmer_id})

    if farmer_phone:
        owner_filters.extend([
            {"farmer_phone": farmer_phone},
            {"farmer_contact": farmer_phone},
            {"phone": farmer_phone},
        ])

    if not owner_filters:
        return {
            "orders": [],
            "total_orders": 0,
            "total_amount": 0,
            "page": page,
            "per_page": per_page,
            "total_pages": 1,
            "has_prev": False,
            "has_next": False,
            "prev_page": 1,
            "next_page": 1,
        }

    query = {"$or": owner_filters}

    q = (q or "").strip()

    if q:
        search_filter = {
            "$or": [
                {"product_name": {"$regex": q, "$options": "i"}},
                {"name": {"$regex": q, "$options": "i"}},
                {"seller_name": {"$regex": q, "$options": "i"}},
                {"seller_farmer_name": {"$regex": q, "$options": "i"}},
                {"farmer_name": {"$regex": q, "$options": "i"}},
                {"source": {"$regex": q, "$options": "i"}},
                {"product_source": {"$regex": q, "$options": "i"}},
                {"status": {"$regex": q, "$options": "i"}},
                {"created_at": {"$regex": q, "$options": "i"}},
            ]
        }

        try:
            numeric_q = float(q)
            search_filter["$or"].extend([
                {"quantity": numeric_q},
                {"unit_price": numeric_q},
                {"total_amount": numeric_q},
                {"amount": numeric_q},
            ])
        except Exception:
            pass

        query = {
            "$and": [
                query,
                search_filter
            ]
        }

    total_orders = mongo.db.orders.count_documents(query)

    amount_rows = list(
        mongo.db.orders.aggregate([
            {"$match": query},
            {
                "$group": {
                    "_id": None,
                    "total_amount": {
                        "$sum": {
                            "$toDouble": {
                                "$ifNull": ["$total_amount", 0]
                            }
                        }
                    }
                }
            }
        ])
    )

    total_amount = amount_rows[0]["total_amount"] if amount_rows else 0

    total_pages = max((total_orders + per_page - 1) // per_page, 1)

    if page > total_pages:
        page = total_pages

    skip_count = (page - 1) * per_page

    orders = list(
        mongo.db.orders
        .find(query)
        .sort("created_at", -1)
        .skip(skip_count)
        .limit(per_page)
    )

    normalized_orders = []

    for order in orders:
        order = dict(order or {})

        if "_id" in order:
            order["_id"] = str(order["_id"])

        source_value = str(
            order.get("source")
            or order.get("product_source")
            or ""
        ).strip().lower()

        product_source_value = str(
            order.get("product_source")
            or order.get("source")
            or ""
        ).strip().lower()

        product_id_value = str(
            order.get("product_id")
            or order.get("farmer_product_id")
            or order.get("source_product_id")
            or ""
        ).strip()

        seller_name = (
            order.get("seller_name")
            or order.get("seller_farmer_name")
            or order.get("seller_farmer")
            or order.get("sold_by")
            or order.get("vendor_name")
            or order.get("farmer_seller_name")
            or ""
        )

        resolved_avpl_product = {}
        resolved_farmer_product = {}

        if product_id_value:
            try:
                product_oid = ObjectId(product_id_value)

                resolved_avpl_product = mongo.db.products.find_one({
                    "_id": product_oid
                }) or {}

                resolved_farmer_product = mongo.db.farmer_products.find_one({
                    "_id": product_oid
                }) or {}
            except Exception:
                resolved_avpl_product = {}
                resolved_farmer_product = {}

        if resolved_farmer_product:
            seller_name = (
                resolved_farmer_product.get("farmer_name")
                or resolved_farmer_product.get("seller_farmer_name")
                or resolved_farmer_product.get("seller_name")
                or resolved_farmer_product.get("farmer_contact")
                or seller_name
                or "Farmer"
            )

        is_avpl = (
            source_value == "avpl"
            or product_source_value == "avpl"
            or bool(resolved_avpl_product)
        )

        if is_avpl and not resolved_farmer_product:
            source_display = "AVPL"
        else:
            source_display = seller_name or "Farmer"

        order["product_name"] = (
            order.get("product_name")
            or order.get("name")
            or "Product"
        )

        order["quantity"] = order.get("quantity") or 0

        order["unit_price"] = (
            order.get("unit_price")
            or order.get("price")
            or order.get("selling_price")
            or 0
        )

        order["total_amount"] = (
            order.get("total_amount")
            or order.get("amount")
            or 0
        )

        order["source"] = (
            order.get("source")
            or order.get("product_source")
            or ""
        )

        order["source_display"] = source_display

        order["status"] = (
            order.get("status")
            or "placed"
        )

        normalized_orders.append(order)

    return {
        "orders": normalized_orders,
        "total_orders": total_orders,
        "total_amount": total_amount,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "prev_page": max(page - 1, 1),
        "next_page": min(page + 1, total_pages),
    }

@modules_bp.route("/buy", methods=["GET", "POST"])
@login_required
def buy():
    # Stage 7: Farmers use the mapped-UFC Marketplace transaction flow.
    # Keep /modules/buy backward-compatible for the mobile/API client, but do
    # not execute the legacy direct-stock mutation path for Farmers.
    if str(session.get("role") or "").strip().lower() == "farmer":
        if request.method == "POST":
            payload = request.get_json(silent=True) if request.is_json else {}
            try:
                result = stage7_create_farmer_order(
                    session.get("user_id"),
                    payload.get("product_id") or request.form.get("product_id"),
                    payload.get("quantity") or request.form.get("quantity"),
                    request_token=payload.get("request_token") or request.form.get("request_token", ""),
                    note=payload.get("note") or request.form.get("note", ""),
                )
            except (ValueError, PermissionError, RuntimeError) as exc:
                if wants_json_response():
                    return json_error(str(exc), 400)
                flash(str(exc), "danger")
                return redirect(url_for("modules.farmer_marketplace"))
            if wants_json_response():
                return jsonify(json_safe({"ok": True, **result})), 201
            flash(result.get("message") or "Order placed successfully.", "success")
            return redirect(url_for("modules.farmer_my_orders"))

        if wants_json_response():
            try:
                overview = get_farmer_marketplace(
                    session.get("user_id"),
                    search=request.args.get("q", ""),
                )
                return jsonify(json_safe({"ok": True, **overview}))
            except (ValueError, PermissionError, RuntimeError) as exc:
                return json_error(str(exc), 400)
        return redirect(url_for("modules.farmer_marketplace"))

    if request.method == "POST":
        payload = request.get_json(silent=True) if request.is_json else {}

        product_id = (
            payload.get("product_id")
            or request.form.get("product_id")
            or ""
        ).strip()

        source = (
            payload.get("source")
            or request.form.get("source")
            or "farmer"
        ).strip().lower()

        try:
            quantity = float(
                payload.get("quantity")
                or request.form.get("quantity")
                or 0
            )
        except Exception:
            quantity = 0

        if not product_id:
            if wants_json_response():
                return json_error("Product ID is required.", 400)

            flash("Product ID is required.", "danger")
            return redirect(url_for("modules.buy"))

        try:
            product_oid = ObjectId(product_id)
        except Exception:
            if wants_json_response():
                return json_error("Invalid product ID.", 400)

            flash("Invalid product ID.", "danger")
            return redirect(url_for("modules.buy"))

        if quantity <= 0:
            if wants_json_response():
                return json_error("Quantity must be greater than zero.", 400)

            flash("Quantity must be greater than zero.", "danger")
            return redirect(url_for("modules.buy"))

        # ----------------------------------------------------
        # AVPL PRODUCT BUY / ORDER
        # Source collection: products
        # ----------------------------------------------------
        if source == "avpl":
            if not current_app.config.get("LEGACY_DIRECT_AVPL_ORDER_ENABLED", False):
                if wants_json_response():
                    return json_error(
                        "Direct AVPL ordering is temporarily disabled during the staged AVPL workflow rollout.",
                        409,
                    )
                flash(
                    "Direct AVPL ordering is temporarily disabled during the staged AVPL workflow rollout.",
                    "warning",
                )
                return redirect(url_for("modules.buy"))

            product = mongo.db.products.find_one({
                "_id": product_oid,
                "is_deleted": {"$ne": True},
                "is_active": {"$ne": False}
            })

            if not product:
                if wants_json_response():
                    return json_error("AVPL product not found.", 404)

                flash("AVPL product not found.", "danger")
                return redirect(url_for("modules.buy"))

            stock = float(
                product.get("available_quantity")
                or product.get("quantity")
                or product.get("stock_quantity")
                or product.get("stock")
                or 0
            )

            has_stock_field = any(
                key in product
                for key in ["available_quantity", "quantity", "stock_quantity", "stock"]
            )

            if has_stock_field and quantity > stock:
                if wants_json_response():
                    return json_error("Not enough stock available.", 400)

                flash("Not enough stock available.", "danger")
                return redirect(url_for("modules.buy"))

            user_id = (
                payload.get("user_id")
                or request.form.get("user_id")
                or session.get("user_id")
            )

            user = {}
            farmer = {}

            if user_id:
                try:
                    user = mongo.db.users.find_one({"_id": ObjectId(user_id)}) or {}
                except Exception:
                    user = {}

            if user:
                farmer = (
                    mongo.db.farmer_master.find_one({"linked_user_id": str(user.get("_id"))})
                    or mongo.db.farmer_master.find_one({"linked_user_id": user.get("_id")})
                    or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
                    or {}
                )

            unit_price = float(
                product.get("price")
                or product.get("unit_price")
                or product.get("mrp")
                or 0
            )

            total_amount = quantity * unit_price

            farmer_phone = farmer.get("contact_no") or user.get("phone")

            order_doc = {
                "user_id": str(user_id or ""),
                "farmer_user_id": str(user_id or ""),

                # Farmer identity fields - keep both app and web-compatible names
                "farmer_id": str(farmer.get("_id") or ""),
                "farmer_name": farmer.get("name") or user.get("name"),
                "farmer_phone": farmer_phone,
                "farmer_contact": farmer_phone,

                "centre_uid": farmer.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid"),
                "mitra_uid": farmer.get("mitra_uid") or user.get("mapped_mitra_uid") or user.get("mitra_uid"),

                "product_id": product_id,
                "product_name": product.get("name") or product.get("product_name"),
                "product_category": product.get("category"),
                "product_type": product.get("type"),

                "quantity": quantity,
                "unit_price": unit_price,
                "total_amount": total_amount,

                # Keep both names because web and app are using mixed naming
                "source": "avpl",
                "product_source": "avpl",
                "order_type": "farmer_purchase",
                "seller_name": "AVPL",
                "sold_by": "AVPL",
                "source_display": "AVPL",

                # Use placed for web compatibility
                "status": "placed",

                "created_at": now_utc(),
                "updated_at": now_utc(),
                "source_reference": _legacy_source_reference("AVPL_ORDER"),
                "accounting_status": "not_posted",
                "migration_status": "legacy_operational",
            }

            mongo.db.orders.insert_one(order_doc)

            # Reduce AVPL stock only if product has stock field
            if has_stock_field:
                stock_field = None

                for key in ["available_quantity", "quantity", "stock_quantity", "stock"]:
                    if key in product:
                        stock_field = key
                        break

                if stock_field:
                    mongo.db.products.update_one(
                        {"_id": product_oid},
                        {
                            "$inc": {
                                stock_field: -quantity
                            },
                            "$set": {
                                "updated_at": now_utc()
                            }
                        }
                    )

            mongo.db.notifications.insert_one({
                "to_user_id": str(user_id or ""),
                "role": "farmer",
                "title": "Order Placed",
                "message": f"Your order for {quantity} {order_doc['product_name']} has been placed.",
                "status": "unread",
                "created_at": now_utc()
            })

            if wants_json_response():
                return jsonify(json_safe({
                    "ok": True,
                    "message": "AVPL product order placed successfully.",
                    "order": order_doc
                })), 201

            flash("AVPL product order placed successfully.", "success")
            return redirect(url_for("modules.buy"))

        # ----------------------------------------------------
        # FARMER PRODUCT BUY
        # Source collection: farmer_products
        # ----------------------------------------------------
        product = mongo.db.farmer_products.find_one({
            "_id": product_oid
        })

        if not product:
            if wants_json_response():
                return json_error("Product not found.", 404)

            flash("Product not found.", "danger")
            return redirect(url_for("modules.buy"))
        
        buyer_user_id = (
            payload.get("user_id")
            or request.form.get("user_id")
            or session.get("user_id")
            or ""
        )

        buyer_user_id = str(buyer_user_id).strip()

        seller_user_id = str(
            product.get("farmer_user_id")
            or product.get("user_id")
            or product.get("owner_user_id")
            or product.get("created_by")
            or product.get("listed_by")
            or ""
        ).strip()

        if buyer_user_id and seller_user_id and buyer_user_id == seller_user_id:
            if wants_json_response():
                return json_error("You cannot buy your own listed product.", 400)

            flash("You cannot buy your own listed product.", "danger")
            return redirect(url_for("modules.buy"))

        available_qty = float(product.get("available_quantity") or 0)

        if quantity > available_qty:
            if wants_json_response():
                return json_error("Not enough quantity available.", 400)

            flash("Not enough quantity available.", "danger")
            return redirect(url_for("modules.buy"))

        mitra_uid = session.get("mitra_uid")
        centre_uid = session.get("centre_uid")

        unit_price = float(product.get("unit_price") or 0)
        total_amount = quantity * unit_price

        purchase_doc = {
            "mitra_uid": mitra_uid,
            "centre_uid": centre_uid,
            "farmer_product_id": product_id,
            "seller_farmer_name": product.get("farmer_name"),
            "seller_farmer_contact": product.get("farmer_contact"),
            "product_name": product.get("product_name"),
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total_amount,
            "status": "purchased",
            "created_at": now_utc()
        }

        buyer_user_id = (
            payload.get("user_id")
            or request.form.get("user_id")
            or session.get("user_id")
        )

        buyer_user = {}

        if buyer_user_id:
            try:
                buyer_user = mongo.db.users.find_one({"_id": ObjectId(buyer_user_id)}) or {}
            except Exception:
                buyer_user = {}

        buyer_role = buyer_user.get("role") or session.get("role") or ""
        farmer_order_doc = None

        if buyer_role == "farmer":
            buyer_farmer = (
                mongo.db.farmer_master.find_one({"linked_user_id": str(buyer_user.get("_id"))})
                or mongo.db.farmer_master.find_one({"linked_user_id": buyer_user.get("_id")})
                or mongo.db.farmer_master.find_one({"contact_no": buyer_user.get("phone")})
                or {}
            )

            farmer_order_doc = {
                "user_id": str(buyer_user_id or ""),
                "farmer_user_id": str(buyer_user_id or ""),
                "farmer_id": str(buyer_farmer.get("_id") or ""),
                "farmer_name": buyer_farmer.get("name") or buyer_user.get("name"),
                "farmer_phone": buyer_farmer.get("contact_no") or buyer_user.get("phone"),
                "farmer_contact": buyer_farmer.get("contact_no") or buyer_user.get("phone"),

                "centre_uid": buyer_farmer.get("centre_uid") or buyer_user.get("mapped_centre_uid") or buyer_user.get("centre_uid"),
                "mitra_uid": buyer_farmer.get("mitra_uid") or buyer_user.get("mapped_mitra_uid") or buyer_user.get("mitra_uid"),

                "product_id": product_id,
                "farmer_product_id": product_id,
                "product_name": product.get("product_name"),
                "product_category": product.get("category") or product.get("product_category"),
                "product_type": product.get("type") or product.get("variety"),

                "seller_name": product.get("farmer_name"),
                "seller_farmer_name": product.get("farmer_name"),
                "seller_farmer_contact": product.get("farmer_contact"),

                "quantity": quantity,
                "unit_price": unit_price,
                "total_amount": total_amount,

                "source": "farmer",
                "product_source": "farmer",
                "order_type": "farmer_product_purchase",

                "status": "placed",
                "created_at": now_utc(),
                "updated_at": now_utc(),
                "source_reference": _legacy_source_reference("FARMER_MARKET_ORDER"),
                "accounting_status": "not_posted",
                "migration_status": "legacy_operational",
            }

        if farmer_order_doc:
            mongo.db.orders.insert_one(farmer_order_doc)

        mongo.db.mitra_product_purchases.insert_one(purchase_doc)

        mongo.db.notifications.insert_one({
            "to_user_id": session.get("user_id"),
            "role": "ufc_mitra",
            "title": "Purchase Successful",
            "message": f"{quantity} {product.get('product_name')} added to your stock.",
            "status": "unread",
            "created_at": now_utc()
        })

        mongo.db.notifications.insert_one({
            "to_user_id": product.get("farmer_user_id"),
            "role": "farmer",
            "title": "Product Sold",
            "message": f"{quantity} {product.get('product_name')} purchased by UFC Mitra.",
            "status": "unread",
            "created_at": now_utc()
        })

        mongo.db.mitra_product_stock.update_one(
            {
                "mitra_uid": mitra_uid,
                "centre_uid": centre_uid,
                "product_name": product.get("product_name")
            },
            {
                "$inc": {
                    "available_quantity": quantity
                },
                "$set": {
                    "mitra_uid": mitra_uid,
                    "centre_uid": centre_uid,
                    "product_name": product.get("product_name"),
                    "updated_at": now_utc()
                },
                "$setOnInsert": {
                    "created_at": now_utc()
                }
            },
            upsert=True
        )

        new_qty = available_qty - quantity

        mongo.db.farmer_products.update_one(
            {"_id": product_oid},
            {
                "$set": {
                    "available_quantity": new_qty,
                    "updated_at": now_utc()
                }
            }
        )

        mongo.db.farmer_product_sales.insert_one({
            "mitra_uid": mitra_uid,
            "centre_uid": centre_uid,
            "product_name": product.get("product_name"),
            "quantity": quantity,
            "unit_price": unit_price,
            "total_amount": total_amount,
            "type": "purchase_from_farmer",
            "created_at": now_utc()
        })

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "message": f"{quantity} {product.get('product_name')} purchased and added to your stock.",
                "purchase": purchase_doc
            }))

        flash(f"{quantity} {product.get('product_name')} purchased and added to your stock.", "success")
        return redirect(url_for("modules.buy"))

   # ===== GET LOGIC =====
    # Stage 3 marketplace rule: UFC users see only products explicitly
    # published by AVPL Admin. Farmers never see AVPL Product Master directly.
    role = str(session.get("role") or "").strip().lower()
    products = _published_avpl_products_for_ufc() if role in {"ufc_admin", "ufc_mitra"} else []

    # Farmers must not see AVPL Product Master records directly in the new
    # workflow. Farmer-facing UFC listings will be implemented in Stage 6.

    view_mode = request.args.get("view", "").strip()

    # Keep the UFC Admin marketplace focused on AVPL-published products only.
    # Farmer-output purchasing is a separate workflow and should not be mixed
    # into the new AVPL -> UFC marketplace screen.
    if role == "ufc_admin":
        farmer_products = []
    else:
        farmer_query = {"status": "active"}
        if view_mode != "all" and role == "ufc_mitra":
            farmer_query["mitra_uid"] = session.get("mitra_uid")
        elif view_mode == "all" and role == "ufc_admin":
            farmer_query["centre_uid"] = session.get("centre_uid")

        farmer_products = list(
            mongo.db.farmer_products.find(farmer_query).sort("created_at", -1)
        )

    if wants_json_response():
        products = [normalize_product_for_app(item) for item in products]
        farmer_products = [normalize_product_for_app(item) for item in farmer_products]

        return jsonify(json_safe({
            "ok": True,
            "products": products,
            "farmer_products": farmer_products
        }))

    return render_template(
        "modules/buy.html",
        products=products,
        farmer_products=farmer_products
    )

def _farmer_product_choices(farmer):
    choices = []
    for item in (farmer or {}).get("activities", []):
        if item == "Poultry":
            choices.append("Chicken")
        choices.append(item)
    choices.extend((farmer or {}).get("agri_sub_categories", []))
    clean = []
    for item in choices:
        if item and item not in clean:
            clean.append(item)
    return clean


@modules_bp.route("/farmer-products/add", methods=["GET", "POST"])
@login_required
def add_farmer_product():
    if session.get("role") != "farmer":
        flash("Only farmers can add available products.", "danger")
        return redirect(url_for("dashboard.home"))

    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}
    farmer = mongo.db.farmer_master.find_one({"linked_user_id": session["user_id"]}) or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")}) or {}
    product_choices = _farmer_product_choices(farmer)

    if request.method == "POST":
        product_name = request.form.get("product_name", "").strip()
        if product_name not in product_choices:
            flash("Please choose a product from your registered activities only.", "danger")
            return render_template("modules/add_farmer_product.html", farmer=farmer, product_choices=product_choices)

        picture = None
        file = request.files.get("product_picture")
        if file and file.filename:
            try:
                picture = save_file(file, "farmer_product")
            except ValueError as exc:
                flash(str(exc), "danger")
                return render_template("modules/add_farmer_product.html", farmer=farmer, product_choices=product_choices)

        mongo.db.farmer_products.insert_one({
            "farmer_user_id": session["user_id"],
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_contact": farmer.get("contact_no") or user.get("phone"),
            "centre_uid": farmer.get("centre_uid") or user.get("mapped_centre_uid"),
            "mitra_uid": farmer.get("mitra_uid") or user.get("mapped_mitra_uid"),
            "state": farmer.get("state") or user.get("state"),
            "district": farmer.get("district") or user.get("district"),
            "block": farmer.get("block") or user.get("block"),
            "village": farmer.get("village") or user.get("village"),
            "product_name": product_name,
            "variety": request.form.get("variety", "").strip(),
            "average_size": request.form.get("average_size", "").strip(),
            "available_quantity": float(request.form.get("available_quantity") or 0),
            "unit_price": float(request.form.get("unit_price") or 0),
            "picture": picture,
            "status": "active",
            "created_at": now_utc(),
            "updated_at": now_utc(),
        })
        flash("Product availability submitted successfully.", "success")
        return redirect(url_for("modules.buy"))

    return render_template("modules/add_farmer_product.html", farmer=farmer, product_choices=product_choices)


@modules_bp.route("/sell", methods=["GET", "POST"])
@login_required
def sell():
    role = session.get("role")

    # Stage 9 web workflow: Farmer output now has a real production-stock-sale
    # lifecycle. Keep the legacy POST code below for older app compatibility,
    # but never show the old "post availability" screen to web Farmers.
    if str(role or "").strip().lower() == "farmer" and request.method == "GET" and not wants_json_response():
        return redirect(url_for("modules.farmer_sales_new"))

    # UFC MITRA SELL MODULE - stock based selling
    if role == "ufc_mitra":
        user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}
        mitra_master = mongo.db.ufc_mitra_master.find_one({
            "linked_user_id": session["user_id"]
        }) or mongo.db.ufc_mitra_master.find_one({
            "linked_user_id": ObjectId(session["user_id"])
        }) or {}

        mitra_uid = session.get("mitra_uid") or user.get("mitra_uid") or mitra_master.get("mitra_uid")
        centre_uid = (
            session.get("centre_uid")
            or session.get("mapped_centre_uid")
            or user.get("mapped_centre_uid")
            or mitra_master.get("mapped_centre_uid")
            or mitra_master.get("centre_uid")
        )

        if request.method == "POST":
            source_product_id = request.form.get("source_product_id")
            buyer_type = request.form.get("buyer_type")
            buyer_farmer_id = request.form.get("buyer_farmer_id")
            quantity = float(request.form.get("quantity") or 0)
            unit_price = float(request.form.get("unit_price") or 0)
            total_amount = quantity * unit_price

            stock_item = mongo.db.mitra_product_stock.find_one({
                "_id": ObjectId(source_product_id),
                "mitra_uid": mitra_uid
            })

            if not stock_item:
                if wants_json_response():
                    return json_error("Selected stock product not found.", 400)

                flash("Selected stock product not found.", "danger")
                return redirect(url_for("modules.sell"))

            available_quantity = float(stock_item.get("available_quantity") or 0)

            if quantity <= 0:
                if wants_json_response():
                    return json_error("Quantity must be greater than zero.", 400)

                flash("Quantity must be greater than zero.", "danger")
                return redirect(url_for("modules.sell"))

            if quantity > available_quantity:
                if wants_json_response():
                    return json_error("Sale quantity cannot be greater than available stock.", 400)

                flash("Sale quantity cannot be greater than available stock.", "danger")
                return redirect(url_for("modules.sell"))

            buyer_farmer = None

            if buyer_type == "farmer":
                if not buyer_farmer_id:
                    if wants_json_response():
                        return json_error("Please select buyer farmer.", 400)

                    flash("Please select buyer farmer.", "danger")
                    return redirect(url_for("modules.sell"))

                buyer_farmer = mongo.db.farmer_master.find_one({
                    "_id": ObjectId(buyer_farmer_id),
                    "centre_uid": centre_uid
                })

                if not buyer_farmer:
                    if wants_json_response():
                        return json_error("Selected farmer not found under this UFC Center.", 400)

                    flash("Selected farmer not found under this UFC Center.", "danger")
                    return redirect(url_for("modules.sell"))

            mongo.db.mitra_product_sales.insert_one({
                "mitra_uid": mitra_uid,
                "centre_uid": centre_uid,
                "source_product_id": source_product_id,
                "product_name": stock_item.get("product_name"),
                "buyer_type": buyer_type,
                "buyer_farmer_id": buyer_farmer_id if buyer_type == "farmer" else None,
                "buyer_farmer_name": buyer_farmer.get("name") if buyer_farmer else None,
                "quantity": quantity,
                "unit_price": unit_price,
                "total_amount": total_amount,
                "status": "sold",
                "created_at": now_utc(),
            })

            mongo.db.mitra_product_stock.update_one(
                {"_id": ObjectId(source_product_id)},
                {
                    "$inc": {
                        "available_quantity": -quantity
                    },
                    "$set": {
                        "updated_at": now_utc()
                    }
                }
            )

            if wants_json_response():
                return jsonify(json_safe({
                    "ok": True,
                    "message": "Product sold successfully."
                }))

            flash("Product sold successfully.", "success")
            return redirect(url_for("modules.sell"))

        q = request.args.get("q", "").strip()

        stock_items = list(mongo.db.mitra_product_stock.find({
            "mitra_uid": mitra_uid,
            "available_quantity": {"$gt": 0}
        }).sort("created_at", -1))

        farmers = list(mongo.db.farmer_master.find({
            "centre_uid": centre_uid
        }).sort("name", 1))

        sales_query = {
            "mitra_uid": mitra_uid
        }

        if q:
            sales_query["$or"] = [
                {"product_name": {"$regex": q, "$options": "i"}},
                {"buyer_type": {"$regex": q, "$options": "i"}},
                {"buyer_farmer_name": {"$regex": q, "$options": "i"}},
                {"status": {"$regex": q, "$options": "i"}},
                {"centre_uid": {"$regex": q, "$options": "i"}}
            ]

        sales = list(
            mongo.db.mitra_product_sales.find(sales_query).sort("created_at", -1)
        )

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "mitra_sell_mode": True,
                "stock_items": stock_items,
                "farmers": farmers,
                "sales": sales,
                "q": q
            }))
       

        return render_template(
            "modules/sell.html",
            mitra_sell_mode=True,
            stock_items=stock_items,
            farmers=farmers,
            sales=sales,
            q=q
        )

        # FARMER / OTHER ROLE SELL MODULE - save into farmer_products so it appears in Buy page
    if request.method == "POST":
        user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}

        farmer = (
            mongo.db.farmer_master.find_one({"linked_user_id": session["user_id"]})
            or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(session["user_id"])})
            or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
            or {}
        )

        picture = None
        file = request.files.get("product_picture")
        if file and file.filename:
            try:
                picture = save_file(file, "farmer_product")
            except ValueError as exc:
                if wants_json_response():
                    return json_error(str(exc), 400)
                flash(str(exc), "danger")
                return redirect(url_for("modules.sell"))

        product_name = request.form.get("product_name", "").strip()

        try:
            quantity = float(request.form.get("quantity") or 0)
        except Exception:
            quantity = 0

        try:
            price = float(request.form.get("price") or 0)
        except Exception:
            price = 0

        if not product_name:
            if wants_json_response():
                return json_error("Please select a product.", 400)

            flash("Please select a product.", "danger")
            return redirect(url_for("modules.sell"))

        if quantity <= 0:
            if wants_json_response():
                return json_error("Quantity must be greater than zero.", 400)

            flash("Quantity must be greater than zero.", "danger")
            return redirect(url_for("modules.sell"))

        if price <= 0:
            if wants_json_response():
                return json_error("Price must be greater than zero.", 400)

            flash("Price must be greater than zero.", "danger")
            return redirect(url_for("modules.sell"))

        product_doc = {
            "farmer_user_id": session["user_id"],
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_contact": farmer.get("contact_no") or user.get("phone"),
            "centre_uid": farmer.get("centre_uid") or user.get("centre_uid") or user.get("mapped_centre_uid"),
            "mitra_uid": farmer.get("mitra_uid") or user.get("mitra_uid") or user.get("mapped_mitra_uid"),
            "state": farmer.get("state") or user.get("state"),
            "district": farmer.get("district") or user.get("district"),
            "block": farmer.get("block") or user.get("block"),
            "village": farmer.get("village") or user.get("village"),
            "product_name": product_name,
            "variety": request.form.get("variety", "").strip(),
            "average_size": request.form.get("average_size", "").strip(),
            "available_quantity": quantity,
            "unit_price": price,
            "picture": picture,
            "status": "active",
            "created_at": now_utc(),
            "updated_at": now_utc(),
        }

        result = mongo.db.farmer_products.insert_one(product_doc)
        product_doc["_id"] = str(result.inserted_id)

        if wants_json_response():
            product_doc = normalize_product_for_app(product_doc)

            return jsonify(json_safe({
                "ok": True,
                "message": "Product posted for selling.",
                "product": product_doc
            })), 201

        flash("Product posted for selling.", "success")
        return redirect(url_for("modules.sell"))

    q = request.args.get("q", "").strip()

    posts_query = {
        "farmer_user_id": session["user_id"]
    }

    if q:
        posts_query["$or"] = [
            {"product_name": {"$regex": q, "$options": "i"}},
            {"variety": {"$regex": q, "$options": "i"}},
            {"average_size": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
        ]

    posts = list(
        mongo.db.farmer_products.find(posts_query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "mitra_sell_mode": False,
            "posts": posts,
            "q": q
        }))

    return render_template(
        "modules/sell.html",
        posts=posts,
        mitra_sell_mode=False,
        q=q
    )

#changes by atlanta
@modules_bp.route("/my-orders")
@login_required
def farmer_my_orders():
    if str(session.get("role") or "").strip().lower() != "farmer":
        if wants_json_response():
            return json_error("Only Farmers can view My Orders.", 403)
        flash("Only Farmers can view My Orders.", "danger")
        return redirect(url_for("dashboard.home"))

    try:
        overview = stage7_get_farmer_order_overview(
            session.get("user_id"),
            search=request.args.get("q", ""),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), "danger")
        overview = {
            "rows": [],
            "centre_uid": "",
            "centre_name": "Mapped UFC",
            "farmer_name": "Farmer",
            "query": request.args.get("q", ""),
            "summary": {"count": 0, "total_value": "0.00"},
        }

    if wants_json_response():
        payload = {"ok": True, **overview}
        payload["orders"] = overview.get("rows", [])
        payload["total_orders"] = overview.get("summary", {}).get("count", 0)
        payload["total_amount"] = overview.get("summary", {}).get("total_value", "0.00")
        return jsonify(json_safe(payload))
    return render_template("modules/farmer_orders.html", overview=overview)


@modules_bp.route("/my-orders/<order_id>")
@login_required
@roles_required("farmer")
def farmer_order_detail(order_id):
    try:
        order = stage7_get_order(order_id, actor_user_id=session.get("user_id"))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_my_orders"))
    payment=None
    if order.get("invoice_id_str"):
        try: payment=get_invoice_payment_context(session.get("user_id"),"ufc_farmer_invoice",order.get("invoice_id_str"))
        except (ValueError,PermissionError,RuntimeError) as exc: payment={"error":str(exc),"invoice":None,"pending_payments":[],"payment_modes":{}}
    if wants_json_response():
        return jsonify(json_safe({"ok": True, "order": order, "payment": payment}))
    return render_template("modules/farmer_order_detail.html", order=order, payment=payment)


@modules_bp.route("/my-orders/<order_id>/receive", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_receive_order(order_id):
    try:
        current=stage7_get_order(order_id,actor_user_id=session.get("user_id"))
        result=stage7_receive_farmer_order(session.get("user_id"),order_id,receipt_note=request.form.get("receipt_note",""),receipt_lines=_receipt_lines_from_request(current.get("items") or []))
        flash(result.get("message") or "Receipt confirmed.","warning" if result.get("financial_warning") else "success")
    except (ValueError,PermissionError,RuntimeError) as exc: flash(str(exc),"danger")
    return redirect(url_for("modules.farmer_order_detail",order_id=order_id))


@modules_bp.route("/my-orders/<order_id>/cancel", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_cancel_order(order_id):
    try:
        result = stage7_cancel_farmer_order(
            session.get("user_id"),
            order_id,
            reason=request.form.get("reason", ""),
        )
        flash(result.get("message") or "Order cancelled.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_my_orders"))


@modules_bp.route("/finance", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def finance():
    user_id = session.get("user_id")

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    }) or {}

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": user_id})
        or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(user_id)})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )

    farmer_phone = farmer.get("contact_no") or user.get("phone")

    sales = list(mongo.db.pos_sales.find({
        "$or": [
            {"farmer_id": str(farmer.get("_id"))},
            {"farmer_phone": farmer_phone}
        ]
    }))

    total_transaction = sum(float(s.get("total_amount") or 0) for s in sales)
    is_eligible = total_transaction >= 30000

    if request.method == "POST":
        payload = request.get_json(silent=True) if request.is_json else {}

        if not is_eligible:
            message = "You can apply only after completing ₹30,000 transaction value."

            if wants_json_response():
                return json_error(message, 403)

            flash(message, "danger")
            return redirect(url_for("modules.finance"))

        amount = (
            payload.get("amount")
            or request.form.get("amount")
            or ""
        )

        purpose = (
            payload.get("purpose")
            or request.form.get("purpose")
            or ""
        ).strip()

        try:
            amount_value = float(amount or 0)
        except Exception:
            amount_value = 0

        if amount_value <= 0:
            if wants_json_response():
                return json_error("Please enter a valid finance amount.", 400)

            flash("Please enter a valid finance amount.", "danger")
            return redirect(url_for("modules.finance"))

        if not purpose:
            if wants_json_response():
                return json_error("Please enter finance purpose.", 400)

            flash("Please enter finance purpose.", "danger")
            return redirect(url_for("modules.finance"))

        address_parts = [
            farmer.get("village"),
            farmer.get("block"),
            farmer.get("district"),
            farmer.get("state")
        ]
        farmer_address = ", ".join([x for x in address_parts if x])

        finance_doc = {
            "farmer_user_id": user_id,
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_mobile": farmer_phone,
            "farmer_address": farmer_address,
            "centre_uid": farmer.get("centre_uid"),
            "mitra_uid": farmer.get("mitra_uid"),
            "amount": amount_value,
            "purpose": purpose,
            "total_transaction": total_transaction,
            "status": "new",
            "visible_to_roles": [
                "avpl_admin",
                "sales_nelocals",
                "sales_unnatfarm",
                "accounts",
                "ufc_mitra"
            ],
            "created_at": now_utc()
        }

        result = mongo.db.financial_assistance_leads.insert_one(finance_doc)
        finance_doc["_id"] = str(result.inserted_id)

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "message": "Finance request submitted successfully.",
                "item": finance_doc
            })), 201

        flash("Finance request submitted successfully.", "success")
        return redirect(url_for("modules.finance"))

    q = request.args.get("q", "").strip()

    finance_query = {
        "farmer_user_id": user_id
    }

    if q:
        finance_query["$or"] = [
            {"amount": {"$regex": q, "$options": "i"}},
            {"purpose": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
            {"farmer_name": {"$regex": q, "$options": "i"}},
            {"farmer_mobile": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}}
        ]

        try:
            numeric_q = float(q)
            finance_query["$or"].append({"total_transaction": numeric_q})
        except ValueError:
            pass

    items = list(
        mongo.db.financial_assistance_leads.find(finance_query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "total_transaction": total_transaction,
            "is_eligible": is_eligible,
            "q": q
        }))

    return render_template(
        "modules/finance.html",
        items=items,
        total_transaction=total_transaction,
        is_eligible=is_eligible,
        q=q
    )

#changes by atlanta
@modules_bp.route("/insurance", methods=["GET", "POST"])
@login_required
def insurance():
    user_id = session.get("user_id")

    user = mongo.db.users.find_one({
        "_id": ObjectId(user_id)
    }) or {}

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": user_id})
        or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(user_id)})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )

    farmer_phone = farmer.get("contact_no") or user.get("phone")

    sales = list(mongo.db.pos_sales.find({
        "$or": [
            {"farmer_id": str(farmer.get("_id"))},
            {"farmer_phone": farmer_phone}
        ]
    }))

    total_transaction = sum(float(s.get("total_amount") or 0) for s in sales)

    livestock_activities = [
        "Pig", "Goat", "Cattle", "Dairy",
        "Poultry", "Chicken", "Duck", "Fishery"
    ]

    farmer_activities = farmer.get("activities", [])

    is_livestock_farmer = any(
        activity in livestock_activities
        for activity in farmer_activities
    )

    is_eligible = is_livestock_farmer and total_transaction >= 30000

    not_eligible_message = (
        "Insurance is available only for livestock farmers after completing "
        "more than ₹30,000 AVPL sales transaction."
    )

    if request.method == "POST":
        payload = request.get_json(silent=True) if request.is_json else {}

        if not is_eligible:
            if wants_json_response():
                return json_error(not_eligible_message, 403)

            flash(not_eligible_message, "danger")
            return redirect(url_for("modules.insurance"))

        livestock_type = (
            payload.get("livestock_type")
            or request.form.get("livestock_type")
            or ""
        ).strip()

        remarks = (
            payload.get("remarks")
            or request.form.get("remarks")
            or ""
        ).strip()

        if not livestock_type:
            if wants_json_response():
                return json_error("Please select livestock type.", 400)

            flash("Please select livestock type.", "danger")
            return redirect(url_for("modules.insurance"))

        insurance_doc = {
            "requested_by": user_id,
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_mobile": farmer_phone,
            "centre_uid": farmer.get("centre_uid"),
            "mitra_uid": farmer.get("mitra_uid"),
            "livestock_type": livestock_type,
            "remarks": remarks,
            "total_transaction": total_transaction,
            "is_livestock_farmer": is_livestock_farmer,
            "status": "pending",
            "created_at": now_utc(),
        }

        result = mongo.db.insurance_requests.insert_one(insurance_doc)
        insurance_doc["_id"] = str(result.inserted_id)

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "message": "Insurance request submitted.",
                "item": insurance_doc
            })), 201

        flash("Insurance request submitted.", "success")
        return redirect(url_for("modules.insurance"))

    q = request.args.get("q", "").strip()

    insurance_query = {
        "requested_by": user_id
    }

    if q:
        insurance_query["$or"] = [
            {"farmer_name": {"$regex": q, "$options": "i"}},
            {"farmer_mobile": {"$regex": q, "$options": "i"}},
            {"livestock_type": {"$regex": q, "$options": "i"}},
            {"remarks": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}}
        ]

    items = list(
        mongo.db.insurance_requests.find(insurance_query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "total_transaction": total_transaction,
            "is_livestock_farmer": is_livestock_farmer,
            "is_eligible": is_eligible,
            "not_eligible_message": not_eligible_message,
            "q": q
        }))

    return render_template(
        "modules/insurance.html",
        items=items,
        total_transaction=total_transaction,
        is_livestock_farmer=is_livestock_farmer,
        is_eligible=is_eligible,
        q=q
    )

@modules_bp.route("/insurance/leads")
@login_required
@roles_required("ufc_mitra")
def insurance_leads():
    q = request.args.get("q", "").strip()

    query = {
        "mitra_uid": session.get("mitra_uid")
    }

    if q:
        query["$or"] = [
            {"farmer_name": {"$regex": q, "$options": "i"}},
            {"farmer_mobile": {"$regex": q, "$options": "i"}},
            {"livestock_type": {"$regex": q, "$options": "i"}},
            {"remarks": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
        ]

    items = list(
        mongo.db.insurance_requests
        .find(query)
        .sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
        "ok": True,
        "items": items,
        "q": q
    }))

    return render_template(
        "modules/insurance_leads.html",
        items=items,
        q=q
    )

@modules_bp.route("/lms")
@login_required
def lms():
    try:
        overview = lms_get_learner_overview(
            session.get("user_id"),
            request.args.get("q", ""),
            request.args.get("category", ""),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {
            "courses": [],
            "legacy_items": [],
            "profile": {},
            "query": request.args.get("q", ""),
            "activity_filter": request.args.get("category", ""),
            "filter_choices": ["all"],
            "summary": {
                "assigned": 0,
                "in_progress": 0,
                "completed": 0,
                "mandatory_pending": 0,
            },
        }

    if wants_json_response():
        return jsonify(json_safe({"ok": True, **overview}))

    return render_template("modules/lms.html", overview=overview)


@modules_bp.route("/lms/courses/<course_id>")
@login_required
def lms_course_view(course_id):
    try:
        context = lms_get_learner_course_context(session.get("user_id"), course_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.lms"))

    if wants_json_response():
        return jsonify(json_safe({"ok": True, **context}))
    return render_template("modules/lms_course_detail.html", **context)


@modules_bp.route("/lms/lessons/<lesson_id>/complete", methods=["POST"])
@login_required
def lms_lesson_complete(lesson_id):
    try:
        result = lms_mark_lesson_complete(
            session.get("user_id"),
            lesson_id,
            completed=request.form.get("completed", "1") != "0",
        )
        flash(result.get("message") or "Learning progress updated.", "success")
        course_id = result.get("course_id")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        course_id = request.form.get("course_id", "")

    if course_id:
        return redirect(url_for("modules.lms_course_view", course_id=course_id))
    return redirect(url_for("modules.lms"))


@modules_bp.route("/lms/resources/<resource_id>/open")
@login_required
def lms_resource_open(resource_id):
    try:
        resource = lms_get_resource_for_learner(session.get("user_id"), resource_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.lms"))

    if resource.get("resource_type") == "link":
        return redirect(resource.get("external_url") or url_for("modules.lms"))
    filename = resource.get("file_name")
    if not filename:
        flash("This learning resource file is unavailable.", "danger")
        return redirect(url_for("modules.lms_course_view", course_id=resource.get("course_id_str")))
    return redirect(url_for("documents.serve", filename=filename))


# ---------------------------------------------------------------------------
# Professional Support Desk
# ---------------------------------------------------------------------------

@modules_bp.route("/support", methods=["GET", "POST"])
@login_required
def support():
    if request.method == "POST":
        try:
            result = support_create_ticket(
                session.get("user_id"),
                subject=request.form.get("subject", "") if not request.is_json else (request.get_json(silent=True) or {}).get("subject", ""),
                problem_type=request.form.get("problem_type", "") if not request.is_json else (request.get_json(silent=True) or {}).get("problem_type", ""),
                priority=request.form.get("priority", "") if not request.is_json else (request.get_json(silent=True) or {}).get("priority", ""),
                message=request.form.get("message", "") if not request.is_json else (request.get_json(silent=True) or {}).get("message", ""),
                files=request.files.getlist("attachments") if not request.is_json else [],
            )
            if wants_json_response():
                return jsonify(json_safe({"ok": True, **result})), 201
            flash(result.get("message") or "Support ticket raised successfully.", "success")
            ticket = result.get("ticket") or {}
            if ticket.get("id"):
                return redirect(url_for("modules.support_ticket_detail", ticket_id=ticket.get("id")))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400 if not isinstance(exc, PermissionError) else 403)
            flash(str(exc), "danger")
        return redirect(url_for("modules.support"))

    try:
        overview = support_get_overview(
            session.get("user_id"),
            search=request.args.get("q", ""),
            status=request.args.get("status", "all"),
            priority=request.args.get("priority", "all"),
            problem_type=request.args.get("problem_type", "all"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 403 if isinstance(exc, PermissionError) else 400)
        flash(str(exc), "danger")
        overview = {
            "tickets": [],
            "summary": {"total": 0, "open": 0, "in_progress": 0, "waiting_for_user": 0, "resolved": 0, "closed": 0},
            "problem_types": [],
            "priorities": {},
            "statuses": {},
            "support_email": SUPPORT_EMAIL,
            "support_number": SUPPORT_NUMBER,
            "can_create": session.get("role") != "super_admin",
            "is_support_admin": session.get("role") == "super_admin",
            "search": request.args.get("q", ""),
            "selected_status": request.args.get("status", "all"),
            "selected_priority": request.args.get("priority", "all"),
            "selected_problem_type": request.args.get("problem_type", "all"),
        }

    if wants_json_response():
        return jsonify(json_safe({"ok": True, "overview": overview}))
    return render_template("modules/support.html", overview=overview)


@modules_bp.route("/support/<ticket_id>")
@login_required
def support_ticket_detail(ticket_id):
    try:
        context = support_get_ticket_detail(session.get("user_id"), ticket_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 403 if isinstance(exc, PermissionError) else 404)
        flash(str(exc), "danger")
        return redirect(url_for("modules.support"))

    if wants_json_response():
        return jsonify(json_safe({"ok": True, **context}))
    return render_template("modules/support_ticket_detail.html", **context)


@modules_bp.route("/support/<ticket_id>/reply", methods=["POST"])
@login_required
def support_ticket_reply(ticket_id):
    payload = request.get_json(silent=True) if request.is_json else {}
    payload = payload or {}
    try:
        result = support_add_ticket_reply(
            session.get("user_id"),
            ticket_id,
            message=payload.get("message", "") if request.is_json else request.form.get("message", ""),
            files=[] if request.is_json else request.files.getlist("attachments"),
            internal=(payload.get("visibility") == "internal") if request.is_json else (request.form.get("visibility") == "internal"),
        )
        if wants_json_response():
            return jsonify(json_safe({"ok": True, **result}))
        flash(result.get("message") or "Reply sent.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 403 if isinstance(exc, PermissionError) else 400)
        flash(str(exc), "danger")
    return redirect(url_for("modules.support_ticket_detail", ticket_id=ticket_id))


@modules_bp.route("/support/<ticket_id>/update", methods=["POST"])
@login_required
def update_support_ticket(ticket_id):
    try:
        result = support_update_ticket(
            session.get("user_id"),
            ticket_id,
            status=request.form.get("status", ""),
            priority=request.form.get("priority", ""),
            progress=request.form.get("progress", ""),
            resolution_note=request.form.get("resolution_note", ""),
        )
        flash(result.get("message") or "Support ticket updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.support_ticket_detail", ticket_id=ticket_id))


@modules_bp.route("/support/<ticket_id>/action", methods=["POST"])
@login_required
def support_ticket_action(ticket_id):
    try:
        result = support_user_ticket_action(
            session.get("user_id"),
            ticket_id,
            request.form.get("action", ""),
        )
        flash(result.get("message") or "Ticket updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.support_ticket_detail", ticket_id=ticket_id))


@modules_bp.route("/orders")
@login_required
def orders():
    role = str(session.get("role") or "").strip().lower()
    q = request.args.get("q", "").strip()

    # Accounts/AVPL management must use the connected Stage 2-5 read model,
    # not the legacy generic `orders` collection.  The shared URL is retained
    # so UFC/Mitra/Sales behavior and existing links are not broken.
    if role in {"accounts", "avpl_admin", "super_admin"}:
        try:
            overview = get_accounts_order_overview(
                session.get("user_id"),
                segment=request.args.get("segment", "supplier"),
                query_text=q,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Accounts order overview unavailable: %s", exc)
            overview = {
                "selected_segment": request.args.get("segment", "supplier") if request.args.get("segment", "supplier") in {"supplier", "ufc"} else "supplier",
                "query": q,
                "supplier_rows": [],
                "ufc_rows": [],
                "summary": {"supplier_order_count": 0, "ufc_order_count": 0},
                "setup_required": True,
                "setup_message": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({"ok": True, "overview": overview}))
        return render_template("modules/accounts_orders.html", overview=overview)

    query = {}
    if role == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")
    elif role == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")

    if q:
        query["$or"] = [
            {"status": {"$regex": q, "$options": "i"}},
            {"product_name": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}}
        ]

        try:
            numeric_q = float(q)
            query["$or"].append({"quantity": numeric_q})
        except ValueError:
            pass

    items = list(mongo.db.orders.find(query).sort("created_at", -1))

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template("modules/orders.html", items=items, q=q)


@modules_bp.route("/products")
@login_required
def products():
    role = str(session.get("role") or "").strip().lower()
    if role in {"ufc_admin", "ufc_mitra"}:
        items = _published_avpl_products_for_ufc()
    elif role == "farmer":
        # Stage 6 source of truth: only products published by the Farmer's
        # mapped UFC are visible. Never expose AVPL Product Master directly.
        try:
            marketplace = get_farmer_marketplace(
                session.get("user_id"),
                search=request.args.get("q", ""),
            )
            items = marketplace.get("rows", [])
        except (ValueError, PermissionError, RuntimeError):
            items = []
        if not wants_json_response():
            return redirect(url_for("modules.farmer_marketplace"))
    else:
        items = list(
            mongo.db.products.find({
                "is_deleted": {"$ne": True},
                "is_active": {"$ne": False}
            }).sort("created_at", -1)
        )
    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items
        }))

    return render_template("modules/products.html", items=items)

#changes by atlanta
@modules_bp.route("/transactions")
@login_required
def transactions():
    role = str(session.get("role") or "").strip().lower()
    q = request.args.get("q", "").strip()

    # Management Accounts view remains payment-focused. Sales UnnatFarm gets
    # a dedicated read-only activity view built from authoritative commerce
    # records; lower-level roles keep their existing scoped transaction views.
    if role in {"accounts", "avpl_admin", "super_admin"}:
        try:
            overview = get_accounts_transaction_overview(
                session.get("user_id"),
                segment=request.args.get("segment", "all"),
                query_text=q,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Accounts transaction overview unavailable: %s", exc)
            selected = request.args.get("segment", "all")
            overview = {
                "rows": [],
                "selected_segment": selected if selected in {"all", "supplier", "ufc"} else "all",
                "query": q,
                "summary": {
                    "supplier_paid": "0.00",
                    "ufc_received": "0.00",
                    "supplier_outstanding": "0.00",
                    "ufc_receivable": "0.00",
                },
                "counts": {"all": 0, "supplier": 0, "ufc": 0},
                "setup_required": True,
                "setup_message": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({"ok": True, "overview": overview}))
        return render_template("modules/accounts_transactions.html", overview=overview)

    if role == "sales_unnatfarm":
        try:
            overview = get_sales_unnatfarm_activity(
                session.get("user_id"),
                period=request.args.get("period", "this_month"),
                channel=request.args.get("channel", "all"),
                q=q,
                page=request.args.get("page", 1),
                per_page=request.args.get("per_page", 25),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Sales UnnatFarm activity unavailable: %s", exc)
            overview = {
                "rows": [], "summary": {"activity_count": 0, "business_value": 0, "in_progress": 0, "completed": 0},
                "channels": [("all", "All Sales")], "periods": [("this_month", "This Month")],
                "selected_period": request.args.get("period", "this_month"),
                "selected_channel": request.args.get("channel", "all"), "q": q,
                "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                "notice": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({
                "ok": True, "overview": overview,
                "items": overview.get("rows") or [], "q": q,
            }))
        return render_template("modules/sales_activity.html", overview=overview)

    if role == "ufc_mitra":
        try:
            overview = get_mitra_transactions(
                session.get("mitra_uid"),
                q=q,
                period=request.args.get("period", "all"),
                activity_type=request.args.get("type", "all"),
                farmer=request.args.get("farmer", ""),
                page=request.args.get("page", 1),
                per_page=request.args.get("per_page", 25),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Mitra transaction overview unavailable: %s", exc)
            overview = {
                "rows": [], "q": q, "selected_period": "all", "selected_type": "all",
                "selected_farmer": "", "periods": [("all", "All Time")],
                "types": [("all", "All Types")], "farmer_options": [],
                "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                "summary": {"transactions": 0, "business_value": 0, "earnings": 0, "farmers_served": 0},
                "notice": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({"ok": True, "overview": overview}))
        return render_template("modules/mitra_transactions.html", overview=overview)

    query = {}

    if role == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")

    elif role == "farmer":
        user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}
        query["farmer_contact"] = user.get("phone")

    if q:
        query["$or"] = [
            {"transaction_type": {"$regex": q, "$options": "i"}},
            {"product_name": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}},
            {"farmer_contact": {"$regex": q, "$options": "i"}},
            {"farmer_name": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
        ]

        try:
            numeric_q = float(q)
            query["$or"].extend([
                {"amount": numeric_q},
                {"quantity": numeric_q},
                {"total_amount": numeric_q},
                {"unit_price": numeric_q},
            ])
        except ValueError:
            pass

    items = list(
        mongo.db.transactions.find(query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template(
        "modules/transactions.html",
        items=items,
        q=q
    )

#changes by atlanta
@modules_bp.route("/profile")
@login_required
def profile():
    is_app = request.args.get("user_id")

    def clean_profile_value(value):
        if value in [None, "", "-", "None", "null"]:
            return None
        return value

    def pick_profile_value(source, possible_keys):
        if not source:
            return None

        for key in possible_keys:
            value = clean_profile_value(source.get(key))
            if value is not None:
                return value

        return None

    dob_keys = [
        "dob",
        "owner_dob",
        "date_of_birth",
        "birth_date",
        "dateOfBirth",
        "dateofbirth",
        "birthdate",
        "dob_date",
        "Date of Birth",
        "DATE OF BIRTH",
        "date of birth",
    ]

    age_keys = [
        "age",
        "owner_age",
        "Age",
        "AGE",
    ]

    if is_app:
        user_id = request.args.get("user_id", "").strip()

        if not user_id:
            return jsonify({"ok": False, "message": "User ID is required"}), 400

        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})

        if not user:
            return jsonify({"ok": False, "message": "User not found"}), 404

        master = None
        role = user.get("role")

        if role == "farmer":
            master = mongo.db.farmer_master.find_one({"linked_user_id": str(user["_id"])})
        elif role == "ufc_admin":
            master = mongo.db.ufc_admin_master.find_one({"linked_user_id": str(user["_id"])})
        elif role == "ufc_mitra":
            master = mongo.db.ufc_mitra_master.find_one({"linked_user_id": str(user["_id"])})

        uid_str = str(user["_id"])
        uid_oid = ObjectId(uid_str)

        docs = list(
            mongo.db.documents.find({
                "$or": [
                    {"linked_user_id": uid_str},
                    {"linked_user_id": uid_oid},

                    {"user_id": uid_str},
                    {"user_id": uid_oid},

                    {"farmer_user_id": uid_str},
                    {"farmer_user_id": uid_oid},

                    {"entity_id": uid_str},
                    {"entity_id": uid_oid},

                    {"uploaded_by": uid_str},
                    {"uploaded_by": uid_oid},

                    {"uploaded_by_user_id": uid_str},
                    {"uploaded_by_user_id": uid_oid},

                    {"uploader_user_id": uid_str},
                    {"uploader_user_id": uid_oid},

                    {"uploaded_user_id": uid_str},
                    {"uploaded_user_id": uid_oid},
                ]
            }).sort("created_at", -1)
        )

        profile_photo_value = (
            user.get("profile_photo")
            or user.get("profile_photo_file")
            or ""
        )

        if not profile_photo_value and master:
            profile_photo_value = (
                master.get("profile_photo")
                or master.get("profile_photo_file")
                or ""
            )

        if not profile_photo_value:
            for doc in docs:
                doc_type = str(
                    doc.get("document_type")
                    or doc.get("doc_type")
                    or doc.get("label")
                    or doc.get("title")
                    or ""
                ).lower()

                if (
                    "passport size photo" in doc_type
                    or "profile photo" in doc_type
                    or "farmer profile photo" in doc_type
                ):
                    profile_photo_value = (
                        doc.get("file_path")
                        or doc.get("filename")
                        or doc.get("file_name")
                        or doc.get("stored_name")
                        or ""
                    )
                    break

        if profile_photo_value:
            user["profile_photo"] = profile_photo_value
            user["profile_photo_file"] = profile_photo_value

        if master:
            master["profile_photo"] = profile_photo_value
            master["profile_photo_file"] = profile_photo_value

        profile_dob = (
            pick_profile_value(user, dob_keys)
            or pick_profile_value(master, dob_keys)
        )

        profile_age = (
            pick_profile_value(user, age_keys)
            or pick_profile_value(master, age_keys)
        )

        user["_id"] = str(user["_id"])

        if master and "_id" in master:
            master["_id"] = str(master["_id"])

        for d in docs:
            if "_id" in d:
                d["_id"] = str(d["_id"])

        return jsonify(json_safe({
            "ok": True,
            "user": user,
            "master": master or {},
            "docs": docs,
            "profile_dob": profile_dob,
            "profile_age": profile_age,
        }))

    if not session.get("user_id"):
        return redirect(url_for("auth.login_select"))

    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})

    master = None
    role = user.get("role") if user else None

    if role == "farmer":
        master = mongo.db.farmer_master.find_one({"linked_user_id": str(user["_id"])})
    elif role == "ufc_admin":
        master = mongo.db.ufc_admin_master.find_one({"linked_user_id": str(user["_id"])})
    elif role == "ufc_mitra":
        master = mongo.db.ufc_mitra_master.find_one({"linked_user_id": str(user["_id"])})

    if role == "ufc_admin" and user:
        master = get_ufc_admin_master(user) or master or {}
        ufc_documents = get_ufc_profile_documents(str(user["_id"]), master)
        latest_profile_update = mongo.db.profile_update_requests.find_one(
            {"user_id": str(user["_id"]), "role": "ufc_admin"},
            sort=[("requested_at", -1), ("reviewed_at", -1), ("_id", -1)]
        )
        pending_profile_update = (
            latest_profile_update
            if latest_profile_update and latest_profile_update.get("status") == "pending"
            else None
        )
        rejected_profile_update = (
            latest_profile_update
            if latest_profile_update and latest_profile_update.get("status") == "rejected"
            else None
        )
        health = profile_health(user, master, ufc_documents)
        return render_template(
            "modules/ufc_admin_profile.html",
            user=user,
            master=master,
            documents=ufc_documents,
            health=health,
            latest_profile_update=latest_profile_update,
            pending_profile_update=pending_profile_update,
            rejected_profile_update=rejected_profile_update,
        )

    all_docs = list(
        mongo.db.documents.find({
            "linked_user_id": str(user["_id"]),
            "status": "active"
        }).sort("created_at", -1)
    ) if user else []

    docs = []
    seen_doc_types = set()

    for doc in all_docs:
        doc_type = (doc.get("document_type") or "").strip().lower()

        if not doc_type:
            continue

        if doc_type in seen_doc_types:
            continue

        seen_doc_types.add(doc_type)
        docs.append(doc)

    latest_profile_update = mongo.db.profile_update_requests.find_one(
        {
            "user_id": str(user["_id"])
        },
        sort=[
            ("requested_at", -1),
            ("reviewed_at", -1),
            ("_id", -1)
        ]
    ) if user else None

    pending_profile_update = (
        latest_profile_update
        if latest_profile_update and latest_profile_update.get("status") == "pending"
        else None
    )

    rejected_profile_update = (
        latest_profile_update
        if latest_profile_update and latest_profile_update.get("status") == "rejected"
        else None
    )

    profile_dob = (
        pick_profile_value(user, dob_keys)
        or pick_profile_value(master, dob_keys)
    )

    profile_age = (
        pick_profile_value(user, age_keys)
        or pick_profile_value(master, age_keys)
    )

    return render_template(
        "modules/profile.html",
        user=user,
        master=master,
        docs=docs,
        profile_dob=profile_dob,
        profile_age=profile_age,
        latest_profile_update=latest_profile_update,
        pending_profile_update=pending_profile_update,
        rejected_profile_update=rejected_profile_update
    )



def _ufc_profile_upload_is_valid(file_storage, max_bytes=5 * 1024 * 1024):
    if not file_storage or not file_storage.filename:
        return True, ""
    extension = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if extension not in {"pdf", "jpg", "jpeg", "png", "webp"}:
        return False, "Only PDF, JPG, PNG or WEBP documents are allowed."
    try:
        file_storage.seek(0, 2)
        size = file_storage.tell()
        file_storage.seek(0)
    except Exception:
        size = 0
    if size > max_bytes:
        return False, "Each document must be 5 MB or smaller."
    return True, ""


@modules_bp.route("/profile/ufc-admin/edit")
@login_required
def edit_ufc_admin_business_profile():
    if str(session.get("role") or "").strip().lower() != "ufc_admin":
        flash("This profile editor is available only for UFC Admin users.", "danger")
        return redirect(url_for("modules.profile"))

    try:
        user = mongo.db.users.find_one({"_id": ObjectId(session.get("user_id"))}) or {}
    except Exception:
        user = {}

    if not user:
        flash("User not found. Please login again.", "danger")
        return redirect(url_for("auth.logout"))

    approval = str(user.get("approval_status") or session.get("approval_status") or "").strip().lower()
    if approval != "approved":
        if approval in {"pending_profile", "rejected"}:
            return redirect(url_for("auth.complete_ufc_admin"))
        return redirect(url_for("dashboard.pending_access"))

    pending_request = mongo.db.profile_update_requests.find_one({
        "user_id": str(user["_id"]),
        "role": "ufc_admin",
        "status": "pending",
    })
    if pending_request:
        flash("Your profile changes are already waiting for AVPL approval. Your current approved profile remains active.", "warning")
        return redirect(url_for("modules.profile"))

    master = get_ufc_admin_master(user)
    documents = get_ufc_profile_documents(str(user["_id"]), master)
    health = profile_health(user, master, documents)
    return render_template(
        "modules/ufc_admin_profile_edit.html",
        user=user,
        master=master,
        documents=documents,
        health=health,
    )


@modules_bp.route("/profile/ufc-admin/update", methods=["POST"])
@login_required
def submit_ufc_admin_business_profile_update():
    if str(session.get("role") or "").strip().lower() != "ufc_admin":
        flash("This profile update is available only for UFC Admin users.", "danger")
        return redirect(url_for("modules.profile"))

    try:
        user = mongo.db.users.find_one({"_id": ObjectId(session.get("user_id"))}) or {}
    except Exception:
        user = {}

    if not user:
        flash("User not found. Please login again.", "danger")
        return redirect(url_for("auth.logout"))

    if str(user.get("approval_status") or "").strip().lower() != "approved":
        flash("Complete the initial Centre profile approval before submitting profile changes.", "warning")
        return redirect(url_for("auth.complete_ufc_admin"))

    user_id = str(user["_id"])
    existing_pending = mongo.db.profile_update_requests.find_one({
        "user_id": user_id,
        "role": "ufc_admin",
        "status": "pending",
    })
    if existing_pending:
        flash("You already have a Centre profile update waiting for AVPL approval.", "warning")
        return redirect(url_for("modules.profile"))

    master = get_ufc_admin_master(user)
    documents = get_ufc_profile_documents(user_id, master)

    name_of_enterprise = request.form.get("name_of_enterprise", "").strip()
    name_of_owner = request.form.get("name_of_owner", "").strip()
    owner_dob = request.form.get("owner_dob", "").strip()
    owner_age = calculate_ufc_owner_age(owner_dob)
    district = request.form.get("district", "").strip()
    block = request.form.get("block", "").strip()
    village = request.form.get("village", "").strip()
    address = request.form.get("address", "").strip()
    email = request.form.get("email", "").strip()
    trader_license_number = request.form.get("trader_license_number", "").strip()
    other_licenses = request.form.get("other_licenses", "").strip()
    state = str(master.get("state") or user.get("state") or "").strip()

    if not all([name_of_enterprise, name_of_owner, owner_dob, owner_age, district, block, village]):
        flash("Please complete the enterprise, owner and Centre location fields.", "danger")
        return redirect(url_for("modules.edit_ufc_admin_business_profile"))

    if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        flash("Enter a valid email address or leave the email field blank.", "danger")
        return redirect(url_for("modules.edit_ufc_admin_business_profile"))

    try:
        pan_number = normalize_pan(request.form.get("pan_number", ""), required=True)
        postal_code = normalize_pin(request.form.get("postal_code", ""))
        gst_number, gst_registered = normalize_gstin(
            request.form.get("gst_number", ""),
            registered=request.form.get("gst_registered"),
            pan=pan_number,
            state=state,
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.edit_ufc_admin_business_profile"))

    upload_fields = {
        "registration_certificate": "Registration Certificate",
        "pan_file": "PAN",
        "gst_file": "GST Registration",
        "trader_license_file": "Trader License",
        "other_license_file": "Other Licenses",
    }
    selected_uploads = {}
    for field, label in upload_fields.items():
        file_storage = request.files.get(field)
        if file_storage and file_storage.filename:
            ok, message = _ufc_profile_upload_is_valid(file_storage)
            if not ok:
                flash(f"{label}: {message}", "danger")
                return redirect(url_for("modules.edit_ufc_admin_business_profile"))
            selected_uploads[field] = file_storage

    missing_required_docs = []
    if not (documents.get("registration_certificate") or {}).get("exists") and "registration_certificate" not in selected_uploads:
        missing_required_docs.append("Registration Certificate")
    if not (documents.get("pan_file") or {}).get("exists") and "pan_file" not in selected_uploads:
        missing_required_docs.append("PAN document")
    if gst_registered and not (documents.get("gst_file") or {}).get("exists") and "gst_file" not in selected_uploads:
        missing_required_docs.append("GST Registration document")

    if missing_required_docs:
        flash("Please upload the missing required document(s): " + ", ".join(missing_required_docs) + ".", "danger")
        return redirect(url_for("modules.edit_ufc_admin_business_profile"))

    uploaded_docs = []
    for field, file_storage in selected_uploads.items():
        label = upload_fields[field]
        try:
            filename = save_file(file_storage, "ufc_profile_update")
        except ValueError as exc:
            flash(f"{label}: {exc}", "danger")
            return redirect(url_for("modules.edit_ufc_admin_business_profile"))
        uploaded_docs.append({
            "field": field,
            "label": label,
            "document_type": label,
            "filename": filename,
            "uploaded_at": now_utc(),
        })

    proposed_fields = {
        "name_of_enterprise": name_of_enterprise,
        "name_of_owner": name_of_owner,
        "owner_dob": owner_dob,
        "owner_age": owner_age,
        "state": state,
        "district": district,
        "block": block,
        "village": village,
        "address": address,
        "postal_code": postal_code,
        "email": email,
        "pan_number": pan_number,
        "gst_registered": bool(gst_registered),
        "gst_number": gst_number,
        "trader_license_number": trader_license_number,
        "other_licenses": other_licenses,
    }

    request_doc = {
        "user_id": user_id,
        "master_id": str(master.get("_id") or ""),
        "role": "ufc_admin",
        "request_type": "ufc_business_profile_update",
        "status": "pending",
        "proposed_fields": proposed_fields,
        "uploaded_docs": uploaded_docs,
        "requested_at": now_utc(),
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": "",
    }
    request_id = mongo.db.profile_update_requests.insert_one(request_doc).inserted_id

    mongo.db.validations.insert_one({
        "entity_id": str(request_id),
        "entity_type": "profile_update_request",
        "submitted_by": user_id,
        "submitted_role": "ufc_admin",
        "status": "pending",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "title": "UFC Centre Business Profile Update",
        "metadata": {
            "request_id": str(request_id),
            "user_id": user_id,
            "role": "ufc_admin",
            "centre_uid": user.get("centre_uid") or master.get("centre_uid") or "",
        },
    })

    flash(
        "Centre profile changes submitted for AVPL approval. Your currently approved business and tax details remain active until the request is approved.",
        "success",
    )
    return redirect(url_for("modules.profile"))

@modules_bp.route("/profile/update-request", methods=["POST"])
@login_required
def request_profile_update():
    is_app = bool(request.form.get("user_id")) or request.headers.get("Accept") == "application/json"

    print("PROFILE UPDATE is_app:", is_app)
    print("PROFILE UPDATE form:", dict(request.form))
    print("PROFILE UPDATE files:", request.files.keys())

    if is_app:
        user_id = request.form.get("user_id", "").strip()

        if not user_id:
            return jsonify({
                "ok": False,
                "message": "User ID is required."
            }), 400

        try:
            user = mongo.db.users.find_one({"_id": ObjectId(user_id)}) or {}
        except Exception:
            return jsonify({
                "ok": False,
                "message": "Invalid user ID."
            }), 400

        if not user:
            return jsonify({
                "ok": False,
                "message": "User not found."
            }), 404

        role = user.get("role")
    else:
        if not session.get("user_id"):
            return redirect(url_for("auth.login_select"))

        user_id = session.get("user_id")
        role = session.get("role")

    if role not in ["farmer", "ufc_mitra", "ufc_admin"]:
        message = "Profile update request is available only for Centre, Mitra and Farmer users."

        if is_app:
            return jsonify({
                "ok": False,
                "message": message
            }), 403

        flash(message, "danger")
        return redirect(url_for("modules.profile"))

    existing_pending = mongo.db.profile_update_requests.find_one({
        "user_id": str(user_id),
        "status": "pending"
    })

    if existing_pending:
        message = "You already have a profile update request pending for AVPL Admin approval."

        if is_app:
            return jsonify({
                "ok": False,
                "message": message
            }), 409

        flash(message, "warning")
        return redirect(url_for("modules.profile"))

    if role == "farmer":
        upload_map = {
        "profile_photo": "Passport Size Photo",
    }
    else:
            upload_map = {
            "profile_photo": "Passport Size Photo",
            "government_id_file": "Government ID / Identity Document",
            "supporting_document": "Supporting Document",
        }

    uploaded_docs = []

    for field, label in upload_map.items():
        file = request.files.get(field)

        if file and file.filename:
            if field == "profile_photo":
                allowed_image_types = {
                    "image/jpeg",
                    "image/png",
                    "image/jpg",
                    "image/webp"
                }

                file.seek(0, 2)
                file_size = file.tell()
                file.seek(0)

                file_ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

                allowed_extensions = {"jpg", "jpeg", "png", "webp"}

                if file.content_type not in allowed_image_types and file_ext not in allowed_extensions:
                    message = "Only JPG, PNG or WEBP files are allowed for profile photo."

                    if is_app:
                        return jsonify({
                            "ok": False,
                            "message": message
                        }), 400

                    flash(message, "danger")
                    return redirect(url_for("modules.profile"))

                if file_size > 2 * 1024 * 1024:
                    message = "Profile photo must be less than or equal to 2 MB."

                    if is_app:
                        return jsonify({
                            "ok": False,
                            "message": message
                        }), 400

                    flash(message, "danger")
                    return redirect(url_for("modules.profile"))

            try:
                filename = save_file(file, "profile_update")
            except ValueError as exc:
                message = str(exc)

                if is_app:
                    return jsonify({
                        "ok": False,
                        "message": message
                    }), 400

                flash(message, "danger")
                return redirect(url_for("modules.profile"))

            uploaded_docs.append({
                "field": field,
                "label": label,
                "filename": filename,
                "document_type": label,
                "uploaded_at": now_utc()
            })

    if not uploaded_docs:
        message = "Please upload at least one document or image to request an update."

        if is_app:
            return jsonify({
                "ok": False,
                "message": message
            }), 400

        flash(message, "danger")
        return redirect(url_for("modules.profile"))

    request_doc = {
        "user_id": str(user_id),
        "role": role,
        "status": "pending",
        "uploaded_docs": uploaded_docs,
        "requested_at": now_utc(),
        "reviewed_by": None,
        "reviewed_at": None,
        "rejection_reason": ""
    }

    request_id = mongo.db.profile_update_requests.insert_one(request_doc).inserted_id

    mongo.db.validations.insert_one({
        "entity_id": str(request_id),
        "entity_type": "profile_update_request",
        "submitted_by": str(user_id),
        "submitted_role": role,
        "status": "pending",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "title": "Profile / Document Update Request",
        "metadata": {
            "request_id": str(request_id),
            "user_id": str(user_id),
            "role": role
        }
    })

    message = "Profile update request sent for AVPL Admin approval."

    if is_app:
        return jsonify({
            "ok": True,
            "message": message,
            "approval_status": "pending",
            "request_id": str(request_id)
        }), 201

    flash(message, "success")
    return redirect(url_for("modules.profile"))

@modules_bp.route("/purchases")
@login_required
def purchases():
    q = request.args.get("q", "").strip()

    # Stage 7: internal UFC -> Farmer purchases are automatic and linked to
    # the delivered Farmer order. Preserve the legacy transaction reader for
    # other roles/backward-compatible app calls.
    if str(session.get("role") or "").strip().lower() == "farmer":
        try:
            overview = stage7_get_farmer_purchase_overview(
                session.get("user_id"),
                search=q,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            flash(str(exc), "danger")
            overview = {
                "rows": [],
                "centre_uid": "",
                "centre_name": "Mapped UFC",
                "farmer_name": "Farmer",
                "query": q,
                "summary": {"count": 0, "total_value": "0.00"},
            }
        # Stage 9: also show purchases the Farmer records from local/outside
        # sellers. Internal UFC purchases remain automatic and are never
        # re-entered manually.
        try:
            outside = stage9_get_external_purchase_rows(session.get("user_id"), search=q)
            outside_rows = outside.get("rows", [])
            overview.setdefault("rows", []).extend(outside_rows)
            try:
                current_total = float(overview.get("summary", {}).get("total_value", 0) or 0)
            except Exception:
                current_total = 0.0
            try:
                outside_total = float(outside.get("total", 0) or 0)
            except Exception:
                outside_total = 0.0
            overview.setdefault("summary", {})["count"] = len(overview.get("rows", []))
            overview["summary"]["total_value"] = f"{current_total + outside_total:.2f}"
        except (ValueError, PermissionError, RuntimeError) as exc:
            if not wants_json_response():
                flash(f"Outside purchases could not be loaded: {exc}", "warning")

        if wants_json_response():
            payload = {"ok": True, **overview}
            payload["items"] = overview.get("rows", [])
            return jsonify(json_safe(payload))
        return render_template("modules/farmer_purchases.html", overview=overview)

    user_id = (
        request.args.get("user_id")
        or session.get("user_id")
        or ""
    ).strip()

    if not user_id:
        if wants_json_response():
            return json_error("User ID missing. Please login again.", 401)

        flash("User ID missing. Please login again.", "danger")
        return redirect(url_for("auth.login_select"))

    try:
        user = mongo.db.users.find_one({"_id": ObjectId(user_id)}) or {}
    except Exception:
        user = {}

    if not user:
        if wants_json_response():
            return json_error("User not found. Please login again.", 404)

        flash("User not found. Please login again.", "danger")
        return redirect(url_for("dashboard.home"))

    farmer = (
        mongo.db.farmer_master.find_one({"linked_user_id": str(user_id)})
        or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(user_id)})
        or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
        or {}
    )

    farmer_phone = (
        farmer.get("contact_no")
        or farmer.get("phone")
        or user.get("phone")
        or ""
    )

    purchase_query = {
        "farmer_contact": farmer_phone,
        "transaction_type": "input_purchase"
    }

    if q:
        purchase_query["$or"] = [
            {"product_name": {"$regex": q, "$options": "i"}},
            {"farmer_contact": {"$regex": q, "$options": "i"}},
            {"transaction_type": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
        ]

        try:
            numeric_q = float(q)
            purchase_query["$or"].extend([
                {"amount": numeric_q},
                {"quantity": numeric_q},
                {"total_amount": numeric_q},
                {"unit_price": numeric_q},
            ])
        except ValueError:
            pass

    items = list(
        mongo.db.transactions
        .find(purchase_query)
        .sort("created_at", -1)
        .limit(20)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template("modules/purchases.html", items=items, q=q)


# UAT Fix 10 — stock-backed UFC POS.
@modules_bp.route('/pos', methods=['GET', 'POST'])
@login_required
@roles_required('ufc_admin')
def pos():
    if request.method == 'POST':
        sale_result = None
        try:
            sale_result = create_ufc_pos_sale(
                session.get('user_id'),
                session.get('centre_uid'),
                request.form.get('cart_json', '[]'),
                buyer_type=request.form.get('buyer_type', 'walk_in'),
                farmer_id=request.form.get('farmer_id', ''),
                buyer_name=request.form.get('buyer_name', ''),
                buyer_phone=request.form.get('buyer_phone', ''),
                buyer_address=request.form.get('buyer_address', ''),
                buyer_state=request.form.get('buyer_state', ''),
                buyer_gstin=request.form.get('buyer_gstin', ''),
                payment_term=request.form.get('payment_term', 'pay_now'),
                credit_days=request.form.get('credit_days', 0),
                sale_date=request.form.get('sale_date'),
                note=request.form.get('note', ''),
                idempotency_key=request.form.get('sale_token', ''),
                mitra_uid=request.form.get('mitra_uid', ''),
            )
            sale = sale_result.get('sale') or {}
            invoice = sale_result.get('invoice') or {}
            flash(sale_result.get('message') or 'POS sale completed.', 'success')

            # Money is intentionally a second step after the physical stock sale.
            # If payment entry fails, stock must never be deducted a second time.
            amount_received = str(request.form.get('amount_received') or '').strip()
            try:
                amount_value = float(amount_received or 0)
            except (TypeError, ValueError):
                amount_value = 0
            if amount_value > 0 and invoice.get('id'):
                try:
                    payment_result = stage8_record_payment(
                        session.get('user_id'),
                        'ufc_pos_invoice',
                        invoice.get('id'),
                        amount_received,
                        request.form.get('payment_mode') or 'cash',
                        reference=request.form.get('payment_reference', ''),
                        note='Collected at UFC POS checkout. ' + str(request.form.get('payment_note') or ''),
                        idempotency_key=request.form.get('payment_token', ''),
                    )
                    flash(payment_result.get('message') or 'POS payment recorded.', 'success')
                except (ValueError, PermissionError, RuntimeError) as exc:
                    flash('Sale and stock update are complete; only payment needs attention: ' + str(exc), 'warning')

            if wants_json_response():
                return jsonify(json_safe({'ok': True, **sale_result})), 201
            return redirect(url_for('modules.pos_invoice', sale_id=sale.get('id')))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            flash(str(exc), 'danger')

    try:
        context = get_ufc_pos_context(
            session.get('user_id'),
            session.get('centre_uid'),
            search=request.args.get('q', ''),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        context = {
            'seller': {}, 'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC', 'catalog': [], 'farmers': [],
            'buyer_types': {}, 'payment_terms': {}, 'sales': [],
            'sale_token': f'UFC-POS-{uuid4().hex.upper()}',
            'payment_token': f'UFC-POS-PAY-{uuid4().hex.upper()}',
            'today': business_today().isoformat(), 'query': request.args.get('q', ''),
            'summary': {'products': 0, 'input_products': 0, 'output_products': 0, 'outstanding': '0.00'},
        }
    return render_template('modules/pos.html', **context)


@modules_bp.route("/all-orders")
@login_required
@roles_required("avpl_admin")
def all_orders():
    q = request.args.get("q", "").strip()

    orders = list(mongo.db.orders.find({}).sort("created_at", -1))

    if q:
        q_lower = q.lower()
        orders = [
            o for o in orders
            if q_lower in str(o.get("farmer_name", "")).lower()
            or q_lower in str(o.get("product_name", "")).lower()
            or q_lower in str(o.get("quantity", "")).lower()
            or q_lower in str(o.get("total_amount", "")).lower()
            or q_lower in str(o.get("status", "")).lower()
            or q_lower in str(o.get("created_at", "")).lower()
        ]

    return render_template("modules/all_orders.html", orders=orders)

@modules_bp.route('/pos/invoice/<sale_id>')
@login_required
@roles_required('ufc_admin', 'avpl_admin', 'sales_unnatfarm', 'accounts', 'super_admin')
def pos_invoice(sale_id):
    try:
        context = get_pos_sale_context(session.get('user_id'), sale_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        if session.get('role') in ['avpl_admin', 'super_admin', 'sales_unnatfarm', 'accounts']:
            return redirect(url_for('modules.sales_details'))
        return redirect(url_for('modules.pos'))
    if wants_json_response():
        return jsonify(json_safe({'ok': True, **context}))
    return render_template('modules/pos_invoice.html', **context)


@modules_bp.route('/pos/<sale_id>/payment', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def pos_record_payment(sale_id):
    try:
        context = get_pos_sale_context(session.get('user_id'), sale_id)
        invoice = context.get('invoice') or {}
        if not invoice.get('id'):
            raise ValueError('This POS invoice is not ready for payment.')
        result = stage8_record_payment(
            session.get('user_id'), 'ufc_pos_invoice', invoice.get('id'),
            request.form.get('amount'), request.form.get('payment_mode'),
            reference=request.form.get('reference', ''), note=request.form.get('note', ''),
            idempotency_key=request.form.get('payment_token', ''),
        )
        flash(result.get('message') or 'POS payment recorded.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.pos_invoice', sale_id=sale_id))


@modules_bp.route('/pos/<sale_id>/payments/<payment_id>/reverse', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def pos_reverse_payment(sale_id, payment_id):
    try:
        context = get_pos_sale_context(session.get('user_id'), sale_id)
        if not any(str(row.get('id')) == str(payment_id) for row in context.get('payments', [])):
            raise ValueError('This payment is not linked to the selected POS sale.')
        result = stage8_reverse_payment(session.get('user_id'), payment_id, request.form.get('reason', ''))
        flash(result.get('message') or 'POS payment reversed.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.pos_invoice', sale_id=sale_id))


@modules_bp.route('/pos/<sale_id>/void', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def pos_void_sale(sale_id):
    try:
        result = void_pos_sale(session.get('user_id'), sale_id, request.form.get('reason', ''))
        flash(result.get('message') or 'POS sale voided.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.pos_invoice', sale_id=sale_id))


@modules_bp.route("/mitra-earnings")
@login_required
@roles_required("ufc_mitra")
def mitra_earnings():
    mitra_uid = str(session.get("mitra_uid") or "").strip()
    if not mitra_uid:
        user_id = session.get("user_id")
        try:
            oid = ObjectId(str(user_id))
        except Exception:
            oid = None
        user = mongo.db.users.find_one({"_id": oid}) if oid else {}
        mitra_uid = str((user or {}).get("mitra_uid") or (user or {}).get("mapped_mitra_uid") or "").strip()

    try:
        overview = get_mitra_earning_overview(
            mitra_uid,
            period=request.args.get("period", "this_month"),
            q=request.args.get("q", ""),
            page=request.args.get("page", 1),
            per_page=request.args.get("per_page", 25),
        )
    except (ValueError, RuntimeError) as exc:
        if wants_json_response():
            return jsonify({"ok": False, "message": str(exc)}), 400
        flash(str(exc), "danger")
        overview = {
            "profile": {"mitra_uid": mitra_uid, "name": "Mitra", "farmer_count": 0},
            "current_rate": 0, "current_rate_display": "0", "current_rate_source": "Unavailable",
            "period": request.args.get("period", "this_month"), "q": request.args.get("q", ""),
            "periods": [("this_month", "This Month"), ("today", "Today"), ("this_year", "This Year"), ("all", "All Time")],
            "rows": [],
            "summary": {"month_business": 0, "month_earning": 0, "lifetime_business": 0, "lifetime_input_earning": 0, "legacy_other_earning": 0, "lifetime_total_earning": 0, "farmer_count": 0},
            "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
        }

    if wants_json_response():
        summary = overview.get("summary") or {}
        return jsonify(json_safe({
            "ok": True,
            "overview": overview,
            # Compatibility aliases retained for older clients. New clients
            # should prefer the structured `overview` payload.
            "mitra_uid": (overview.get("profile") or {}).get("mitra_uid") or mitra_uid,
            "current_month_earning": summary.get("month_earning", 0),
            "total_earning": summary.get("lifetime_total_earning", 0),
            "monthly_avpl_earning": summary.get("month_earning", 0),
            "total_avpl_earning": summary.get("lifetime_input_earning", 0),
            "monthly_farmer_earning": 0,
            "total_farmer_earning": summary.get("legacy_other_earning", 0),
            "recent_sales": overview.get("rows", []),
            "q": overview.get("q", ""),
        }))
    return render_template("modules/mitra_earnings.html", overview=overview)


@modules_bp.route("/finance/leads")
@login_required
@roles_required("avpl_admin", "sales_nelocals", "sales_unnatfarm", "accounts", "ufc_mitra")
def finance_leads():
    role = str(session.get("role") or "").strip().lower()
    q = request.args.get("q", "").strip()

    if role == "sales_unnatfarm":
        try:
            overview = get_sales_finance_leads(
                session.get("user_id"),
                q=q,
                followup=request.args.get("followup", "all"),
                page=request.args.get("page", 1),
                per_page=request.args.get("per_page", 25),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Sales finance leads unavailable: %s", exc)
            overview = {
                "rows": [], "q": q, "selected_followup": "all",
                "followup_statuses": [("all", "All Follow-up")],
                "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                "summary": {"total": 0, "open": 0, "new": 0, "follow_up": 0, "forwarded": 0},
                "notice": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({
                "ok": True, "overview": overview,
                "items": overview.get("rows") or [],
                "leads": overview.get("rows") or [], "q": q,
            }))
        return render_template("modules/sales_finance_leads.html", overview=overview)

    if role == "ufc_mitra":
        leads = list(mongo.db.financial_assistance_leads.find({
            "mitra_uid": session.get("mitra_uid")
        }).sort("created_at", -1))
    elif role == "accounts":
        leads = list(mongo.db.financial_assistance_leads.find({}).sort("created_at", -1))
    else:
        leads = list(mongo.db.financial_assistance_leads.find({
            "visible_to_roles": role
        }).sort("created_at", -1))

    if q:
        q_lower = q.lower()
        leads = [
            lead for lead in leads
            if q_lower in str(lead.get("created_at", "")).lower()
            or q_lower in str(lead.get("farmer_name", "")).lower()
            or q_lower in str(lead.get("farmer_mobile", "")).lower()
            or q_lower in str(lead.get("farmer_address", "")).lower()
            or q_lower in str(lead.get("centre_uid", "")).lower()
            or q_lower in str(lead.get("amount", "")).lower()
            or q_lower in str(lead.get("purpose", "")).lower()
            or q_lower in str(lead.get("total_transaction", "")).lower()
            or q_lower in str(lead.get("status", "")).lower()
            or q_lower in str(lead.get("mitra_uid", "")).lower()
        ]

    if wants_json_response():
        return jsonify(json_safe({"ok": True, "items": leads, "leads": leads, "q": q}))
    return render_template("modules/finance_leads.html", leads=leads)


@modules_bp.route("/finance/leads/<lead_id>/sales-follow-up", methods=["POST"])
@login_required
@roles_required("sales_unnatfarm")
def sales_finance_lead_follow_up(lead_id):
    try:
        result = update_sales_finance_lead_followup(
            session.get("user_id"),
            lead_id,
            followup_status=request.form.get("followup_status", ""),
            note=request.form.get("note", ""),
        )
        if wants_json_response():
            return jsonify(json_safe({"ok": True, **result}))
        flash(result.get("message") or "Finance lead follow-up updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), "danger")
    return redirect(url_for(
        "modules.finance_leads",
        q=request.args.get("q", ""),
        followup=request.args.get("followup", "all"),
    ))


@modules_bp.route("/sales-details")
@login_required
@roles_required("super_admin", "avpl_admin", "sales_unnatfarm", "accounts")
def sales_details():
    role = str(session.get("role") or "").strip().lower()
    q = request.args.get("q", "").strip()

    # Accounts/AVPL keep their finance-focused purchase-vs-sales summary.
    # Sales UnnatFarm uses its own read-only, cross-channel sales workspace.
    if role in {"accounts", "avpl_admin", "super_admin"}:
        try:
            overview = get_purchase_sales_summary(
                session.get("user_id"),
                query_text=q,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Accounts purchase/sales summary unavailable: %s", exc)
            overview = {
                "query": q,
                "summary": {
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
                },
                "supplier_rows": [],
                "ufc_rows": [],
                "setup_required": True,
                "setup_message": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({"ok": True, "overview": overview}))
        return render_template("modules/accounts_purchase_sales_summary.html", overview=overview)

    if role == "sales_unnatfarm":
        try:
            overview = get_sales_unnatfarm_overview(
                session.get("user_id"),
                period=request.args.get("period", "this_month"),
                channel=request.args.get("channel", "all"),
                payment_status=request.args.get("payment", "all"),
                q=q,
                page=request.args.get("page", 1),
                per_page=request.args.get("per_page", 25),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Sales UnnatFarm overview unavailable: %s", exc)
            overview = {
                "rows": [], "summary": {"sales_count": 0, "sales_value": 0, "received": 0, "outstanding": 0, "receipt_adjustments": 0},
                "channels": [("all", "All Sales")], "periods": [("this_month", "This Month")],
                "payment_statuses": [("all", "All Payments")],
                "selected_period": request.args.get("period", "this_month"),
                "selected_channel": request.args.get("channel", "all"),
                "selected_payment": request.args.get("payment", "all"), "q": q,
                "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
                "notice": str(exc),
            }
        if wants_json_response():
            return jsonify(json_safe({
                "ok": True, "overview": overview,
                "sales": overview.get("rows") or [], "q": q,
            }))
        return render_template("modules/sales_details.html", overview=overview)

    return render_template("modules/sales_details.html", overview={
        "rows": [], "summary": {"sales_count": 0, "sales_value": 0, "received": 0, "outstanding": 0, "receipt_adjustments": 0},
        "channels": [("all", "All Sales")], "periods": [("this_month", "This Month")],
        "payment_statuses": [("all", "All Payments")], "selected_period": "this_month",
        "selected_channel": "all", "selected_payment": "all", "q": q,
        "pagination": {"page": 1, "per_page": 25, "total": 0, "total_pages": 1, "has_prev": False, "has_next": False},
    })
   
@modules_bp.route("/notifications")
@login_required
def notifications():
    q = request.args.get("q", "").strip()

    user_id = (
        request.args.get("user_id")
        or session.get("user_id")
        or ""
    ).strip()

    if not user_id:
        if wants_json_response():
            return json_error("User ID missing. Please login again.", 401)

        flash("User ID missing. Please login again.", "danger")
        return redirect(url_for("auth.login_select"))

    notification_query = {
        "to_user_id": str(user_id)
    }

    if q:
        notification_query["$or"] = [
            {"title": {"$regex": q, "$options": "i"}},
            {"message": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}}
        ]

    items = list(
        mongo.db.notifications.find(notification_query).sort("created_at", -1)
    )

    mongo.db.notifications.update_many(
        {
            "to_user_id": str(user_id),
            "status": "unread"
        },
        {
            "$set": {
                "status": "read",
                "read_at": now_utc()
            }
        }
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template("modules/notifications.html", items=items, q=q)

@modules_bp.route("/mitra-stock")
@login_required
@roles_required("ufc_mitra")
def mitra_stock():
    q = request.args.get("q", "").strip()

    stock_query = {
        "mitra_uid": session.get("mitra_uid")
    }

    if q:
        stock_query["$or"] = [
            {"product_name": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}}
        ]

        try:
            numeric_q = float(q)
            stock_query["$or"].append({"available_quantity": numeric_q})
        except ValueError:
            pass

    items = list(mongo.db.mitra_product_stock.find(stock_query).sort("created_at", -1))

    for item in items:
        item["low_stock"] = float(item.get("available_quantity") or 0) < 5

    if q and q.lower() in ["low", "low stock", "lowstock"]:
        items = [item for item in items if item.get("low_stock")]

    if q and q.lower() in ["available", "in stock", "stock"]:
        items = [item for item in items if not item.get("low_stock")]

    summary = {
        "product_lines": len(items),
        "low_stock_lines": sum(1 for item in items if item.get("low_stock")),
    }

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "summary": summary,
            "q": q
        }))

    return render_template(
        "modules/mitra_stock.html",
        items=items,
        summary=summary,
        q=q
    )

# ---------------------------------------------------------------------------
# Stage 7 — Farmer -> UFC Orders, Reservation, Delivery and linked sales
# ---------------------------------------------------------------------------


@modules_bp.route("/centre-orders")
@login_required
@roles_required("ufc_admin")
def centre_orders():
    try:
        overview = stage7_get_ufc_order_overview(
            session.get("user_id"),
            session.get("centre_uid"),
            search=request.args.get("q", ""),
            status_filter=request.args.get("status", "all"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), "danger")
        overview = {
            "rows": [],
            "centre_uid": session.get("centre_uid") or "",
            "centre_name": session.get("centre_uid") or "UFC",
            "query": request.args.get("q", ""),
            "selected_status": request.args.get("status", "all"),
            "summary": {"total": 0, "requested": 0, "approved": 0, "rejected": 0, "cancelled": 0, "delivered": 0},
        }
    if wants_json_response():
        payload = {"ok": True, **overview}
        payload["orders"] = overview.get("rows", [])
        return jsonify(json_safe(payload))
    return render_template("modules/centre_orders.html", overview=overview)


@modules_bp.route("/centre-orders/<order_id>")
@login_required
@roles_required("ufc_admin")
def centre_order_detail(order_id):
    try:
        order = stage7_get_order(
            order_id,
            actor_user_id=session.get("user_id"),
            centre_uid=session.get("centre_uid"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.centre_orders"))
    return render_template("modules/ufc_farmer_order_detail.html", order=order)


@modules_bp.route("/centre-orders/<order_id>/approve", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def approve_farmer_order_view(order_id):
    try:
        current = stage7_get_order(order_id, actor_user_id=session.get("user_id"), centre_uid=session.get("centre_uid"))
        if current.get("is_multi_item_order"):
            approvals = []
            for line in current.get("items", []):
                line_id = str(line.get("line_id") or "")
                approvals.append({"line_id": line_id, "approved_quantity": request.form.get(f"approved_quantity_{line_id}", "0")})
            result = stage7_approve_farmer_cart_order(
                session.get("user_id"), session.get("centre_uid"), order_id, approvals,
                note=request.form.get("note", ""), payment_due_days=request.form.get("payment_due_days"),
            )
        else:
            result = stage7_approve_farmer_order(
                session.get("user_id"),
                session.get("centre_uid"),
                order_id,
                request.form.get("approved_quantity"),
                note=request.form.get("note", ""),
                payment_due_days=request.form.get("payment_due_days"),
            )
        flash(result.get("message") or "Farmer order approved.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.centre_order_detail", order_id=order_id))


@modules_bp.route("/centre-orders/<order_id>/reject", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def reject_farmer_order_view(order_id):
    try:
        result = stage7_reject_farmer_order(
            session.get("user_id"),
            session.get("centre_uid"),
            order_id,
            reason=request.form.get("reason", ""),
        )
        flash(result.get("message") or "Farmer order rejected.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.centre_order_detail", order_id=order_id))


@modules_bp.route("/centre-orders/<order_id>/cancel", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def cancel_farmer_order_view(order_id):
    try:
        result = stage7_cancel_farmer_order(
            session.get("user_id"),
            order_id,
            centre_uid_hint=session.get("centre_uid"),
            reason=request.form.get("reason", ""),
        )
        flash(result.get("message") or "Farmer order cancelled.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.centre_order_detail", order_id=order_id))


@modules_bp.route("/centre-orders/<order_id>/deliver", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def deliver_farmer_order_view(order_id):
    try:
        result = stage7_deliver_farmer_order(
            session.get("user_id"),
            session.get("centre_uid"),
            order_id,
            delivery_note=request.form.get("delivery_note", ""),
        )
        warning = result.get("financial_warning")
        if warning:
            flash(
                "Dispatch succeeded. Sales documents will be created after Farmer receipt. " + warning,
                "warning",
            )
        else:
            flash(
                result.get("message")
                or "Order dispatched. Farmer will confirm actual receipt before payment.",
                "success",
            )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.centre_order_detail", order_id=order_id))


@modules_bp.route("/centre-orders/<order_id>/repair-financials", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def repair_farmer_order_financials(order_id):
    try:
        result = stage7_ensure_delivery_documents(session.get("user_id"), order_id)
        flash(result.get("message") or "Sales documents repaired.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.centre_order_detail", order_id=order_id))


@modules_bp.route("/farmer/order", methods=["POST"])
@modules_bp.route("/farmer-marketplace/order", methods=["POST"])
@login_required
@roles_required("farmer")
def place_farmer_order():
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        result = stage7_create_farmer_order(
            session.get("user_id"),
            payload.get("product_id") or request.form.get("product_id"),
            payload.get("quantity") or request.form.get("quantity"),
            request_token=payload.get("request_token") or request.form.get("request_token", ""),
            note=payload.get("note") or request.form.get("note", ""),
            payment_term=payload.get("payment_term") or request.form.get("payment_term", "cod"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_marketplace"))

    if wants_json_response():
        return jsonify(json_safe({"ok": True, **result})), 201
    flash(result.get("message") or "Order placed successfully.", "success")
    return redirect(url_for("modules.farmer_my_orders"))


@modules_bp.route("/farmer-marketplace/cart", methods=["POST"])
@login_required
@roles_required("farmer")
def place_farmer_cart_order():
    try:
        raw = request.form.get("cart_json", "[]")
        items = json.loads(raw) if raw else []
        result = stage7_create_farmer_cart_order(
            session.get("user_id"),
            items,
            request_token=request.form.get("request_token", ""),
            note=request.form.get("note", ""),
            payment_term=request.form.get("payment_term", "cod"),
        )
        order = result.get("order") or {}
        flash(result.get("message") or "Cart order placed.", "success")
        if order.get("id"):
            return redirect(url_for("modules.farmer_order_detail", order_id=order.get("id")))
        return redirect(url_for("modules.farmer_my_orders"))
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_marketplace"))


@modules_bp.route("/ufc-farmer-sales")
@login_required
@roles_required("ufc_admin")
def ufc_farmer_sales():
    try:
        overview = stage7_get_ufc_sales_overview(
            session.get("user_id"),
            session.get("centre_uid"),
            search=request.args.get("q", ""),
            payment_status=request.args.get("payment", "all"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {
            "rows": [],
            "centre_uid": session.get("centre_uid") or "",
            "centre_name": session.get("centre_uid") or "UFC",
            "query": request.args.get("q", ""),
            "selected_payment": request.args.get("payment", "all"),
            "summary": {"count": 0, "total_sales": "0.00", "outstanding": "0.00"},
        }
    return render_template("modules/ufc_farmer_sales.html", overview=overview)


@modules_bp.route("/farmer-sales-invoices/<invoice_id>/print")
@login_required
def farmer_sales_invoice_print(invoice_id):
    try:
        context = stage7_get_invoice_print_context(
            invoice_id,
            actor_user_id=session.get("user_id"),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        role = str(session.get("role") or "").strip().lower()
        if role == "ufc_admin":
            return redirect(url_for("modules.ufc_farmer_sales"))
        return redirect(url_for("modules.farmer_my_orders"))
    invoice_obj = context.get("invoice") if isinstance(context, dict) else None
    raw_lines = invoice_obj.get("items") if isinstance(invoice_obj, dict) else []
    context["invoice_lines"] = [dict(line) for line in raw_lines if isinstance(line, dict)] if isinstance(raw_lines, list) else []
    return render_template("modules/ufc_farmer_invoice_print.html", **context)


# ---------------------------------------------------------------------------
# Stage 8 — Unified UFC/Farmer payments, receipts and settlement
# ---------------------------------------------------------------------------


@modules_bp.route("/payments")
@login_required
@roles_required("ufc_admin")
def ufc_payments():
    try:
        overview = get_ufc_payment_overview(session.get("user_id"))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"centre_uid": session.get("centre_uid") or "", "avpl_payables": [], "pending_avpl_payments": [], "farmer_receivables": [], "pending_farmer_payments": [], "farmer_produce_payables": [], "recent_payments": [], "payment_modes": {}, "summary": {"avpl_due": "0.00", "avpl_pending_confirmation": "0.00", "avpl_pending_count": 0, "farmer_due": "0.00", "farmer_pending_confirmation": "0.00", "farmer_pending_count": 0, "farmer_produce_due": "0.00", "recent_count": 0}}
    return render_template("modules/ufc_payments.html", overview=overview)


@modules_bp.route("/payments/refresh-tax-documents", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def refresh_ufc_tax_documents():
    try:
        result = stage8_refresh_ufc_farmer_tax_documents(
            session.get("user_id"),
            session.get("centre_uid"),
        )
        category = "warning" if result.get("errors") else "success"
        flash(result.get("message") or "HSN/GST documents refreshed.", category)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.ufc_payments"))


@modules_bp.route("/payments/record", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def record_ufc_payment():
    return_order_id = str(request.form.get("return_order_id") or "").strip()
    try:
        result = stage8_record_payment(
            session.get("user_id"),
            request.form.get("source_type"),
            request.form.get("invoice_id"),
            request.form.get("amount"),
            request.form.get("payment_mode"),
            reference=request.form.get("reference", ""),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")

    if return_order_id and str(request.form.get("source_type") or "").strip().lower() == "avpl_ufc_invoice":
        return redirect(url_for("modules.ufc_avpl_order_detail", order_id=return_order_id))
    return redirect(url_for("modules.ufc_payments"))


@modules_bp.route("/payments/farmer-reports/<payment_id>/confirm", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def confirm_farmer_reported_payment(payment_id):
    try:
        result = stage8_confirm_reported_payment(session.get("user_id"), payment_id)
        flash(result.get("message") or "Farmer payment confirmed received.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.ufc_payments"))


@modules_bp.route("/payments/farmer-reports/<payment_id>/return", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def return_farmer_reported_payment(payment_id):
    try:
        result = stage8_reject_reported_payment(
            session.get("user_id"),
            payment_id,
            request.form.get("reason", ""),
        )
        flash(result.get("message") or "Farmer payment report returned.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.ufc_payments"))


@modules_bp.route("/payments/<payment_id>/reverse", methods=["POST"])
@login_required
@roles_required("ufc_admin")
def reverse_ufc_payment(payment_id):
    try:
        result = stage8_reverse_payment(session.get("user_id"), payment_id, request.form.get("reason", ""))
        flash(result.get("message") or "Payment reversed.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.ufc_payments"))


@modules_bp.route("/my-payments")
@login_required
@roles_required("farmer")
def farmer_payments():
    try:
        overview = get_farmer_payment_overview(session.get("user_id"))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"payables": [], "receivables": [], "pending_ufc_payments": [], "recent_payments": [], "payment_modes": {}, "summary": {"outstanding": "0.00", "payable_outstanding": "0.00", "ufc_pending_confirmation": "0.00", "ufc_pending_count": 0, "receivable_outstanding": "0.00", "recent_count": 0}}
    return render_template("modules/farmer_payments.html", overview=overview)


@modules_bp.route("/my-payments/ufc/report", methods=["POST"])
@login_required
@roles_required("farmer")
def report_farmer_ufc_payment():
    try:
        result = stage8_record_payment(
            session.get("user_id"),
            "ufc_farmer_invoice",
            request.form.get("invoice_id"),
            request.form.get("amount"),
            request.form.get("payment_mode"),
            reference=request.form.get("reference", ""),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Payment reported to your UFC.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_payments"))


@modules_bp.route("/payment-receipts/<payment_id>/print")
@login_required
def payment_receipt_print(payment_id):
    try:
        context = get_payment_receipt_context(session.get("user_id"), payment_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        role = str(session.get("role") or "").strip().lower()
        if role == "ufc_admin":
            return redirect(url_for("modules.ufc_payments"))
        if role == "farmer":
            return redirect(url_for("modules.farmer_payments"))
        return redirect(url_for("dashboard.home"))
    return render_template("modules/payment_receipt_print.html", **context)


# ---------------------------------------------------------------------------
# Stage 9 — Farmer Production, Produce Stock & External Sales
# ---------------------------------------------------------------------------


@modules_bp.route("/farmer-purchases/outside/new", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_external_purchase_new():
    if request.method == "POST":
        purchase_succeeded = False
        result = None
        try:
            result = stage9_create_external_purchase(
                session.get("user_id"),
                request.form.get("seller_name"),
                request.form.get("product_name"),
                request.form.get("quantity"),
                request.form.get("unit_code"),
                request.form.get("total_amount"),
                purchase_date=request.form.get("purchase_date"),
                bill_number=request.form.get("bill_number", ""),
                payment_term=request.form.get("payment_term", "pay_now"),
                credit_days=request.form.get("credit_days", 0),
                note=request.form.get("note", ""),
                idempotency_key=request.form.get("purchase_token", ""),
            )
            purchase_succeeded = True
            invoice = result.get("invoice") or {}
            flash(result.get("message") or "Outside purchase saved.", "success")

            payment_option = str(request.form.get("payment_option") or "none").strip().lower()
            if payment_option in {"full", "partial"} and invoice.get("id"):
                amount_to_record = invoice.get("outstanding_display") if payment_option == "full" else request.form.get("amount_paid")
                payment_result = stage8_record_payment(
                    session.get("user_id"),
                    "farmer_external_purchase_invoice",
                    invoice.get("id"),
                    amount_to_record,
                    request.form.get("payment_mode") or "cash",
                    reference=request.form.get("payment_reference", ""),
                    note="Payment recorded with outside purchase. " + str(request.form.get("payment_note") or ""),
                    idempotency_key=request.form.get("payment_token", ""),
                )
                flash(payment_result.get("message") or "Purchase payment recorded.", "success")

            if wants_json_response():
                return jsonify(json_safe({"ok": True, **result})), 201
            return redirect(url_for("modules.purchases"))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            if purchase_succeeded:
                flash(f"Purchase was saved, but payment needs attention: {exc}", "warning")
                return redirect(url_for("modules.purchases"))
            flash(str(exc), "danger")

    try:
        context = stage9_get_external_purchase_form_context(session.get("user_id"))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        context = {
            "farmer": {}, "unit_choices": {}, "today": business_today().isoformat(),
            "purchase_token": f"FPUR-{uuid4().hex.upper()}", "payment_token": f"FPAY-{uuid4().hex.upper()}",
        }
    return render_template("modules/farmer_external_purchase_form.html", **context)


@modules_bp.route("/farmer-purchases/outside/<invoice_id>/print")
@login_required
@roles_required("farmer")
def farmer_external_purchase_print(invoice_id):
    try:
        context = stage9_get_external_purchase_print_context(session.get("user_id"), invoice_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.purchases"))
    return render_template("modules/farmer_external_purchase_print.html", **context)


@modules_bp.route("/my-payments/record-outside-purchase-payment", methods=["POST"])
@login_required
@roles_required("farmer")
def record_farmer_outside_purchase_payment():
    try:
        result = stage8_record_payment(
            session.get("user_id"),
            "farmer_external_purchase_invoice",
            request.form.get("invoice_id"),
            request.form.get("amount"),
            request.form.get("payment_mode"),
            reference=request.form.get("reference", ""),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Purchase payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_payments"))


@modules_bp.route("/farmer-production", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_production():
    if request.method == "POST":
        try:
            result = stage9_record_production(
                session.get("user_id"),
                request.form.get("product_name"),
                request.form.get("quantity"),
                request.form.get("unit_code"),
                harvest_date=request.form.get("harvest_date"),
                variety=request.form.get("variety", ""),
                grade=request.form.get("grade", ""),
                estimated_cost=request.form.get("estimated_cost", 0),
                notes=request.form.get("notes", ""),
                idempotency_key=request.form.get("production_token", ""),
            )
            if wants_json_response():
                return jsonify(json_safe({"ok": True, **result})), 201
            flash(result.get("message") or "Production added to My Produce Stock.", "success")
            return redirect(url_for("modules.farmer_produce_stock"))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            flash(str(exc), "danger")

    try:
        overview = stage9_get_production_overview(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {
            "farmer": {}, "product_choices": [], "unit_choices": {}, "expense_categories": {},
            "productions": [], "stock_rows": [], "expenses": [], "query": "", "today": business_today().isoformat(),
            "production_token": f"PROD-{uuid4().hex.upper()}", "expense_token": f"EXP-{uuid4().hex.upper()}",
            "summary": {"production_batches": 0, "products_in_stock": 0, "sales_value": "0.00", "cash_received": "0.00", "input_cost": "0.00", "other_expenses": "0.00", "estimated_balance": "0.00", "estimated_balance_raw": 0},
        }
    if wants_json_response():
        return jsonify(json_safe({"ok": True, **overview}))
    return render_template("modules/farmer_production.html", overview=overview)


@modules_bp.route("/farmer-production/expenses", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_production_expense():
    try:
        result = stage9_record_expense(
            session.get("user_id"),
            request.form.get("category"),
            request.form.get("amount"),
            expense_date=request.form.get("expense_date"),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("expense_token", ""),
        )
        flash(result.get("message") or "Expense saved.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_production"))


@modules_bp.route("/farmer-produce-stock")
@login_required
@roles_required("farmer")
def farmer_produce_stock():
    try:
        overview = stage9_get_stock_overview(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"farmer": {}, "rows": [], "movements": [], "query": "", "loss_token": f"LOSS-{uuid4().hex.upper()}", "adjustment_token": f"ADJ-{uuid4().hex.upper()}"}
    if wants_json_response():
        return jsonify(json_safe({"ok": True, **overview}))
    return render_template("modules/farmer_produce_stock.html", overview=overview)


@modules_bp.route("/farmer-produce-stock/adjust", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_produce_stock_adjust():
    try:
        result = stage9_record_stock_adjustment(
            session.get("user_id"),
            request.form.get("product_key"),
            request.form.get("direction"),
            request.form.get("quantity"),
            reason=request.form.get("reason", "stock_count"),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("adjustment_token", ""),
        )
        flash(result.get("message") or "Produce stock updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_stock"))


@modules_bp.route("/farmer-produce-stock/loss", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_produce_stock_loss():
    try:
        result = stage9_record_stock_loss(
            session.get("user_id"),
            request.form.get("product_key"),
            request.form.get("quantity"),
            reason=request.form.get("reason", "wastage"),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("loss_token", ""),
        )
        flash(result.get("message") or "Produce stock updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_stock"))


@modules_bp.route("/farmer-pos", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_pos():
    if request.method == "POST":
        sale_result = None
        try:
            sale_result = create_farmer_pos_sale(
                session.get("user_id"),
                request.form.get("cart_json", "[]"),
                buyer_type=request.form.get("buyer_type", "local_buyer"),
                buyer_name=request.form.get("buyer_name", ""),
                buyer_phone=request.form.get("buyer_phone", ""),
                buyer_address=request.form.get("buyer_address", ""),
                buyer_state=request.form.get("buyer_state", ""),
                payment_term=request.form.get("payment_term", "pay_now"),
                credit_days=request.form.get("credit_days", 0),
                sale_date=request.form.get("sale_date"),
                note=request.form.get("note", ""),
                idempotency_key=request.form.get("sale_token", ""),
            )
            sale = sale_result.get("sale") or {}
            invoice = sale_result.get("invoice") or {}
            flash(sale_result.get("message") or "Farmer POS sale completed.", "success")

            amount_received = str(request.form.get("amount_received") or "").strip()
            try:
                amount_value = float(amount_received or 0)
            except (TypeError, ValueError):
                amount_value = 0
            if amount_value > 0 and invoice.get("id"):
                try:
                    payment_result = stage8_record_payment(
                        session.get("user_id"), "farmer_pos_invoice", invoice.get("id"),
                        amount_received, request.form.get("payment_mode") or "cash",
                        reference=request.form.get("payment_reference", ""),
                        note="Collected at Farmer POS checkout. " + str(request.form.get("payment_note") or ""),
                        idempotency_key=request.form.get("payment_token", ""),
                    )
                    flash(payment_result.get("message") or "Buyer payment recorded.", "success")
                except (ValueError, PermissionError, RuntimeError) as exc:
                    flash("Sale and produce stock update are complete; only payment needs attention: " + str(exc), "warning")

            if wants_json_response():
                return jsonify(json_safe({"ok": True, **sale_result})), 201
            return redirect(url_for("modules.farmer_pos_invoice", sale_id=sale.get("id")))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            flash(str(exc), "danger")

    try:
        context = get_farmer_pos_context(session.get("user_id"), search=request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        context = {
            "farmer": {}, "catalog": [], "mapped_ufc": {}, "buyer_types": {}, "payment_terms": {}, "sales": [],
            "sale_token": f"FARMER-POS-{uuid4().hex.upper()}",
            "payment_token": f"FARMER-POS-PAY-{uuid4().hex.upper()}",
            "today": business_today().isoformat(), "query": request.args.get("q", ""),
            "summary": {"products": 0, "outstanding": "0.00"},
        }
    return render_template("modules/farmer_sale_form.html", **context)


@modules_bp.route("/farmer-pos/invoice/<sale_id>")
@login_required
@roles_required("farmer")
def farmer_pos_invoice(sale_id):
    try:
        context = get_pos_sale_context(session.get("user_id"), sale_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_pos"))
    return render_template("modules/farmer_pos_invoice.html", **context)


@modules_bp.route("/farmer-pos/<sale_id>/payment", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_pos_record_payment(sale_id):
    try:
        context = get_pos_sale_context(session.get("user_id"), sale_id)
        invoice = context.get("invoice") or {}
        if not invoice.get("id"):
            raise ValueError("This Farmer POS receipt is not ready for payment.")
        result = stage8_record_payment(
            session.get("user_id"), "farmer_pos_invoice", invoice.get("id"),
            request.form.get("amount"), request.form.get("payment_mode"),
            reference=request.form.get("reference", ""), note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Buyer payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_pos_invoice", sale_id=sale_id))


@modules_bp.route("/farmer-pos/<sale_id>/payments/<payment_id>/reverse", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_pos_reverse_payment(sale_id, payment_id):
    try:
        context = get_pos_sale_context(session.get("user_id"), sale_id)
        if not any(str(row.get("id")) == str(payment_id) for row in context.get("payments", [])):
            raise ValueError("This payment is not linked to the selected Farmer POS sale.")
        result = stage8_reverse_payment(session.get("user_id"), payment_id, request.form.get("reason", ""))
        flash(result.get("message") or "Buyer payment reversed.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_pos_invoice", sale_id=sale_id))


@modules_bp.route("/farmer-pos/<sale_id>/void", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_pos_void_sale(sale_id):
    try:
        result = void_pos_sale(session.get("user_id"), sale_id, request.form.get("reason", ""))
        flash(result.get("message") or "Farmer POS sale voided.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_pos_invoice", sale_id=sale_id))


# Legacy single-item outside-sale route retained for old links/history.
@modules_bp.route("/farmer-sales/new", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_sales_new():
    if request.method == "POST":
        sale_succeeded = False
        result = None
        try:
            result = stage9_create_external_sale(
                session.get("user_id"),
                request.form.get("product_key"),
                request.form.get("quantity"),
                request.form.get("unit_price"),
                buyer_type=request.form.get("buyer_type", "local_buyer"),
                buyer_name=request.form.get("buyer_name", ""),
                buyer_phone=request.form.get("buyer_phone", ""),
                buyer_address=request.form.get("buyer_address", ""),
                sale_date=request.form.get("sale_date"),
                payment_term=request.form.get("payment_term", "pay_now"),
                credit_days=request.form.get("credit_days", 0),
                note=request.form.get("note", ""),
                idempotency_key=request.form.get("sale_token", ""),
            )
            sale_succeeded = True
            sale = result.get("sale") or {}
            invoice = result.get("invoice") or {}
            flash(result.get("message") or "Sale saved.", "success")

            # Optional collection at the moment the Farmer records the sale.
            # Sale/stock remains completed even if the settlement needs repair,
            # preventing a failed payment field from deducting stock twice.
            collection_option = str(request.form.get("collection_option") or "none").strip().lower()
            if collection_option in {"full", "partial"} and invoice.get("id"):
                amount_to_record = invoice.get("outstanding_display") if collection_option == "full" else request.form.get("amount_received")
                payment_result = stage8_record_payment(
                    session.get("user_id"),
                    "farmer_external_invoice",
                    invoice.get("id"),
                    amount_to_record,
                    request.form.get("payment_mode") or "cash",
                    reference=request.form.get("payment_reference", ""),
                    note="Collected while recording Farmer produce sale. " + str(request.form.get("payment_note") or ""),
                    idempotency_key=request.form.get("payment_token", ""),
                )
                flash(payment_result.get("message") or "Buyer payment recorded.", "success")

            if wants_json_response():
                return jsonify(json_safe({"ok": True, **result})), 201
            return redirect(url_for("modules.farmer_sale_detail", sale_id=sale.get("id")))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if wants_json_response():
                return json_error(str(exc), 400)
            if sale_succeeded:
                flash(f"Sale and stock update succeeded, but payment collection needs attention: {exc}", "warning")
                sale = (result or {}).get("sale") or {}
                if sale.get("id"):
                    return redirect(url_for("modules.farmer_sale_detail", sale_id=sale.get("id")))
            else:
                flash(str(exc), "danger")

    try:
        context = stage9_get_sale_form_context(session.get("user_id"))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        context = {"farmer": {}, "stock_rows": [], "buyer_types": {}, "payment_terms": {}, "mapped_ufc": {}, "today": business_today().isoformat(), "sale_token": f"FSALE-{uuid4().hex.upper()}", "payment_token": f"FPAY-{uuid4().hex.upper()}"}
    return render_template("modules/farmer_sale_form_legacy.html", **context)


@modules_bp.route("/farmer-sales")
@login_required
@roles_required("farmer")
def farmer_sales():
    try:
        overview = stage9_get_sales_overview(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"farmer": {}, "rows": [], "query": "", "summary": {"sale_count": 0, "sale_value": "0.00", "received": "0.00", "outstanding": "0.00"}}
    if wants_json_response():
        return jsonify(json_safe({"ok": True, **overview}))
    return render_template("modules/farmer_sales.html", overview=overview)


@modules_bp.route("/farmer-sales/<sale_id>")
@login_required
@roles_required("farmer")
def farmer_sale_detail(sale_id):
    try:
        context = stage9_get_sale_detail(session.get("user_id"), sale_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_sales"))
    return render_template("modules/farmer_sale_detail.html", **context)


@modules_bp.route("/farmer-sales/<sale_id>/void", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_sale_void(sale_id):
    try:
        result = stage9_void_external_sale(session.get("user_id"), sale_id, request.form.get("reason", ""))
        flash(result.get("message") or "Sale cancelled.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_sale_detail", sale_id=sale_id))


@modules_bp.route("/farmer-sales/receipts/<invoice_id>/print")
@login_required
@roles_required("farmer")
def farmer_external_invoice_print(invoice_id):
    try:
        context = stage9_get_invoice_print_context(session.get("user_id"), invoice_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_sales"))
    return render_template("modules/farmer_external_invoice_print.html", **context)


@modules_bp.route("/my-payments/record-buyer-payment", methods=["POST"])
@login_required
@roles_required("farmer")
def record_farmer_buyer_payment():
    sale_id = request.form.get("sale_id", "")
    try:
        result = stage8_record_payment(
            session.get("user_id"),
            "farmer_external_invoice",
            request.form.get("invoice_id"),
            request.form.get("amount"),
            request.form.get("payment_mode"),
            reference=request.form.get("reference", ""),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Buyer payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    if sale_id:
        return redirect(url_for("modules.farmer_sale_detail", sale_id=sale_id))
    return redirect(url_for("modules.farmer_payments"))


@modules_bp.route("/my-payments/<payment_id>/reverse-buyer-payment", methods=["POST"])
@login_required
@roles_required("farmer")
def reverse_farmer_buyer_payment(payment_id):
    sale_id = request.form.get("sale_id", "")
    try:
        result = stage8_reverse_payment(session.get("user_id"), payment_id, request.form.get("reason", ""))
        flash(result.get("message") or "Buyer payment reversed.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    if sale_id:
        return redirect(url_for("modules.farmer_sale_detail", sale_id=sale_id))
    return redirect(url_for("modules.farmer_payments"))



# ---------------------------------------------------------------------------
# Corrected Stage 9 — Farmer Produce Marketplace & network commerce
# ---------------------------------------------------------------------------


def _stage9_market_package_options_from_form():
    labels = request.form.getlist("package_label")
    sizes = request.form.getlist("package_quantity")
    prices = request.form.getlist("package_price")
    count = max(len(labels), len(sizes), len(prices))
    rows = []
    for idx in range(count):
        rows.append({
            "label": labels[idx] if idx < len(labels) else "",
            "quantity_per_bag": sizes[idx] if idx < len(sizes) else "",
            "price_per_bag": prices[idx] if idx < len(prices) else "",
        })
    return rows


def _stage9_save_market_images():
    saved = []
    for file in request.files.getlist("images")[:4]:
        if not file or not file.filename:
            continue
        filename = save_file(file, "farmer_market")
        if filename:
            saved.append(filename)
    return saved


def _stage9_cleanup_saved_images(filenames):
    folder = current_app.config.get("UPLOAD_FOLDER")
    if not folder:
        return
    for filename in filenames or []:
        try:
            path = os.path.join(folder, filename)
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


@modules_bp.route("/farmer-produce-market")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin", "accounts")
def farmer_produce_market():
    try:
        overview = stage9_market_get_marketplace(
            session.get("user_id"),
            request.args.get("q", ""),
            only_available=request.args.get("available") == "1",
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"viewer": {}, "rows": [], "query": "", "order_token": f"FMORD-{uuid4().hex.upper()}", "payment_terms": {}, "summary": {"listing_count": 0, "available_count": 0}}
    if wants_json_response():
        return jsonify(json_safe({"ok": True, **overview}))
    return render_template("modules/farmer_produce_marketplace.html", overview=overview)


@modules_bp.route("/farmer-produce-market/listings/<listing_id>")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin", "accounts")
def farmer_produce_market_listing_detail(listing_id):
    try:
        listing = stage9_market_get_listing(session.get("user_id"), listing_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_produce_market"))
    return render_template(
        "modules/farmer_produce_marketplace_detail.html",
        listing=listing,
        order_token=f"FMORD-{uuid4().hex.upper()}",
    )


@modules_bp.route("/farmer-produce-market/order", methods=["POST"])
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_order():
    try:
        result = stage9_market_place_order(
            session.get("user_id"),
            request.form.get("listing_id"),
            request.form.get("purchase_mode", "loose"),
            request.form.get("quantity"),
            package_index=request.form.get("package_index"),
            payment_term=request.form.get("payment_term", "pay_on_receipt"),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("order_token", ""),
        )
        flash(result.get("message") or "Order request sent.", "success")
        order = result.get("order") or {}
        return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order.get("id")))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_produce_market"))


@modules_bp.route("/farmer-produce-market/cart", methods=["POST"])
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_cart_order():
    try:
        raw = request.form.get("cart_json", "[]")
        items = json.loads(raw) if raw else []
        result = stage9_market_place_cart_orders(
            session.get("user_id"), items,
            payment_term=request.form.get("payment_term", "pay_on_receipt"),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("order_token", ""),
        )
        flash(result.get("message") or "Produce cart checkout completed.", "success")
        orders = result.get("orders") or []
        if len(orders) == 1 and orders[0].get("id"):
            return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=orders[0].get("id")))
        return redirect(url_for("modules.farmer_produce_market_my_orders"))
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_produce_market"))


@modules_bp.route("/farmer-produce-market/my-orders")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_my_orders():
    try:
        overview = stage9_market_get_orders(session.get("user_id"), side="buyer", search=request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"rows": [], "summary": {}, "side": "buyer", "query": ""}
    return render_template("modules/farmer_marketplace_my_orders.html", overview=overview)


@modules_bp.route("/farmer-produce-market/purchases")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_purchases():
    try:
        overview = stage9_market_get_purchases(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"buyer": {}, "rows": [], "stock_rows": [], "query": ""}
    return render_template("modules/farmer_marketplace_purchases.html", overview=overview)


@modules_bp.route("/farmer-produce-market/orders/<order_id>")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin", "accounts")
def farmer_produce_market_order_detail(order_id):
    try:
        context = stage9_market_get_order_detail(session.get("user_id"), order_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        role = session.get("role")
        if role == "farmer":
            return redirect(url_for("modules.farmer_produce_market_my_orders"))
        return redirect(url_for("modules.farmer_produce_market"))

    # Keep payment state on the same invoice/payment engine used by the unified
    # Payments page. This avoids a second settlement workflow on order detail.
    context["payment"] = None
    invoice = context.get("invoice") or {}
    if invoice.get("id"):
        try:
            context["payment"] = get_invoice_payment_context(
                session.get("user_id"),
                "farmer_marketplace_invoice",
                invoice.get("id"),
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            context["payment"] = {"error": str(exc), "invoice": None, "pending_payments": [], "payment_modes": {}}
    return render_template("modules/farmer_marketplace_order_detail.html", **context)


@modules_bp.route("/farmer-produce-market/orders/<order_id>/cancel", methods=["POST"])
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_cancel_order(order_id):
    try:
        result = stage9_market_cancel_order(session.get("user_id"), order_id, request.form.get("reason", ""))
        flash(result.get("message") or "Order cancelled.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/farmer-produce-market/orders/<order_id>/receive", methods=["POST"])
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin")
def farmer_produce_market_receive_order(order_id):
    try:
        context=stage9_market_get_order_detail(session.get("user_id"),order_id)
        current=context.get("order") or {}
        result=stage9_market_receive_order(session.get("user_id"),order_id,receipt_lines=_receipt_lines_from_request(current.get("items") or []),receipt_note=request.form.get("receipt_note",""))
        message=result.get("message") or "Goods received."
        flash(message,"warning" if "repair" in message.lower() else "success")
    except (ValueError,PermissionError,RuntimeError) as exc:
        flash(str(exc),"danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail",order_id=order_id))


@modules_bp.route("/my-produce-market/listings")
@login_required
@roles_required("farmer")
def farmer_marketplace_listings():
    try:
        overview = stage9_market_get_my_listings(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"rows": [], "summary": {}, "query": ""}
    return render_template("modules/farmer_marketplace_listings.html", overview=overview)


@modules_bp.route("/my-produce-market/listings/new", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_listing_new():
    return _farmer_marketplace_listing_form(None)


@modules_bp.route("/my-produce-market/listings/<listing_id>/edit", methods=["GET", "POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_listing_edit(listing_id):
    return _farmer_marketplace_listing_form(listing_id)


def _farmer_marketplace_listing_form(listing_id):
    if request.method == "POST":
        saved_images = []
        try:
            saved_images = _stage9_save_market_images()
            result = stage9_market_save_listing(
                session.get("user_id"),
                request.form.get("product_key"),
                request.form.get("listed_quantity"),
                request.form.get("selling_mode", "loose"),
                loose_price=request.form.get("loose_price"),
                min_order_quantity=request.form.get("min_order_quantity", 1),
                package_options=_stage9_market_package_options_from_form(),
                title=request.form.get("title", ""),
                description=request.form.get("description", ""),
                grade=request.form.get("grade", ""),
                variety=request.form.get("variety", ""),
                images=saved_images,
                publish=request.form.get("action") == "publish",
                listing_id=listing_id,
                available_quantity=request.form.get("available_quantity") if listing_id else None,
            )
            flash(result.get("message") or "Listing saved.", "success")
            return redirect(url_for("modules.farmer_marketplace_listings"))
        except (ValueError, PermissionError, RuntimeError) as exc:
            _stage9_cleanup_saved_images(saved_images)
            flash(str(exc), "danger")
    try:
        context = stage9_market_get_listing_form_context(session.get("user_id"), listing_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_marketplace_listings"))
    context["selected_product_key"] = request.args.get("product_key", "")
    return render_template("modules/farmer_marketplace_listing_form.html", **context)


@modules_bp.route("/my-produce-market/listings/<listing_id>/status", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_listing_status(listing_id):
    try:
        result = stage9_market_set_listing_status(session.get("user_id"), listing_id, request.form.get("status"))
        flash(result.get("message") or "Listing updated.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_marketplace_listings"))


@modules_bp.route("/my-produce-market/orders")
@login_required
@roles_required("farmer")
def farmer_marketplace_orders_received():
    try:
        overview = stage9_market_get_orders(session.get("user_id"), side="seller", search=request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"rows": [], "summary": {}, "side": "seller", "query": ""}
    return render_template("modules/farmer_marketplace_orders_received.html", overview=overview)


@modules_bp.route("/my-produce-market/orders/<order_id>/approve", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_order_approve(order_id):
    try:
        result = stage9_market_approve_order(session.get("user_id"), order_id, credit_days=request.form.get("credit_days", 0))
        flash(result.get("message") or "Order approved.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/my-produce-market/orders/<order_id>/reject", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_order_reject(order_id):
    try:
        result = stage9_market_reject_order(session.get("user_id"), order_id, request.form.get("reason", ""))
        flash(result.get("message") or "Order rejected.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/my-produce-market/orders/<order_id>/dispatch", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_order_dispatch(order_id):
    try:
        result = stage9_market_dispatch_order(session.get("user_id"), order_id)
        category = "warning" if "needs repair" in str(result.get("message") or "").lower() else "success"
        flash(result.get("message") or "Order dispatched.", category)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/my-produce-market/orders/<order_id>/delivered", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_order_delivered(order_id):
    try:
        result = stage9_market_confirm_delivery(session.get("user_id"), order_id)
        flash(result.get("message") or "Delivery marked.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/my-produce-market/orders/<order_id>/repair", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_order_repair(order_id):
    try:
        result = stage9_market_repair_financial_documents(session.get("user_id"), order_id)
        flash(result.get("message") or "Sales documents repaired.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))


@modules_bp.route("/my-produce-market/sales")
@login_required
@roles_required("farmer")
def farmer_marketplace_sales():
    try:
        overview = stage9_market_get_sales(session.get("user_id"), request.args.get("q", ""))
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        overview = {"rows": [], "summary": {}, "query": ""}
    return render_template("modules/farmer_marketplace_sales.html", overview=overview)


@modules_bp.route("/farmer-produce-market/receipts/<invoice_id>/print")
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin", "accounts")
def farmer_marketplace_invoice_print(invoice_id):
    try:
        context = stage9_market_get_invoice_print_context(session.get("user_id"), invoice_id)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
        return redirect(url_for("modules.farmer_produce_market"))
    invoice_obj = context.get("invoice") if isinstance(context, dict) else None
    raw_lines = invoice_obj.get("items") if isinstance(invoice_obj, dict) else []
    context["invoice_lines"] = [dict(line) for line in raw_lines if isinstance(line, dict)] if isinstance(raw_lines, list) else []
    return render_template("modules/farmer_marketplace_invoice_print.html", **context)


@modules_bp.route("/farmer-produce-market/payments/<payment_id>/confirm", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_confirm_payment(payment_id):
    order_id = request.form.get("order_id") or ""
    try:
        result = stage8_confirm_reported_payment(session.get("user_id"), payment_id)
        flash(result.get("message") or "Payment confirmed received.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    if order_id:
        return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))
    return redirect(url_for("modules.farmer_payments"))


@modules_bp.route("/farmer-produce-market/payments/<payment_id>/return", methods=["POST"])
@login_required
@roles_required("farmer")
def farmer_marketplace_return_payment(payment_id):
    order_id = request.form.get("order_id") or ""
    try:
        result = stage8_reject_reported_payment(session.get("user_id"), payment_id, request.form.get("reason", ""))
        flash(result.get("message") or "Payment report returned.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    if order_id:
        return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))
    return redirect(url_for("modules.farmer_payments"))


@modules_bp.route("/farmer-produce-market/payments/record", methods=["POST"])
@login_required
@roles_required("farmer", "ufc_admin", "avpl_admin", "super_admin", "accounts")
def farmer_marketplace_record_payment():
    order_id = request.form.get("order_id") or ""
    try:
        result = stage8_record_payment(
            session.get("user_id"),
            "farmer_marketplace_invoice",
            request.form.get("invoice_id"),
            request.form.get("amount"),
            request.form.get("payment_mode"),
            reference=request.form.get("reference", ""),
            note=request.form.get("note", ""),
            idempotency_key=request.form.get("payment_token", ""),
        )
        flash(result.get("message") or "Payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), "danger")
    if order_id:
        return redirect(url_for("modules.farmer_produce_market_order_detail", order_id=order_id))
    return redirect(url_for("modules.farmer_produce_market_my_orders"))


# ---------------------------------------------------------------------------
# Stage 4 — UFC -> AVPL Order Requests, Receipt and UFC Stock
# ---------------------------------------------------------------------------


@modules_bp.route('/avpl-orders/request', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def request_avpl_order():
    try:
        result = create_ufc_order_request(
            session.get('user_id'),
            session.get('centre_uid'),
            request.form.get('product_id'),
            request.form.get('quantity'),
            note=request.form.get('request_note', ''),
            payment_term=request.form.get('payment_term', 'credit'),
        )
        order = result.get('order') or {}
        if wants_json_response():
            return jsonify(json_safe({
                'ok': True,
                'message': result.get('message'),
                'order': order,
            })), 201
        flash(result.get('message') or 'Order request sent to AVPL.', 'success')
        return redirect(url_for('modules.ufc_avpl_order_detail', order_id=order.get('id')))
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), 'danger')
        return redirect(url_for('modules.buy'))


@modules_bp.route('/avpl-orders/cart', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def request_avpl_cart_order():
    try:
        raw = request.form.get('cart_json', '[]')
        items = json.loads(raw) if raw else []
        result = create_ufc_cart_order_request(
            session.get('user_id'), session.get('centre_uid'), items,
            note=request.form.get('request_note', ''),
            payment_term=request.form.get('payment_term', 'credit'),
            idempotency_key=request.form.get('checkout_token', ''),
        )
        order = result.get('order') or {}
        flash(result.get('message') or 'Cart order request sent to AVPL.', 'success')
        if order.get('id'):
            return redirect(url_for('modules.ufc_avpl_order_detail', order_id=order.get('id')))
        return redirect(url_for('modules.ufc_avpl_orders'))
    except (ValueError, PermissionError, RuntimeError, json.JSONDecodeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('modules.buy'))


@modules_bp.route('/avpl-orders')
@login_required
@roles_required('ufc_admin')
def ufc_avpl_orders():
    try:
        overview = get_ufc_order_overview(
            session.get('user_id'),
            session.get('centre_uid'),
            status_filter=request.args.get('status', 'all'),
            search=request.args.get('q', ''),
            page=request.args.get('page', 1, type=int) or 1,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'selected_status': request.args.get('status', 'all'),
            'query': request.args.get('q', ''),
            'statuses': {},
            'counts': {},
            'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC',
            'pagination': {
                'page': 1,
                'total': 0,
                'total_pages': 1,
                'has_prev': False,
                'has_next': False,
            },
        }
    return render_template('modules/ufc_avpl_orders.html', overview=overview)


@modules_bp.route('/avpl-orders/<order_id>')
@login_required
@roles_required('ufc_admin')
def ufc_avpl_order_detail(order_id):
    try:
        # Resolve Centre UID through the overview helper first so a manipulated
        # URL can never expose another UFC's order.
        overview = get_ufc_order_overview(
            session.get('user_id'), session.get('centre_uid'), page=1
        )
        centre_uid = overview.get('centre_uid')
        order = get_avpl_ufc_order(order_id, centre_uid=centre_uid)
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('modules.ufc_avpl_orders'))

    payment = None
    invoice_id = order.get('avpl_sales_invoice_id_str')
    if invoice_id:
        try:
            payment = get_invoice_payment_context(
                session.get('user_id'),
                'avpl_ufc_invoice',
                invoice_id,
            )
        except (ValueError, PermissionError, RuntimeError) as exc:
            # Payment visibility must never make the physical order detail fail.
            payment = {'error': str(exc), 'invoice': None, 'pending_payments': [], 'payment_modes': {}}

    return render_template('modules/ufc_avpl_order_detail.html', order=order, payment=payment)


@modules_bp.route('/avpl-orders/<order_id>/receive', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def receive_avpl_order(order_id):
    try:
        # Physical receipt and money settlement are deliberately separate.
        # This action only confirms goods and increases UFC stock. Pay Later,
        # partial payment or payment reporting are handled from Payments.
        current = get_avpl_ufc_order(order_id, centre_uid=session.get('centre_uid'))
        result = receive_ufc_order(
            session.get('user_id'),
            session.get('centre_uid'),
            order_id,
            receipt_note=request.form.get('receipt_note', ''),
            receipt_lines=_receipt_lines_from_request(current.get('items') or []),
        )
        received_order = result.get('order') or {}

        if wants_json_response():
            return jsonify(json_safe({
                'ok': True,
                'message': result.get('message'),
                'order': received_order,
                'purchase': result.get('purchase'),
            }))

        flash(result.get('message') or 'Goods received and added to UFC stock.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_avpl_order_detail', order_id=order_id))


@modules_bp.route('/ufc-opening-stock', methods=['GET', 'POST'])
@login_required
@roles_required('ufc_admin')
def ufc_opening_stock():
    if request.method == 'POST':
        proof_doc = None
        try:
            proof_file = request.files.get('proof')
            if proof_file and proof_file.filename:
                proof_doc = store_document(
                    proof_file,
                    session.get('user_id'),
                    None,
                    session.get('user_id'),
                    session.get('role'),
                    'UFC Opening Stock Proof',
                )
            result = create_ufc_opening_stock(
                session.get('user_id'), session.get('centre_uid'),
                product_id=request.form.get('product_id'),
                quantity=request.form.get('quantity'),
                unit_cost=request.form.get('unit_cost'),
                warehouse_bin=request.form.get('warehouse_bin'),
                batch_number=request.form.get('batch_number'),
                manufacturing_date=request.form.get('manufacturing_date'),
                expiry_date=request.form.get('expiry_date'),
                opening_date=request.form.get('opening_date'),
                reference=request.form.get('reference'),
                note=request.form.get('note'),
                proof_filename=(proof_doc or {}).get('filename') or '',
                proof_document_id=(proof_doc or {}).get('_id'),
                idempotency_key=request.form.get('opening_token'),
            )
            entry = result.get('entry') or {}
            log_action(
                session.get('user_id'), 'create_ufc_opening_stock', 'opening_stock',
                str(entry.get('_id') or ''),
                metadata={
                    'opening_number': entry.get('opening_number'), 'centre_uid': entry.get('centre_uid'),
                    'product_name': entry.get('product_name'), 'quantity': entry.get('opening_quantity'),
                },
            )
            flash(result.get('message') or 'UFC opening stock saved.', 'success')
            return redirect(url_for('modules.ufc_opening_stock'))
        except (ValueError, PermissionError, RuntimeError) as exc:
            if proof_doc and proof_doc.get('_id'):
                mongo.db.documents.update_one(
                    {'_id': proof_doc['_id']}, {'$set': {'status': 'orphaned', 'updated_at': now_utc()}}
                )
            flash(str(exc), 'danger')
            return redirect(url_for('modules.ufc_opening_stock'))

    try:
        overview = get_ufc_opening_stock_overview(
            session.get('user_id'), session.get('centre_uid'), search=request.args.get('q', '')
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'mode': {'enabled': False, 'status': 'closed'}, 'rows': [], 'products': [],
            'query': request.args.get('q', ''), 'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC',
            'summary': {'active_entries': 0, 'product_count': 0, 'opening_value': '0.00'},
        }
    overview['opening_token'] = f"OPEN-UFC-{uuid4().hex.upper()}"
    return render_template('modules/ufc_opening_stock.html', overview=overview)


@modules_bp.route('/ufc-opening-stock/<entry_id>/correct', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def ufc_correct_opening_stock(entry_id):
    try:
        result = correct_opening_stock_entry(
            session.get('user_id'), entry_id,
            new_quantity=request.form.get('new_quantity'),
            new_unit_cost=request.form.get('new_unit_cost'),
            reason=request.form.get('reason'),
            centre_uid_hint=session.get('centre_uid'),
        )
        entry = result.get('entry') or {}
        log_action(
            session.get('user_id'), 'correct_ufc_opening_stock', 'opening_stock', entry_id,
            metadata={'opening_number': entry.get('opening_number'), 'centre_uid': entry.get('centre_uid'), 'reason': request.form.get('reason') or ''},
        )
        flash(result.get('message') or 'Opening stock corrected.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_opening_stock'))


@modules_bp.route('/ufc-opening-stock/<entry_id>/void', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def ufc_void_opening_stock(entry_id):
    try:
        result = void_opening_stock_entry(
            session.get('user_id'), entry_id,
            reason=request.form.get('reason'),
            centre_uid_hint=session.get('centre_uid'),
        )
        entry = result.get('entry') or {}
        log_action(
            session.get('user_id'), 'void_ufc_opening_stock', 'opening_stock', entry_id,
            metadata={'opening_number': entry.get('opening_number'), 'centre_uid': entry.get('centre_uid'), 'reason': request.form.get('reason') or ''},
        )
        flash(result.get('message') or 'Opening stock entry voided.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_opening_stock'))


@modules_bp.route('/ufc-stock')
@login_required
@roles_required('ufc_admin')
def ufc_stock():
    q = request.args.get('q', '')
    try:
        overview = get_ufc_stock_overview(
            session.get('user_id'), session.get('centre_uid'), search=q,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [], 'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC', 'query': q,
            'summary': {'product_count': 0, 'stock_value': '0.00'},
        }
    try:
        output_overview = get_ufc_output_stock_overview(
            session.get('user_id'), session.get('centre_uid'), search=q,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash('Farmer Produce stock could not be loaded: ' + str(exc), 'warning')
        output_overview = {
            'rows': [], 'centre_uid': overview.get('centre_uid') or session.get('centre_uid') or '',
            'centre_name': overview.get('centre_name') or session.get('centre_uid') or 'UFC', 'query': q,
            'summary': {'product_count': 0, 'stock_value': '0.00'},
        }
    return render_template('modules/ufc_stock.html', overview=overview, output_overview=output_overview)


@modules_bp.route('/ufc-purchases')
@login_required
@roles_required('ufc_admin')
def ufc_purchases():
    try:
        overview = get_ufc_purchase_overview(
            session.get('user_id'),
            session.get('centre_uid'),
            search=request.args.get('q', ''),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC',
            'query': request.args.get('q', ''),
            'summary': {'count': 0, 'total_value': '0.00'},
        }
    return render_template('modules/ufc_purchases.html', overview=overview)


# ---------------------------------------------------------------------------
# Stage 6 — UFC -> Farmer Marketplace publication and mapped-Farmer visibility
# ---------------------------------------------------------------------------


@modules_bp.route('/ufc-farmer-marketplace', methods=['GET', 'POST'])
@login_required
@roles_required('ufc_admin')
def ufc_farmer_marketplace():
    # Handle the delivery switch on the existing marketplace endpoint so the
    # template never depends on a newly-added endpoint during incremental UAT.
    if request.method == 'POST':
        try:
            enabled = str(request.form.get('delivery_enabled') or '').strip() == '1'
            result = set_farmer_delivery_enabled(
                session.get('user_id'),
                session.get('centre_uid'),
                enabled,
            )
            flash(result.get('message') or 'Delivery setting updated.', 'success')
        except (ValueError, PermissionError, RuntimeError) as exc:
            flash(str(exc), 'danger')
        return redirect(url_for('modules.ufc_farmer_marketplace'))

    try:
        overview = get_ufc_marketplace_setup(
            session.get('user_id'),
            session.get('centre_uid'),
            search=request.args.get('q', ''),
            status_filter=request.args.get('status', 'all'),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'centre_uid': session.get('centre_uid') or '',
            'centre_name': session.get('centre_uid') or 'UFC',
            'query': request.args.get('q', ''),
            'selected_status': request.args.get('status', 'all'),
            'delivery_enabled': True,
            'delivery_label': 'Delivery ON',
            'summary': {'stock_products': 0, 'published': 0, 'needs_price': 0, 'out_of_stock': 0},
        }
    return render_template('modules/ufc_farmer_marketplace.html', overview=overview)


@modules_bp.route('/ufc-farmer-marketplace/delivery', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def update_ufc_farmer_delivery():
    try:
        enabled = str(request.form.get('delivery_enabled') or '').strip() == '1'
        result = set_farmer_delivery_enabled(
            session.get('user_id'),
            session.get('centre_uid'),
            enabled,
        )
        flash(result.get('message') or 'Delivery setting updated.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_farmer_marketplace'))


@modules_bp.route('/ufc-farmer-marketplace/<product_id>/setup', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def save_ufc_farmer_marketplace_setup(product_id):
    try:
        save_product_selling_setup(
            session.get('user_id'),
            session.get('centre_uid'),
            product_id,
            selling_price=request.form.get('selling_price'),
            min_order_quantity=request.form.get('min_order_quantity') or 1,
            max_order_quantity=request.form.get('max_order_quantity') or 0,
            notes=request.form.get('notes', ''),
        )
        flash('Farmer selling setup saved. Publishing does not change UFC stock.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_farmer_marketplace'))


@modules_bp.route('/ufc-farmer-marketplace/publication', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def update_ufc_farmer_marketplace_publication():
    product_ids = request.form.getlist('product_ids')
    action = request.form.get('action', '')
    try:
        result = bulk_update_ufc_farmer_publication(
            session.get('user_id'),
            session.get('centre_uid'),
            product_ids,
            action,
        )
        flash(result.get('message') or 'Farmer Marketplace updated.', 'success')
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_farmer_marketplace'))


@modules_bp.route('/farmer-marketplace')
@login_required
@roles_required('farmer')
def farmer_marketplace():
    try:
        overview = get_farmer_marketplace(
            session.get('user_id'),
            search=request.args.get('q', ''),
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        overview = {
            'rows': [],
            'centre_uid': '',
            'centre_name': 'Mapped UFC',
            'farmer_name': 'Farmer',
            'query': request.args.get('q', ''),
            'delivery_enabled': True,
            'delivery_message': '',
            'summary': {'published': 0, 'in_stock': 0, 'out_of_stock': 0},
        }
    if wants_json_response():
        return jsonify(json_safe({'ok': True, **overview}))
    return render_template('modules/farmer_marketplace.html', overview=overview)


# ---------------------------------------------------------------------------
# Stage 5 — UFC access to the AVPL Sales Invoice linked to its own order
# ---------------------------------------------------------------------------


@modules_bp.route('/avpl-sales-invoices/<invoice_id>/print')
@login_required
@roles_required('ufc_admin')
def ufc_avpl_sales_invoice_print(invoice_id):
    try:
        # Resolve the authoritative Centre UID through the Stage 4 service path
        # before allowing access to the commercial invoice.
        overview = get_ufc_order_overview(
            session.get('user_id'), session.get('centre_uid'), page=1
        )
        centre_uid = overview.get('centre_uid')
        context = get_sales_invoice_print_context(
            invoice_id,
            actor_user_id=session.get('user_id'),
            centre_uid=centre_uid,
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        flash(str(exc), 'danger')
        return redirect(url_for('modules.ufc_avpl_orders'))
    return render_template('admin/ufc_sales_invoice_print.html', **context, viewer='ufc')
