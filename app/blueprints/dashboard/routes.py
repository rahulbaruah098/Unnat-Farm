from bson import ObjectId
from flask import Blueprint, render_template, session, redirect, url_for
from app.extensions import mongo
from app.utils.decorators import login_required
from app.services.dashboard_service import (
    get_system_overview,
    get_centre_dashboard,
    get_mitra_dashboard,
    get_farmer_dashboard,
)

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")


def _to_number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


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



@dashboard_bp.route("/")
@login_required
def home():
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
        return render_template("dashboard/accounts.html", stats=get_system_overview(), product_summary=product_summary, farmer_products=farmer_products)

    if role == "sales_nelocals":
        product_summary, farmer_products = get_farmer_product_dashboard()
        return render_template("dashboard/sales_nelocals.html", product_summary=product_summary, farmer_products=farmer_products)

    if role == "sales_unnatfarm":
        product_summary, farmer_products = get_farmer_product_dashboard()
        return render_template("dashboard/sales_unnatfarm.html", product_summary=product_summary, farmer_products=farmer_products)

    if role == "ufc_admin":
        data = get_centre_dashboard(session.get("centre_uid"))
        return render_template("dashboard/ufc_admin.html", data=data)

    if role == "ufc_mitra":
        data = get_mitra_dashboard(session.get("mitra_uid"))
        return render_template("dashboard/ufc_mitra.html", data=data)

    if role == "farmer":
        data = get_farmer_dashboard(user.get("phone"))
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

    return render_template(
        "dashboard/pending_access.html",
        user=user,
        latest_validation=latest_validation,
        rejection_reason=rejection_reason,
        correction_url=correction_url,
    )