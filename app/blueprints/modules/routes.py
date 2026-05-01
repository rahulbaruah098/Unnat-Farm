import re
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
                flash(str(exc), "danger")
                return redirect(url_for("modules.sell"))

        mongo.db.farmer_products.insert_one({
            "farmer_user_id": session["user_id"],
            "farmer_name": farmer.get("name") or user.get("name"),
            "farmer_contact": farmer.get("contact_no") or user.get("phone"),
            "centre_uid": farmer.get("centre_uid") or user.get("centre_uid") or user.get("mapped_centre_uid"),
            "mitra_uid": farmer.get("mitra_uid") or user.get("mitra_uid") or user.get("mapped_mitra_uid"),
            "state": farmer.get("state") or user.get("state"),
            "district": farmer.get("district") or user.get("district"),
            "block": farmer.get("block") or user.get("block"),
            "village": farmer.get("village") or user.get("village"),
            "product_name": request.form.get("product_name", "").strip(),
            "variety": request.form.get("variety", "").strip(),
            "average_size": request.form.get("average_size", "").strip(),
            "available_quantity": float(request.form.get("quantity") or 0),
            "unit_price": float(request.form.get("price") or 0),
            "picture": picture,
            "status": "active",
            "created_at": now_utc(),
            "updated_at": now_utc(),
        })

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

    return render_template(
        "modules/sell.html",
        posts=posts,
        mitra_sell_mode=False,
        q=q
    )


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

    return render_template(
        "modules/finance.html",
        items=items,
        total_transaction=total_transaction,
        is_eligible=is_eligible,
        q=q
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

    q = request.args.get("q", "").strip()

    insurance_query = {
        "requested_by": session["user_id"]
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

    q = request.args.get("q", "").strip()

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

    return render_template("modules/lms.html", items=items, q=q)


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

    q = request.args.get("q", "").strip()

    ticket_query = {
        "user_id": session["user_id"]
    }

    if q:
        ticket_query["$or"] = [
            {"subject": {"$regex": q, "$options": "i"}},
            {"message": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}}
        ]

    tickets = list(
        mongo.db.support_tickets.find(ticket_query).sort("created_at", -1)
    )

    return render_template(
        "modules/support.html",
        tickets=tickets,
        q=q
    )


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
    return render_template("modules/orders.html", items=items, q=q)


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

    q = request.args.get("q", "").strip()

    if q:
        query["$or"] = [
            {"transaction_type": {"$regex": q, "$options": "i"}},
            {"product_name": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}},
            {"farmer_contact": {"$regex": q, "$options": "i"}}
        ]

        try:
            numeric_q = float(q)
            query["$or"].append({"amount": numeric_q})
        except ValueError:
            pass

    items = list(mongo.db.transactions.find(query).sort("created_at", -1))
    return render_template("modules/transactions.html", items=items, q=q)

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
    q = request.args.get("q", "").strip()

    purchase_query = {
        "farmer_contact": user.get("phone"),
        "transaction_type": "input_purchase"
    }

    if q:
        purchase_query["$or"] = [
            {"product_name": {"$regex": q, "$options": "i"}},
            {"farmer_contact": {"$regex": q, "$options": "i"}}
        ]

        try:
            numeric_q = float(q)
            purchase_query["$or"].append({"amount": numeric_q})
        except ValueError:
            pass

    items = list(mongo.db.transactions.find(purchase_query).sort("created_at", -1).limit(20))
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

    notification_query = {
        "to_user_id": session.get("user_id")
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

    return render_template(
        "modules/mitra_stock.html",
        items=items,
        q=q
    )

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

    q = request.args.get("q", "").strip()

    query = {
        "centre_uid": centre_uid
    }

    if q:
        query["$or"] = [
            {"farmer_name": {"$regex": q, "$options": "i"}},
            {"product_name": {"$regex": q, "$options": "i"}},
            {"status": {"$regex": q, "$options": "i"}},
            {"created_at": {"$regex": q, "$options": "i"}},
            {"mitra_uid": {"$regex": q, "$options": "i"}},
            {"centre_uid": {"$regex": q, "$options": "i"}}
        ]

    orders = list(
        mongo.db.orders.find(query).sort("created_at", -1)
    )

    return render_template(
        "modules/centre_orders.html",
        orders=orders,
        q=q
    )

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




