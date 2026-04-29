from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.utils.security import save_file
from datetime import datetime

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
@roles_required("farmer")
def finance():
    user_id = session.get("user_id")

    user = mongo.db.users.find_one({"_id": ObjectId(user_id)}) or {}

    farmer = mongo.db.farmer_master.find_one({
        "linked_user_id": user_id
    }) or mongo.db.farmer_master.find_one({
        "linked_user_id": ObjectId(user_id)
    }) or mongo.db.farmer_master.find_one({
        "contact_no": user.get("phone")
    }) or {}

    farmer_phone = farmer.get("contact_no") or user.get("phone")

    # 🔹 Calculate total POS transaction
    sales = list(mongo.db.pos_sales.find({
        "$or": [
            {"farmer_id": str(farmer.get("_id"))},
            {"farmer_phone": farmer_phone}
        ]
    }))

    total_transaction = sum(float(s.get("total_amount") or 0) for s in sales)

    is_eligible = total_transaction >= 30000

    if request.method == "POST":

        if not is_eligible:
            flash("You can apply only after completing ₹30,000 transaction value.", "danger")
            return redirect(url_for("modules.finance"))

        amount = request.form.get("amount")
        purpose = request.form.get("purpose")

        address_parts = [
            farmer.get("village"),
            farmer.get("block"),
            farmer.get("district"),
            farmer.get("state")
        ]
        farmer_address = ", ".join([x for x in address_parts if x])

        # 🔹 Save as LEAD
        mongo.db.financial_assistance_leads.insert_one({
            "farmer_user_id": user_id,
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_mobile": farmer_phone,
            "farmer_address": farmer_address,
            "centre_uid": farmer.get("centre_uid"),
            "amount": amount,
            "purpose": purpose,
            "total_transaction": total_transaction,
            "status": "new",
            "visible_to_roles": [
                "avpl_admin",
                "sales_nelocals",
                "sales_unnatfarm"
            ],
            "created_at": now_utc()
        })

        flash("Finance request submitted successfully.", "success")
        return redirect(url_for("modules.finance"))

    items = list(
        mongo.db.financial_assistance_leads.find({
            "farmer_user_id": user_id
        }).sort("created_at", -1)
    )

    return render_template(
        "modules/finance.html",
        items=items,
        total_transaction=total_transaction,
        is_eligible=is_eligible
    )


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
    user_id = session.get("user_id")

    query = {
        "$or": [
            {"audience": audience},
            {"audience": "all"}
        ]
    }

    if audience == "farmer":
        farmer_profile = mongo.db.farmer_master.find_one({
            "linked_user_id": user_id
        })

        if not farmer_profile:
            farmer_profile = mongo.db.farmer_master.find_one({
                "linked_user_id": ObjectId(user_id)
            })

        farmer_activities = farmer_profile.get("activities", []) if farmer_profile else []

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
                        {"activity_category": {"$in": farmer_activities}}
                    ]
                }
            ]
        }

    items = list(
        mongo.db.lms_materials.find(query).sort("created_at", -1)
    )

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
    items = list(mongo.db.transactions.find({
        "farmer_contact": user.get("phone"),
        "transaction_type": "input_purchase"
    }).sort("created_at", -1).limit(20))
    return render_template("modules/purchases.html", items=items)


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

    products = list(mongo.db.products.find({
        '$or': [
            {'available_centres': 'all'},
            {'available_centres': {'$in': ['all', centre_uid]}}
        ]
    }).sort('name', 1))

    if request.method == 'POST':
        sale_type = request.form.get('sale_type', 'registered')

        farmer_id = None
        farmer_name = ''
        farmer_phone = ''

        if sale_type == 'registered':
            farmer_id = request.form.get('farmer_id')
            farmer = mongo.db.farmer_master.find_one({'_id': ObjectId(farmer_id)}) if farmer_id else None

            if farmer:
                farmer_name = farmer.get('name', '')
                farmer_phone = farmer.get('contact_no', '')
        else:
            farmer_name = request.form.get('unregistered_farmer_name', '').strip()
            farmer_phone = request.form.get('unregistered_farmer_phone', '').strip()

        product_id = request.form.get('product_id')
        product = mongo.db.products.find_one({'_id': ObjectId(product_id)}) if product_id else None

        quantity = float(request.form.get('quantity') or 0)
        price = float(product.get('price') or 0) if product else 0
        total_amount = quantity * price

        sale_doc = {
            'centre_uid': centre_uid,
            'ufc_user_id': user_id,
            'sale_type': sale_type,
            'farmer_id': farmer_id,
            'farmer_name': farmer_name,
            'farmer_phone': farmer_phone,
            'product_id': product_id,
            'product_name': product.get('name') if product else '',
            'product_category': product.get('category') if product else '',
            'product_type': product.get('type') if product else '',
            'quantity': quantity,
            'unit_price': price,
            'total_amount': total_amount,
            'invoice_no': f"AVPL-{centre_uid}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}",
            'created_at': datetime.utcnow()
        }

        result = mongo.db.pos_sales.insert_one(sale_doc)

        flash('Sale recorded successfully. Invoice generated.', 'success')
        return redirect(url_for('modules.pos_invoice', sale_id=str(result.inserted_id)))

    sales = list(mongo.db.pos_sales.find({
        'centre_uid': centre_uid
    }).sort('created_at', -1).limit(20))

    return render_template(
        'modules/pos.html',
        centre_uid=centre_uid,
        farmers=mapped_farmers,
        products=products,
        sales=sales
    )


@modules_bp.route('/pos/invoice/<sale_id>')
@login_required
@roles_required('ufc_admin')
def pos_invoice(sale_id):
    sale = mongo.db.pos_sales.find_one({'_id': ObjectId(sale_id)})

    if not sale:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('modules.pos'))

    return render_template('modules/pos_invoice.html', sale=sale)

@modules_bp.route("/finance/leads")
@login_required
@roles_required("avpl_admin", "sales_nelocals", "sales_unnatfarm")
def finance_leads():
    role = session.get("role")

    leads = list(mongo.db.financial_assistance_leads.find({
        "visible_to_roles": role
    }).sort("created_at", -1))

    return render_template(
        "modules/finance_leads.html",
        leads=leads
    )