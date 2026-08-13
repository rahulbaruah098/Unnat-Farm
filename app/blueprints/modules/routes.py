import re
from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.utils.security import save_file
from app.services.avpl_ufc_order_service import (
    create_ufc_order_request,
    get_order as get_avpl_ufc_order,
    get_ufc_order_overview,
    get_ufc_purchase_overview,
    get_ufc_stock_overview,
    receive_ufc_order,
)
from app.services.avpl_ufc_sales_service import (
    get_sales_invoice_print_context,
)
from app.services.ufc_farmer_marketplace_service import (
    bulk_update_publication as bulk_update_ufc_farmer_publication,
    get_farmer_marketplace,
    get_ufc_marketplace_setup,
    save_product_selling_setup,
)
from app.services.ufc_farmer_order_service import (
    approve_farmer_order as stage7_approve_farmer_order,
    cancel_farmer_order as stage7_cancel_farmer_order,
    create_farmer_order as stage7_create_farmer_order,
    deliver_farmer_order as stage7_deliver_farmer_order,
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
from app.services.payment_service import (
    get_farmer_payment_overview,
    get_payment_receipt_context,
    get_ufc_payment_overview,
    record_payment as stage8_record_payment,
    reverse_payment as stage8_reverse_payment,
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
                if expiry_date < datetime.utcnow().date():
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
    audience = session.get("role")
    user_id = session.get("user_id")
    q = request.args.get("q", "").strip()

    query = {
        "$or": [
            {"audience": audience},
            {"audience": "all"}
        ]
    }

    if audience == "farmer":
        farmer_profile = (
            mongo.db.farmer_master.find_one({"linked_user_id": user_id})
            or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(user_id)})
        )

        farmer_activities = []

        if farmer_profile:
            for key in ["activities", "registered_activities", "activity", "farmer_activities"]:
                value = farmer_profile.get(key)

                if isinstance(value, list):
                    farmer_activities.extend(value)
                elif isinstance(value, str) and value.strip():
                    farmer_activities.append(value.strip())

        farmer_activities = list(set(farmer_activities))

        query = {
            "$and": [
                {
                    "$or": [
                        {"audience": "farmer"},
                        {"audience": "all"}
                    ]
                },
                {
                    "$or": [
                        {"activity_category": "all"},
                        {"activity_category": "All"},
                        {"activity_category": {"$in": farmer_activities}},
                    ]
                }
            ]
        }

    if q:
        query = {
            "$and": [
                query,
                {
                    "$or": [
                        {"title": {"$regex": q, "$options": "i"}},
                        {"lms_type": {"$regex": q, "$options": "i"}},
                        {"activity_category": {"$regex": q, "$options": "i"}},
                        {"description": {"$regex": q, "$options": "i"}},
                        {"file_name": {"$regex": q, "$options": "i"}}
                    ]
                }
            ]
        }

    items = list(
        mongo.db.lms_materials.find(query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template("modules/lms.html", items=items, q=q)

#changes by atlanta
@modules_bp.route("/support", methods=["GET", "POST"])
@login_required
def support():
    payload = request.get_json(silent=True) if request.is_json else {}
    payload = payload or {}

    user_id = (
        payload.get("user_id")
        or request.args.get("user_id")
        or session.get("user_id")
        or ""
    )

    role = session.get("role")

    user = {}
    if user_id:
        try:
            user = mongo.db.users.find_one({"_id": ObjectId(user_id)}) or {}
        except Exception:
            user = {}

    if not role and user:
        role = user.get("role")

    if request.method == "POST":
        if role == "super_admin":
            if wants_json_response():
                return json_error("Super Admin can update tickets from the ticket table.", 403)

            flash("Super Admin can update tickets from the ticket table.", "warning")
            return redirect(url_for("modules.support"))

        subject = (
            payload.get("subject")
            or request.form.get("subject")
            or ""
        ).strip()

        problem_type = (
            payload.get("problem_type")
            or request.form.get("problem_type")
            or ""
        ).strip()

        priority = (
            payload.get("priority")
            or request.form.get("priority")
            or ""
        ).strip()

        message = (
            payload.get("message")
            or request.form.get("message")
            or ""
        ).strip()

        if not user_id or not subject or not problem_type or not priority or not message:
            if wants_json_response():
                return json_error("Please fill all required support ticket fields.", 400)

            flash("Please fill all required support ticket fields.", "danger")
            return redirect(url_for("modules.support"))

        ticket_ref = "TCK-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")

        mongo.db.support_tickets.insert_one({
            "ticket_ref": ticket_ref,
            "user_id": str(user_id),
            "user_name": user.get("name") or session.get("name") or session.get("username") or "Unknown User",
            "username": user.get("username") or session.get("username"),
            "role": role,
            "phone": user.get("phone") or user.get("contact_no") or "",
            "email": user.get("email") or "",
            "subject": subject,
            "problem_type": problem_type,
            "priority": priority,
            "message": message,
            "support_email": "ites@sayanant.com",
            "support_number": "9957367398",
            "status": "open",
            "progress": "Ticket received",
            "resolution_note": "",
            "created_at": now_utc(),
            "updated_at": now_utc(),
            "resolved_at": None,
            "updated_by": None
        })

        if wants_json_response():
            return jsonify({
                "ok": True,
                "message": "Support ticket raised successfully. Super Admin will review it.",
                "ticket_ref": ticket_ref
            }), 201

        flash("Support ticket raised successfully. Super Admin will review it.", "success")
        return redirect(url_for("modules.support"))

    q = request.args.get("q", "").strip()

    if role == "super_admin":
        ticket_query = {}
    else:
        ticket_query = {"user_id": str(user_id)}

    if q:
        search_filter = {
            "$or": [
                {"ticket_ref": {"$regex": q, "$options": "i"}},
                {"user_name": {"$regex": q, "$options": "i"}},
                {"username": {"$regex": q, "$options": "i"}},
                {"role": {"$regex": q, "$options": "i"}},
                {"subject": {"$regex": q, "$options": "i"}},
                {"problem_type": {"$regex": q, "$options": "i"}},
                {"priority": {"$regex": q, "$options": "i"}},
                {"message": {"$regex": q, "$options": "i"}},
                {"status": {"$regex": q, "$options": "i"}},
                {"progress": {"$regex": q, "$options": "i"}}
            ]
        }

        if ticket_query:
            ticket_query = {"$and": [ticket_query, search_filter]}
        else:
            ticket_query = search_filter

    tickets = list(
        mongo.db.support_tickets.find(ticket_query).sort("created_at", -1)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "tickets": tickets,
            "q": q,
            "support_email": "ites@sayanant.com",
            "support_number": "9957367398"
        }))

    return render_template(
        "modules/support.html",
        tickets=tickets,
        q=q,
        support_email="ites@sayanant.com",
        support_number="9957367398"
    )

