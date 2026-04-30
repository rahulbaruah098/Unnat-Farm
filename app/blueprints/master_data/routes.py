from flask import Blueprint, render_template, request, session
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


@master_bp.route("/farmers")
@login_required
@roles_required("super_admin", "avpl_admin", "ufc_admin", "ufc_mitra")
def farmers():
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

    items = list(mongo.db.farmer_master.find(query).sort("created_at", -1))

    return render_template(
        "master/farmers.html",
        items=items,
        title="Farmer Master Data",
        q=search
    )


@master_bp.route("/ufc-admins")
@login_required
@roles_required("super_admin", "avpl_admin")
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


@master_bp.route("/ufc-mitras")
@login_required
@roles_required("super_admin", "avpl_admin", "ufc_admin")
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

    return render_template(
        "master/ufc_mitras.html",
        items=items,
        title="UFC Mitra Master Data",
        q=search
    )