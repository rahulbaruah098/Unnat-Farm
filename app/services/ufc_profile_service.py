from app.utils.timezone import business_today
import re
from datetime import date, datetime

from bson import ObjectId

from app.extensions import mongo
from app.services.document_service import find_document_path


GSTIN_FORMAT = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_FORMAT = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PIN_FORMAT = re.compile(r"^[1-9][0-9]{5}$")

# Numeric state codes used by GSTIN. Only the state/UT mapping is used here;
# this is an offline consistency check, not live GST registration verification.
INDIA_GST_STATE_CODES = {
    "Jammu and Kashmir": "01", "Himachal Pradesh": "02", "Punjab": "03",
    "Chandigarh": "04", "Uttarakhand": "05", "Haryana": "06", "Delhi": "07",
    "Rajasthan": "08", "Uttar Pradesh": "09", "Bihar": "10", "Sikkim": "11",
    "Arunachal Pradesh": "12", "Nagaland": "13", "Manipur": "14", "Mizoram": "15",
    "Tripura": "16", "Meghalaya": "17", "Assam": "18", "West Bengal": "19",
    "Jharkhand": "20", "Odisha": "21", "Chhattisgarh": "22", "Madhya Pradesh": "23",
    "Gujarat": "24", "Dadra and Nagar Haveli and Daman and Diu": "26",
    "Maharashtra": "27", "Karnataka": "29", "Goa": "30", "Lakshadweep": "31",
    "Kerala": "32", "Tamil Nadu": "33", "Puducherry": "34",
    "Andaman and Nicobar Islands": "35", "Telangana": "36", "Andhra Pradesh": "37",
    "Ladakh": "38",
}

LEGACY_STATE_CODES = {
    "JK": "01", "HP": "02", "PB": "03", "CH": "04", "UK": "05", "HR": "06",
    "DL": "07", "RJ": "08", "UP": "09", "BR": "10", "SK": "11", "AR": "12",
    "NL": "13", "MN": "14", "MZ": "15", "TR": "16", "ML": "17", "AS": "18",
    "WB": "19", "JH": "20", "OD": "21", "CG": "22", "MP": "23", "GJ": "24",
    "MH": "27", "KA": "29", "GA": "30", "LD": "31", "KL": "32", "TN": "33",
    "PY": "34", "AN": "35", "TS": "36", "AP": "37", "LA": "38",
}

UFC_DOCUMENTS = {
    "registration_certificate": {
        "label": "Registration Certificate",
        "aliases": ["Registration Certificate", "Centre Registration Certificate"],
        "required": True,
    },
    "pan_file": {
        "label": "PAN",
        "aliases": ["PAN", "PAN Card", "PAN Document"],
        "required": True,
    },
    "gst_file": {
        "label": "GST Registration",
        "aliases": ["GST", "GST Certificate", "GST Registration", "GST Registration Document"],
        "required": False,
    },
    "trader_license_file": {
        "label": "Trader License",
        "aliases": ["Trader License", "Trade License", "Trader Licence", "Trade Licence"],
        "required": False,
    },
    "other_license_file": {
        "label": "Other Licenses",
        "aliases": ["Other Licenses", "Other License", "Other Licences"],
        "required": False,
    },
}


def _clean(value):
    return str(value or "").strip()


def _to_oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def parse_bool(value, default=None):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "registered"}:
        return True
    if text in {"0", "false", "no", "n", "off", "unregistered"}:
        return False
    return default


def gst_state_code(state):
    text = _clean(state)
    if not text:
        return ""
    if text.isdigit() and len(text) <= 2:
        return text.zfill(2)
    if text.upper() in LEGACY_STATE_CODES:
        return LEGACY_STATE_CODES[text.upper()]
    for name, code in INDIA_GST_STATE_CODES.items():
        if name.lower() == text.lower():
            return code
    return ""


def normalize_pan(value, required=True):
    pan = "".join(_clean(value).split()).upper()
    if not pan and not required:
        return ""
    if not PAN_FORMAT.fullmatch(pan):
        raise ValueError("Enter a valid 10-character PAN (for example ABCDE1234F).")
    return pan