@modules_bp.route("/support/<ticket_id>/update", methods=["POST"])
@login_required
def update_support_ticket(ticket_id):
    if session.get("role") != "super_admin":
        flash("Only Super Admin can update support tickets.", "danger")
        return redirect(url_for("modules.support"))

    status = request.form.get("status", "").strip()
    progress = request.form.get("progress", "").strip()
    resolution_note = request.form.get("resolution_note", "").strip()

    allowed_statuses = ["open", "in_progress", "resolved", "closed"]

    if status not in allowed_statuses:
        flash("Invalid ticket status selected.", "danger")
        return redirect(url_for("modules.support"))

    update_doc = {
        "status": status,
        "progress": progress,
        "resolution_note": resolution_note,
        "updated_at": now_utc(),
        "updated_by": session.get("username") or "super_admin"
    }

    if status in ["resolved", "closed"]:
        update_doc["resolved_at"] = now_utc()

    mongo.db.support_tickets.update_one(
        {"_id": ObjectId(ticket_id)},
        {"$set": update_doc}
    )

    flash("Support ticket progress updated successfully.", "success")
    return redirect(url_for("modules.support"))

@modules_bp.route("/orders")
@login_required
def orders():
    query = {}
    if session.get("role") == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")
    elif session.get("role") == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")

    q = request.args.get("q", "").strip()

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
    query = {}

    if session.get("role") == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")

    elif session.get("role") == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")

    elif session.get("role") == "farmer":
        user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}
        query["farmer_contact"] = user.get("phone")

    q = request.args.get("q", "").strip()

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

