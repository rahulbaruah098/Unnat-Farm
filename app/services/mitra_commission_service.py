from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from bson import ObjectId

from app.extensions import mongo
from app.utils.helpers import now_utc
from app.utils.timezone import business_today, to_india_datetime

INPUT_BONUS_TYPE = "avpl_product_sale"
SYSTEM_DEFAULT_INPUT_PERCENTAGE = Decimal("2.00")
MONEY = Decimal("0.01")


def _clean(value) -> str:
    return str(value or "").strip()


def _dec(value, default="0") -> Decimal:
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _money(value) -> float:
    return float(_dec(value).quantize(MONEY, rounding=ROUND_HALF_UP))


def _oid(value):
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _setting_sort():
    return [("updated_at", -1), ("created_at", -1), ("_id", -1)]


def _valid_percentage(value) -> Decimal:
    try:
        rate = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Commission percentage must be a valid number.") from exc
    if not rate.is_finite():
        raise ValueError("Commission percentage must be a valid number.")
    if rate < 0 or rate > 100:
        raise ValueError("Commission percentage must be between 0 and 100.")
    return rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _find_active_setting(mitra_uid, category):
    query = {
        "bonus_type": INPUT_BONUS_TYPE,
        "category": _clean(category).lower() or "all",
        "mitra_uid": mitra_uid,
        "is_active": {"$ne": False},
    }
    return mongo.db.mitra_bonus_settings.find_one(query, sort=_setting_sort())


def resolve_input_commission(mitra_uid, category="all") -> dict:
    """Resolve the percentage used for one UFC input-product sale line.

    Priority is deliberately predictable:
      1. Mitra-wide override (new AVPL-managed setting)
      2. Mitra + category legacy override
      3. Global + category legacy override
      4. Global default
      5. System fallback (2%) until AVPL explicitly configures a default

    The returned source/setting id is intended to be snapshotted on the sale.
    Old sale snapshots must never be recomputed when a setting changes later.
    """
    mitra_uid = _clean(mitra_uid)
    category_key = _clean(category).lower() or "all"
    if not mitra_uid:
        return {
            "percentage": Decimal("0"),
            "percentage_float": 0.0,
            "source": "no_mapped_mitra",
            "source_label": "No mapped Mitra",
            "setting_id": "",
            "category": category_key,
        }

    candidates = [(mitra_uid, "all", "mitra_default", "Mitra override")]
    if category_key != "all":
        candidates.append((mitra_uid, category_key, "mitra_category", "Mitra category override"))
        candidates.append((None, category_key, "global_category", "AVPL category default"))
    candidates.append((None, "all", "global_default", "AVPL default"))

    for candidate_uid, candidate_category, source, label in candidates:
        setting = _find_active_setting(candidate_uid, candidate_category)
        if not setting:
            continue
        rate = _valid_percentage(setting.get("percentage") if setting.get("percentage") is not None else 0)
        return {
            "percentage": rate,
            "percentage_float": float(rate),
            "source": source,
            "source_label": label,
            "setting_id": str(setting.get("_id") or ""),
            "category": category_key,
        }

    return {
        "percentage": SYSTEM_DEFAULT_INPUT_PERCENTAGE,
        "percentage_float": float(SYSTEM_DEFAULT_INPUT_PERCENTAGE),
        "source": "system_default",
        "source_label": "System default until AVPL configures a rate",
        "setting_id": "",
        "category": category_key,
    }


def get_global_input_policy() -> dict:
    setting = _find_active_setting(None, "all")
    if setting:
        rate = _valid_percentage(setting.get("percentage") if setting.get("percentage") is not None else 0)
        return {
            "percentage": float(rate),
            "percentage_display": f"{rate.normalize():f}" if rate != rate.to_integral() else f"{rate:.0f}",
            "source": "global_default",
            "source_label": "AVPL configured default",
            "setting_id": str(setting.get("_id") or ""),
            "updated_at": setting.get("updated_at"),
            "reason": setting.get("reason") or "",
        }
    rate = SYSTEM_DEFAULT_INPUT_PERCENTAGE
    return {
        "percentage": float(rate),
        "percentage_display": f"{rate:.0f}",
        "source": "system_default",
        "source_label": "System fallback — save once to make it an AVPL setting",
        "setting_id": "",
        "updated_at": None,
        "reason": "",
    }


