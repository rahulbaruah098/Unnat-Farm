from app.extensions import mongo


def count_documents(coll, query=None):
    return mongo.db[coll].count_documents(query or {})


def get_system_overview():
    db = mongo.db
    return {
        "total_users": db.users.count_documents({}),
        "total_ufc_admins": db.ufc_admin_master.count_documents({}),
        "total_ufc_mitras": db.ufc_mitra_master.count_documents({}),
        "total_farmers": db.farmer_master.count_documents({}),
        "pending_validations": db.validations.count_documents({"status": "pending"}),
        "total_products": db.products.count_documents({}),
        "total_orders": db.orders.count_documents({}),
        "total_transactions": db.transactions.count_documents({}),
    }


def get_centre_dashboard(centre_uid):
    db = mongo.db
    mitras = list(db.ufc_mitra_master.find({"mapped_centre_uid": centre_uid}))
    mitra_uids = [m.get("mitra_uid") for m in mitras]
    farmers_count = db.farmer_master.count_documents({"centre_uid": centre_uid})
    orders = list(db.orders.find({"centre_uid": centre_uid}).sort("created_at", -1).limit(10))
    return {
        "mitra_count": len(mitras),
        "farmer_count": farmers_count,
        "orders": orders,
        "mitras": mitras[:10],
    }


def get_mitra_dashboard(mitra_uid):
    db = mongo.db
    farmers = list(db.farmer_master.find({"mitra_uid": mitra_uid}))
    transactions = list(db.transactions.find({"mitra_uid": mitra_uid}).sort("created_at", -1).limit(10))
    total_purchase = sum(float(t.get("amount", 0)) for t in transactions if t.get("transaction_type") == "input_purchase")
    total_sale = sum(float(t.get("amount", 0)) for t in transactions if t.get("transaction_type") == "output_sale")
    return {
        "farmer_count": len(farmers),
        "farmers": farmers[:20],
        "transactions": transactions,
        "monthly_sales_total": total_sale,
        "monthly_purchase_total": total_purchase,
        "input_bonus": round(total_purchase * 0.02, 2),
        "output_bonus": round(total_sale * 0.02, 2),
    }


def get_farmer_dashboard(phone):
    db = mongo.db
    farmer = db.farmer_master.find_one({"contact_no": phone}) or {}
    purchases = list(db.transactions.find({"farmer_contact": phone, "transaction_type": "input_purchase"}).sort("created_at", -1).limit(10))
    sales = list(db.transactions.find({"farmer_contact": phone, "transaction_type": "output_sale"}).sort("created_at", -1).limit(10))
    total_volume = sum(float(x.get("amount", 0)) for x in purchases + sales)
    finance_enabled = total_volume >= 5000
    insurance_enabled = total_volume >= 3000 and any(a in ["Pig", "Goat", "Cattle"] for a in farmer.get("activities", []))
    return {
        "farmer": farmer,
        "purchases": purchases,
        "sales": sales,
        "finance_enabled": finance_enabled,
        "insurance_enabled": insurance_enabled,
    }
