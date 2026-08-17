from bson import ObjectId
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify, current_app, send_file, abort
from app.blueprints.documents.routes import _find_document_path
from app.extensions import mongo
from pathlib import Path
from app.utils.decorators import login_required
from datetime import datetime, timedelta
from app.services.dashboard_service import (
    get_system_overview,
    get_centre_dashboard,
    get_mitra_dashboard,
    get_farmer_dashboard,
)
from app.services.ufc_farmer_marketplace_service import get_farmer_marketplace
from app.services.avpl_accounts_operations_service import get_accounts_dashboard_overview

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _to_number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0

def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [json_safe(item) for item in value]

    if isinstance(value, dict):
        return {
            key: json_safe(val)
            for key, val in value.items()
        }

    return value

#changes by atlanta
def _product_image_value(item):
    if not item:
        return ""

    # Direct image fields
    for key in [
        "image",
        "picture",
        "product_image",
        "image_url",
        "image_path",
        "file_path",
        "filename",
        "file_name",
        "stored_name",
        "image_name",
    ]:
        value = item.get(key)
        if value:
            return str(value)

    # Nested file dict support
    file_data = item.get("file")
    if isinstance(file_data, dict):
        for key in ["file_path", "filename", "file_name", "stored_name"]:
            value = file_data.get(key)
            if value:
                return str(value)

    return ""

#changes by atlanta
def _normalize_product_for_app(item):
    item = dict(item or {})

    if "_id" in item:
        item["_id"] = str(item["_id"])

    image_value = _product_image_value(item)

    image_name = (
        item.get("image_name")
        or item.get("filename")
        or item.get("file_name")
        or item.get("stored_name")
    )

    # Products table stores image reference, actual image details are in documents.
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

        if (
            not image_value.startswith("http")
            and not image_value.startswith("/")
            and not image_value.startswith("uploads/")
            and not image_value.startswith("static/")
        ):
            image_value = f"/dashboard/app-image/{image_value}"

        item["image"] = image_value
        item["image_url"] = image_value
        item["picture"] = image_value

    return item

def _get_app_farmer_products(user, limit=30):
    """Stage 6 app read-model: products published by the Farmer's mapped UFC.

    This replaces the legacy cross-farmer product feed for the Farmer dashboard.
    The app and web therefore share the same visibility boundary.
    """
    user_id = str(user.get("_id") or "").strip()
    if not user_id:
        return []
    try:
        overview = get_farmer_marketplace(user_id)
    except Exception:
        return []
    items = list(overview.get("rows", []))[:max(int(limit or 30), 1)]
    return [_normalize_product_for_app(item) for item in items]

def _get_app_farmer_orders(user, limit=20):
    user = dict(user or {})

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

    filters = []

    if user_id:
        filters.extend([
            {"user_id": user_id},
            {"farmer_user_id": user_id},
            {"buyer_user_id": user_id},
        ])

    if farmer_id:
        filters.append({"farmer_id": farmer_id})

    if farmer_phone:
        filters.extend([
            {"farmer_phone": farmer_phone},
            {"farmer_contact": farmer_phone},
            {"phone": farmer_phone},
        ])

    if not filters:
        return []

    orders = list(
        mongo.db.orders
        .find({"$or": filters})
        .sort("created_at", -1)
        .limit(limit)
    )

    normalized_orders = []

    for order in orders:
        order = dict(order or {})

        if "_id" in order:
            order["_id"] = str(order["_id"])

        # Keep app/web field compatibility
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
            or "avpl"
        )

        order["status"] = (
            order.get("status")
            or "placed"
        )

        normalized_orders.append(order)

    return normalized_orders


def _normalize_dashboard_products(data):
    if not isinstance(data, dict):
        return {}

    for key in ["recommended_products", "products", "avpl_products"]:
        if isinstance(data.get(key), list):
            data[key] = [
                _normalize_product_for_app(item)
                for item in data.get(key, [])
                if isinstance(item, dict)
            ]

    return data