def get_mitra_bonus_percentage(mitra_uid, bonus_type, category):
    setting = mongo.db.mitra_bonus_settings.find_one({
        "mitra_uid": mitra_uid,
        "bonus_type": bonus_type,
        "category": category
    })

    if setting:
        return float(setting.get("percentage") or 2)

    setting = mongo.db.mitra_bonus_settings.find_one({
        "mitra_uid": None,
        "bonus_type": bonus_type,
        "category": category
    })

    if setting:
        return float(setting.get("percentage") or 2)

    setting = mongo.db.mitra_bonus_settings.find_one({
        "mitra_uid": mitra_uid,
        "bonus_type": bonus_type,
        "category": "all"
    })

    if setting:
        return float(setting.get("percentage") or 2)

    setting = mongo.db.mitra_bonus_settings.find_one({
        "mitra_uid": None,
        "bonus_type": bonus_type,
        "category": "all"
    })

    if setting:
        return float(setting.get("percentage") or 2)

    return 2

#changes by atlanta
@modules_bp.route('/pos', methods=['GET', 'POST'])
@login_required
@roles_required('ufc_admin')
def pos():
    user_id = session.get('user_id')

    ufc_profile = mongo.db.ufc_admin_master.find_one({
        'linked_user_id': user_id
    })

    if not ufc_profile:
        ufc_profile = mongo.db.ufc_admin_master.find_one({
            'linked_user_id': ObjectId(user_id)
        })

    centre_uid = ufc_profile.get('centre_uid') if ufc_profile else None

    mapped_farmers = list(mongo.db.farmer_master.find({
        'centre_uid': centre_uid,
        'approval_status': 'approved'
    }).sort('name', 1))
   
    mitras = list(mongo.db.ufc_mitra_master.find({
        '$and': [
            {'mitra_uid': {'$exists': True, '$ne': ''}},
            {
                '$or': [
                    {'centre_uid': centre_uid},
                    {'mapped_centre_uid': centre_uid},
                    {'center_uid': centre_uid},
                    {'mapped_center_uid': centre_uid}
                ]
            }
        ]
    }).sort('name', 1))

    # Stage 3: legacy available_centres no longer controls UFC visibility.
    # Until UFC-owned stock is introduced, POS can only reference products
    # explicitly published by AVPL Admin.
    products = sorted(
        _published_avpl_products_for_ufc(),
        key=lambda item: str(item.get('name') or '').lower(),
    )

    if request.method == 'POST':
        sale_type = request.form.get('sale_type', 'registered')

        farmer_id = None
        farmer_name = ''
        farmer_phone = ''
        mitra_uid = request.form.get('mitra_uid', '').strip()

        farmer = None

        if sale_type == 'registered':
            farmer_id = request.form.get('farmer_id')
            farmer = mongo.db.farmer_master.find_one({'_id': ObjectId(farmer_id)}) if farmer_id else None

        if farmer:
            farmer_name = farmer.get('name', '')
            farmer_phone = farmer.get('contact_no', '')
            mitra_uid = mitra_uid or farmer.get('mitra_uid', '')
        else:
            farmer_name = request.form.get('unregistered_farmer_name', '').strip()
            farmer_phone = request.form.get('unregistered_farmer_phone', '').strip()
            mitra_uid = request.form.get('mitra_uid', '').strip()

        product_id = request.form.get('product_id')
        product = mongo.db.products.find_one({
            '_id': ObjectId(product_id),
            "is_deleted": {"$ne": True},
            "is_active": {"$ne": False}
        }) if product_id else None

        if not product:
            flash("This product is currently unavailable.", "danger")
            return redirect(url_for("modules.pos"))

        if not _is_avpl_product_published_to_ufc(product.get('_id')):
            flash("This product is not published to the UFC Marketplace.", "danger")
            return redirect(url_for("modules.pos"))

        quantity = float(request.form.get('quantity') or 0)
        price = float(product.get('price') or 0) if product else 0
        total_amount = quantity * price

        product_category = product.get('category') if product else ''

        bonus_percentage = get_mitra_bonus_percentage(
            mitra_uid,
            'avpl_product_sale',
            product_category
        )

        bonus_amount = round((total_amount * bonus_percentage) / 100, 2)

        sale_doc = {
            'centre_uid': centre_uid,
            'ufc_user_id': user_id,
            'sale_type': sale_type,
            'farmer_id': farmer_id,
            'farmer_name': farmer_name,
            'farmer_phone': farmer_phone,
            'mitra_uid': mitra_uid,
            'product_id': product_id,
            'product_name': product.get('name') if product else '',
            'product_category': product_category,
            'product_type': product.get('type') if product else '',
            'quantity': quantity,
            'unit_price': price,
            'total_amount': total_amount,
            'sale_source': 'avpl_product_sale',
            'bonus_type': 'avpl_product_sale',
            'bonus_percentage': bonus_percentage,
            'bonus_amount': bonus_amount,
            'invoice_no': f"AVPL-{centre_uid}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            'source_reference': _legacy_source_reference("UFC_POS_SALE"),
            'accounting_status': 'not_posted',
            'migration_status': 'legacy_operational',
            'created_at': datetime.utcnow()
        }

        result = mongo.db.pos_sales.insert_one(sale_doc)

        sale_doc["_id"] = str(result.inserted_id)

        if wants_json_response():
            return jsonify(json_safe({
                "ok": True,
                "message": "Sale recorded successfully.",
                "sale": sale_doc,
                "sale_id": str(result.inserted_id)
            }))

        flash('Sale recorded successfully. Invoice generated.', 'success')
        return redirect(url_for('modules.pos_invoice', sale_id=str(result.inserted_id)))

    q = request.args.get("q", "").strip()

    sales_query = {
        'centre_uid': centre_uid
    }

    if q:
        safe_q = re.escape(q)

        search_conditions = [
            {'farmer_name': {'$regex': safe_q, '$options': 'i'}},
            {'farmer_phone': {'$regex': safe_q, '$options': 'i'}},
            {'product_name': {'$regex': safe_q, '$options': 'i'}},
            {'product_category': {'$regex': safe_q, '$options': 'i'}},
            {'mitra_uid': {'$regex': safe_q, '$options': 'i'}},
            {'invoice_no': {'$regex': safe_q, '$options': 'i'}},
            {'centre_uid': {'$regex': safe_q, '$options': 'i'}}
        ]

        try:
            numeric_q = float(q)
            search_conditions.extend([
                {'quantity': numeric_q},
                {'unit_price': numeric_q},
                {'total_amount': numeric_q}
            ])
        except ValueError:
            pass

        sales_query['$or'] = search_conditions

    sales = list(
        mongo.db.pos_sales.find(sales_query).sort('created_at', -1).limit(20)
    )

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "centre_uid": centre_uid,
            "farmers": mapped_farmers,
            "mitras": mitras,
            "products": products,
            "sales": sales,
            "q": q
        }))

    return render_template(
        'modules/pos.html',
        centre_uid=centre_uid,
        farmers=mapped_farmers,
        mitras=mitras,
        products=products,
        sales=sales,
        q=q
    )

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
@roles_required('ufc_admin', 'avpl_admin', 'sales_unnatfarm', 'accounts')
def pos_invoice(sale_id):
    try:
        sale = mongo.db.pos_sales.find_one({'_id': ObjectId(sale_id)})
    except Exception:
        flash('Invalid invoice ID.', 'danger')
        if session.get('role') in ['avpl_admin', 'sales_unnatfarm', 'accounts']:
            return redirect(url_for('modules.sales_details'))
        return redirect(url_for('modules.pos'))

    if not sale:
        flash('Invoice not found.', 'danger')
        if session.get('role') in ['avpl_admin', 'sales_unnatfarm', 'accounts']:
            return redirect(url_for('modules.sales_details'))
        return redirect(url_for('modules.pos'))

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "sale": sale
        }))

    return render_template('modules/pos_invoice.html', sale=sale)

