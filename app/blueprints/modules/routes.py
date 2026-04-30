from bson import ObjectId
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify
from app.extensions import mongo
from app.utils.decorators import login_required, roles_required
from app.utils.helpers import now_utc
from app.utils.security import save_file
from datetime import datetime

modules_bp = Blueprint("modules", __name__, url_prefix="/modules")


@modules_bp.route("/buy", methods=["GET", "POST"])
@login_required
def buy():
    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = float(request.form.get("quantity") or 0)

        product = mongo.db.farmer_products.find_one({
            "_id": ObjectId(product_id)
        })

        if not product:
            flash("Product not found.", "danger")
            return redirect(url_for("modules.buy"))

        available_qty = float(product.get("available_quantity") or 0)

        if quantity <= 0:
            flash("Quantity must be greater than zero.", "danger")
            return redirect(url_for("modules.buy"))

        if quantity > available_qty:
            flash("Not enough quantity available.", "danger")
            return redirect(url_for("modules.buy"))

        mitra_uid = session.get("mitra_uid")
        centre_uid = session.get("centre_uid")

        unit_price = float(product.get("unit_price") or 0)
        total_amount = quantity * unit_price

        # ✅ Purchase record
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

        mongo.db.mitra_product_purchases.insert_one(purchase_doc)

        # ✅ Notifications
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

        # ✅ Add to Mitra stock
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

        # ✅ Reduce farmer quantity
        current_qty = float(product.get("available_quantity") or 0)
        new_qty = current_qty - quantity

        mongo.db.farmer_products.update_one(
            {"_id": ObjectId(product_id)},
            {
                "$set": {
                    "available_quantity": new_qty,
                    "updated_at": now_utc()
                }
            }
        )

        # ✅ Transaction log
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

        flash(f"{quantity} {product.get('product_name')} purchased and added to your stock.", "success")
        return redirect(url_for("modules.buy"))

    # ===== GET LOGIC =====
    products = list(mongo.db.products.find({}).sort("created_at", -1))

    farmer_query = {"status": "active"}

    if session.get("role") == "ufc_mitra":
        farmer_query["mitra_uid"] = session.get("mitra_uid")
    elif session.get("role") == "ufc_admin":
        farmer_query["centre_uid"] = session.get("centre_uid")

    farmer_products = list(
        mongo.db.farmer_products.find(farmer_query).sort("created_at", -1)
    )

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
                flash("Selected stock product not found.", "danger")
                return redirect(url_for("modules.sell"))

            available_quantity = float(stock_item.get("available_quantity") or 0)

            if quantity <= 0:
                flash("Quantity must be greater than zero.", "danger")
                return redirect(url_for("modules.sell"))

            if quantity > available_quantity:
                flash("Sale quantity cannot be greater than available stock.", "danger")
                return redirect(url_for("modules.sell"))

            buyer_farmer = None

            if buyer_type == "farmer":
                if not buyer_farmer_id:
                    flash("Please select buyer farmer.", "danger")
                    return redirect(url_for("modules.sell"))

                buyer_farmer = mongo.db.farmer_master.find_one({
                    "_id": ObjectId(buyer_farmer_id),
                    "centre_uid": centre_uid
                })

                if not buyer_farmer:
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

            flash("Product sold successfully.", "success")
            return redirect(url_for("modules.sell"))

        stock_items = list(mongo.db.mitra_product_stock.find({
            "mitra_uid": mitra_uid,
            "available_quantity": {"$gt": 0}
        }).sort("created_at", -1))

        farmers = list(mongo.db.farmer_master.find({
            "centre_uid": centre_uid
        }).sort("name", 1))

        sales = list(mongo.db.mitra_product_sales.find({
            "mitra_uid": mitra_uid
        }).sort("created_at", -1))

        return render_template(
            "modules/sell.html",
            mitra_sell_mode=True,
            stock_items=stock_items,
            farmers=farmers,
            sales=sales
        )

    # EXISTING FARMER / OTHER ROLE SELL MODULE - keep unchanged
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
    return render_template("modules/sell.html", posts=posts, mitra_sell_mode=False)


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
            "mitra_uid": farmer.get("mitra_uid"),
            "amount": amount,
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
    user = mongo.db.users.find_one({
        "_id": ObjectId(session["user_id"])
    }) or {}

    farmer = mongo.db.farmer_master.find_one({
        "linked_user_id": session["user_id"]
    }) or mongo.db.farmer_master.find_one({
        "linked_user_id": ObjectId(session["user_id"])
    }) or mongo.db.farmer_master.find_one({
        "contact_no": user.get("phone")
    }) or {}

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
        activity in livestock_activities for activity in farmer_activities
    )

    is_eligible = is_livestock_farmer and total_transaction >= 30000

    if request.method == "POST":
        if not is_eligible:
            flash(
                "Insurance can be applied only by livestock farmers after completing more than ₹30,000 AVPL sales transaction.",
                "danger"
            )
            return redirect(url_for("modules.insurance"))

        mongo.db.insurance_requests.insert_one({
            "requested_by": session["user_id"],
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_mobile": farmer_phone,
            "centre_uid": farmer.get("centre_uid"),
            "mitra_uid": farmer.get("mitra_uid"),
            "livestock_type": request.form.get("livestock_type"),
            "remarks": request.form.get("remarks"),
            "total_transaction": total_transaction,
            "is_livestock_farmer": is_livestock_farmer,
            "status": "pending",
            "created_at": now_utc(),
        })

        flash("Insurance request submitted.", "success")
        return redirect(url_for("modules.insurance"))

    items = list(
        mongo.db.insurance_requests.find({
            "requested_by": session["user_id"]
        }).sort("created_at", -1)
    )

    return render_template(
        "modules/insurance.html",
        items=items,
        total_transaction=total_transaction,
        is_livestock_farmer=is_livestock_farmer,
        is_eligible=is_eligible
    )


