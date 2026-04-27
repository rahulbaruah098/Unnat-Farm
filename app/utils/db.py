from pymongo import ASCENDING
from pymongo.errors import OperationFailure
from app.services.location_service import seed_locations


def _safe_drop_index(collection, index_name):
    try:
        collection.drop_index(index_name)
    except Exception:
        pass


def _drop_conflicting_indexes(collection, keys, desired_name=None):
    """Drop indexes on the same key pattern before recreating.

    This keeps the project runnable even when an older ZIP/build already created
    indexes with default names such as contact_no_1 or with older options.
    """
    key_pattern = list(keys)
    try:
        for name, meta in collection.index_information().items():
            if name == "_id_":
                continue
            meta_key = meta.get("key", [])
            if list(meta_key) == key_pattern or (desired_name and name == desired_name):
                _safe_drop_index(collection, name)
    except Exception:
        pass


def _ensure_index(collection, keys, name, **options):
    _drop_conflicting_indexes(collection, keys, desired_name=name)
    try:
        collection.create_index(keys, name=name, **options)
    except OperationFailure:
        _drop_conflicting_indexes(collection, keys, desired_name=name)
        collection.create_index(keys, name=name, **options)


def _ensure_unique_string_index(collection, field, name):
    _ensure_index(
        collection,
        [(field, ASCENDING)],
        name=name,
        unique=True,
        partialFilterExpression={field: {"$type": "string", "$gt": ""}},
    )


def init_indexes(db):
    # User auth indexes. Optional fields use partial indexes so None/missing does not collide.
    _ensure_unique_string_index(db.users, "username", "username_unique_string")
    _ensure_unique_string_index(db.users, "phone", "phone_unique_string")
    _ensure_unique_string_index(db.users, "user_ref_id", "user_ref_id_unique_string")
    _ensure_unique_string_index(db.users, "centre_uid", "centre_uid_unique_string")
    _ensure_unique_string_index(db.users, "mitra_uid", "mitra_uid_unique_string")

    # Master UID indexes.
    _ensure_unique_string_index(db.ufc_admin_master, "centre_uid", "ufc_admin_centre_uid_unique_string")
    _ensure_unique_string_index(db.ufc_mitra_master, "mitra_uid", "ufc_mitra_uid_unique_string")

    # Operational indexes. These are intentionally non-unique unless business rules require unique values.
    _ensure_index(db.farmer_master, [("contact_no", ASCENDING)], name="farmer_contact_idx")
    _ensure_index(db.farmer_master, [("centre_uid", ASCENDING)], name="farmer_centre_idx")
    _ensure_index(db.farmer_master, [("mitra_uid", ASCENDING)], name="farmer_mitra_idx")
    _ensure_index(db.documents, [("linked_user_id", ASCENDING)], name="documents_linked_user_idx")
    _ensure_index(db.validations, [("entity_type", ASCENDING), ("status", ASCENDING)], name="validations_entity_status_idx")
    _ensure_index(db.validations, [("approver_role", ASCENDING), ("status", ASCENDING)], name="validations_approver_status_idx")
    _ensure_index(db.products, [("category", ASCENDING)], name="products_category_idx")
    _ensure_index(db.products, [("type", ASCENDING)], name="products_type_idx")
    _ensure_index(db.orders, [("status", ASCENDING)], name="orders_status_idx")
    _ensure_index(db.orders, [("centre_uid", ASCENDING)], name="orders_centre_idx")
    _ensure_index(db.transactions, [("transaction_type", ASCENDING)], name="transactions_type_idx")
    _ensure_index(db.transactions, [("centre_uid", ASCENDING)], name="transactions_centre_idx")
    _ensure_index(db.transactions, [("mitra_uid", ASCENDING)], name="transactions_mitra_idx")
    _ensure_index(db.audit_logs, [("created_at", ASCENDING)], name="audit_created_at_idx")
    _ensure_index(db.location_master, [("state", ASCENDING)], name="location_state_idx")
    _ensure_index(db.location_master, [("state", ASCENDING), ("district", ASCENDING), ("block", ASCENDING)], name="location_hierarchy_idx")
    seed_locations()
