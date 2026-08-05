from pymongo import ASCENDING
from pymongo.errors import OperationFailure

from app.services.location_service import seed_locations


_INDEX_OPTION_KEYS = (
    "unique",
    "sparse",
    "partialFilterExpression",
    "expireAfterSeconds",
    "collation",
)


def _safe_drop_index(collection, index_name):
    try:
        collection.drop_index(index_name)
    except Exception:
        pass


def _normalized_keys(keys):
    return [(str(field), int(direction)) for field, direction in list(keys)]


def _index_matches(meta, keys, options):
    """Return True when an existing Mongo index already meets the contract.

    Older builds dropped and recreated every matching index during each app
    start. Apart from unnecessary write locks, that could briefly remove a
    unique protection. Stage 0 keeps an exact compatible index in place and
    replaces only a genuinely conflicting definition.
    """
    if _normalized_keys(meta.get("key", [])) != _normalized_keys(keys):
        return False

    for option in _INDEX_OPTION_KEYS:
        expected = options.get(option)
        existing = meta.get(option)

        if option in {"unique", "sparse"}:
            expected = bool(expected)
            existing = bool(existing)

        if expected != existing:
            return False

    return True


def _ensure_index(collection, keys, name, **options):
    keys = list(keys)

    try:
        index_info = collection.index_information()
    except Exception:
        index_info = {}

    named = index_info.get(name)
    if named and _index_matches(named, keys, options):
        return name

    # An exact compatible index with an older/default name is sufficient. Keep
    # it instead of forcing a drop solely to rename it.
    for existing_name, meta in index_info.items():
        if existing_name == "_id_":
            continue
        if _index_matches(meta, keys, options):
            return existing_name

    # Remove only conflicting definitions on the desired name or key pattern.
    for existing_name, meta in index_info.items():
        if existing_name == "_id_":
            continue
        same_name = existing_name == name
        same_keys = _normalized_keys(meta.get("key", [])) == _normalized_keys(keys)
        if same_name or same_keys:
            _safe_drop_index(collection, existing_name)

    try:
        return collection.create_index(keys, name=name, **options)
    except OperationFailure:
        # A concurrent startup may have created the index after our initial
        # inspection. Re-read once before attempting a controlled replacement.
        try:
            refreshed = collection.index_information()
            for existing_name, meta in refreshed.items():
                if _index_matches(meta, keys, options):
                    return existing_name
        except Exception:
            pass

        _safe_drop_index(collection, name)
        return collection.create_index(keys, name=name, **options)


def _ensure_unique_string_index(collection, field, name):
    return _ensure_index(
        collection,
        [(field, ASCENDING)],
        name=name,
        unique=True,
        partialFilterExpression={field: {"$type": "string", "$gt": ""}},
    )


def init_indexes(db):
    # User auth indexes. Optional fields use partial indexes so None/missing
    # values do not collide.
    _ensure_unique_string_index(db.users, "username", "username_unique_string")
    _ensure_unique_string_index(db.users, "phone", "phone_unique_string")
    _ensure_unique_string_index(db.users, "user_ref_id", "user_ref_id_unique_string")
    _ensure_unique_string_index(db.users, "centre_uid", "centre_uid_unique_string")
    _ensure_unique_string_index(db.users, "mitra_uid", "mitra_uid_unique_string")

    # Master UID indexes.
    _ensure_unique_string_index(
        db.ufc_admin_master,
        "centre_uid",
        "ufc_admin_centre_uid_unique_string",
    )
    _ensure_unique_string_index(
        db.ufc_mitra_master,
        "mitra_uid",
        "ufc_mitra_uid_unique_string",
    )

    # Existing operational indexes.
    _ensure_index(db.farmer_master, [("contact_no", ASCENDING)], name="farmer_contact_idx")
    _ensure_index(db.farmer_master, [("centre_uid", ASCENDING)], name="farmer_centre_idx")
    _ensure_index(db.farmer_master, [("mitra_uid", ASCENDING)], name="farmer_mitra_idx")
    _ensure_index(db.documents, [("linked_user_id", ASCENDING)], name="documents_linked_user_idx")
    _ensure_index(
        db.validations,
        [("entity_type", ASCENDING), ("status", ASCENDING)],
        name="validations_entity_status_idx",
    )
    _ensure_index(
        db.validations,
        [("approver_role", ASCENDING), ("status", ASCENDING)],
        name="validations_approver_status_idx",
    )
    _ensure_index(db.products, [("category", ASCENDING)], name="products_category_idx")
    _ensure_index(db.products, [("type", ASCENDING)], name="products_type_idx")
    _ensure_unique_string_index(
        db.products,
        "product_code",
        "products_product_code_unique_string",
    )
    _ensure_unique_string_index(
        db.products,
        "barcode_normalized",
        "products_barcode_unique_string",
    )
    _ensure_index(
        db.products,
        [("product_role", ASCENDING), ("unnatfarm_eligible", ASCENDING)],
        name="products_role_eligibility_idx",
    )
    _ensure_index(
        db.products,
        [("is_deleted", ASCENDING), ("is_active", ASCENDING), ("created_at", ASCENDING)],
        name="products_active_created_idx",
    )
    _ensure_index(db.orders, [("status", ASCENDING)], name="orders_status_idx")
    _ensure_index(db.orders, [("centre_uid", ASCENDING)], name="orders_centre_idx")
    _ensure_index(
        db.orders,
        [("centre_uid", ASCENDING), ("created_at", ASCENDING)],
        name="orders_centre_created_idx",
    )
    _ensure_index(
        db.orders,
        [("farmer_user_id", ASCENDING), ("created_at", ASCENDING)],
        name="orders_farmer_created_idx",
    )
    _ensure_unique_string_index(db.orders, "source_reference", "orders_source_reference_unique")
    _ensure_index(
        db.orders,
        [("accounting_posting_id", ASCENDING)],
        name="orders_accounting_posting_idx",
        partialFilterExpression={"accounting_posting_id": {"$type": "string", "$gt": ""}},
    )

    _ensure_index(db.transactions, [("transaction_type", ASCENDING)], name="transactions_type_idx")
    _ensure_index(db.transactions, [("centre_uid", ASCENDING)], name="transactions_centre_idx")
    _ensure_index(db.transactions, [("mitra_uid", ASCENDING)], name="transactions_mitra_idx")
    _ensure_index(
        db.transactions,
        [("farmer_contact", ASCENDING), ("transaction_type", ASCENDING), ("created_at", ASCENDING)],
        name="transactions_farmer_type_created_idx",
    )
    _ensure_unique_string_index(
        db.transactions,
        "source_reference",
        "transactions_source_reference_unique",
    )

    _ensure_index(
        db.pos_sales,
        [("centre_uid", ASCENDING), ("created_at", ASCENDING)],
        name="pos_sales_centre_created_idx",
    )
    _ensure_index(db.pos_sales, [("invoice_no", ASCENDING)], name="pos_sales_invoice_idx")
    _ensure_unique_string_index(
        db.pos_sales,
        "source_reference",
        "pos_sales_source_reference_unique",
    )

    _ensure_index(db.audit_logs, [("created_at", ASCENDING)], name="audit_created_at_idx")
    _ensure_index(db.location_master, [("state", ASCENDING)], name="location_state_idx")
    _ensure_index(
        db.location_master,
        [("state", ASCENDING), ("district", ASCENDING), ("block", ASCENDING)],
        name="location_hierarchy_idx",
    )

    seed_locations()