def _write_history(*, scope, mitra_uid, old_percentage, new_percentage, actor_user_id, action, reason, setting_id=""):
    mongo.db.mitra_commission_setting_history.insert_one({
        "scope": scope,
        "mitra_uid": _clean(mitra_uid) or None,
        "bonus_type": INPUT_BONUS_TYPE,
        "category": "all",
        "old_percentage": _money(old_percentage),
        "new_percentage": _money(new_percentage),
        "action": action,
        "reason": _clean(reason),
        "setting_id": _clean(setting_id),
        "changed_by": _oid(actor_user_id) or actor_user_id,
        "changed_by_str": _clean(actor_user_id),
        "changed_at": now_utc(),
    })


def save_global_input_rate(actor_user_id, percentage, *, reason="") -> dict:
    rate = _valid_percentage(percentage)
    current = get_global_input_policy()
    existing = mongo.db.mitra_bonus_settings.find_one(
        {"bonus_type": INPUT_BONUS_TYPE, "category": "all", "mitra_uid": None},
        sort=_setting_sort(),
    )
    timestamp = now_utc()
    payload = {
        "bonus_type": INPUT_BONUS_TYPE,
        "category": "all",
        "mitra_uid": None,
        "scope": "global",
        "percentage": float(rate),
        "is_active": True,
        "configured_by": _oid(actor_user_id) or actor_user_id,
        "configured_by_str": _clean(actor_user_id),
        "reason": _clean(reason),
        "updated_at": timestamp,
    }
    if existing:
        mongo.db.mitra_bonus_settings.update_one({"_id": existing["_id"]}, {"$set": payload})
        setting_id = existing["_id"]
    else:
        payload["created_at"] = timestamp
        setting_id = mongo.db.mitra_bonus_settings.insert_one(payload).inserted_id
    # Make any older duplicate global-default rows non-authoritative.
    mongo.db.mitra_bonus_settings.update_many(
        {
            "_id": {"$ne": setting_id},
            "bonus_type": INPUT_BONUS_TYPE,
            "mitra_uid": None,
            "is_active": {"$ne": False},
        },
        {"$set": {"is_active": False, "superseded_at": timestamp, "updated_at": timestamp}},
    )
    _write_history(
        scope="global", mitra_uid=None, old_percentage=current.get("percentage", 0),
        new_percentage=rate, actor_user_id=actor_user_id, action="set_global_default",
        reason=reason, setting_id=setting_id,
    )
    return get_global_input_policy()


def _find_mitra_master(mitra_uid):
    mitra_uid = _clean(mitra_uid)
    if not mitra_uid:
        return None
    return (
        mongo.db.ufc_mitra_master.find_one({"mitra_uid": mitra_uid})
        or mongo.db.ufc_mitra_master.find_one({"mapped_mitra_uid": mitra_uid})
    )


