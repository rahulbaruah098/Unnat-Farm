from flask import Blueprint, render_template, request, session, Response, jsonify
import csv
import io
from bson import ObjectId
from datetime import datetime
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required

master_bp = Blueprint("master", __name__, url_prefix="/master")

def build_search_or(search, fields):
    if not search:
        return None

    return [
        {field: {"$regex": search, "$options": "i"}}
        for field in fields
    ]

def wants_json_response():
    return (
        request.headers.get("Accept") == "application/json"
        or request.is_json
        or request.args.get("format") == "json"
    )


def json_safe(value):
    if isinstance(value, ObjectId):
        return str(value)

    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, list):
        return [json_safe(item) for item in value]

    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}

    return value

def build_farmer_master_query():
    query = {}

    if session.get("role") == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")
    elif session.get("role") == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")

    search = request.args.get("q", "").strip()

    search_or = build_search_or(search, [
        "name",
        "contact_no",
        "phone",
        "mobile",
        "centre_uid",
        "mitra_uid",
        "state",
        "district",
        "block",
        "village"
    ])

    if search_or:
        query["$or"] = search_or

    return query, search


@master_bp.route("/farmers")
@login_required
@roles_required("super_admin", "avpl_admin", "ufc_admin", "ufc_mitra")
def farmers():
    query, search = build_farmer_master_query()

    items = list(mongo.db.farmer_master.find(query).sort("created_at", -1))

    return render_template(
        "master/farmers.html",
        items=items,
        title="Farmer Master Data",
        q=search
    )



@master_bp.route("/farmers/download-csv")
@login_required
@roles_required("super_admin", "avpl_admin", "ufc_admin", "ufc_mitra")
def download_farmers_csv():
    query, search = build_farmer_master_query()

    farmers_data = list(
        mongo.db.farmer_master.find(query).sort("created_at", -1)
    )

    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
    "Sl. No.",
    "Name",
    "Contact No",
    "Gender",
    "Age",
    "Centre UID",
    "Mitra UID",
    "State",
    "District",
    "Block",
    "Village",
    "Activities",
    "Agri Sub Categories",
    "Approval Status"
]

    writer.writerow(headers)

    def csv_text(value):
        if value is None:
            return ""
        value = str(value).strip()
        if not value:
            return ""
        return f'="{value}"'

    for index, farmer in enumerate(farmers_data, start=1):
        activities = farmer.get("activities", "")
        if isinstance(activities, list):
            activities = ", ".join(str(x) for x in activities)

        agri_sub_categories = farmer.get("agri_sub_categories", "")
        if isinstance(agri_sub_categories, list):
            agri_sub_categories = ", ".join(str(x) for x in agri_sub_categories)

        writer.writerow([
    index,
    farmer.get("name", ""),
    csv_text(farmer.get("contact_no", "")),
    farmer.get("gender", ""),
    farmer.get("age", ""),
    farmer.get("centre_uid", ""),
    farmer.get("mitra_uid", ""),
    farmer.get("state", ""),
    farmer.get("district", ""),
    farmer.get("block", ""),
    farmer.get("village", ""),
    activities,
    agri_sub_categories,
    farmer.get("approval_status", ""),
])

    csv_data = output.getvalue()
    output.close()

    filename_date = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"farmer_master_data_{filename_date}.csv"

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )

@master_bp.route("/ufc-admins")
@login_required
@roles_required("super_admin")
def ufc_admins():
    query = {}
    search = request.args.get("q", "").strip()

    search_or = build_search_or(search, [
        "name",
        "phone",
        "contact_no",
        "mobile",
        "username",
        "email",
        "centre_uid",
        "state",
        "district",
        "block",
        "village"
    ])

    if search_or:
        query["$or"] = search_or

    items = list(mongo.db.ufc_admin_master.find(query).sort("created_at", -1))

    return render_template(
        "master/ufc_admins.html",
        items=items,
        title="UFC Admin Master Data",
        q=search
    )

#changes by atlanta
@master_bp.route("/ufc-mitras")
@login_required
@roles_required("super_admin")
def ufc_mitras():
    query = {}

    if session.get("role") == "ufc_admin":
        query["mapped_centre_uid"] = session.get("centre_uid")

    search = request.args.get("q", "").strip()

    search_or = build_search_or(search, [
        "name",
        "phone",
        "contact_no",
        "mobile",
        "username",
        "email",
        "mitra_uid",
        "mapped_centre_uid",
        "centre_uid",
        "state",
        "district",
        "block",
        "village"
    ])

    if search_or:
        query["$or"] = search_or

    items = list(mongo.db.ufc_mitra_master.find(query).sort("created_at", -1))

    for item in items:
        linked_user_id = item.get("linked_user_id")
        user = None

        if linked_user_id:
            try:
                user = mongo.db.users.find_one({"_id": ObjectId(linked_user_id)})
            except Exception:
                user = None

            if not user:
                user = mongo.db.users.find_one({"_id": linked_user_id})

        if not user:
            user = mongo.db.users.find_one({
                "$or": [
                    {"mitra_uid": item.get("mitra_uid")},
                    {"mapped_mitra_uid": item.get("mitra_uid")},
                    {"username": item.get("username")},
                    {"phone": item.get("phone")},
                ]
            }) or {}

        item["name"] = item.get("name") or user.get("name") or "-"
        item["phone"] = (
            item.get("phone")
            or item.get("contact_no")
            or item.get("mobile")
            or user.get("phone")
            or user.get("contact_no")
            or user.get("mobile")
            or "-"
        )
        item["username"] = item.get("username") or user.get("username") or "-"
        item["email"] = item.get("email") or user.get("email") or "-"
        item["approval_status"] = item.get("approval_status") or user.get("approval_status") or "pending_profile"

    if wants_json_response():
        return jsonify(json_safe({
            "ok": True,
            "items": items,
            "q": search
        }))

    return render_template(
        "master/ufc_mitras.html",
        items=items,
        title="UFC Mitra Master Data",
        q=search
    )