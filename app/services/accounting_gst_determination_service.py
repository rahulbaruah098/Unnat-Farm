from app.utils.timezone import business_today
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from bson import ObjectId
from bson.decimal128 import Decimal128
from pymongo import ASCENDING

from app.extensions import mongo
from app.services.accounting_configuration_service import INDIA_STATE_CODES
from app.services.accounting_gst_tax_service import get_effective_gst_tax_rate
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.services.accounting_product_mapping_service import (
    get_product_accounting_mapping_for_posting,
)


AVPL_ENTITY_CODE = "AVPL"
PARTY_LEDGER_COLLECTION = "ledgers"
PRODUCT_MAPPING_COLLECTION = "accounting_product_mappings"
POLICY_COLLECTION = "accounting_settings"

STATUS_ACTIVE = "active"
STATUS_APPROVED = "approved"

VIEW_PERMISSION = "accounting.gst_determination.view"
PREVIEW_PERMISSION = "accounting.gst_determination.preview"

TRANSACTION_TYPES = {
    "sales": {
        "label": "Sales",
        "party_role": "customer",
        "party_label": "Customer / buyer",
        "tax_direction": "output",
    },
    "purchase": {
        "label": "Purchase",
        "party_role": "supplier",
        "party_label": "Supplier",
        "tax_direction": "input",
    },
}

TAXABILITY_LABELS = {
    "TAXABLE": "Taxable",
    "EXEMPT": "Exempt",
    "NIL_RATED": "Nil Rated",
    "NON_GST": "Non-GST",
}

STATE_NAMES_BY_CODE = {}
for _state_name, _state_code in INDIA_STATE_CODES.items():
    STATE_NAMES_BY_CODE.setdefault(_state_code, _state_name)


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _decimal_value(value, label="Amount"):
    try:
        if isinstance(value, Decimal128):
            number = value.to_decimal()
        elif isinstance(value, Decimal):
            number = value
        else:
            number = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a valid number.") from exc

    if not number.is_finite():
        raise ValueError(f"{label} must be a finite number.")
    return number