def save_mitra_input_rate(mitra_uid, actor_user_id, percentage, *, reason="") -> dict:
    mitra_uid = _clean(mitra_uid)
    if not _find_mitra_master(mitra_uid):
        raise ValueError("UFC Mitra was not found.")
    rate = _valid_percentage(percentage)
    before = resolve_input_commission(mitra_uid, "all")
    existing = mongo.db.mitra_bonus_settings.find_one(
        {"bonus_type": INPUT_BONUS_TYPE, "category": "all", "mitra_uid": mitra_uid},
        sort=_setting_sort(),
    )
    timestamp = now_utc()
    payload = {
        "bonus_type": INPUT_BONUS_TYPE,
        "category": "all",
        "mitra_uid": mitra_uid,
        "scope": "mitra",
        "percentage": float(rate),
        "is_active": True,
        "configured_by": _oid(actor_user_id) or actor_user_id,
        "configured_by_str": _clean(actor_user_id),
        "reason": _clean(reason),
        "updated_at": timestamp,
    }
    if existing:
        mongo.db.mitra_bonus_settings.update_one({"_id": existing["_id"]}, {"$set": payload})
        setting_id = existing["_id"]
    else:
        payload["created_at"] = timestamp
        setting_id = mongo.db.mitra_bonus_settings.insert_one(payload).inserted_id
    # A newly saved Mitra-wide override supersedes older duplicate Mitra-wide rows.
    mongo.db.mitra_bonus_settings.update_many(
        {
            "_id": {"$ne": setting_id},
            "bonus_type": INPUT_BONUS_TYPE,
            "mitra_uid": mitra_uid,
            "is_active": {"$ne": False},
        },
        {"$set": {"is_active": False, "superseded_at": timestamp, "updated_at": timestamp}},
    )
    _write_history(
        scope="mitra", mitra_uid=mitra_uid, old_percentage=before.get("percentage", 0),
        new_percentage=rate, actor_user_id=actor_user_id, action="set_mitra_override",
        reason=reason, setting_id=setting_id,
    )
    return get_mitra_setting_context(mitra_uid)


def reset_mitra_input_rate(mitra_uid, actor_user_id, *, reason="") -> dict:
    mitra_uid = _clean(mitra_uid)
    if not _find_mitra_master(mitra_uid):
        raise ValueError("UFC Mitra was not found.")
    before = resolve_input_commission(mitra_uid, "all")
    timestamp = now_utc()
    mongo.db.mitra_bonus_settings.update_many(
        {
            "bonus_type": INPUT_BONUS_TYPE,
            "mitra_uid": mitra_uid,
            "is_active": {"$ne": False},
        },
        {"$set": {
            "is_active": False,
            "disabled_at": timestamp,
            "disabled_by": _oid(actor_user_id) or actor_user_id,
            "disabled_by_str": _clean(actor_user_id),
            "disable_reason": _clean(reason),
            "updated_at": timestamp,
        }},
    )
    after = resolve_input_commission(mitra_uid, "all")
    _write_history(
        scope="mitra", mitra_uid=mitra_uid, old_percentage=before.get("percentage", 0),
        new_percentage=after.get("percentage", 0), actor_user_id=actor_user_id,
        action="reset_to_default", reason=reason,
    )
    return get_mitra_setting_context(mitra_uid)


def resolve_mitra_for_user(user_id) -> dict:
    oid = _oid(user_id)
    user = mongo.db.users.find_one({"_id": oid}) if oid else None
    if not user:
        user = mongo.db.users.find_one({"_id": user_id}) or {}
    if _clean(user.get("role")).lower() != "ufc_mitra":
        raise ValueError("This user is not a UFC Mitra.")
    uid = _clean(user.get("mitra_uid") or user.get("mapped_mitra_uid"))
    master = None
    if uid:
        master = _find_mitra_master(uid)
    if not master:
        values = [str(user.get("_id") or "")]
        if oid:
            values.append(oid)
        master = mongo.db.ufc_mitra_master.find_one({"linked_user_id": {"$in": values}}) or {}
        uid = _clean(master.get("mitra_uid") or master.get("mapped_mitra_uid") or uid)
    if not uid:
        raise ValueError("This Mitra account does not have a Mitra UID yet.")
    return {"user": user, "master": master or {}, "mitra_uid": uid}