@modules_bp.route("/mitra-earnings")
@login_required
@roles_required("ufc_mitra")
def mitra_earnings():
    user_id = session.get("user_id")
    q = request.args.get("q", "").strip()

    mitra_profile = mongo.db.ufc_mitra_master.find_one({
        "linked_user_id": user_id
    }) or mongo.db.ufc_mitra_master.find_one({
        "linked_user_id": ObjectId(user_id)
    })

    mitra_uid = mitra_profile.get("mitra_uid") if mitra_profile else None

    today = datetime.utcnow()
    month_start = datetime(today.year, today.month, 1)

    if today.month == 12:
        next_month_start = datetime(today.year + 1, 1, 1)
    else:
        next_month_start = datetime(today.year, today.month + 1, 1)

    monthly_pos_sales = list(mongo.db.pos_sales.find({
        "mitra_uid": mitra_uid,
        "created_at": {
            "$gte": month_start,
            "$lt": next_month_start
        }
    }))

    total_pos_sales = list(mongo.db.pos_sales.find({
        "mitra_uid": mitra_uid
    }))

    monthly_avpl_earning = sum(float(s.get("bonus_amount") or 0) for s in monthly_pos_sales)
    total_avpl_earning = sum(float(s.get("bonus_amount") or 0) for s in total_pos_sales)

    monthly_farmer_sales = list(mongo.db.farmer_product_sales.find({
        "mitra_uid": mitra_uid,
        "created_at": {
            "$gte": month_start,
            "$lt": next_month_start
        }
    }))

    total_farmer_sales = list(mongo.db.farmer_product_sales.find({
        "mitra_uid": mitra_uid
    }))

    monthly_farmer_earning = sum(float(s.get("bonus_amount") or 0) for s in monthly_farmer_sales)
    total_farmer_earning = sum(float(s.get("bonus_amount") or 0) for s in total_farmer_sales)

    current_month_earning = monthly_avpl_earning + monthly_farmer_earning
    total_earning = total_avpl_earning + total_farmer_earning

    recent_sales_source = monthly_pos_sales + monthly_farmer_sales

    if q:
        q_lower = q.lower()

        def sale_matches_search(s):
            searchable_text = " ".join([
                str(s.get("bonus_type") or ""),
                str(s.get("product_name") or ""),
                str(s.get("sale_source") or ""),
                str(s.get("total_amount") or ""),
                str(s.get("bonus_percentage") or ""),
                str(s.get("bonus_amount") or ""),
                str(s.get("created_at") or "")
            ]).lower()

            return q_lower in searchable_text

        recent_sales_source = [
            s for s in recent_sales_source
            if sale_matches_search(s)
        ]

    recent_sales = sorted(
        recent_sales_source,
        key=lambda x: x.get("created_at"),
        reverse=True
    )[:20]

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "mitra_uid": mitra_uid,
            "current_month_earning": current_month_earning,
            "total_earning": total_earning,
            "monthly_avpl_earning": monthly_avpl_earning,
            "total_avpl_earning": total_avpl_earning,
            "monthly_farmer_earning": monthly_farmer_earning,
            "total_farmer_earning": total_farmer_earning,
            "recent_sales": recent_sales,
            "q": q
        }))

    return render_template(
        "modules/mitra_earnings.html",
        mitra_profile=mitra_profile,
        mitra_uid=mitra_uid,
        current_month_earning=current_month_earning,
        total_earning=total_earning,
        monthly_avpl_earning=monthly_avpl_earning,
        total_avpl_earning=total_avpl_earning,
        monthly_farmer_earning=monthly_farmer_earning,
        total_farmer_earning=total_farmer_earning,
        recent_sales=recent_sales,
        q=q
    )