def _money(value):
    return _decimal_value(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal_string(value, places="0.01"):
    number = _decimal_value(value).quantize(Decimal(places), rounding=ROUND_HALF_UP)
    return format(number, "f")


def _rate_string(value):
    number = _decimal_value(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_date(value, label="Transaction date"):
    if isinstance(value, datetime):
        return datetime.combine(value.date(), time.min)
    if isinstance(value, date):
        return datetime.combine(value, time.min)

    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD format.") from exc


def _get_actor(actor_user_id):
    actor_id = _to_object_id(actor_user_id)
    if not actor_id:
        raise ValueError("Invalid authenticated user.")

    actor = mongo.db.users.find_one(
        {"_id": actor_id},
        {
            "name": 1,
            "full_name": 1,
            "username": 1,
            "phone": 1,
            "role": 1,
            "active": 1,
            "is_active": 1,
            "status": 1,
        },
    )
    if not actor:
        raise ValueError("Authenticated user was not found.")

    if (
        actor.get("active", True) is False
        or actor.get("is_active", True) is False
        or actor.get("status") == "inactive"
    ):
        raise PermissionError("Inactive users cannot use GST determination.")

    role = str(actor.get("role") or "").strip().lower()
    if role not in {"super_admin", "avpl_admin", "accounts"}:
        raise PermissionError("You are not authorized to use GST determination.")

    actor["resolved_role"] = role
    actor["resolved_name"] = (
        actor.get("name")
        or actor.get("full_name")
        or actor.get("username")
        or actor.get("phone")
        or role.replace("_", " ").title()
    )
    return actor


def _assert_active_avpl_entity(entity_id=None):
    query = {
        "entity_code": AVPL_ENTITY_CODE,
        "entity_type": "avpl",
        "status": STATUS_ACTIVE,
        "accounting_enabled": {"$ne": False},
        "is_deleted": {"$ne": True},
    }
    object_id = _to_object_id(entity_id) if entity_id else None
    if object_id:
        query["_id"] = object_id

    entity = mongo.db.accounting_entities.find_one(query)
    if not entity:
        raise ValueError("The active AVPL Accounting entity was not found.")
    if not str(entity.get("state_code") or "").strip():
        raise ValueError(
            "Approve the AVPL entity profile with a valid business state before GST determination."
        )
    return entity


def _require_permission(actor, entity_id, permission):
    access = get_accounting_access(
        user_id=actor["_id"],
        session_role=actor.get("resolved_role") or actor.get("role"),
    )
    if not access.get("enabled"):
        raise PermissionError(access.get("message") or "Accounting access is disabled.")

    if actor.get("resolved_role") != "super_admin":
        allowed_entity_ids = {str(value) for value in access.get("entity_ids") or []}
        if str(entity_id) not in allowed_entity_ids:
            raise PermissionError("You do not have access to this Accounting entity.")

    if not has_accounting_permission(access, permission):
        raise PermissionError("You do not have permission to perform this action.")
    return access


def _active_policy(entity_id):
    return mongo.db[POLICY_COLLECTION].find_one(
        {
            "accounting_entity_id": entity_id,
            "status": STATUS_APPROVED,
            "is_active": True,
            "is_deleted": {"$ne": True},
        },
        sort=[("revision_number", -1)],
    )


def _state_name(state_code):
    return STATE_NAMES_BY_CODE.get(str(state_code or "").strip(), "Unknown state")


def _resolve_state_code(value, label="State"):
    text = str(value or "").strip()
    if not text:
        return ""

    if text in STATE_NAMES_BY_CODE:
        return text
    if text in INDIA_STATE_CODES:
        return INDIA_STATE_CODES[text]
    raise ValueError(f"Select a valid Indian {label.lower()}.")


def _get_party_ledger(entity_id, party_ledger_id, transaction_type):
    transaction_meta = TRANSACTION_TYPES[transaction_type]
    party_id = _to_object_id(party_ledger_id)
    if not party_id:
        raise ValueError(f"Select a valid {transaction_meta['party_label'].lower()} ledger.")

    party = mongo.db[PARTY_LEDGER_COLLECTION].find_one(
        {
            "_id": party_id,
            "accounting_entity_id": entity_id,
            "party_role": transaction_meta["party_role"],
            "status": STATUS_ACTIVE,
            "is_active": True,
            "is_deleted": {"$ne": True},
        }
    )
    if not party:
        raise ValueError(
            f"The selected ledger is not an active {transaction_meta['party_label'].lower()}."
        )
    if not str(party.get("state_code") or "").strip():
        raise ValueError("The selected party ledger does not have a valid state code.")
    return party


def _gst_charge_allowed(entity, party, transaction_type, taxability_code):
    if taxability_code != "TAXABLE":
        return True, ""

    entity_status = str(entity.get("gst_registration_status") or "").strip().lower()
    party_status = str(party.get("gst_registration_status") or "").strip().lower()

    if transaction_type == "sales":
        if entity_status != "registered_regular":
            return False, (
                "AVPL must be GST Registered - Regular to charge GST on a taxable sales document."
            )
    else:
        if party_status != "registered_regular":
            return False, (
                "The supplier must be GST Registered - Regular to charge input GST on this taxable purchase."
            )
    return True, ""


# ---------------------------------------------------------------------------
# Core posting-ready GST determination
# ---------------------------------------------------------------------------


def determine_gst_for_transaction(
    accounting_entity_id,
    party_ledger_id,
    source_product_id,
    transaction_type,
    transaction_date,
    taxable_value,
    place_of_supply_state_code=None,
):
    """
    Resolve GST treatment for one future Accounting transaction line.

    This function does not post a voucher, mutate stock, update a ledger balance,
    or persist a preview. Later purchase/sales document services can call it
    immediately before line calculation and controlled posting.
    """
    transaction_type = str(transaction_type or "").strip().lower()
    if transaction_type not in TRANSACTION_TYPES:
        raise ValueError("Transaction type must be Sales or Purchase.")

    entity = _assert_active_avpl_entity(accounting_entity_id)
    party = _get_party_ledger(entity["_id"], party_ledger_id, transaction_type)
    date_value = _parse_date(transaction_date)

    amount = _money(taxable_value)
    if amount <= 0:
        raise ValueError("Taxable value must be greater than zero.")

    mapping_bundle = get_product_accounting_mapping_for_posting(
        entity["_id"],
        source_product_id,
        transaction_date=date_value,
        operation=transaction_type,
    )

    mapping = mapping_bundle["mapping"]
    hsn = mapping_bundle["hsn"]
    taxability_code = str(hsn.get("taxability_code") or "").strip().upper()
    if taxability_code not in TAXABILITY_LABELS:
        raise ValueError("The product mapping has an unsupported taxability classification.")

    policy = _active_policy(entity["_id"])
    policy_payload = (policy or {}).get("payload") or {}

    explicit_pos_code = _resolve_state_code(
        place_of_supply_state_code,
        label="place of supply state",
    )
    entity_state_code = str(entity.get("state_code") or "").strip()
    party_state_code = str(party.get("state_code") or "").strip()
    policy_pos_code = str(
        policy_payload.get("default_place_of_supply_state_code") or ""
    ).strip()

    if transaction_type == "sales":
        seller_document = entity
        recipient_document = party
        seller_state_code = entity_state_code
        transaction_default_pos_code = party_state_code
        transaction_default_pos_source = "Customer ledger state"
    else:
        seller_document = party
        recipient_document = entity
        seller_state_code = party_state_code
        transaction_default_pos_code = entity_state_code
        transaction_default_pos_source = "AVPL registered state"

    place_of_supply_code = (
        explicit_pos_code
        or transaction_default_pos_code
        or policy_pos_code
        or seller_state_code
    )
    if not place_of_supply_code:
        raise ValueError("Place of supply could not be resolved.")

    if explicit_pos_code:
        place_source = "Explicit document selection"
    elif transaction_default_pos_code:
        place_source = transaction_default_pos_source
    elif policy_pos_code:
        place_source = "Approved Accounting policy default"
    else:
        place_source = "Seller state fallback"

    supply_type = (
        "intra_state"
        if seller_state_code == place_of_supply_code
        else "inter_state"
    )

    charge_allowed, charge_block_reason = _gst_charge_allowed(
        entity,
        party,
        transaction_type,
        taxability_code,
    )
    if not charge_allowed:
        raise ValueError(charge_block_reason)

    components = []
    effective_rate = None
    total_tax = Decimal("0.00")

    if taxability_code == "TAXABLE":
        rate_code = str(hsn.get("gst_rate_code") or "").strip().upper()
        if not rate_code:
            raise ValueError("The taxable product mapping does not contain a GST rate code.")

        effective_rate = get_effective_gst_tax_rate(
            entity["_id"],
            rate_code=rate_code,
            transaction_date=date_value,
        )

        if supply_type == "intra_state":
            component_rates = (
                ("CGST", _decimal_value(effective_rate.get("cgst_rate"))),
                ("SGST", _decimal_value(effective_rate.get("sgst_rate"))),
            )
        else:
            component_rates = (
                ("IGST", _decimal_value(effective_rate.get("igst_rate"))),
            )

        for component_code, component_rate in component_rates:
            component_amount = _money(amount * component_rate / Decimal("100"))
            total_tax += component_amount
            components.append(
                {
                    "code": component_code,
                    "rate": _rate_string(component_rate),
                    "amount": _decimal_string(component_amount),
                    "ledger_direction": TRANSACTION_TYPES[transaction_type]["tax_direction"],
                }
            )
    else:
        non_tax_reason = {
            "EXEMPT": "Exempt supply: no GST component is posted.",
            "NIL_RATED": "Nil-rated supply: GST rate is zero and no GST component is posted.",
            "NON_GST": "Non-GST supply: transaction remains outside GST component posting.",
        }[taxability_code]
        components.append(
            {
                "code": "NO_GST",
                "rate": "0",
                "amount": "0.00",
                "ledger_direction": "none",
                "reason": non_tax_reason,
            }
        )

    total_tax = _money(total_tax)
    gross_value = _money(amount + total_tax)

    return {
        "transaction_type": transaction_type,
        "transaction_type_label": TRANSACTION_TYPES[transaction_type]["label"],
        "transaction_date": date_value.strftime("%Y-%m-%d"),
        "transaction_date_display": date_value.strftime("%d %b %Y"),
        "tax_direction": TRANSACTION_TYPES[transaction_type]["tax_direction"],
        "seller": {
            "name": (
                seller_document.get("trade_name")
                or seller_document.get("display_name")
                or seller_document.get("name")
                or seller_document.get("legal_name")
                or "Seller"
            ),
            "state_name": (
                seller_document.get("state")
                or seller_document.get("state_name")
                or _state_name(seller_state_code)
            ),
            "state_code": seller_state_code,
            "gst_registration_status": seller_document.get("gst_registration_status") or "",
            "gstin": seller_document.get("gstin") or "",
        },
        "recipient": {
            "name": (
                recipient_document.get("trade_name")
                or recipient_document.get("display_name")
                or recipient_document.get("name")
                or recipient_document.get("legal_name")
                or "Recipient"
            ),
            "state_name": (
                recipient_document.get("state")
                or recipient_document.get("state_name")
                or _state_name(
                    recipient_document.get("state_code")
                    or entity_state_code
                )
            ),
            "state_code": str(
                recipient_document.get("state_code")
                or entity_state_code
            ).strip(),
            "gst_registration_status": recipient_document.get("gst_registration_status") or "",
            "gstin": recipient_document.get("gstin") or "",
        },
        "party": {
            "id": str(party.get("_id") or ""),
            "ledger_code": party.get("ledger_code") or "",
            "name": party.get("name") or party.get("legal_name") or "",
            "party_role": party.get("party_role") or "",
            "state_name": party.get("state_name") or _state_name(party_state_code),
            "state_code": party_state_code,
            "gst_registration_status": party.get("gst_registration_status") or "",
            "gstin": party.get("gstin") or "",
        },
        "place_of_supply": {
            "state_name": _state_name(place_of_supply_code),
            "state_code": place_of_supply_code,
            "source": place_source,
        },
        "supply_type": supply_type,
        "supply_type_label": (
            "Intra-state — CGST + SGST"
            if supply_type == "intra_state"
            else "Inter-state — IGST"
        ),
        "product": {
            "source_product_id": mapping.get("source_product_id") or "",
            "name": mapping.get("source_product_name") or "",
            "mapping_code": mapping.get("mapping_code") or "",
            "hsn_code": hsn.get("hsn_code") or "",
            "taxability_code": taxability_code,
            "taxability_label": TAXABILITY_LABELS[taxability_code],
            "gst_rate_code": hsn.get("gst_rate_code") or "",
        },
        "effective_rate": {
            "rate_code": (effective_rate or {}).get("rate_code") or "",
            "name": (effective_rate or {}).get("name") or "",
            "total_rate": _rate_string((effective_rate or {}).get("total_rate") or 0),
            "effective_from": (
                (effective_rate or {}).get("effective_from").strftime("%Y-%m-%d")
                if isinstance((effective_rate or {}).get("effective_from"), datetime)
                else ""
            ),
        },
        "taxable_value": _decimal_string(amount),
        "components": components,
        "total_tax": _decimal_string(total_tax),
        "gross_value": _decimal_string(gross_value),
        "posting_rule": (
            "Post CGST and SGST separately"
            if supply_type == "intra_state" and taxability_code == "TAXABLE"
            else "Post IGST only"
            if supply_type == "inter_state" and taxability_code == "TAXABLE"
            else "Do not post GST component ledgers"
        ),
        "is_taxable": taxability_code == "TAXABLE",
        "is_preview": True,
    }


# ---------------------------------------------------------------------------
# Permission-protected dashboard helpers
# ---------------------------------------------------------------------------


def preview_gst_determination(accounting_entity_id, actor_user_id, form):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], PREVIEW_PERMISSION)

    mapping_id = _to_object_id(form.get("product_mapping_id"))
    if not mapping_id:
        raise ValueError("Select an Accounting-ready product mapping.")

    mapping = mongo.db[PRODUCT_MAPPING_COLLECTION].find_one(
        {
            "_id": mapping_id,
            "accounting_entity_id": entity["_id"],
            "status": STATUS_ACTIVE,
            "is_active": True,
            "is_accounting_eligible": True,
            "is_deleted": {"$ne": True},
        },
        {"source_product_id": 1},
    )
    if not mapping:
        raise ValueError("The selected product mapping is not active and Accounting-ready.")

    return determine_gst_for_transaction(
        accounting_entity_id=entity["_id"],
        party_ledger_id=form.get("party_ledger_id"),
        source_product_id=mapping.get("source_product_id"),
        transaction_type=form.get("transaction_type"),
        transaction_date=form.get("transaction_date"),
        taxable_value=form.get("taxable_value"),
        place_of_supply_state_code=form.get("place_of_supply_state_code"),
    )