def _mitra_profile(mitra_uid) -> dict:
    master = _find_mitra_master(mitra_uid) or {}
    linked = master.get("linked_user_id")
    user = {}
    if linked:
        user_oid = _oid(linked)
        if user_oid:
            user = mongo.db.users.find_one({"_id": user_oid}) or {}
        if not user:
            user = mongo.db.users.find_one({"_id": linked}) or {}
    if not user:
        user = mongo.db.users.find_one({"$or": [
            {"mitra_uid": mitra_uid}, {"mapped_mitra_uid": mitra_uid}
        ]}) or {}
    centre_uid = _clean(master.get("mapped_centre_uid") or master.get("centre_uid") or user.get("mapped_centre_uid") or user.get("centre_uid"))
    centre = mongo.db.ufc_admin_master.find_one({"centre_uid": centre_uid}) or {}
    return {
        "mitra_uid": mitra_uid,
        "name": master.get("name") or user.get("name") or mitra_uid,
        "phone": master.get("phone") or master.get("contact_no") or user.get("phone") or "",
        "centre_uid": centre_uid,
        "centre_name": centre.get("name_of_enterprise") or centre.get("enterprise_name") or centre.get("name") or centre_uid,
        "linked_user_id": str(user.get("_id") or linked or ""),
        "farmer_count": mongo.db.farmer_master.count_documents({"mitra_uid": mitra_uid}),
    }


def get_mitra_setting_context(mitra_uid) -> dict:
    profile = _mitra_profile(_clean(mitra_uid))
    current = resolve_input_commission(profile["mitra_uid"], "all")
    global_policy = get_global_input_policy()
    active_override = _find_active_setting(profile["mitra_uid"], "all")
    return {
        "profile": profile,
        "current_percentage": float(current["percentage"]),
        "current_source": current.get("source") or "",
        "current_source_label": current.get("source_label") or "",
        "global_percentage": global_policy["percentage"],
        "has_override": bool(active_override),
        "override_percentage": float(_dec((active_override or {}).get("percentage"))) if active_override else None,
        "override_reason": (active_override or {}).get("reason") or "",
        "override_updated_at": (active_override or {}).get("updated_at"),
    }


def _sale_is_registered_farmer(sale) -> bool:
    buyer = sale.get("buyer") or {}
    return (
        _clean(sale.get("buyer_type")).lower() == "registered_farmer"
        or _clean(buyer.get("type")).lower() == "registered_farmer"
        or _clean(sale.get("sale_type")).lower() == "registered"
    )


def _input_rows_from_pos(sale) -> list[dict]:
    if not _sale_is_registered_farmer(sale):
        return []
    mitra_uid = _clean(sale.get("mitra_uid") or (sale.get("buyer") or {}).get("mitra_uid"))
    if not mitra_uid:
        return []
    buyer = sale.get("buyer") or {}
    farmer_name = buyer.get("name") or sale.get("farmer_name") or "Farmer"
    items = [x for x in (sale.get("items") or []) if isinstance(x, dict)]
    rows = []
    if items:
        for item in items:
            if _clean(item.get("source_type")).lower() not in {"", "input"}:
                continue
            earning = _dec(item.get("bonus_amount"))
            basis = _dec(item.get("bonus_basis_amount") if item.get("bonus_basis_amount") is not None else item.get("line_total"))
            rate = _dec(item.get("bonus_percentage"))
            # The report only surfaces stored snapshots. It never reconstructs a
            # current percentage for an old sale.
            rows.append({
                "source": "pos_sale",
                "source_id": str(sale.get("_id") or ""),
                "reference": sale.get("sale_number") or sale.get("invoice_no") or "",
                "sale_date": sale.get("sale_date") or sale.get("created_at"),
                "created_at": sale.get("created_at"),
                "mitra_uid": mitra_uid,
                "farmer_name": farmer_name,
                "farmer_contact": buyer.get("phone") or sale.get("farmer_phone") or "",
                "product_name": item.get("product_name") or "Product",
                "category": item.get("category") or "",
                "quantity": item.get("quantity") or 0,
                "unit_code": item.get("unit_code") or "Unit",
                "business_value": _money(basis),
                "percentage": float(rate),
                "earning": _money(earning),
                "setting_source": item.get("bonus_setting_source") or "stored_snapshot",
                "setting_source_label": item.get("bonus_setting_source_label") or "Stored sale snapshot",
                "status": sale.get("status") or "completed",
            })
    elif _dec(sale.get("bonus_amount")) != 0:
        basis = _dec(sale.get("bonus_base_total") if sale.get("bonus_base_total") is not None else sale.get("grand_total") or sale.get("total_amount"))
        rows.append({
            "source": "pos_sale", "source_id": str(sale.get("_id") or ""),
            "reference": sale.get("sale_number") or sale.get("invoice_no") or "",
            "sale_date": sale.get("sale_date") or sale.get("created_at"), "created_at": sale.get("created_at"),
            "mitra_uid": mitra_uid, "farmer_name": farmer_name,
            "farmer_contact": buyer.get("phone") or sale.get("farmer_phone") or "",
            "product_name": sale.get("product_name") or "Input Sale", "category": sale.get("product_category") or "",
            "quantity": sale.get("quantity") or 0, "unit_code": sale.get("unit_code") or "Unit",
            "business_value": _money(basis), "percentage": float(_dec(sale.get("bonus_percentage"))),
            "earning": _money(sale.get("bonus_amount")), "setting_source": "stored_snapshot",
            "setting_source_label": "Stored sale snapshot", "status": sale.get("status") or "completed",
        })
    return rows


