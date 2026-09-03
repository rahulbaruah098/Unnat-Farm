from __future__ import annotations
from app.utils.timezone import business_today, to_india_datetime

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
        return expiry < business_today()
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


# ---------------------------------------------------------------------------
# Simple role dashboards (web)
# ---------------------------------------------------------------------------


def _current_business_month(value):
    """True when a stored date/datetime belongs to the current IST month."""
    if value in (None, ""):
        return False
    converted = to_india_datetime(value)
    if converted is None:
        return False
    today = business_today()
    return converted.year == today.year and converted.month == today.month


def _row_in_current_month(row, *fields):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            return _current_business_month(value)
    return False


def _amount_from(row, *fields):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            return _num(value)
    return 0.0


def _quantity_from(row, *fields):
    return _amount_from(row, *fields)


def _clean_unit(value):
    text = str(value or "").strip().upper()
    return text or "UNIT"


def _quantity_summary(rows, quantity_fields=("quantity",), unit_fields=("unit_code",), *, max_units=2):
    totals = {}
    for row in rows or []:
        qty = _quantity_from(row, *quantity_fields)
        if qty <= 0:
            continue
        unit = "UNIT"
        for field in unit_fields:
            if (row or {}).get(field) not in (None, ""):
                unit = _clean_unit((row or {}).get(field))
                break
        totals[unit] = totals.get(unit, 0.0) + qty
    if not totals:
        return "0"
    ordered = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    parts = []
    for unit, qty in ordered[:max_units]:
        qty_text = f"{qty:.2f}".rstrip("0").rstrip(".")
        parts.append(f"{qty_text} {unit}")
    if len(ordered) > max_units:
        parts.append(f"+{len(ordered) - max_units} more")
    return " · ".join(parts)


def _activity_time(row, *fields):
    for field in fields:
        value = (row or {}).get(field)
        if value not in (None, ""):
            converted = to_india_datetime(value)
            if converted is not None:
                return converted
    return None


def _sort_activities(rows, limit=8):
    safe = [row for row in (rows or []) if row.get("at") is not None]
    safe.sort(key=lambda row: row["at"], reverse=True)
    return safe[:limit]


def _centre_name(centre_uid):
    centre_uid = str(centre_uid or "").strip()
    if not centre_uid:
        return "My UFC Centre"
    row = (
        mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid})
        or mongo.db.ufc_centre_master.find_one({"centre_uid": centre_uid})
        or {}
    )
    return (
        row.get("name_of_enterprise")
        or row.get("centre_name")
        or row.get("name")
        or centre_uid
    )


def _count_ufc_stock_products(centre_uid):
    """Count distinct saleable product pools without adding unlike units together."""
    products = set()
    for row in mongo.db.ufc_inventory_lots.find(
        {"centre_uid": centre_uid, "status": {"$ne": "cancelled"}},
        {"product_id": 1, "product_key": 1, "product_name": 1, "available_quantity": 1,
         "reserved_quantity": 1, "damaged_quantity": 1, "blocked_quantity": 1, "status": 1, "expiry_date": 1},
    ):
        available = max(_num(row.get("available_quantity")), 0)
        reserved = min(max(_num(row.get("reserved_quantity")), 0), available)
        damaged = min(max(_num(row.get("damaged_quantity")), 0), available)
        blocked = min(max(_num(row.get("blocked_quantity")), 0), available)
        if row.get("status") in {"expired", "cancelled"} or _lot_is_expired(row):
            continue
        if max(available - reserved - damaged - blocked, 0) <= 0:
            continue
        key = str(row.get("product_id") or row.get("product_key") or row.get("product_name") or "").strip().lower()
        if key:
            products.add(("input", key))

    for row in mongo.db.farmer_marketplace_buyer_stock_lots.find(
        {"buyer_type": "ufc", "buyer_key": centre_uid, "status": "active"},
        {"product_key": 1, "product_name": 1, "available_quantity": 1},
    ):
        if _num(row.get("available_quantity")) <= 0:
            continue
        key = str(row.get("product_key") or row.get("product_name") or "").strip().lower()
        if key:
            products.add(("produce", key))
    return len(products)