def get_farmer_product_dashboard():
    products = list(mongo.db.farmer_products.find({"status": "active"}).sort("created_at", -1))
    summary_map = {}
    for item in products:
        name = item.get("product_name") or "Unknown"
        area_parts = [item.get("village"), item.get("block"), item.get("district")]
        area = ", ".join([x for x in area_parts if x]) or "-"
        if name not in summary_map:
            summary_map[name] = {"product_name": name, "available_quantity": 0, "areas": set()}
        summary_map[name]["available_quantity"] += _to_number(item.get("available_quantity"))
        if area != "-":
            summary_map[name]["areas"].add(area)
    summary = []
    for row in summary_map.values():
        qty = row["available_quantity"]
        summary.append({
            "product_name": row["product_name"],
            "available_quantity": int(qty) if qty.is_integer() else qty,
            "areas": ", ".join(sorted(row["areas"])) or "-"
        })
    return summary, products

def get_ufc_admin_sales_trends():
    now = datetime.utcnow()

    week_start = now - timedelta(days=7)
    prev_week_start = now - timedelta(days=14)

    month_start = datetime(now.year, now.month, 1)
    if now.month == 1:
        prev_month_start = datetime(now.year - 1, 12, 1)
    else:
        prev_month_start = datetime(now.year, now.month - 1, 1)

    year_start = datetime(now.year, 1, 1)
    prev_year_start = datetime(now.year - 1, 1, 1)

    sales = list(mongo.db.pos_sales.find({}))

    centre_map = {}

    for s in sales:
        centre_uid = s.get("centre_uid") or "Unknown"
        amount = _to_number(s.get("total_amount"))
        created_at = s.get("created_at")

        if not created_at:
            continue

        if centre_uid not in centre_map:
            centre_map[centre_uid] = {
                "centre_uid": centre_uid,
                "weekly_sales": 0,
                "previous_weekly_sales": 0,
                "monthly_sales": 0,
                "previous_monthly_sales": 0,
                "yearly_sales": 0,
                "previous_yearly_sales": 0,
            }

        if created_at >= week_start:
            centre_map[centre_uid]["weekly_sales"] += amount
        elif prev_week_start <= created_at < week_start:
            centre_map[centre_uid]["previous_weekly_sales"] += amount

        if created_at >= month_start:
            centre_map[centre_uid]["monthly_sales"] += amount
        elif prev_month_start <= created_at < month_start:
            centre_map[centre_uid]["previous_monthly_sales"] += amount

        if created_at >= year_start:
            centre_map[centre_uid]["yearly_sales"] += amount
        elif prev_year_start <= created_at < year_start:
            centre_map[centre_uid]["previous_yearly_sales"] += amount

    rows = []

    for row in centre_map.values():
        row["weekly_status"] = "up" if row["weekly_sales"] >= row["previous_weekly_sales"] else "down"
        row["monthly_status"] = "up" if row["monthly_sales"] >= row["previous_monthly_sales"] else "down"
        row["yearly_status"] = "up" if row["yearly_sales"] >= row["previous_yearly_sales"] else "down"
        rows.append(row)

    rows.sort(key=lambda x: x["yearly_sales"], reverse=True)

    return rows

def get_ufc_admin_app_sales_summary(centre_uid):
    centre_uid = (centre_uid or "").strip()

    if not centre_uid:
        return {
            "mitra_sales": [],
            "farmer_sales": [],
        }

    sales = list(
        mongo.db.pos_sales
        .find({"centre_uid": centre_uid})
        .sort("created_at", -1)
    )

    mitra_map = {}
    farmer_map = {}

    for sale in sales:
        amount = _to_number(
            sale.get("total_amount")
            or sale.get("payable_amount")
            or sale.get("grand_total")
            or sale.get("amount")
        )

        # -----------------------------
        # Mitra-wise sales
        # -----------------------------
        mitra_uid = (
            sale.get("mitra_uid")
            or sale.get("mitra_id")
            or sale.get("mapped_mitra_uid")
            or "Unknown"
        )

        if mitra_uid not in mitra_map:
            mitra_map[mitra_uid] = {
                "mitra_uid": mitra_uid,
                "total_orders": 0,
                "total_sales": 0,
            }

        mitra_map[mitra_uid]["total_orders"] += 1
        mitra_map[mitra_uid]["total_sales"] += amount

        # -----------------------------
        # Farmer-wise sales
        # -----------------------------
        farmer_name = (
            sale.get("farmer_name")
            or sale.get("unregistered_farmer_name")
            or sale.get("customer_name")
            or sale.get("source")
            or "Unknown"
        )

        farmer_phone = (
            sale.get("farmer_phone")
            or sale.get("farmer_contact")
            or sale.get("unregistered_farmer_phone")
            or sale.get("phone")
            or "-"
        )

        farmer_key = f"{farmer_name}|{farmer_phone}"

        if farmer_key not in farmer_map:
            farmer_map[farmer_key] = {
                "farmer_name": farmer_name,
                "phone": farmer_phone,
                "total_orders": 0,
                "total_sales": 0,
            }

        farmer_map[farmer_key]["total_orders"] += 1
        farmer_map[farmer_key]["total_sales"] += amount

    mitra_sales = list(mitra_map.values())
    farmer_sales = list(farmer_map.values())

    mitra_sales.sort(key=lambda x: x.get("total_sales", 0), reverse=True)
    farmer_sales.sort(key=lambda x: x.get("total_sales", 0), reverse=True)

    return {
        "mitra_sales": mitra_sales,
        "farmer_sales": farmer_sales,
    }