@modules_bp.route("/finance/leads")
@login_required
@roles_required("avpl_admin", "sales_nelocals", "sales_unnatfarm", "accounts", "ufc_mitra")
def finance_leads():
    role = session.get("role")
    q = request.args.get("q", "").strip()

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
        return jsonify(json_safe({
        "ok": True,
        "items": leads,
        "leads": leads,
        "q": q
    }))

    return render_template(
        "modules/finance_leads.html",
        leads=leads
    )
   
@modules_bp.route("/sales-details")
@login_required
@roles_required("avpl_admin", "sales_unnatfarm", "accounts")
def sales_details():
    q = request.args.get("q", "").strip()

    sales = list(mongo.db.pos_sales.find({}).sort("created_at", -1))

    if q:
        q_lower = q.lower()
        sales = [
            s for s in sales
            if q_lower in str(s.get("created_at", "")).lower()
            or q_lower in str(s.get("centre_uid", "")).lower()
            or q_lower in str(s.get("farmer_name", "")).lower()
            or q_lower in str(s.get("farmer_phone", "")).lower()
            or q_lower in str(s.get("mitra_uid", "")).lower()
            or q_lower in str(s.get("product_name", "")).lower()
            or q_lower in str(s.get("product_category", "")).lower()
            or q_lower in str(s.get("quantity", "")).lower()
            or q_lower in str(s.get("unit_price", "")).lower()
            or q_lower in str(s.get("total_amount", "")).lower()
            or q_lower in str(s.get("bonus_percentage", "")).lower()
            or q_lower in str(s.get("bonus_amount", "")).lower()
        ]

    return render_template(
        "modules/sales_details.html",
        sales=sales
    )
   
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

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": q
        }))

    return render_template(
        "modules/mitra_stock.html",
        items=items,
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
    delivery_succeeded = False
    try:
        result = stage7_deliver_farmer_order(
            session.get("user_id"),
            session.get("centre_uid"),
            order_id,
            delivery_note=request.form.get("delivery_note", ""),
        )
        delivery_succeeded = True
        warning = result.get("financial_warning")
        if warning:
            flash("Delivery and UFC stock update succeeded. Sales document sync needs attention: " + warning, "warning")
        else:
            flash(result.get("message") or "Order delivered.", "success")

        # Stage 8: payment is a separate idempotent settlement after physical
        # delivery. A payment failure must never roll back or repeat stock.
        collection_option = str(request.form.get("collection_option") or "none").strip().lower()
        if delivery_succeeded and not warning and collection_option in {"full", "partial"}:
            delivered_order = result.get("order") or {}
            invoice_id = delivered_order.get("invoice_id_str") or delivered_order.get("ufc_farmer_invoice_id_str")
            if not invoice_id:
                raise RuntimeError("Delivery succeeded, but the invoice is not ready for payment collection yet. Use Payments after repairing the sales document.")
            if collection_option == "full":
                amount_to_record = delivered_order.get("outstanding_amount") or delivered_order.get("grand_total") or delivered_order.get("total_amount")
            else:
                amount_to_record = request.form.get("amount_received")
            payment_result = stage8_record_payment(
                session.get("user_id"),
                "ufc_farmer_invoice",
                invoice_id,
                amount_to_record,
                request.form.get("payment_mode") or "cash",
                reference=request.form.get("payment_reference", ""),
                note="Collected during Farmer order delivery. " + str(request.form.get("payment_note") or ""),
                idempotency_key=request.form.get("payment_token", ""),
            )
            flash(payment_result.get("message") or "Delivery payment recorded.", "success")
    except (ValueError, PermissionError, RuntimeError) as exc:
        # If delivery already succeeded, make the distinction explicit so the
        # operator never clicks Deliver again just because settlement failed.
        if delivery_succeeded:
            flash("Delivery is complete; only payment recording needs attention: " + str(exc), "warning")
        else:
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
        overview = {"centre_uid": session.get("centre_uid") or "", "avpl_payables": [], "farmer_receivables": [], "recent_payments": [], "payment_modes": {}, "summary": {"avpl_due": "0.00", "farmer_due": "0.00", "recent_count": 0}}
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
        overview = {"payables": [], "recent_payments": [], "summary": {"outstanding": "0.00", "recent_count": 0}}
    return render_template("modules/farmer_payments.html", overview=overview)


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
    return render_template('modules/ufc_avpl_order_detail.html', order=order)


@modules_bp.route('/avpl-orders/<order_id>/receive', methods=['POST'])
@login_required
@roles_required('ufc_admin')
def receive_avpl_order(order_id):
    receipt_succeeded = False
    result = None
    try:
        # Physical receipt is always completed first. Payment is a separate,
        # idempotent Stage 8 settlement so a settlement error can never cause
        # the operator to receive the same stock twice.
        result = receive_ufc_order(
            session.get('user_id'),
            session.get('centre_uid'),
            order_id,
            receipt_note=request.form.get('receipt_note', ''),
        )
        receipt_succeeded = True
        received_order = result.get('order') or {}

        if wants_json_response():
            return jsonify(json_safe({
                'ok': True,
                'message': result.get('message'),
                'order': received_order,
                'purchase': result.get('purchase'),
            }))

        flash(result.get('message') or 'Goods received and added to UFC stock.', 'success')

        # Optional Pay-on-Receipt collection. Credit orders normally keep the
        # amount outstanding and are settled later from Payments & Settlement.
        collection_option = str(request.form.get('collection_option') or 'none').strip().lower()
        if collection_option in {'full', 'partial'}:
            invoice_id = received_order.get('avpl_sales_invoice_id_str')
            if not invoice_id:
                raise RuntimeError(
                    'Receipt and UFC stock update succeeded, but the AVPL Sales Invoice is not ready for settlement yet. '
                    'Use Payments & Settlement after the invoice is repaired/synchronized.'
                )

            if collection_option == 'full':
                amount_to_record = (
                    received_order.get('outstanding_amount')
                    if received_order.get('outstanding_amount') not in (None, '')
                    else received_order.get('invoice_grand_total') or received_order.get('total_amount')
                )
            else:
                amount_to_record = request.form.get('amount_paid_now')

            payment_result = stage8_record_payment(
                session.get('user_id'),
                'avpl_ufc_invoice',
                invoice_id,
                amount_to_record,
                request.form.get('payment_mode') or 'cash',
                reference=request.form.get('payment_reference', ''),
                note='Recorded when UFC confirmed AVPL goods receipt. ' + str(request.form.get('payment_note') or ''),
                idempotency_key=request.form.get('payment_token') or f'AVPLRECEIPTPAY-{order_id}',
            )
            flash(payment_result.get('message') or 'Payment recorded successfully.', 'success')

    except (ValueError, PermissionError, RuntimeError) as exc:
        if wants_json_response():
            return json_error(str(exc), 400)
        if receipt_succeeded:
            flash(
                'Goods receipt and UFC stock update are complete; only payment recording needs attention: ' + str(exc),
                'warning',
            )
        else:
            flash(str(exc), 'danger')
    return redirect(url_for('modules.ufc_avpl_order_detail', order_id=order_id))


@modules_bp.route('/ufc-stock')
@login_required
@roles_required('ufc_admin')
def ufc_stock():
    try:
        overview = get_ufc_stock_overview(
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
            'summary': {'product_count': 0, 'stock_value': '0.00'},
        }
    return render_template('modules/ufc_stock.html', overview=overview)


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


@modules_bp.route('/ufc-farmer-marketplace')
@login_required
@roles_required('ufc_admin')
def ufc_farmer_marketplace():
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
            'summary': {'stock_products': 0, 'published': 0, 'needs_price': 0, 'out_of_stock': 0},
        }
    return render_template('modules/ufc_farmer_marketplace.html', overview=overview)


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
