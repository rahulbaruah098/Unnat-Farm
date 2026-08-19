from __future__ import annotations

from datetime import date, datetime
from bson import ObjectId

from app.extensions import mongo


def _num(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _oid(value):
    try:
        return value if isinstance(value, ObjectId) else ObjectId(str(value))
    except Exception:
        return None


def _sum(collection, query, *fields):
    total = 0.0
    projection = {field: 1 for field in fields}
    for row in mongo.db[collection].find(query or {}, projection):
        for field in fields:
            if row.get(field) not in (None, ""):
                total += _num(row.get(field))
                break
    return round(total, 2)


def _lot_is_expired(row):
    expiry = row.get("expiry_date")
    if not expiry:
        return False
    try:
        if isinstance(expiry, datetime):
            expiry = expiry.date()
        elif not isinstance(expiry, date):
            expiry = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
        return expiry < date.today()
    except (TypeError, ValueError):
        return False


def _stock_totals(collection, query):
    """Return physical, reserved and truly saleable stock from lot records.

    Physical stock remains visible even if it is damaged/blocked/expired. Saleable
    stock excludes those classifications exactly as the order services do.
    """
    physical = reserved = saleable = 0.0
    projection = {
        "available_quantity": 1,
        "reserved_quantity": 1,
        "damaged_quantity": 1,
        "blocked_quantity": 1,
        "status": 1,
        "expiry_date": 1,
    }
    for row in mongo.db[collection].find(query or {}, projection):
        available = max(_num(row.get("available_quantity")), 0)
        held = min(max(_num(row.get("reserved_quantity")), 0), available)
        damaged = min(max(_num(row.get("damaged_quantity")), 0), available)
        blocked = min(max(_num(row.get("blocked_quantity")), 0), available)
        unusable = str(row.get("status") or "").lower() in {"cancelled", "expired"} or _lot_is_expired(row)

        physical += available
        reserved += held
        if not unusable:
            saleable += max(available - held - damaged - blocked, 0)

    return {
        "physical": round(physical, 4),
        "reserved": round(reserved, 4),
        "saleable": round(max(saleable, 0), 4),
    }


def _normalize_quantity(doc):
    for key in ("available_quantity", "quantity", "stock_quantity", "stock"):
        if doc.get(key) not in (None, ""):
            value = _num(doc.get(key))
            return int(value) if value.is_integer() else value
    return "-"


def normalize_quantity(doc):
    # Kept public for older callers.
    return _normalize_quantity(doc)


def get_system_overview():
    """Network-wide live dashboard using the Stage 2-9 source collections.

    Legacy counters are retained for backwards compatibility, while the new keys
    represent the actual linked procurement, inventory, sales and produce flows.
    """
    db = mongo.db
    avpl_stock = _stock_totals("avpl_inventory_lots", {"status": {"$ne": "cancelled"}})

    supplier_orders = db.avpl_purchase_orders.count_documents({"status": {"$ne": "cancelled"}})
    avpl_ufc_orders = db.avpl_ufc_orders.count_documents({"status": {"$ne": "cancelled"}})
    farmer_input_orders = db.ufc_farmer_orders.count_documents({"status": {"$ne": "cancelled"}})
    produce_orders = db.farmer_produce_marketplace_orders.count_documents({"status": {"$ne": "cancelled"}})
    linked_orders = supplier_orders + avpl_ufc_orders + farmer_input_orders + produce_orders

    pending_orders = (
        db.avpl_purchase_orders.count_documents({"status": {"$in": ["draft", "pending_approval", "returned_for_correction", "approved", "partially_received"]}})
        + db.avpl_ufc_orders.count_documents({"status": {"$in": ["requested", "approved", "dispatched"]}})
        + db.ufc_farmer_orders.count_documents({"status": {"$in": ["requested", "approved"]}})
        + db.farmer_produce_marketplace_orders.count_documents({"status": {"$in": ["requested", "approved", "dispatched"]}})
    )

    return {
        "total_users": db.users.count_documents({}),
        "total_ufc_admins": db.ufc_admin_master.count_documents({}),
        "total_ufc_mitras": db.ufc_mitra_master.count_documents({}),
        "total_farmers": db.farmer_master.count_documents({}),
        "pending_validations": db.validations.count_documents({"status": "pending"}),
        "total_products": db.products.count_documents({"is_deleted": {"$ne": True}}),
        "total_orders": linked_orders or db.orders.count_documents({}),
        "total_transactions": (
            db.payments.count_documents({})
            + db.avpl_supplier_invoices.count_documents({"status": {"$ne": "cancelled"}})
            + db.avpl_ufc_sales.count_documents({})
            + db.ufc_farmer_sales.count_documents({})
            + db.farmer_marketplace_sales.count_documents({})
        ),
        "supplier_order_count": supplier_orders,
        "avpl_ufc_order_count": avpl_ufc_orders,
        "farmer_input_order_count": farmer_input_orders,
        "produce_order_count": produce_orders,
        "pending_orders": pending_orders,
        "avpl_stock_physical": avpl_stock["physical"],
        "avpl_stock_reserved": avpl_stock["reserved"],
        "avpl_stock_saleable": avpl_stock["saleable"],
        "active_farmer_listings": db.farmer_produce_marketplace_listings.count_documents({"status": "published"}),
        "payments_recorded": db.payments.count_documents({"status": {"$ne": "reversed"}}),
        "supplier_outstanding": _sum("avpl_supplier_invoices", {"payable_posted": True, "posting_status": "posted"}, "outstanding_amount"),
        "ufc_receivable": _sum("avpl_receivables", {"status": {"$ne": "closed"}}, "outstanding_amount"),
        "farmer_receivable": _sum("farmer_marketplace_receivables", {"status": {"$ne": "closed"}}, "outstanding_amount"),
    }


def get_centre_dashboard(centre_uid):
    db = mongo.db
    centre_uid = str(centre_uid or "").strip()
    mitras = list(db.ufc_mitra_master.find({"mapped_centre_uid": centre_uid}).limit(20))
    farmers_count = db.farmer_master.count_documents({"centre_uid": centre_uid})

    # Stage 7 is the authoritative UFC -> Farmer order stream. Keep legacy orders
    # only as a fallback for older pre-reset data.
    orders = list(db.ufc_farmer_orders.find({"centre_uid": centre_uid}).sort("created_at", -1).limit(10))
    if not orders:
        orders = list(db.orders.find({"centre_uid": centre_uid}).sort("created_at", -1).limit(10))

    # Direct POS and Stage 7 fulfilled order sales are both real sales channels.
    pos_sales = list(db.pos_sales.find({"centre_uid": centre_uid}).sort("created_at", -1))
    order_sales = list(db.ufc_farmer_sales.find({"centre_uid": centre_uid}).sort("sale_date", -1))

    mitra_sales_map = {}
    farmer_sales_map = {}
    for sale in pos_sales:
        amount = _num(sale.get("total_amount") or sale.get("grand_total") or sale.get("amount"))
        mitra_uid = sale.get("mitra_uid") or sale.get("mapped_mitra_uid") or "Direct UFC"
        row = mitra_sales_map.setdefault(mitra_uid, {"mitra_uid": mitra_uid, "total_sales": 0.0, "total_orders": 0})
        row["total_sales"] += amount
        row["total_orders"] += 1
        farmer_key = sale.get("farmer_phone") or sale.get("farmer_name") or "Unknown Farmer"
        frow = farmer_sales_map.setdefault(farmer_key, {
            "farmer_name": sale.get("farmer_name") or "Unknown Farmer",
            "farmer_phone": sale.get("farmer_phone") or "-",
            "total_sales": 0.0,
            "total_orders": 0,
        })
        frow["total_sales"] += amount
        frow["total_orders"] += 1

    for sale in order_sales:
        amount = _num(sale.get("grand_total") or sale.get("total_amount"))
        farmer_key = str(sale.get("farmer_user_id") or sale.get("farmer_name") or "Unknown Farmer")
        frow = farmer_sales_map.setdefault(farmer_key, {
            "farmer_name": sale.get("farmer_name") or "Farmer",
            "farmer_phone": sale.get("farmer_phone") or "-",
            "total_sales": 0.0,
            "total_orders": 0,
        })
        frow["total_sales"] += amount
        frow["total_orders"] += 1

    stock = _stock_totals("ufc_inventory_lots", {"centre_uid": centre_uid, "status": {"$ne": "cancelled"}})
    produce_buy_query = {"buyer_type": "ufc", "buyer.centre_uid": centre_uid}
    return {
        "mitra_count": len(mitras),
        "farmer_count": farmers_count,
        "orders": orders,
        "recent_orders": orders,
        "mitras": mitras[:10],
        "mitra_sales": sorted(mitra_sales_map.values(), key=lambda x: x["total_sales"], reverse=True),
        "farmer_sales": sorted(farmer_sales_map.values(), key=lambda x: x["total_sales"], reverse=True),
        "stock_physical": stock["physical"],
        "stock_reserved": stock["reserved"],
        "stock_saleable": stock["saleable"],
        "avpl_orders": db.avpl_ufc_orders.count_documents({"centre_uid": centre_uid, "status": {"$ne": "cancelled"}}),
        "avpl_orders_pending": db.avpl_ufc_orders.count_documents({"centre_uid": centre_uid, "status": {"$in": ["requested", "approved", "dispatched"]}}),
        "farmer_orders": db.ufc_farmer_orders.count_documents({"centre_uid": centre_uid, "status": {"$ne": "cancelled"}}),
        "farmer_orders_pending": db.ufc_farmer_orders.count_documents({"centre_uid": centre_uid, "status": {"$in": ["requested", "approved"]}}),
        "farmer_sales_value": round(sum(_num(x.get("grand_total") or x.get("total_amount")) for x in order_sales) + sum(_num(x.get("total_amount")) for x in pos_sales), 2),
        "farmer_receivable": _sum("ufc_farmer_receivables", {"centre_uid": centre_uid, "status": {"$ne": "closed"}}, "outstanding_amount"),
        "avpl_payable": _sum("ufc_payables", {"centre_uid": centre_uid, "status": {"$ne": "closed"}}, "outstanding_amount"),
        "produce_orders": db.farmer_produce_marketplace_orders.count_documents(produce_buy_query),
        "produce_orders_pending": db.farmer_produce_marketplace_orders.count_documents({**produce_buy_query, "status": {"$in": ["requested", "approved", "dispatched"]}}),
    }


def get_mitra_dashboard(mitra_uid):
    db = mongo.db
    mitra_uid = str(mitra_uid or "").strip()
    master = db.ufc_mitra_master.find_one({"mitra_uid": mitra_uid}) or {}
    centre_uid = master.get("mapped_centre_uid") or master.get("centre_uid") or ""
    farmers = list(db.farmer_master.find({"mitra_uid": mitra_uid}))

    transactions = list(db.transactions.find({"mitra_uid": mitra_uid}).sort("created_at", -1).limit(10))
    total_purchase = sum(_num(t.get("amount")) for t in transactions if t.get("transaction_type") == "input_purchase")
    total_sale = sum(_num(t.get("amount")) for t in transactions if t.get("transaction_type") == "output_sale")

    monthly_sales_pipeline = [
        {"$match": {"mitra_uid": mitra_uid}},
        {"$group": {"_id": {"year": {"$year": "$created_at"}, "month": {"$month": "$created_at"}}, "total_sales": {"$sum": "$total_amount"}, "total_orders": {"$sum": 1}}},
        {"$sort": {"_id.year": -1, "_id.month": -1}},
    ]
    monthly_sales = list(db.pos_sales.aggregate(monthly_sales_pipeline))
    pos_total = _sum("pos_sales", {"mitra_uid": mitra_uid}, "total_amount", "grand_total")
    total_sale = max(total_sale, pos_total)

    return {
        "centre_uid": centre_uid,
        "farmer_count": len(farmers),
        "approved_farmer_count": sum(1 for f in farmers if (f.get("approval_status") or "approved") == "approved"),
        "pending_farmer_count": sum(1 for f in farmers if (f.get("approval_status") or "approved") != "approved"),
        "farmers": farmers[:20],
        "transactions": transactions,
        "monthly_sales": monthly_sales,
        "monthly_sales_total": round(total_sale, 2),
        "monthly_purchase_total": round(total_purchase, 2),
        "input_bonus": round(total_purchase * 0.02, 2),
        "output_bonus": round(total_sale * 0.02, 2),
        "centre_farmer_orders_pending": db.ufc_farmer_orders.count_documents({"centre_uid": centre_uid, "status": "requested"}) if centre_uid else 0,
    }


def get_farmer_dashboard(phone, user_id=None):
    db = mongo.db
    phone = str(phone or "").strip()
    farmer = db.farmer_master.find_one({"contact_no": phone}) or {}
    actor_id = _oid(user_id or farmer.get("linked_user_id"))
    if not farmer and actor_id:
        farmer = db.farmer_master.find_one({"$or": [{"linked_user_id": actor_id}, {"linked_user_id": str(actor_id)}]}) or {}

    owner_values = [v for v in [actor_id, str(actor_id) if actor_id else None] if v is not None]
    owner_query = {"$in": owner_values} if owner_values else None

    purchases = []
    if owner_query:
        for row in db.farmer_purchase_entries.find({"farmer_user_id": owner_query}).sort("created_at", -1).limit(10):
            purchases.append({
                **row,
                "product_name": row.get("product_name") or "Product",
                "amount": row.get("grand_total") or row.get("total_amount") or row.get("amount") or 0,
            })
    if not purchases and phone:
        purchases = list(db.transactions.find({"farmer_contact": phone, "transaction_type": "input_purchase"}).sort("created_at", -1).limit(10))

    sales = []
    if owner_query:
        sales = list(db.farmer_marketplace_sales.find({"seller_farmer_user_id": owner_query}).sort("created_at", -1).limit(10))
    if not sales and phone:
        sales = list(db.transactions.find({"farmer_contact": phone, "transaction_type": "output_sale"}).sort("created_at", -1).limit(10))

    my_orders = []
    if owner_query:
        my_orders = list(db.ufc_farmer_orders.find({"farmer_user_id": owner_query}).sort("created_at", -1).limit(10))
    if not my_orders and phone:
        my_orders = list(db.orders.find({"farmer_contact": phone}).sort("created_at", -1).limit(10))

    input_purchase_value = sum(_num(x.get("grand_total") or x.get("total_amount") or x.get("amount")) for x in purchases)
    output_sale_value = sum(_num(x.get("total_amount") or x.get("grand_total") or x.get("amount")) for x in sales)
    total_volume = input_purchase_value + output_sale_value

    livestock_activities = {"Pig", "Goat", "Cattle", "Poultry", "Chicken", "Duck", "Fishery"}
    is_livestock_farmer = any(a in livestock_activities for a in farmer.get("activities", []))
    finance_enabled = total_volume >= 30000
    insurance_enabled = is_livestock_farmer and total_volume >= 30000

    # Recommended inputs remain fallback data here; the dashboard route replaces
    # this with the mapped UFC marketplace read-model whenever available.
    recommended_products = list(db.products.find({"is_deleted": {"$ne": True}, "is_active": {"$ne": False}}).sort("created_at", -1).limit(8))
    for product in recommended_products:
        product["available_quantity"] = _normalize_quantity(product)

    farmer_products_marketplace = list(db.farmer_produce_marketplace_listings.find({"status": "published"}).sort("created_at", -1).limit(12))
    if not farmer_products_marketplace:
        farmer_products_marketplace = list(db.farmer_products.find({"status": "active"}).sort("created_at", -1).limit(12))
    for product in farmer_products_marketplace:
        product["available_quantity"] = _normalize_quantity(product)

    produce_stock = {"physical": 0, "reserved": 0, "saleable": 0}
    active_listings = orders_received_pending = 0
    money_to_receive = money_to_pay = 0.0
    if owner_query:
        produce_stock = _stock_totals("farmer_produce_lots", {"farmer_user_id": owner_query, "status": "active"})
        active_listings = db.farmer_produce_marketplace_listings.count_documents({"farmer_user_id": owner_query, "status": "published"})
        orders_received_pending = db.farmer_produce_marketplace_orders.count_documents({"seller_farmer_user_id": owner_query, "status": {"$in": ["requested", "approved", "dispatched"]}})
        money_to_receive = _sum("farmer_marketplace_receivables", {"farmer_user_id": owner_query, "status": {"$ne": "closed"}}, "outstanding_amount")
        money_to_pay = _sum("farmer_payables", {"farmer_user_id": owner_query, "status": {"$ne": "closed"}}, "outstanding_amount")
        money_to_pay += _sum(
            "farmer_marketplace_payables",
            {"buyer_type": "farmer", "buyer_key": {"$in": [str(v) for v in owner_values]}, "status": {"$ne": "closed"}},
            "outstanding_amount",
        )

    return {
        "farmer": farmer,
        "purchases": purchases,
        "sales": sales,
        "finance_enabled": finance_enabled,
        "insurance_enabled": insurance_enabled,
        "my_orders": my_orders,
        "recommended_products": recommended_products,
        "farmer_products_marketplace": farmer_products_marketplace,
        "total_volume": round(total_volume, 2),
        "is_livestock_farmer": is_livestock_farmer,
        "input_purchase_value": round(input_purchase_value, 2),
        "produce_sales_value": round(output_sale_value, 2),
        "produce_stock_physical": produce_stock["physical"],
        "produce_stock_reserved": produce_stock["reserved"],
        "produce_stock_saleable": produce_stock["saleable"],
        "active_listings": active_listings,
        "orders_received_pending": orders_received_pending,
        "money_to_receive": money_to_receive,
        "money_to_pay": money_to_pay,
    }