@modules_bp.route("/insurance/leads")
@login_required
@roles_required("ufc_mitra")
def insurance_leads():
    items = list(mongo.db.insurance_requests.find({
        "mitra_uid": session.get("mitra_uid")
    }).sort("created_at", -1))

    return render_template("modules/insurance_leads.html", items=items)

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

#changes by atlanta
@modules_bp.route("/profile")
def profile():
    is_app = request.args.get("user_id")

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

        docs = list(
            mongo.db.documents.find({"linked_user_id": str(user["_id"])}).sort("created_at", -1)
        )

        user["_id"] = str(user["_id"])

        if master and "_id" in master:
            master["_id"] = str(master["_id"])

        for d in docs:
            if "_id" in d:
                d["_id"] = str(d["_id"])

        return jsonify({
            "ok": True,
            "user": user,
            "master": master or {},
            "docs": docs,
        })

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

    docs = list(
        mongo.db.documents.find({"linked_user_id": str(user["_id"])}).sort("created_at", -1)
    ) if user else []

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
        mitra_uid = request.form.get('mitra_uid', '').strip()

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
        product = mongo.db.products.find_one({'_id': ObjectId(product_id)}) if product_id else None

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
        mitras=mitras,
        products=products,
        sales=sales
    )

@modules_bp.route("/all-orders")
@login_required
@roles_required("avpl_admin")
def all_orders():
    orders = list(mongo.db.orders.find({}).sort("created_at", -1))
    return render_template("modules/all_orders.html", orders=orders)

@modules_bp.route('/pos/invoice/<sale_id>')
@login_required
@roles_required('ufc_admin')
def pos_invoice(sale_id):
    sale = mongo.db.pos_sales.find_one({'_id': ObjectId(sale_id)})

    if not sale:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('modules.pos'))

    return render_template('modules/pos_invoice.html', sale=sale)

@modules_bp.route("/mitra-earnings")
@login_required
@roles_required("ufc_mitra")
def mitra_earnings():
    user_id = session.get("user_id")

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

    recent_sales = sorted(
        monthly_pos_sales + monthly_farmer_sales,
        key=lambda x: x.get("created_at"),
        reverse=True
    )[:20]

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
        recent_sales=recent_sales
    )

@modules_bp.route("/finance/leads")
@login_required
@roles_required("avpl_admin", "sales_nelocals", "sales_unnatfarm", "accounts", "ufc_mitra")
def finance_leads():
    role = session.get("role")

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

    return render_template(
        "modules/finance_leads.html",
        leads=leads
    )
    