def _input_rows_from_order_sale(sale) -> list[dict]:
    mitra_uid = _clean(sale.get("mitra_uid"))
    if not mitra_uid:
        return []
    rows = []
    items = [x for x in (sale.get("items") or []) if isinstance(x, dict)]
    for item in items:
        earning = _dec(item.get("bonus_amount"))
        if not item.get("bonus_snapshot_version") and earning == 0:
            continue
        basis = _dec(item.get("bonus_basis_amount") if item.get("bonus_basis_amount") is not None else item.get("line_total"))
        rows.append({
            "source": "farmer_order", "source_id": str(sale.get("_id") or ""),
            "reference": sale.get("order_number") or sale.get("sale_number") or "",
            "sale_date": sale.get("sale_date") or sale.get("created_at"), "created_at": sale.get("created_at"),
            "mitra_uid": mitra_uid, "farmer_name": sale.get("farmer_name") or "Farmer", "farmer_contact": sale.get("farmer_contact") or "",
            "product_name": item.get("product_name") or "Product", "category": item.get("category") or "",
            "quantity": item.get("quantity") or item.get("accepted_quantity") or 0, "unit_code": item.get("unit_code") or "Unit",
            "business_value": _money(basis), "percentage": float(_dec(item.get("bonus_percentage"))),
            "earning": _money(earning), "setting_source": item.get("bonus_setting_source") or "stored_snapshot",
            "setting_source_label": item.get("bonus_setting_source_label") or "Stored sale snapshot",
            "status": sale.get("status") or "received",
        })
    if not items and sale.get("bonus_snapshot_version"):
        basis = _dec(sale.get("bonus_base_total") if sale.get("bonus_base_total") is not None else sale.get("grand_total"))
        rows.append({
            "source": "farmer_order", "source_id": str(sale.get("_id") or ""), "reference": sale.get("order_number") or sale.get("sale_number") or "",
            "sale_date": sale.get("sale_date") or sale.get("created_at"), "created_at": sale.get("created_at"), "mitra_uid": mitra_uid,
            "farmer_name": sale.get("farmer_name") or "Farmer", "farmer_contact": sale.get("farmer_contact") or "",
            "product_name": sale.get("product_name") or "Input Sale", "category": sale.get("product_category") or "",
            "quantity": sale.get("quantity") or 0, "unit_code": sale.get("unit_code") or "Unit",
            "business_value": _money(basis), "percentage": float(_dec(sale.get("bonus_percentage"))),
            "earning": _money(sale.get("bonus_amount")), "setting_source": "stored_snapshot",
            "setting_source_label": "Stored sale snapshot", "status": sale.get("status") or "received",
        })
    return rows


def _row_dt(row):
    for value in (row.get("created_at"), row.get("sale_date")):
        dt = to_india_datetime(value)
        if dt:
            return dt
        if isinstance(value, str):
            try:
                parsed = datetime.strptime(value[:10], "%Y-%m-%d")
                return parsed
            except Exception:
                pass
    return None