def get_gst_determination_overview(accounting_entity_id, actor_user_id):
    actor = _get_actor(actor_user_id)
    entity = _assert_active_avpl_entity(accounting_entity_id)
    _require_permission(actor, entity["_id"], VIEW_PERMISSION)

    policy = _active_policy(entity["_id"])
    policy_payload = (policy or {}).get("payload") or {}

    parties = list(
        mongo.db[PARTY_LEDGER_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "party_role": {"$in": ["supplier", "customer"]},
                "status": STATUS_ACTIVE,
                "is_active": True,
                "is_deleted": {"$ne": True},
            },
            {
                "ledger_code": 1,
                "name": 1,
                "legal_name": 1,
                "party_role": 1,
                "state_name": 1,
                "state_code": 1,
                "gst_registration_status": 1,
                "gstin": 1,
            },
        )
        .sort([("party_role", ASCENDING), ("name", ASCENDING)])
    )

    mappings = list(
        mongo.db[PRODUCT_MAPPING_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity["_id"],
                "status": STATUS_ACTIVE,
                "is_active": True,
                "is_accounting_eligible": True,
                "is_deleted": {"$ne": True},
            },
            {
                "mapping_code": 1,
                "source_product_id": 1,
                "source_product_name": 1,
                "hsn_code": 1,
                "taxability_code": 1,
                "taxability_name": 1,
                "gst_rate_code": 1,
            },
        )
        .sort([("source_product_name", ASCENDING)])
    )

    serialized_parties = [
        {
            "id": str(row.get("_id") or ""),
            "ledger_code": row.get("ledger_code") or "",
            "name": row.get("name") or row.get("legal_name") or "",
            "party_role": row.get("party_role") or "",
            "party_role_label": "Supplier" if row.get("party_role") == "supplier" else "Customer / buyer",
            "state_name": row.get("state_name") or _state_name(row.get("state_code")),
            "state_code": row.get("state_code") or "",
            "gst_registration_status": row.get("gst_registration_status") or "",
            "gstin": row.get("gstin") or "",
        }
        for row in parties
    ]

    serialized_mappings = [
        {
            "id": str(row.get("_id") or ""),
            "mapping_code": row.get("mapping_code") or "",
            "source_product_id": str(row.get("source_product_id") or ""),
            "source_product_name": row.get("source_product_name") or "",
            "hsn_code": row.get("hsn_code") or "",
            "taxability_code": row.get("taxability_code") or "",
            "taxability_name": row.get("taxability_name") or row.get("taxability_code") or "",
            "gst_rate_code": row.get("gst_rate_code") or "",
        }
        for row in mappings
    ]

    suppliers = [row for row in serialized_parties if row["party_role"] == "supplier"]
    customers = [row for row in serialized_parties if row["party_role"] == "customer"]

    seller_state_code = str(entity.get("state_code") or "").strip()
    default_pos_code = str(
        policy_payload.get("default_place_of_supply_state_code") or seller_state_code
    ).strip()

    return {
        "entity_id": str(entity.get("_id") or ""),
        "entity_code": entity.get("entity_code") or AVPL_ENTITY_CODE,
        "seller": {
            "name": entity.get("trade_name") or entity.get("display_name") or entity.get("legal_name") or "AVPL",
            "state_name": entity.get("state") or _state_name(seller_state_code),
            "state_code": seller_state_code,
            "gst_registration_status": entity.get("gst_registration_status") or "",
            "gstin": entity.get("gstin") or "",
        },
        "default_place_of_supply": {
            "state_name": policy_payload.get("default_place_of_supply_state") or _state_name(default_pos_code),
            "state_code": default_pos_code,
            "source": "Approved Accounting policy" if policy else "Seller state fallback",
        },
        "parties": serialized_parties,
        "suppliers": suppliers,
        "customers": customers,
        "product_mappings": serialized_mappings,
        "states": [
            {"name": name, "code": code}
            for name, code in sorted(INDIA_STATE_CODES.items(), key=lambda item: (item[1], item[0]))
        ],
        "transaction_types": [
            {"code": code, **meta}
            for code, meta in TRANSACTION_TYPES.items()
        ],
        "today": business_today().strftime("%Y-%m-%d"),
        "counts": {
            "suppliers": len(suppliers),
            "customers": len(customers),
            "product_mappings": len(serialized_mappings),
        },
        "prerequisites": {
            "seller_state_ready": bool(seller_state_code),
            "seller_gst_profile_ready": bool(entity.get("gst_registration_status")),
            "has_policy": bool(policy),
            "has_parties": bool(serialized_parties),
            "has_suppliers": bool(suppliers),
            "has_customers": bool(customers),
            "has_product_mappings": bool(serialized_mappings),
            "is_ready": bool(seller_state_code and serialized_parties and serialized_mappings),
        },
    }