@dashboard_bp.route("/")
def home():
    is_json = request.is_json or request.args.get("user_id")

    if is_json:
        user_id = request.args.get("user_id", "").strip()

        if not user_id:
            return jsonify({"ok": False, "message": "User ID is required"}), 400

        user = mongo.db.users.find_one({"_id": ObjectId(user_id)})

        if not user:
            return jsonify({"ok": False, "message": "User not found"}), 404

        role = (user.get("role") or "").strip().lower()

        latest_validation = mongo.db.validations.find_one(
            {"entity_id": user_id},
            sort=[("updated_at", -1), ("created_at", -1)]
        ) or {}

        approval = user.get("approval_status") or "pending_profile"

        # ─────────────────────────────────────────────
        # ❌ If NOT approved → return status only
        # ─────────────────────────────────────────────
        if approval != "approved":
            return jsonify({
                "ok": True,
                "approval_status": approval,
                "rejection_reason": (
                    user.get("latest_rejection_reason")
                    or latest_validation.get("rejection_reason")
                    or latest_validation.get("action_remarks")
                    or ""
                ),
                "data": None
            }), 200

        # ─────────────────────────────────────────────
        # ✅ ROLE BASED DASHBOARD
        # ─────────────────────────────────────────────

        # 🔹 UFC ADMIN DASHBOARD
        if role == "ufc_admin":
            centre_uid = user.get("centre_uid") or ""
            data = get_centre_dashboard(centre_uid)

            app_sales = get_ufc_admin_app_sales_summary(centre_uid)

            centre_master = (
                mongo.db.ufc_admin_master.find_one({"linked_user_id": str(user.get("_id"))})
                or mongo.db.ufc_admin_master.find_one({"linked_user_id": user.get("_id")})
                or mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid})
                or mongo.db.ufc_centre_master.find_one({"centre_uid": centre_uid})
                or {}
            )

            centre_name = (
                centre_master.get("name_of_enterprise")
                or centre_master.get("centre_name")
                or centre_master.get("name")
                or user.get("name_of_enterprise")
                or user.get("centre_name")
                or user.get("name")
                or "UFC Admin"
            )
            return jsonify(json_safe({
                "ok": True,
                "approval_status": "approved",
                "role": role,
                "data": {
                    "centre_uid": centre_uid,
                    "centre_name": centre_name,
                    "mitra_count": data.get("mitra_count", 0),
                    "farmer_count": data.get("farmer_count", 0),

                    # Existing order data
                    "orders": data.get("orders", []),

                    # Same dashboard sections as web
                    "mitra_sales": (
                        data.get("mitra_sales")
                        or data.get("mitra_wise_sales")
                        or data.get("mitra_sales_rows")
                        or app_sales.get("mitra_sales", [])
                    ),
                    "farmer_sales": (
                        data.get("farmer_sales")
                        or data.get("farmer_wise_sales")
                        or data.get("farmer_sales_rows")
                        or app_sales.get("farmer_sales", [])
                    ),
                    "recent_orders": (
                        data.get("recent_orders")
                        or data.get("orders")
                        or []
                    ),
                }
            })), 200

        # 🔹 UFC MITRA DASHBOARD
        if role == "ufc_mitra":
            data = get_mitra_dashboard(user.get("mitra_uid"))

            return jsonify(json_safe({
                "ok": True,
                "approval_status": "approved",
                "role": role,
                "data": {
                    "mitra_uid": user.get("mitra_uid") or "",
                    "farmer_count": data.get("farmer_count", 0),
                    "input_bonus": data.get("input_bonus", 0),
                    "output_bonus": data.get("output_bonus", 0),
                    "monthly_sales": data.get("monthly_sales", []),
                    "farmers": data.get("farmers", [])
                }
            })), 200

        # 🔹 FARMER DASHBOARD
        if role == "farmer":
            data = get_farmer_dashboard(user.get("phone"))

            if not isinstance(data, dict):
                data = {}

            data = _normalize_dashboard_products(data)

            # Add farmer marketplace products directly for app dashboard.
            data["farmer_products"] = _get_app_farmer_products(user)

            # IMPORTANT:
            # App AVPL orders are inserted into mongo.db.orders with user_id/farmer_user_id.
            # So fetch them directly here instead of depending only on get_farmer_dashboard(phone).
            app_orders = _get_app_farmer_orders(user)

            data["orders"] = app_orders
            data["recent_orders"] = app_orders
            data["my_orders"] = app_orders
            data["total_orders"] = len(app_orders)
            data["total_purchase"] = sum(
                float(order.get("total_amount") or 0)
                for order in app_orders
            )

            return jsonify(json_safe({
                "ok": True,
                "approval_status": "approved",
                "role": role,
                "data": data
            })), 200

        # ❌ fallback
        return jsonify({"ok": False, "message": "Invalid role"}), 403

    # WEB FLOW BELOW
    if not session.get("user_id"):
        return redirect(url_for("auth.login_select"))

    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})
    role = session.get("role")

    approval = (
        (user or {}).get("approval_status")
        or session.get("approval_status")
        or "pending"
    )

    session["approval_status"] = approval

    if role == "ufc_admin" and approval == "pending_profile":
        return redirect(url_for("auth.complete_ufc_admin"))

    if role == "ufc_mitra" and approval == "pending_profile":
        return redirect(url_for("auth.complete_ufc_mitra"))

    if role in {"ufc_admin", "ufc_mitra", "farmer"} and approval != "approved":
        return redirect(url_for("dashboard.pending_access"))

    if role == "super_admin":
        return render_template("dashboard/super_admin.html", stats=get_system_overview())

    if role == "avpl_admin":
        return render_template("dashboard/avpl_admin.html", stats=get_system_overview())

    if role == "accounts":
        product_summary, farmer_products = get_farmer_product_dashboard()
        try:
            accounts_overview = get_accounts_dashboard_overview(session.get("user_id"))
        except (ValueError, PermissionError, RuntimeError) as exc:
            current_app.logger.warning("Accounts dashboard financial overview unavailable: %s", exc)
            accounts_overview = {
                "supplier_purchase_value": "0.00",
                "ufc_sales_value": "0.00",
                "supplier_outstanding": "0.00",
                "ufc_receivable": "0.00",
                "cost_of_goods_sold": "0.00",
                "gross_margin": "0.00",
                "gross_margin_percent": "0.00",
                "supplier_order_count": 0,
                "ufc_order_count": 0,
                "transaction_count": 0,
            }
        return render_template(
            "dashboard/accounts.html",
            stats=get_system_overview(),
            accounts_overview=accounts_overview,
            product_summary=product_summary,
            farmer_products=farmer_products,
        )

    if role == "sales_nelocals":
        product_summary, farmer_products = get_farmer_product_dashboard()
        return render_template("dashboard/sales_nelocals.html", product_summary=product_summary, farmer_products=farmer_products)

    if role == "sales_unnatfarm":
        product_summary, farmer_products = get_farmer_product_dashboard()
        ufc_sales_trends = get_ufc_admin_sales_trends()

        return render_template(
            "dashboard/sales_unnatfarm.html",
            product_summary=product_summary,
            farmer_products=farmer_products,
            ufc_sales_trends=ufc_sales_trends
        )

    if role == "ufc_admin":
        data = get_centre_dashboard(session.get("centre_uid"))
        return render_template("dashboard/ufc_admin.html", data=data)

    if role == "ufc_mitra":
        data = get_mitra_dashboard(session.get("mitra_uid"))

        mitra_master = (
            mongo.db.ufc_mitra_master.find_one({"linked_user_id": session.get("user_id")})
            or mongo.db.ufc_mitra_master.find_one({"linked_user_id": ObjectId(session.get("user_id"))})
            or mongo.db.ufc_mitra_master.find_one({"mitra_uid": session.get("mitra_uid")})
            or {}
        )

        passport_doc = mongo.db.documents.find_one(
            {
                "linked_user_id": session.get("user_id"),
                "$or": [
                    {"document_type": "Passport Size Photo"},
                    {"label": "Passport Size Photo"},
                    {"title": "Passport Size Photo"},
                    {"doc_type": "Passport Size Photo"},
                ]
            },
            sort=[("created_at", -1)]
        )

        data["profile_photo"] = (
            mitra_master.get("passport_photo_file")
            or (passport_doc or {}).get("file_path")
            or (passport_doc or {}).get("filename")
            or (passport_doc or {}).get("file_name")
        )

        data["mitra_name"] = (
            mitra_master.get("name")
            or (user or {}).get("name")
            or session.get("name")
            or "Mitra User"
        )

        return render_template("dashboard/ufc_mitra.html", data=data)

    if role == "farmer":
        data = get_farmer_dashboard(user.get("phone"))

        if not isinstance(data, dict):
            data = {}

        farmer_master = (
            mongo.db.farmer_master.find_one({"linked_user_id": session.get("user_id")})
            or mongo.db.farmer_master.find_one({"linked_user_id": ObjectId(session.get("user_id"))})
            or mongo.db.farmer_master.find_one({"contact_no": user.get("phone")})
            or {}
        )

        data["profile_photo"] = (
            farmer_master.get("profile_photo")
            or farmer_master.get("passport_photo_file")
            or farmer_master.get("photo")
        )

        data["farmer_name"] = (
            farmer_master.get("name")
            or user.get("name")
            or session.get("name")
            or "Farmer"
        )

        # Same order fetch logic used by app dashboard
        web_orders = _get_app_farmer_orders(user)

        data["orders"] = web_orders
        data["recent_orders"] = web_orders
        data["my_orders"] = web_orders
        data["total_orders"] = len(web_orders)
        data["total_purchase"] = sum(
            float(order.get("total_amount") or 0)
            for order in web_orders
        )

        return render_template("dashboard/farmer.html", data=data)

    return render_template("dashboard/pending_access.html")