def _period_match(row, period):
    key = _clean(period).lower() or "this_month"
    if key == "all":
        return True
    dt = _row_dt(row)
    if not dt:
        return False
    today = business_today()
    day = dt.date()
    if key == "today":
        return day == today
    if key == "this_month":
        return day.year == today.year and day.month == today.month
    if key == "this_year":
        return day.year == today.year
    return True


def _format_row(row):
    result = dict(row)
    dt = _row_dt(row)
    result["date_display"] = dt.strftime("%d %b %Y") if dt else "-"
    result["business_value_display"] = f"{_dec(row.get('business_value')).quantize(MONEY):.2f}"
    result["earning_display"] = f"{_dec(row.get('earning')).quantize(MONEY):.2f}"
    rate = _dec(row.get("percentage"))
    result["percentage_display"] = f"{rate:.2f}".rstrip("0").rstrip(".")
    quantity = _dec(row.get("quantity"))
    result["quantity_display"] = f"{quantity:f}".rstrip("0").rstrip(".") or "0"
    return result


def get_mitra_earning_overview(mitra_uid, *, period="this_month", q="", page=1, per_page=25) -> dict:
    mitra_uid = _clean(mitra_uid)
    if not mitra_uid:
        raise ValueError("Mitra UID is unavailable for this account.")
    try:
        page = max(int(page or 1), 1)
    except Exception:
        page = 1
    try:
        per_page = min(max(int(per_page or 25), 10), 100)
    except Exception:
        per_page = 25

    rows = []
    pos_query = {"mitra_uid": mitra_uid, "status": "completed"}
    for sale in mongo.db.pos_sales.find(pos_query).sort("created_at", -1):
        rows.extend(_input_rows_from_pos(sale))
    order_query = {"mitra_uid": mitra_uid, "status": {"$nin": ["cancelled", "voided"]}, "bonus_financial_sync_status": "complete"}
    for sale in mongo.db.ufc_farmer_sales.find(order_query).sort("created_at", -1):
        rows.extend(_input_rows_from_order_sale(sale))

    all_rows = [_format_row(row) for row in rows]
    all_rows.sort(key=lambda row: ((_row_dt(row) or datetime.min).timestamp() if _row_dt(row) else 0, row.get("source_id") or ""), reverse=True)
    filtered = [row for row in all_rows if _period_match(row, period)]
    query = _clean(q).lower()
    if query:
        filtered = [row for row in filtered if query in " ".join([
            _clean(row.get("reference")), _clean(row.get("farmer_name")), _clean(row.get("product_name")),
            _clean(row.get("category")), _clean(row.get("business_value_display")), _clean(row.get("percentage_display")),
            _clean(row.get("earning_display")), _clean(row.get("date_display")),
        ]).lower()]

    month_rows = [row for row in all_rows if _period_match(row, "this_month")]
    lifetime_business = sum(_dec(row.get("business_value")) for row in all_rows)
    lifetime_earning = sum(_dec(row.get("earning")) for row in all_rows)
    month_business = sum(_dec(row.get("business_value")) for row in month_rows)
    month_earning = sum(_dec(row.get("earning")) for row in month_rows)

    # Preserve historical non-input stored bonus as an informational total only;
    # it is not controlled by the new AVPL input commission setting.
    legacy_other = Decimal("0")
    for sale in mongo.db.farmer_product_sales.find({"mitra_uid": mitra_uid}):
        legacy_other += _dec(sale.get("bonus_amount"))

    total = len(filtered)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    current = resolve_input_commission(mitra_uid, "all")
    profile = _mitra_profile(mitra_uid)
    return {
        "profile": profile,
        "current_rate": float(current["percentage"]),
        "current_rate_display": f"{current['percentage']:.2f}".rstrip("0").rstrip("."),
        "current_rate_source": current.get("source_label") or "",
        "period": _clean(period).lower() or "this_month",
        "q": _clean(q),
        "periods": [("this_month", "This Month"), ("today", "Today"), ("this_year", "This Year"), ("all", "All Time")],
        "rows": filtered[start:start + per_page],
        "summary": {
            "month_business": _money(month_business),
            "month_earning": _money(month_earning),
            "lifetime_business": _money(lifetime_business),
            "lifetime_input_earning": _money(lifetime_earning),
            "legacy_other_earning": _money(legacy_other),
            "lifetime_total_earning": _money(lifetime_earning + legacy_other),
            "farmer_count": profile.get("farmer_count", 0),
        },
        "pagination": {
            "page": page, "per_page": per_page, "total": total, "total_pages": total_pages,
            "has_prev": page > 1, "has_next": page < total_pages,
        },
    }