def get_simple_ufc_dashboard(centre_uid, actor_user_id=None):
    """Focused UFC Admin dashboard.

    The dashboard intentionally exposes only action-oriented counts and current
    business figures. Detailed accounting and farmer analytics stay in Reports.
    """
    db = mongo.db
    centre_uid = str(centre_uid or "").strip()
    base = get_centre_dashboard(centre_uid)

    payment_summary = {
        "avpl_due": "0.00",
        "avpl_pending_confirmation": "0.00",
        "avpl_pending_count": 0,
        "farmer_due": "0.00",
        "farmer_pending_confirmation": "0.00",
        "farmer_pending_count": 0,
        "farmer_produce_due": "0.00",
        "farmer_produce_pending_confirmation": "0.00",
        "farmer_produce_pending_count": 0,
    }
    recent_payments = []
    if actor_user_id:
        try:
            from app.services.payment_service import get_ufc_payment_overview
            payment_overview = get_ufc_payment_overview(actor_user_id)
            payment_summary.update(payment_overview.get("summary") or {})
            recent_payments = list(payment_overview.get("recent_payments") or [])[:8]
        except (ValueError, PermissionError, RuntimeError):
            pass

    farmer_orders_to_review = db.ufc_farmer_orders.count_documents({
        "centre_uid": centre_uid,
        "status": "requested",
    })
    avpl_to_receive = db.avpl_ufc_orders.count_documents({
        "centre_uid": centre_uid,
        "status": "dispatched",
    })
    produce_to_receive = db.farmer_produce_marketplace_orders.count_documents({
        "buyer_type": "ufc",
        "buyer_key": centre_uid,
        "status": "dispatched",
        "seller_delivery_confirmed": True,
    })

    money_to_pay = round(
        _num(payment_summary.get("avpl_due")) + _num(payment_summary.get("farmer_produce_due")),
        2,
    )
    money_to_collect = round(_num(payment_summary.get("farmer_due")), 2)

    order_sales = list(db.ufc_farmer_sales.find({
        "centre_uid": centre_uid,
        "status": {"$ne": "voided"},
    }, {"grand_total": 1, "total_amount": 1, "amount": 1, "sale_date": 1, "created_at": 1,
        "farmer_user_id": 1, "farmer_name": 1, "sale_number": 1, "document_number": 1}))
    pos_sales = list(db.pos_sales.find({
        "centre_uid": centre_uid,
        "status": "completed",
        "$or": [{"seller_type": "ufc"}, {"seller_type": {"$exists": False}}],
    }, {"grand_total": 1, "total_amount": 1, "amount": 1, "sale_date": 1, "created_at": 1,
        "sale_number": 1, "buyer": 1}))
    current_order_sales = [row for row in order_sales if _row_in_current_month(row, "sale_date", "created_at")]
    current_pos_sales = [row for row in pos_sales if _row_in_current_month(row, "sale_date", "created_at")]
    sales_this_month = round(
        sum(_amount_from(row, "grand_total", "total_amount", "amount") for row in current_order_sales)
        + sum(_amount_from(row, "grand_total", "total_amount", "amount") for row in current_pos_sales),
        2,
    )

    produce_purchases = list(db.farmer_marketplace_purchase_entries.find({
        "buyer_type": "ufc",
        "buyer_key": centre_uid,
        "status": {"$ne": "voided"},
    }, {"total_amount": 1, "received_at": 1, "created_at": 1, "seller_farmer_user_id": 1,
        "seller_farmer_name": 1, "product_name": 1, "quantity": 1, "unit_code": 1, "purchase_number": 1}))
    current_produce_purchases = [row for row in produce_purchases if _row_in_current_month(row, "received_at", "created_at")]
    produce_bought_this_month = round(sum(_amount_from(row, "total_amount") for row in current_produce_purchases), 2)

    mapped_farmers = list(db.farmer_master.find(
        {"centre_uid": centre_uid},
        {"_id": 1, "linked_user_id": 1, "contact_no": 1},
    ))
    mapped_user_ids = set()
    for farmer in mapped_farmers:
        linked = farmer.get("linked_user_id")
        if linked:
            mapped_user_ids.add(str(linked))

    active_ids = set()
    current_input_orders = list(db.ufc_farmer_orders.find(
        {"centre_uid": centre_uid},
        {"farmer_user_id": 1, "created_at": 1, "updated_at": 1, "delivered_at": 1},
    ))
    for row in current_input_orders:
        if _row_in_current_month(row, "delivered_at", "updated_at", "created_at"):
            key = str(row.get("farmer_user_id") or "")
            if key and (not mapped_user_ids or key in mapped_user_ids):
                active_ids.add(key)
    for row in current_produce_purchases:
        key = str(row.get("seller_farmer_user_id") or "")
        if key and (not mapped_user_ids or key in mapped_user_ids):
            active_ids.add(key)
    for row in current_order_sales:
        key = str(row.get("farmer_user_id") or "")
        if key and (not mapped_user_ids or key in mapped_user_ids):
            active_ids.add(key)

    stock_product_count = _count_ufc_stock_products(centre_uid)

    needs_attention = []
    if farmer_orders_to_review:
        needs_attention.append({
            "label": "Farmer orders need review",
            "count": farmer_orders_to_review,
            "kind": "order",
            "endpoint": "modules.centre_orders",
        })
    if avpl_to_receive:
        needs_attention.append({
            "label": "AVPL deliveries ready to receive",
            "count": avpl_to_receive,
            "kind": "stock",
            "endpoint": "modules.ufc_avpl_orders",
        })
    if produce_to_receive:
        needs_attention.append({
            "label": "Farmer produce ready to receive",
            "count": produce_to_receive,
            "kind": "stock",
            "endpoint": "modules.farmer_produce_market_my_orders",
        })
    farmer_payment_confirm_count = int(payment_summary.get("farmer_pending_count") or 0)
    if farmer_payment_confirm_count:
        needs_attention.append({
            "label": "Farmer payments need confirmation",
            "count": farmer_payment_confirm_count,
            "kind": "payment",
            "endpoint": "modules.ufc_payments",
        })
    if money_to_pay > 0:
        needs_attention.append({
            "label": "Payments are ready to make",
            "amount": money_to_pay,
            "kind": "payment",
            "endpoint": "modules.ufc_payments",
        })

    activities = []
    for row in list(db.avpl_ufc_orders.find(
        {"centre_uid": centre_uid, "status": "received"},
        {"order_number": 1, "product_name": 1, "received_at": 1, "updated_at": 1},
    ).sort("updated_at", -1).limit(6)):
        at = _activity_time(row, "received_at", "updated_at")
        if at:
            activities.append({"at": at, "title": "AVPL stock received", "detail": row.get("product_name") or row.get("order_number") or "Order"})
    for row in list(db.farmer_marketplace_purchase_entries.find(
        {"buyer_type": "ufc", "buyer_key": centre_uid},
        {"seller_farmer_name": 1, "product_name": 1, "received_at": 1, "created_at": 1},
    ).sort("received_at", -1).limit(6)):
        at = _activity_time(row, "received_at", "created_at")
        if at:
            detail = " · ".join(x for x in [row.get("product_name"), row.get("seller_farmer_name")] if x)
            activities.append({"at": at, "title": "Farmer produce received", "detail": detail or "Produce purchase"})
    for row in recent_payments:
        at = _activity_time(row, "completed_at", "confirmed_at", "reported_at", "created_at")
        if at:
            amount = _amount_from(row, "amount")
            status = str(row.get("status") or "").replace("_", " ").title()
            activities.append({"at": at, "title": f"Payment {status.lower() or 'updated'}", "detail": f"₹{amount:.2f}"})
    for row in current_pos_sales[-6:]:
        at = _activity_time(row, "created_at", "sale_date")
        if at:
            buyer = (row.get("buyer") or {}).get("name") or "Customer"
            activities.append({"at": at, "title": "POS sale", "detail": f"{buyer} · ₹{_amount_from(row, 'grand_total', 'total_amount'):.2f}"})

    return {
        **base,
        "centre_name": _centre_name(centre_uid),
        "centre_uid": centre_uid,
        "kpis": {
            "farmer_orders_to_review": farmer_orders_to_review,
            "avpl_to_receive": avpl_to_receive,
            "produce_to_receive": produce_to_receive,
            "money_to_pay": money_to_pay,
            "money_to_collect": money_to_collect,
            "sales_this_month": sales_this_month,
        },
        "snapshot": {
            "mapped_farmers": len(mapped_farmers),
            "active_farmers_this_month": len(active_ids),
            "stock_product_count": stock_product_count,
            "produce_bought_this_month": produce_bought_this_month,
        },
        "payment_summary": payment_summary,
        "needs_attention": needs_attention[:6],
        "recent_activity": _sort_activities(activities, limit=8),
    }