def is_valid_gstin(value):
    return bool(GSTIN_FORMAT.fullmatch("".join(_clean(value).split()).upper()))


def normalize_gstin(value, registered=None, pan="", state=""):
    gstin = "".join(_clean(value).split()).upper()
    registered = parse_bool(registered, default=None)

    if registered is False:
        if gstin:
            raise ValueError("GSTIN was entered while GST Registration is set to No. Choose Yes or clear the GSTIN.")
        return "", False

    if registered is None:
        registered = bool(gstin)

    if not registered:
        return "", False

    if not gstin:
        raise ValueError("GSTIN is required when GST Registration is set to Yes.")

    if not GSTIN_FORMAT.fullmatch(gstin):
        raise ValueError("Enter a valid 15-character GSTIN. This check validates the GSTIN structure only.")

    clean_pan = "".join(_clean(pan).split()).upper()
    if clean_pan and PAN_FORMAT.fullmatch(clean_pan) and gstin[2:12] != clean_pan:
        raise ValueError("The PAN embedded in the GSTIN does not match the PAN entered in the profile.")

    expected_state_code = gst_state_code(state)
    if expected_state_code and gstin[:2] != expected_state_code:
        raise ValueError(
            f"GSTIN state code {gstin[:2]} does not match the Centre state ({state}, GST code {expected_state_code})."
        )

    return gstin, True


def normalize_pin(value):
    pin = "".join(_clean(value).split())
    if not pin:
        return ""
    if not PIN_FORMAT.fullmatch(pin):
        raise ValueError("Enter a valid 6-digit PIN code.")
    return pin


def calculate_age(dob_value):
    if not dob_value:
        return ""
    try:
        dob = datetime.strptime(str(dob_value), "%Y-%m-%d").date()
    except Exception:
        return ""
    today = business_today()
    if dob > today:
        return ""
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return str(age) if age >= 0 else ""


def get_ufc_admin_master(user):
    user = user or {}
    user_id = user.get("_id")
    uid = _clean(user_id)
    centre_uid = _clean(user.get("centre_uid") or user.get("mapped_centre_uid"))
    terms = []
    if uid:
        terms.append({"linked_user_id": uid})
        oid = _to_oid(uid)
        if oid:
            terms.append({"linked_user_id": oid})
    if centre_uid:
        terms.append({"centre_uid": centre_uid})
    if not terms:
        return {}
    return mongo.db.ufc_admin_master.find_one({"$or": terms}) or {}


def _document_owner_terms(user_id, master_id=None):
    uid = _clean(user_id)
    terms = []
    if uid:
        for key in [
            "linked_user_id", "user_id", "owner_user_id", "created_by_user_id",
            "uploaded_by", "uploader_user_id", "entity_user_id", "entity_id",
        ]:
            terms.append({key: uid})
        oid = _to_oid(uid)
        if oid:
            for key in [
                "linked_user_id", "user_id", "owner_user_id", "created_by_user_id",
                "uploaded_by", "uploader_user_id", "entity_user_id", "entity_id",
            ]:
                terms.append({key: oid})
    if master_id:
        for value in [master_id, _clean(master_id)]:
            if not value:
                continue
            for key in ["linked_master_id", "master_id", "record_id", "entity_master_id", "parent_id"]:
                terms.append({key: value})
    return terms