def get_admin_mitra_earning_overview(*, month="", q="") -> dict:
    mitras = list(mongo.db.ufc_mitra_master.find({}).sort("name", 1))
    profile_by_uid = {}
    for master in mitras:
        uid = _clean(master.get("mitra_uid") or master.get("mapped_mitra_uid"))
        if uid:
            profile_by_uid[uid] = _mitra_profile(uid)

    aggregates = {uid: {"month_business": Decimal("0"), "month_earning": Decimal("0"), "lifetime_business": Decimal("0"), "lifetime_earning": Decimal("0")} for uid in profile_by_uid}
    selected_month = _clean(month)
    if not selected_month:
        today = business_today()
        selected_month = f"{today.year:04d}-{today.month:02d}"

    def add_rows(source_rows):
        for row in source_rows:
            uid = _clean(row.get("mitra_uid"))
            if uid not in aggregates:
                continue
            business = _dec(row.get("business_value")); earning = _dec(row.get("earning"))
            aggregates[uid]["lifetime_business"] += business
            aggregates[uid]["lifetime_earning"] += earning
            dt = _row_dt(row)
            if dt and f"{dt.year:04d}-{dt.month:02d}" == selected_month:
                aggregates[uid]["month_business"] += business
                aggregates[uid]["month_earning"] += earning

    pos_rows = []
    for sale in mongo.db.pos_sales.find({"mitra_uid": {"$exists": True, "$nin": [None, ""]}, "status": "completed"}):
        pos_rows.extend(_input_rows_from_pos(sale))
    order_rows = []
    for sale in mongo.db.ufc_farmer_sales.find({"mitra_uid": {"$exists": True, "$nin": [None, ""]}, "status": {"$nin": ["cancelled", "voided"]}, "bonus_financial_sync_status": "complete"}):
        order_rows.extend(_input_rows_from_order_sale(sale))
    add_rows(pos_rows); add_rows(order_rows)

    q_lower = _clean(q).lower()
    rows = []
    for uid, profile in profile_by_uid.items():
        current = resolve_input_commission(uid, "all")
        agg = aggregates.get(uid) or {}
        row = {
            **profile,
            "current_rate": float(current["percentage"]),
            "current_rate_display": f"{current['percentage']:.2f}".rstrip("0").rstrip("."),
            "rate_source": current.get("source_label") or "",
            "has_override": current.get("source") in {"mitra_default", "mitra_category"},
            "month_business": _money(agg.get("month_business", 0)),
            "month_earning": _money(agg.get("month_earning", 0)),
            "lifetime_business": _money(agg.get("lifetime_business", 0)),
            "lifetime_earning": _money(agg.get("lifetime_earning", 0)),
        }
        if q_lower and q_lower not in " ".join([
            _clean(row.get("name")), uid, _clean(row.get("centre_uid")), _clean(row.get("centre_name")), _clean(row.get("phone"))
        ]).lower():
            continue
        rows.append(row)
    rows.sort(key=lambda row: (-row.get("month_earning", 0), row.get("name") or ""))
    return {
        "rows": rows,
        "selected_month": selected_month,
        "q": _clean(q),
        "global_policy": get_global_input_policy(),
        "summary": {
            "mitras": len(rows),
            "farmers": sum(int(row.get("farmer_count") or 0) for row in rows),
            "month_business": round(sum(row.get("month_business", 0) for row in rows), 2),
            "month_earning": round(sum(row.get("month_earning", 0) for row in rows), 2),
        },
    }