@dashboard_bp.route("/pending-access")
@login_required
def pending_access():
    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})

    role = (session.get("role") or "").strip().lower().replace(" ", "_")

    approval = (
        (user or {}).get("approval_status")
        or session.get("approval_status")
        or "pending"
    )

    session["approval_status"] = approval

    latest_validation = mongo.db.validations.find_one(
        {"entity_id": session["user_id"]},
        sort=[("updated_at", -1), ("created_at", -1)]
    ) or {}

    rejection_reason = (
        (user or {}).get("latest_rejection_reason")
        or latest_validation.get("rejection_reason")
        or latest_validation.get("action_remarks")
        or ""
    )

    if rejection_reason:
        session["rejection_reason"] = rejection_reason

    correction_url = None

    if role == "ufc_admin":
        correction_url = url_for("auth.complete_ufc_admin")

    elif role == "ufc_mitra":
        correction_url = url_for("auth.complete_ufc_mitra")

    elif role == "farmer":
        correction_url = url_for("auth.complete_farmer")

    return render_template(
        "dashboard/pending_access.html",
        user=user,
        latest_validation=latest_validation,
        rejection_reason=rejection_reason,
        correction_url=correction_url,
    )

#changes by atlanta
@dashboard_bp.route("/app-image/<path:filename>")
def app_image(filename):
    if not filename:
        abort(404)

    filename = str(filename).replace("\\", "/").strip()
    clean_name = Path(filename).name

    doc = mongo.db.documents.find_one({
        "$or": [
            {"filename": filename},
            {"filename": clean_name},
            {"file_name": filename},
            {"file_name": clean_name},
            {"stored_name": filename},
            {"stored_name": clean_name},
            {"original_name": filename},
            {"original_name": clean_name},
            {"file_path": {"$regex": clean_name, "$options": "i"}},
            {"path": {"$regex": clean_name, "$options": "i"}},
        ]
    })

    possible_names = [clean_name]

    if doc:
        for key in ["file_path", "path", "url", "filename", "file_name", "stored_name", "original_name"]:
            value = doc.get(key)
            if value:
                possible_names.append(Path(str(value).replace("\\", "/")).name)

    possible_names = list(dict.fromkeys(possible_names))

    for name in possible_names:
        file_path = _find_document_path(name)
        if file_path:
            return send_file(
                file_path,
                as_attachment=False,
                download_name=Path(file_path).name,
                conditional=True,
            )

    abort(404)