def get_ufc_profile_documents(user_id, master=None):
    master = master or {}
    owner_terms = _document_owner_terms(user_id, master.get("_id"))
    result = {}

    for field, config in UFC_DOCUMENTS.items():
        aliases = config["aliases"]
        query = {
            "$and": [
                {"$or": owner_terms},
                {
                    "$or": [
                        {"document_type": {"$in": aliases}},
                        {"label": {"$in": aliases}},
                        {"document_label": {"$in": aliases}},
                        {"doc_type": {"$in": aliases}},
                        {"title": {"$in": aliases}},
                    ]
                },
                {"status": {"$nin": ["replaced", "historical", "deleted", "cancelled"]}},
            ]
        }
        candidates = list(mongo.db.documents.find(query).sort([
            ("updated_at", -1), ("created_at", -1), ("_id", -1)
        ])) if owner_terms else []

        chosen = None
        missing_candidate = None
        for doc in candidates:
            file_ref = (
                doc.get("filename") or doc.get("file_path") or doc.get("file_name")
                or doc.get("stored_name") or doc.get("path") or ""
            )
            actual_path = find_document_path(file_ref) if file_ref else None
            row = {
                "field": field,
                "label": config["label"],
                "required": bool(config.get("required")),
                "document_id": str(doc.get("_id") or ""),
                "filename": file_ref,
                "path": file_ref,
                "url": file_ref,
                "name": doc.get("original_filename") or doc.get("original_name") or file_ref,
                "exists": bool(actual_path),
                "missing_file": bool(file_ref and not actual_path),
                "created_at": doc.get("created_at"),
            }
            if actual_path:
                chosen = row
                break
            if missing_candidate is None:
                missing_candidate = row

        fallback_ref = str(master.get(field) or "").strip()
        fallback_path = find_document_path(fallback_ref) if fallback_ref else None
        fallback_row = None
        if fallback_ref:
            fallback_row = {
                "field": field,
                "label": config["label"],
                "required": bool(config.get("required")),
                "document_id": "",
                "filename": fallback_ref,
                "path": fallback_ref,
                "url": fallback_ref,
                "name": fallback_ref,
                "exists": bool(fallback_path),
                "missing_file": bool(not fallback_path),
                "created_at": None,
            }

        result[field] = chosen or missing_candidate or fallback_row or {
            "field": field,
            "label": config["label"],
            "required": bool(config.get("required")),
            "document_id": "",
            "filename": "",
            "path": "",
            "url": "",
            "name": "",
            "exists": False,
            "missing_file": False,
            "created_at": None,
        }

    return result


def profile_health(user, master, documents):
    user = user or {}
    master = master or {}
    gstin = _clean(master.get("gst_number") or master.get("gstin")).upper()
    gst_registered = parse_bool(master.get("gst_registered"), default=None)
    if gst_registered is None:
        gst_registered = bool(gstin)

    checks = [
        ("Enterprise name", bool(_clean(master.get("name_of_enterprise")))),
        ("Owner name", bool(_clean(master.get("name_of_owner")))),
        ("Owner date of birth", bool(_clean(master.get("owner_dob")))),
        ("District", bool(_clean(master.get("district") or user.get("district")))),
        ("Block", bool(_clean(master.get("block") or user.get("block")))),
        ("Village", bool(_clean(master.get("village") or user.get("village")))),
        ("PAN", bool(PAN_FORMAT.fullmatch(_clean(master.get("pan_number")).upper()))),
        ("Registration Certificate", bool((documents.get("registration_certificate") or {}).get("exists"))),
        ("PAN document", bool((documents.get("pan_file") or {}).get("exists"))),
    ]

    tax_issue = ""
    if gst_registered:
        gst_ok = is_valid_gstin(gstin)
        checks.append(("Valid GSTIN", gst_ok))
        checks.append(("GST Registration document", bool((documents.get("gst_file") or {}).get("exists"))))
        if gstin and not gst_ok:
            tax_issue = "GSTIN needs correction before this Centre can charge GST on UFC-to-Farmer invoices."
        elif not gstin:
            tax_issue = "Centre is marked GST-registered but GSTIN is missing."
    elif gstin:
        checks.append(("GST registration status", False))
        tax_issue = "GSTIN is present but the Centre is marked non-GST. Please correct the tax profile."

    completed = sum(1 for _, ok in checks if ok)
    total = max(len(checks), 1)
    percent = int(round((completed / total) * 100))
    missing = [label for label, ok in checks if not ok]

    return {
        "percent": percent,
        "complete": percent == 100,
        "missing": missing,
        "gst_registered": bool(gst_registered),
        "gstin": gstin,
        "gstin_valid": is_valid_gstin(gstin) if gstin else False,
        "tax_issue": tax_issue,
    }
