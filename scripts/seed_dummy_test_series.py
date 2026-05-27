import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bson import ObjectId

from app import create_app
from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.security import hash_password


app = create_app()

DUMMY_GROUP_ID = "DUMMY-UF-TEST-001"

TEST_AVPL_USER_REF = "TEST-AVP-0001"
TEST_CENTRE_USER_REF = "TEST-UFC-0001"
TEST_MITRA_USER_REF = "TEST-MITRA-0001"
TEST_FARMER_USER_REF = "TEST-FARMER-0001"

TEST_CENTRE_UID = "TEST-CENTRE-0001"
TEST_MITRA_UID = "TEST-MITRA-0001"

TEST_AVPL_USERNAME = "testavpladmin"
TEST_CENTRE_USERNAME = "testcentre"
TEST_MITRA_USERNAME = "testmitra"

TEST_AVPL_PHONE = "9100000001"
TEST_CENTRE_PHONE = "9100000002"
TEST_MITRA_PHONE = "9100000003"
TEST_FARMER_PHONE = "9100000004"

TEST_PASSWORD = "test123"


def delete_existing_dummy_data():
    dummy_user_query = {
        "$or": [
            {"dummy_group_id": DUMMY_GROUP_ID},
            {"test_series": "dummy_test_series"},
            {"is_dummy": True},

            {"username": {"$in": [
                TEST_AVPL_USERNAME,
                TEST_CENTRE_USERNAME,
                TEST_MITRA_USERNAME,
            ]}},

            {"phone": {"$in": [
                TEST_AVPL_PHONE,
                TEST_CENTRE_PHONE,
                TEST_MITRA_PHONE,
                TEST_FARMER_PHONE,
            ]}},

            {"user_ref_id": {"$in": [
                TEST_AVPL_USER_REF,
                TEST_CENTRE_USER_REF,
                TEST_MITRA_USER_REF,
                TEST_FARMER_USER_REF,
            ]}},

            {"centre_uid": TEST_CENTRE_UID},
            {"mapped_centre_uid": TEST_CENTRE_UID},
            {"mitra_uid": TEST_MITRA_UID},
            {"mapped_mitra_uid": TEST_MITRA_UID},
        ]
    }

    old_users = list(mongo.db.users.find(dummy_user_query, {"_id": 1}))

    old_user_ids = [str(user["_id"]) for user in old_users]

    object_ids = []
    for uid in old_user_ids:
        try:
            object_ids.append(ObjectId(uid))
        except Exception:
            pass

    cleanup_query = {
        "$or": [
            {"dummy_group_id": DUMMY_GROUP_ID},
            {"test_series": "dummy_test_series"},
            {"is_dummy": True},

            {"linked_user_id": {"$in": old_user_ids}},
            {"user_id": {"$in": old_user_ids}},
            {"created_by": {"$in": old_user_ids}},
            {"requested_by": {"$in": old_user_ids}},
            {"submitted_by": {"$in": old_user_ids}},
            {"uploaded_by_user_id": {"$in": old_user_ids}},

            {"centre_uid": TEST_CENTRE_UID},
            {"mapped_centre_uid": TEST_CENTRE_UID},
            {"mitra_uid": TEST_MITRA_UID},
            {"mapped_mitra_uid": TEST_MITRA_UID},

            {"phone": {"$in": [
                TEST_AVPL_PHONE,
                TEST_CENTRE_PHONE,
                TEST_MITRA_PHONE,
                TEST_FARMER_PHONE,
            ]}},
            {"contact_no": TEST_FARMER_PHONE},
        ]
    }

    mongo.db.ufc_admin_master.delete_many(cleanup_query)
    mongo.db.ufc_mitra_master.delete_many(cleanup_query)
    mongo.db.farmer_master.delete_many(cleanup_query)
    mongo.db.validations.delete_many(cleanup_query)
    mongo.db.documents.delete_many(cleanup_query)
    mongo.db.orders.delete_many(cleanup_query)
    mongo.db.transactions.delete_many(cleanup_query)
    mongo.db.farmer_products.delete_many(cleanup_query)
    mongo.db.support_tickets.delete_many(cleanup_query)
    mongo.db.insurance_requests.delete_many(cleanup_query)
    mongo.db.financial_assistance_leads.delete_many(cleanup_query)
    mongo.db.mitra_product_purchases.delete_many(cleanup_query)
    mongo.db.mitra_product_sales.delete_many(cleanup_query)
    mongo.db.mitra_product_stock.delete_many(cleanup_query)
    mongo.db.profile_update_requests.delete_many(cleanup_query)

    mongo.db.users.delete_many(dummy_user_query)

    if object_ids:
        mongo.db.users.delete_many({"_id": {"$in": object_ids}})