@modules_bp.route("/sales-details")
@login_required
@roles_required("avpl_admin", "sales_unnatfarm", "accounts")
def sales_details():
    sales = list(mongo.db.pos_sales.find({}).sort("created_at", -1))

    return render_template(
        "modules/sales_details.html",
        sales=sales
    )    
    
@modules_bp.route("/notifications")
@login_required
def notifications():
    items = list(
        mongo.db.notifications.find({
            "to_user_id": session.get("user_id")
        }).sort("created_at", -1)
    )

    mongo.db.notifications.update_many(
        {
            "to_user_id": session.get("user_id"),
            "status": "unread"
        },
        {
            "$set": {
                "status": "read",
                "read_at": now_utc()
            }
        }
    )

    return render_template("modules/notifications.html", items=items)    

@modules_bp.route("/mitra-stock")
@login_required
@roles_required("ufc_mitra")
def mitra_stock():
    items = list(mongo.db.mitra_product_stock.find({
        "mitra_uid": session.get("mitra_uid")
    }).sort("created_at", -1))

    for item in items:
        item["low_stock"] = float(item.get("available_quantity") or 0) < 5

    return render_template("modules/mitra_stock.html", items=items)

@modules_bp.route("/centre-orders", methods=["GET", "POST"])
@login_required
@roles_required("ufc_admin")
def centre_orders():
    centre_uid = session.get("centre_uid")

    if request.method == "POST":
        order_id = request.form.get("order_id")
        status = request.form.get("status")

        order = mongo.db.orders.find_one({
            "_id": ObjectId(order_id),
            "centre_uid": centre_uid
        })

        if not order:
            flash("Order not found for this centre.", "danger")
            return redirect(url_for("modules.centre_orders"))

        mongo.db.orders.update_one(
            {"_id": ObjectId(order_id)},
            {
                "$set": {
                    "status": status,
                    "updated_at": now_utc()
                }
            }
        )

        mongo.db.notifications.insert_one({
            "to_user_id": order.get("farmer_user_id"),
            "role": "farmer",
            "title": "Order Status Updated",
            "message": f"Your order for {order.get('product_name')} is now {status}.",
            "status": "unread",
            "created_at": now_utc()
        })

        flash("Order status updated and farmer notified.", "success")
        return redirect(url_for("modules.centre_orders"))

    orders = list(mongo.db.orders.find({
        "centre_uid": centre_uid
    }).sort("created_at", -1))

    return render_template("modules/centre_orders.html", orders=orders)

@modules_bp.route("/farmer/order", methods=["POST"])
@login_required
@roles_required("farmer")
def place_farmer_order():
    product_id = request.form.get("product_id")
    product_name = request.form.get("product_name")
    quantity = float(request.form.get("quantity") or 0)
    unit_price = float(request.form.get("unit_price") or 0)

    if quantity <= 0:
        flash("Invalid quantity.", "danger")
        return redirect(url_for("dashboard.home"))

    user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}

    farmer = mongo.db.farmer_master.find_one({
        "linked_user_id": session["user_id"]
    }) or mongo.db.farmer_master.find_one({
        "linked_user_id": ObjectId(session["user_id"])
    }) or mongo.db.farmer_master.find_one({
        "contact_no": user.get("phone")
    }) or {}

    total_amount = quantity * unit_price

    mongo.db.orders.insert_one({
        "product_id": product_id,
        "product_name": product_name,
        "quantity": quantity,
        "unit_price": unit_price,
        "total_amount": total_amount,
        "farmer_user_id": session.get("user_id"),
        "farmer_id": str(farmer.get("_id")),
        "farmer_name": farmer.get("name") or user.get("name"),
        "farmer_contact": farmer.get("contact_no") or user.get("phone"),
        "centre_uid": farmer.get("centre_uid"),
        "mitra_uid": farmer.get("mitra_uid"),
        "order_type": "farmer_purchase",
        "status": "placed",
        "created_at": now_utc()
    })

    flash("Order placed successfully.", "success")
    return redirect(url_for("dashboard.home"))