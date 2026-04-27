from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from app.extensions import mongo
from app.utils.decorators import login_required
from app.utils.helpers import now_utc
from app.utils.security import save_file

modules_bp = Blueprint("modules", __name__, url_prefix="/modules")


@modules_bp.route("/buy")
@login_required
def buy():
    products = list(mongo.db.products.find({}).sort("created_at", -1))
    farmer_products = list(mongo.db.farmer_products.find({"status": "active"}).sort("created_at", -1))
    return render_template("modules/buy.html", products=products, farmer_products=farmer_products)


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
            "available_quantity": request.form.get("available_quantity", "").strip(),
            "unit_price": request.form.get("unit_price", "").strip(),
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
    if request.method == "POST":
        mongo.db.marketplace_posts.insert_one({
            "posted_by": session["user_id"],
            "role": session["role"],
            "product_name": request.form.get("product_name"),
            "quantity": request.form.get("quantity"),
            "price": request.form.get("price"),
            "centre_uid": session.get("centre_uid") or session.get("mapped_centre_uid"),
            "status": "active",
            "created_at": now_utc(),
        })
        flash("Product posted for selling.", "success")
        return redirect(url_for("modules.sell"))
    posts = list(mongo.db.marketplace_posts.find({}).sort("created_at", -1))
    return render_template("modules/sell.html", posts=posts)


@modules_bp.route("/finance", methods=["GET", "POST"])
@login_required
def finance():
    if request.method == "POST":
        mongo.db.finance_requests.insert_one({
            "requested_by": session["user_id"],
            "role": session["role"],
            "amount": request.form.get("amount"),
            "purpose": request.form.get("purpose"),
            "status": "pending",
            "created_at": now_utc(),
        })
        flash("Finance request submitted.", "success")
        return redirect(url_for("modules.finance"))
    items = list(mongo.db.finance_requests.find({"requested_by": session["user_id"]}).sort("created_at", -1))
    return render_template("modules/finance.html", items=items)


@modules_bp.route("/insurance", methods=["GET", "POST"])
@login_required
def insurance():
    if request.method == "POST":
        mongo.db.insurance_requests.insert_one({
            "requested_by": session["user_id"],
            "livestock_type": request.form.get("livestock_type"),
            "remarks": request.form.get("remarks"),
            "status": "pending",
            "created_at": now_utc(),
        })
        flash("Insurance request submitted.", "success")
        return redirect(url_for("modules.insurance"))
    items = list(mongo.db.insurance_requests.find({"requested_by": session["user_id"]}).sort("created_at", -1))
    return render_template("modules/insurance.html", items=items)


@modules_bp.route("/lms")
@login_required
def lms():
    audience = session.get("role")
    items = list(mongo.db.lms_materials.find({"$or": [{"audience": audience}, {"audience": "all"}]}).sort("created_at", -1))
    return render_template("modules/lms.html", items=items)


@modules_bp.route("/support", methods=["GET", "POST"])
@login_required
def support():
    if request.method == "POST":
        mongo.db.support_tickets.insert_one({
            "user_id": session["user_id"],
            "role": session["role"],
            "subject": request.form.get("subject"),
            "message": request.form.get("message"),
            "status": "open",
            "created_at": now_utc(),
        })
        flash("Support ticket submitted.", "success")
        return redirect(url_for("modules.support"))
    tickets = list(mongo.db.support_tickets.find({"user_id": session["user_id"]}).sort("created_at", -1))
    return render_template("modules/support.html", tickets=tickets)


@modules_bp.route("/orders")
@login_required
def orders():
    query = {}
    if session.get("role") == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")
    elif session.get("role") == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")
    items = list(mongo.db.orders.find(query).sort("created_at", -1))
    return render_template("modules/orders.html", items=items)


@modules_bp.route("/products")
@login_required
def products():
    items = list(mongo.db.products.find({}).sort("created_at", -1))
    return render_template("modules/products.html", items=items)


@modules_bp.route("/transactions")
@login_required
def transactions():
    query = {}
    if session.get("role") == "ufc_admin":
        query["centre_uid"] = session.get("centre_uid")
    elif session.get("role") == "ufc_mitra":
        query["mitra_uid"] = session.get("mitra_uid")
    elif session.get("role") == "farmer":
        user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})
        query["farmer_contact"] = user.get("phone")
    items = list(mongo.db.transactions.find(query).sort("created_at", -1))
    return render_template("modules/transactions.html", items=items)


@modules_bp.route("/pos")
@login_required
def pos():
    return render_template("modules/pos.html")

@modules_bp.route("/profile")
@login_required
def profile():
    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})
    master = None
    role = user.get("role") if user else None
    if role == "farmer":
        master = mongo.db.farmer_master.find_one({"linked_user_id": str(user["_id"])})
    elif role == "ufc_admin":
        master = mongo.db.ufc_admin_master.find_one({"linked_user_id": str(user["_id"])})
    elif role == "ufc_mitra":
        master = mongo.db.ufc_mitra_master.find_one({"linked_user_id": str(user["_id"])})
    docs = list(mongo.db.documents.find({"linked_user_id": str(user["_id"])}).sort("created_at", -1)) if user else []
    return render_template("modules/profile.html", user=user, master=master, docs=docs)

@modules_bp.route("/purchases")
@login_required
def purchases():
    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])})
    items = list(mongo.db.transactions.find({"farmer_contact": user.get("phone"), "transaction_type": "input_purchase"}).sort("created_at", -1).limit(20))
    return render_template("modules/purchases.html", items=items)