def get_simple_farmer_dashboard(phone, user_id=None):
    """Focused Farmer dashboard built from the same operational records as the modules."""
    db = mongo.db
    base = get_farmer_dashboard(phone, user_id=user_id)
    farmer = base.get("farmer") or {}
    actor_id = _oid(user_id or farmer.get("linked_user_id"))
    if not actor_id:
        return base
    actor_str = str(actor_id)
    owner_values = [actor_id, actor_str]
    owner_query = {"$in": owner_values}

    payment_summary = {
        "outstanding": "0.00",
        "payable_outstanding": "0.00",
        "ufc_pending_confirmation": "0.00",
        "ufc_pending_count": 0,
        "receivable_outstanding": "0.00",
        "marketplace_pending_confirmation": "0.00",
        "marketplace_pending_count": 0,
    }
    recent_payments = []
    try:
        from app.services.payment_service import get_farmer_payment_overview
        payment_overview = get_farmer_payment_overview(actor_id)
        payment_summary.update(payment_overview.get("summary") or {})
        recent_payments = list(payment_overview.get("recent_payments") or [])[:8]
    except (ValueError, PermissionError, RuntimeError):
        pass

    stock_rows = list(db.farmer_produce_lots.find({
        "farmer_user_id": owner_query,
        "status": "active",
    }, {"product_key": 1, "product_name": 1, "unit_code": 1, "available_quantity": 1,
        "reserved_quantity": 1, "damaged_quantity": 1, "blocked_quantity": 1}))
    ready_products = set()
    for row in stock_rows:
        available = max(_num(row.get("available_quantity")), 0)
        reserved = min(max(_num(row.get("reserved_quantity")), 0), available)
        damaged = min(max(_num(row.get("damaged_quantity")), 0), available)
        blocked = min(max(_num(row.get("blocked_quantity")), 0), available)
        if max(available - reserved - damaged - blocked, 0) > 0:
            key = str(row.get("product_key") or row.get("product_name") or "").strip().lower()
            if key:
                ready_products.add(key)

    seller_orders = list(db.farmer_produce_marketplace_orders.find({
        "seller_farmer_user_id": owner_query,
        "status": {"$in": ["requested", "approved", "dispatched", "received"]},
    }).sort("updated_at", -1).limit(100))
    orders_to_approve = sum(1 for row in seller_orders if row.get("status") == "requested")
    orders_to_dispatch = sum(1 for row in seller_orders if row.get("status") == "approved")
    deliveries_to_confirm = sum(
        1 for row in seller_orders
        if row.get("status") == "dispatched" and row.get("seller_delivery_confirmed") is not True
    )
    payments_to_confirm = int(payment_summary.get("marketplace_pending_count") or 0)
    orders_needing_action = orders_to_approve + orders_to_dispatch + deliveries_to_confirm + payments_to_confirm

    external_sales = list(db.farmer_external_sales.find({
        "farmer_user_id": owner_query,
        "status": "completed",
    }))
    marketplace_sales = list(db.farmer_marketplace_sales.find({
        "seller_farmer_user_id": owner_query,
        "status": {"$ne": "voided"},
    }))
    pos_sales = list(db.pos_sales.find({
        "seller_type": "farmer",
        "$or": [{"seller_user_id": actor_id}, {"seller_user_id_str": actor_str}, {"seller_key": actor_str}],
        "status": "completed",
    }))
    month_external = [row for row in external_sales if _row_in_current_month(row, "sale_date", "created_at")]
    month_market = [row for row in marketplace_sales if _row_in_current_month(row, "sale_date", "created_at")]
    month_pos = [row for row in pos_sales if _row_in_current_month(row, "sale_date", "created_at")]
    sales_this_month = round(
        sum(_amount_from(row, "grand_total", "total_amount", "amount") for row in month_external)
        + sum(_amount_from(row, "grand_total", "total_amount", "amount") for row in month_market)
        + sum(_amount_from(row, "grand_total", "total_amount", "amount") for row in month_pos),
        2,
    )

    productions = list(db.farmer_production_entries.find({
        "farmer_user_id": owner_query,
        "status": {"$ne": "voided"},
    }, {"quantity": 1, "unit_code": 1, "harvest_date": 1, "created_at": 1, "product_name": 1}))
    month_productions = [row for row in productions if _row_in_current_month(row, "harvest_date", "created_at")]

    sold_month_rows = []
    for source_rows in (month_external, month_market):
        sold_month_rows.extend(source_rows)
    for row in month_pos:
        for item in row.get("items") or []:
            sold_month_rows.append({
                "quantity": item.get("quantity") or 0,
                "unit_code": item.get("unit_code") or "UNIT",
            })

    # Marketplace and external sales use quantity fields; POS is normalized above.
    produced_summary = _quantity_summary(month_productions, ("quantity",), ("unit_code",))
    sold_summary = _quantity_summary(sold_month_rows, ("quantity", "base_quantity"), ("unit_code",))

    completed_received_payments = list(db.payments.find({
        "payee_key": actor_str,
        "status": "completed",
    }, {"amount": 1, "completed_at": 1, "confirmed_at": 1, "created_at": 1}))
    money_received_this_month = round(sum(
        _amount_from(row, "amount")
        for row in completed_received_payments
        if _row_in_current_month(row, "confirmed_at", "completed_at", "created_at")
    ), 2)

    needs_attention = []
    if orders_to_approve:
        needs_attention.append({"label": "Produce orders need your approval", "count": orders_to_approve, "endpoint": "modules.farmer_marketplace_orders_received", "kind": "order"})
    if orders_to_dispatch:
        needs_attention.append({"label": "Approved orders are ready to dispatch", "count": orders_to_dispatch, "endpoint": "modules.farmer_marketplace_orders_received", "kind": "order"})
    if deliveries_to_confirm:
        needs_attention.append({"label": "Dispatches need delivery confirmation", "count": deliveries_to_confirm, "endpoint": "modules.farmer_marketplace_orders_received", "kind": "order"})
    if payments_to_confirm:
        needs_attention.append({"label": "Buyer payments need your confirmation", "count": payments_to_confirm, "endpoint": "modules.farmer_payments", "kind": "payment"})
    payable = _num(payment_summary.get("payable_outstanding"))
    if payable > 0:
        needs_attention.append({"label": "You have payments to make", "amount": payable, "endpoint": "modules.farmer_payments", "kind": "payment"})

    activities = []
    for row in list(db.farmer_produce_movements.find(
        {"farmer_user_id": owner_query},
        {"movement_type": 1, "product_name": 1, "quantity": 1, "unit_code": 1, "created_at": 1},
    ).sort("created_at", -1).limit(8)):
        at = _activity_time(row, "created_at")
        if not at:
            continue
        movement = str(row.get("movement_type") or "Stock update").replace("_", " ").title()
        detail = row.get("product_name") or "Produce"
        qty = _num(row.get("quantity"))
        if qty:
            detail += f" · {qty:g} {_clean_unit(row.get('unit_code'))}"
        activities.append({"at": at, "title": movement, "detail": detail})
    for row in seller_orders[:6]:
        at = _activity_time(row, "updated_at", "created_at")
        if at:
            title = f"Produce order {str(row.get('status') or '').replace('_', ' ').title()}"
            detail = row.get("product_name") or row.get("order_number") or "Order"
            activities.append({"at": at, "title": title, "detail": detail})
    for row in recent_payments:
        at = _activity_time(row, "completed_at", "confirmed_at", "reported_at", "created_at")
        if at:
            amount = _amount_from(row, "amount")
            status = str(row.get("status") or "").replace("_", " ").title()
            activities.append({"at": at, "title": f"Payment {status.lower() or 'updated'}", "detail": f"₹{amount:.2f}"})

    centre_uid = str(farmer.get("centre_uid") or "").strip()
    return {
        **base,
        "centre_name": _centre_name(centre_uid),
        "payment_summary": payment_summary,
        "kpis": {
            "produce_products_ready": len(ready_products),
            "money_to_receive": round(_num(payment_summary.get("receivable_outstanding")), 2),
            "orders_needing_action": orders_needing_action,
            "sales_this_month": sales_this_month,
        },
        "month_snapshot": {
            "produced": produced_summary,
            "sold": sold_summary,
            "sales_value": sales_this_month,
            "money_received": money_received_this_month,
            "production_batches": len(month_productions),
            "active_listings": base.get("active_listings", 0),
        },
        "needs_attention": needs_attention[:6],
        "recent_activity": _sort_activities(activities, limit=8),
    }