with app.app_context():
    print("Removing old dummy test series only...")
    delete_existing_dummy_data()

    common_dummy_fields = {
        "dummy_group_id": DUMMY_GROUP_ID,
        "is_dummy": True,
        "test_series": "dummy_test_series",
    }

    # -------------------------------------------------
    # 1. Dummy AVPL Admin
    # -------------------------------------------------
    avpl_user = {
        "user_ref_id": TEST_AVPL_USER_REF,
        "name": "Test AVPL Admin",
        "username": TEST_AVPL_USERNAME,
        "phone": TEST_AVPL_PHONE,
        "password_hash": hash_password(TEST_PASSWORD),
        "role": "avpl_admin",
        "status": "active",
        "active": True,
        "approval_status": "approved",
        "created_by": "dummy_seed",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "last_login": None,
        **common_dummy_fields,
    }

    avpl_id = mongo.db.users.insert_one(avpl_user).inserted_id

    # -------------------------------------------------
    # 2. Dummy Centre / UFC Admin
    # -------------------------------------------------
    centre_user = {
        "user_ref_id": TEST_CENTRE_USER_REF,
        "centre_uid": TEST_CENTRE_UID,
        "name": "Test Centre Admin",
        "username": TEST_CENTRE_USERNAME,
        "phone": TEST_CENTRE_PHONE,
        "password_hash": hash_password(TEST_PASSWORD),
        "role": "ufc_admin",
        "status": "active",
        "active": True,
        "approval_status": "approved",
        "created_by": str(avpl_id),
        "state": "Dummy State",
        "district": "Dummy District",
        "block": "Dummy Block",
        "village": "Dummy Village",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "last_login": None,
        **common_dummy_fields,
    }

    centre_id = mongo.db.users.insert_one(centre_user).inserted_id

    mongo.db.ufc_admin_master.insert_one(
        {
            "linked_user_id": str(centre_id),
            "centre_uid": TEST_CENTRE_UID,
            "name_of_enterprise": "Test UnnatFarm Centre",
            "name_of_owner": "Test Centre Owner",
            "owner_dob": "1990-01-01",
            "owner_age": "36",
            "state": "Dummy State",
            "district": "Dummy District",
            "block": "Dummy Block",
            "village": "Dummy Village",
            "pan_number": "ABCDE1234F",
            "gst_number": "TESTGST123456",
            "approval_status": "approved",
            "created_at": now_utc(),
            "updated_at": now_utc(),
            **common_dummy_fields,
        }
    )

    # -------------------------------------------------
    # 3. Dummy Mitra
    # -------------------------------------------------
    mitra_user = {
        "user_ref_id": TEST_MITRA_USER_REF,
        "mitra_uid": TEST_MITRA_UID,
        "mapped_centre_uid": TEST_CENTRE_UID,
        "name": "Test Mitra",
        "username": TEST_MITRA_USERNAME,
        "phone": TEST_MITRA_PHONE,
        "password_hash": hash_password(TEST_PASSWORD),
        "role": "ufc_mitra",
        "status": "active",
        "active": True,
        "approval_status": "approved",
        "created_by": str(centre_id),
        "state": "Dummy State",
        "district": "Dummy District",
        "block": "Dummy Block",
        "village": "Dummy Village",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "last_login": None,
        **common_dummy_fields,
    }

    mitra_id = mongo.db.users.insert_one(mitra_user).inserted_id

    mongo.db.ufc_mitra_master.insert_one(
        {
            "linked_user_id": str(mitra_id),
            "mitra_uid": TEST_MITRA_UID,
            "mapped_centre_uid": TEST_CENTRE_UID,
            "name": "Test Mitra",
            "care_of": "Test Guardian",
            "dob": "1995-01-01",
            "age": "31",
            "education": "Graduate",
            "gender": "Male",
            "government_id_number": "TESTID123456",
            "state": "Dummy State",
            "district": "Dummy District",
            "block": "Dummy Block",
            "village": "Dummy Village",
            "approval_status": "approved",
            "created_at": now_utc(),
            "updated_at": now_utc(),
            **common_dummy_fields,
        }
    )

    # -------------------------------------------------
    # 4. Dummy Farmer
    # -------------------------------------------------
    farmer_user = {
        "user_ref_id": TEST_FARMER_USER_REF,
        "name": "Test Farmer",
        "phone": TEST_FARMER_PHONE,
        "password_hash": hash_password(TEST_PASSWORD),
        "role": "farmer",
        "status": "active",
        "active": True,
        "approval_status": "approved",
        "created_by": str(mitra_id),
        "mapped_centre_uid": TEST_CENTRE_UID,
        "mapped_mitra_uid": TEST_MITRA_UID,
        "state": "Dummy State",
        "district": "Dummy District",
        "block": "Dummy Block",
        "village": "Dummy Village",
        "created_at": now_utc(),
        "updated_at": now_utc(),
        "last_login": None,
        **common_dummy_fields,
    }

    farmer_id = mongo.db.users.insert_one(farmer_user).inserted_id

    mongo.db.farmer_master.insert_one(
        {
            "linked_user_id": str(farmer_id),
            "name": "Test Farmer",
            "gender": "Male",
            "date_of_birth": "1998-01-01",
            "age": "28",
            "contact_no": TEST_FARMER_PHONE,
            "centre_uid": TEST_CENTRE_UID,
            "mitra_uid": TEST_MITRA_UID,
            "state": "Dummy State",
            "district": "Dummy District",
            "block": "Dummy Block",
            "village": "Dummy Village",
            "activities": ["Agriculture"],
            "agri_sub_categories": ["Vegetables"],
            "approval_status": "approved",
            "created_at": now_utc(),
            "updated_at": now_utc(),
            **common_dummy_fields,
        }
    )

    print("")
    print("Dummy isolated test series created successfully.")
    print("---------------------------------------------")
    print("Dummy Group ID:", DUMMY_GROUP_ID)
    print("")
    print("AVPL Admin")
    print("Username:", TEST_AVPL_USERNAME)
    print("Password:", TEST_PASSWORD)
    print("User Ref:", TEST_AVPL_USER_REF)
    print("")
    print("Centre")
    print("Username:", TEST_CENTRE_USERNAME)
    print("Password:", TEST_PASSWORD)
    print("User Ref:", TEST_CENTRE_USER_REF)
    print("Centre UID:", TEST_CENTRE_UID)
    print("")
    print("Mitra")
    print("Username:", TEST_MITRA_USERNAME)
    print("Password:", TEST_PASSWORD)
    print("User Ref:", TEST_MITRA_USER_REF)
    print("Mitra UID:", TEST_MITRA_UID)
    print("")
    print("Farmer")
    print("Phone:", TEST_FARMER_PHONE)
    print("Password:", TEST_PASSWORD)
    print("User Ref:", TEST_FARMER_USER_REF)
    print("---------------------------------------------")