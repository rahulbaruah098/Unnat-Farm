from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.extensions import mongo
from app.services.mitra_commission_service import resolve_input_commission
from app.utils.timezone import business_today, to_india_datetime


def _dec(value, default="0") -> Decimal:
    if hasattr(value, "to_decimal"):
        value = value.to_decimal()
    try:
        number = Decimal(str(value if value not in (None, "") else default))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)
    return number if number.is_finite() else Decimal(default)


def _num(value) -> float:
    return float(_dec(value))


def _clean(value) -> str:
    return str(value or "").strip()


def _money(value) -> str:
    return f"{_dec(value).quantize(Decimal('0.01')):.2f}"


def _qty(value) -> str:
    number = _dec(value).quantize(Decimal("0.0001"))
    return f"{number:f}".rstrip("0").rstrip(".") or "0"


def _status(value, fallback="Completed") -> str:
    text = _clean(value)
    if not text:
        return fallback
    return text.replace("_", " ").title()


def _row_date(row, *fields):
    for field in fields:
        converted = to_india_datetime((row or {}).get(field))
        if converted is not None:
            return converted
    return None


def _period_bounds(period: str):
    """Return inclusive business-date bounds for display filtering.

    Mongo timestamps remain UTC. We filter after conversion to IST so boundary
    behaviour is consistent with the rest of the MIS business calendar.
    """
    today = business_today()
    key = _clean(period).lower() or "all"
    if key == "today":
        return today, today
    if key == "7d":
        return today - timedelta(days=6), today
    if key == "30d":
        return today - timedelta(days=29), today
    if key == "this_month":
        return today.replace(day=1), today
    return None, None


def _in_period(row_date, start, end) -> bool:
    if row_date is None:
        return start is None and end is None
    day = row_date.date()
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def _search_match(row, q: str) -> bool:
    q = _clean(q).lower()
    if not q:
        return True
    haystack = " ".join(
        _clean(row.get(key))
        for key in (
            "type_label", "farmer_name", "farmer_contact", "product_name",
            "quantity_display", "unit_code", "value_display", "earning_display",
            "status_label", "reference",
        )
    ).lower()
    return q in haystack


def _farmer_match(row, farmer: str) -> bool:
    farmer = _clean(farmer)
    if not farmer:
        return True
    return farmer in {
        _clean(row.get("farmer_id")),
        _clean(row.get("farmer_user_id")),
        _clean(row.get("farmer_contact")),
    }


def _type_match(row, activity_type: str) -> bool:
    activity_type = _clean(activity_type).lower()
    return not activity_type or activity_type == "all" or row.get("activity_type") == activity_type


def _finalize_row(*, source, source_id, activity_type, type_label, farmer_name="", farmer_contact="", farmer_id="",
                  product_name="", quantity=0, unit_code="", value=0, earning=0, status="completed",
                  reference="", created_at=None, earning_source="", is_business_basis=True):
    dt = to_india_datetime(created_at)
    return {
        "source": source,
        "source_id": _clean(source_id),
        "activity_type": activity_type,
        "type_label": type_label,
        "farmer_name": _clean(farmer_name) or "-",
        "farmer_contact": _clean(farmer_contact),
        "farmer_id": _clean(farmer_id),
        "product_name": _clean(product_name) or "Product",
        "quantity": _num(quantity),
        "quantity_display": _qty(quantity),
        "unit_code": _clean(unit_code) or "Unit",
        "value": round(_num(value), 2),
        "value_display": _money(value),
        "earning": round(_num(earning), 2),
        "earning_display": _money(earning),
        "has_earning": abs(_num(earning)) > 0.0001,
        "earning_source": _clean(earning_source),
        "is_business_basis": bool(is_business_basis),
        "status_label": _status(status),
        "reference": _clean(reference),
        "created_at": created_at,
        "display_datetime": dt.strftime("%d %b %Y, %I:%M %p") if dt else "-",
        "display_date": dt.strftime("%d %b %Y") if dt else "-",
        "sort_ts": dt.timestamp() if dt else 0,
    }


def _pos_rows(mitra_uid: str) -> list[dict]:
    rows = []
    query = {"mitra_uid": mitra_uid, "status": "completed"}
    for sale in mongo.db.pos_sales.find(query).sort("created_at", -1):
        buyer = sale.get("buyer") or {}
        if _clean(sale.get("buyer_type") or buyer.get("type")).lower() != "registered_farmer" and _clean(sale.get("sale_type")).lower() != "registered":
            continue
        farmer_name = buyer.get("name") or sale.get("farmer_name") or "Farmer / Customer"
        farmer_contact = buyer.get("phone") or sale.get("farmer_phone") or ""
        farmer_id = buyer.get("farmer_master_id_str") or buyer.get("farmer_user_id_str") or sale.get("farmer_id") or ""
        items = [x for x in (sale.get("items") or []) if isinstance(x, dict)]
        if items:
            for item in items:
                # Bonus is stored line-wise by the current POS service. Never
                # reconstruct a percentage here; backend-stored business rules
                # remain authoritative.
                line_value = item.get("line_total") if item.get("line_total") is not None else item.get("total_amount")
                earning = item.get("bonus_amount") or 0
                source_type = _clean(item.get("source_type")).lower()
                activity_type = "input_sale" if source_type in {"", "input"} else "produce_sale"
                type_label = "Input Sale" if activity_type == "input_sale" else "Produce Sale"
                rows.append(_finalize_row(
                    source="pos_sales", source_id=sale.get("_id"), activity_type=activity_type,
                    type_label=type_label, farmer_name=farmer_name, farmer_contact=farmer_contact,
                    farmer_id=farmer_id, product_name=item.get("product_name"), quantity=item.get("quantity"),
                    unit_code=item.get("unit_code"), value=line_value, earning=earning,
                    status=sale.get("status") or "completed", reference=sale.get("sale_number"),
                    created_at=sale.get("created_at"), earning_source="Stored POS commission" if _num(earning) else "",
                ))
        else:
            rows.append(_finalize_row(
                source="pos_sales", source_id=sale.get("_id"), activity_type="input_sale",
                type_label="Input Sale", farmer_name=farmer_name, farmer_contact=farmer_contact,
                farmer_id=farmer_id, product_name=sale.get("product_name") or "POS Sale",
                quantity=sale.get("quantity"), unit_code=sale.get("unit_code"),
                value=sale.get("grand_total") if sale.get("grand_total") is not None else sale.get("total_amount"),
                earning=sale.get("bonus_amount") or 0, status=sale.get("status") or "completed",
                reference=sale.get("sale_number"), created_at=sale.get("created_at"),
                earning_source="Stored POS commission" if _num(sale.get("bonus_amount")) else "",
            ))
    return rows


def _purchase_rows(mitra_uid: str) -> list[dict]:
    rows = []
    seen = set()
    for purchase in mongo.db.mitra_product_purchases.find({"mitra_uid": mitra_uid}).sort("created_at", -1):
        key = (
            _clean(purchase.get("product_name")).lower(),
            round(_num(purchase.get("quantity")), 4),
            round(_num(purchase.get("total_amount")), 2),
            (_row_date(purchase, "created_at").strftime("%Y-%m-%d %H:%M:%S") if _row_date(purchase, "created_at") else ""),
        )
        seen.add(key)
        rows.append(_finalize_row(
            source="mitra_product_purchases", source_id=purchase.get("_id"), activity_type="produce_purchase",
            type_label="Produce Purchase", farmer_name=purchase.get("seller_farmer_name") or "Farmer",
            farmer_contact=purchase.get("seller_farmer_contact"), farmer_id=purchase.get("seller_farmer_id"),
            product_name=purchase.get("product_name"), quantity=purchase.get("quantity"), unit_code=purchase.get("unit_code"),
            value=purchase.get("total_amount"), earning=purchase.get("bonus_amount") or 0,
            status=purchase.get("status") or "purchased", reference=purchase.get("purchase_number") or "",
            created_at=purchase.get("created_at"), earning_source="Stored produce commission" if _num(purchase.get("bonus_amount")) else "",
        ))

    # Historical code also wrote farmer_product_sales for a Mitra produce
    # purchase. Include it only when there is no matching purchase record, so
    # legacy history remains visible without double-counting current records.
    for sale in mongo.db.farmer_product_sales.find({"mitra_uid": mitra_uid}).sort("created_at", -1):
        dt = _row_date(sale, "created_at")
        key = (
            _clean(sale.get("product_name")).lower(),
            round(_num(sale.get("quantity")), 4),
            round(_num(sale.get("total_amount")), 2),
            (dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""),
        )
        if key in seen:
            continue
        rows.append(_finalize_row(
            source="farmer_product_sales", source_id=sale.get("_id"), activity_type="produce_purchase",
            type_label="Produce Purchase", farmer_name=sale.get("farmer_name") or sale.get("seller_farmer_name") or "Farmer",
            farmer_contact=sale.get("farmer_contact") or sale.get("seller_farmer_contact"),
            farmer_id=sale.get("farmer_user_id") or sale.get("seller_farmer_user_id"),
            product_name=sale.get("product_name"), quantity=sale.get("quantity"), unit_code=sale.get("unit_code"),
            value=sale.get("total_amount"), earning=sale.get("bonus_amount") or 0,
            status=sale.get("status") or "completed", reference=sale.get("sale_number") or "",
            created_at=sale.get("created_at"), earning_source="Stored produce commission" if _num(sale.get("bonus_amount")) else "",
        ))
    return rows



def _farmer_order_sale_rows(mitra_uid: str) -> list[dict]:
    """Mapped-Farmer UFC marketplace/order sales carrying stored commission."""
    rows = []
    query = {
        "mitra_uid": mitra_uid,
        "status": {"$nin": ["cancelled", "voided"]},
        "bonus_snapshot_version": {"$exists": True},
        "bonus_financial_sync_status": "complete",
    }
    for sale in mongo.db.ufc_farmer_sales.find(query).sort("created_at", -1):
        items = [x for x in (sale.get("items") or []) if isinstance(x, dict)]
        if items:
            for item in items:
                if not item.get("bonus_snapshot_version"):
                    continue
                rows.append(_finalize_row(
                    source="ufc_farmer_sales", source_id=sale.get("_id"), activity_type="input_sale",
                    type_label="Farmer UFC Order", farmer_name=sale.get("farmer_name") or "Farmer",
                    farmer_contact=sale.get("farmer_contact"), farmer_id=sale.get("farmer_user_id_str") or sale.get("farmer_user_id"),
                    product_name=item.get("product_name"), quantity=item.get("quantity") or item.get("accepted_quantity"),
                    unit_code=item.get("unit_code"), value=item.get("bonus_basis_amount") if item.get("bonus_basis_amount") is not None else item.get("line_total"),
                    earning=item.get("bonus_amount") or 0, status=sale.get("status") or "received",
                    reference=sale.get("order_number") or sale.get("sale_number") or "", created_at=sale.get("created_at"),
                    earning_source="Stored Farmer-order commission" if _num(item.get("bonus_amount")) else "",
                ))
        elif sale.get("bonus_snapshot_version"):
            rows.append(_finalize_row(
                source="ufc_farmer_sales", source_id=sale.get("_id"), activity_type="input_sale",
                type_label="Farmer UFC Order", farmer_name=sale.get("farmer_name") or "Farmer",
                farmer_contact=sale.get("farmer_contact"), farmer_id=sale.get("farmer_user_id_str") or sale.get("farmer_user_id"),
                product_name=sale.get("product_name") or "Input Sale", quantity=sale.get("quantity"), unit_code=sale.get("unit_code"),
                value=sale.get("bonus_base_total") if sale.get("bonus_base_total") is not None else sale.get("grand_total"),
                earning=sale.get("bonus_amount") or 0, status=sale.get("status") or "received",
                reference=sale.get("order_number") or sale.get("sale_number") or "", created_at=sale.get("created_at"),
                earning_source="Stored Farmer-order commission" if _num(sale.get("bonus_amount")) else "",
            ))
    return rows

def _sale_rows(mitra_uid: str) -> list[dict]:
    rows = []
    for sale in mongo.db.mitra_product_sales.find({"mitra_uid": mitra_uid}).sort("created_at", -1):
        buyer_type = _clean(sale.get("buyer_type")).replace("_", " ").title()
        rows.append(_finalize_row(
            source="mitra_product_sales", source_id=sale.get("_id"), activity_type="produce_sale",
            type_label="Produce Sale", farmer_name=sale.get("buyer_farmer_name") or buyer_type or "Buyer",
            farmer_contact=sale.get("buyer_farmer_contact"), farmer_id=sale.get("buyer_farmer_id"),
            product_name=sale.get("product_name"), quantity=sale.get("quantity"), unit_code=sale.get("unit_code"),
            value=sale.get("total_amount"), earning=sale.get("bonus_amount") or 0,
            status=sale.get("status") or "sold", reference=sale.get("sale_number") or "",
            created_at=sale.get("created_at"), earning_source="Stored sale commission" if _num(sale.get("bonus_amount")) else "",
            is_business_basis=False,
        ))
    return rows


def _mapped_farmers(mitra_uid: str) -> list[dict]:
    return list(mongo.db.farmer_master.find({"mitra_uid": mitra_uid}).sort("name", 1))


def _served_farmer_count(rows, farmers) -> int:
    """Count mapped Farmers represented in rows without guessing ownership."""
    represented = set()
    for farmer in farmers or []:
        fid = _clean(farmer.get("_id"))
        linked = _clean(farmer.get("linked_user_id"))
        contact = _clean(farmer.get("contact_no") or farmer.get("phone"))
        name = _clean(farmer.get("name") or farmer.get("farmer_name")).casefold()
        for row in rows or []:
            row_ids = {_clean(row.get("farmer_id")), _clean(row.get("farmer_user_id"))}
            row_contact = _clean(row.get("farmer_contact"))
            row_name = _clean(row.get("farmer_name")).casefold()
            if (fid and fid in row_ids) or (linked and linked in row_ids) or (contact and contact == row_contact) or (name and name == row_name):
                represented.add(fid or linked or contact or name)
                break
    return len(represented)


def get_mitra_transactions(mitra_uid, *, q="", period="all", activity_type="all", farmer="", page=1, per_page=25):
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

    start, end = _period_bounds(period)
    rows = _pos_rows(mitra_uid) + _farmer_order_sale_rows(mitra_uid) + _purchase_rows(mitra_uid) + _sale_rows(mitra_uid)
    rows = [r for r in rows if _in_period(to_india_datetime(r.get("created_at")), start, end)]
    rows = [r for r in rows if _type_match(r, activity_type) and _farmer_match(r, farmer) and _search_match(r, q)]
    rows.sort(key=lambda r: (r.get("sort_ts") or 0, r.get("source_id") or ""), reverse=True)

    total = len(rows)
    total_pages = max((total + per_page - 1) // per_page, 1)
    page = min(page, total_pages)
    start_index = (page - 1) * per_page
    page_rows = rows[start_index:start_index + per_page]

    farmers = _mapped_farmers(mitra_uid)
    farmer_options = []
    for f in farmers:
        value = _clean(f.get("contact_no") or f.get("_id"))
        farmer_options.append({
            "value": value,
            "label": _clean(f.get("name") or f.get("farmer_name") or f.get("contact_no") or "Farmer"),
            "contact": _clean(f.get("contact_no")),
        })

    return {
        "rows": page_rows,
        "q": _clean(q),
        "selected_period": _clean(period).lower() or "all",
        "selected_type": _clean(activity_type).lower() or "all",
        "selected_farmer": _clean(farmer),
        "periods": [
            ("all", "All Time"), ("today", "Today"), ("7d", "Last 7 Days"),
            ("30d", "Last 30 Days"), ("this_month", "This Month"),
        ],
        "types": [
            ("all", "All Types"), ("input_sale", "Input Sale"),
            ("produce_purchase", "Produce Purchase"), ("produce_sale", "Produce Sale"),
        ],
        "farmer_options": farmer_options,
        "pagination": {
            "page": page, "per_page": per_page, "total": total, "total_pages": total_pages,
            "has_prev": page > 1, "has_next": page < total_pages,
        },
        "summary": {
            "transactions": total,
            "business_value": round(sum(_num(r.get("value")) for r in rows if r.get("is_business_basis") is not False), 2),
            "earnings": round(sum(_num(r.get("earning")) for r in rows), 2),
            "farmers_served": _served_farmer_count(rows, farmers),
        },
    }


def get_mitra_dashboard_data(mitra_uid):
    """Simple Mitra dashboard read-model from current operational sources.

    Compatibility keys used by the mobile API are preserved, but old 2%-from-
    generic-transactions calculations are not used. Earnings are only amounts
    already stored by the active business flows.
    """
    mitra_uid = _clean(mitra_uid)
    if not mitra_uid:
        raise ValueError("Mitra UID is unavailable for this account.")

    master = mongo.db.ufc_mitra_master.find_one({"mitra_uid": mitra_uid}) or {}
    centre_uid = _clean(master.get("mapped_centre_uid") or master.get("centre_uid"))
    farmers = _mapped_farmers(mitra_uid)
    pending_validations = mongo.db.validations.count_documents({
        "approver_role": "ufc_mitra",
        "metadata.mapped_mitra_uid": mitra_uid,
        "status": "pending",
    })

    all_rows = _pos_rows(mitra_uid) + _farmer_order_sale_rows(mitra_uid) + _purchase_rows(mitra_uid) + _sale_rows(mitra_uid)
    all_rows.sort(key=lambda r: (r.get("sort_ts") or 0, r.get("source_id") or ""), reverse=True)
    month_start = business_today().replace(day=1)
    month_rows = [r for r in all_rows if _in_period(to_india_datetime(r.get("created_at")), month_start, business_today())]

    input_earnings = sum(_num(r.get("earning")) for r in month_rows if r.get("activity_type") == "input_sale")
    produce_earnings = sum(_num(r.get("earning")) for r in month_rows if r.get("activity_type") in {"produce_purchase", "produce_sale"})
    business_value = sum(_num(r.get("value")) for r in month_rows if r.get("is_business_basis") is not False)
    earnings = input_earnings + produce_earnings
    current_commission = resolve_input_commission(mitra_uid, "all")

    # Legacy mobile response expects monthly_sales as grouped month/year rows.
    grouped = {}
    for row in all_rows:
        dt = to_india_datetime(row.get("created_at"))
        if not dt:
            continue
        key = (dt.year, dt.month)
        bucket = grouped.setdefault(key, {"_id": {"year": dt.year, "month": dt.month}, "total_sales": 0.0, "total_orders": 0})
        if row.get("is_business_basis") is not False:
            bucket["total_sales"] += _num(row.get("value"))
            bucket["total_orders"] += 1
    monthly_sales = []
    for key in sorted(grouped.keys(), reverse=True)[:12]:
        bucket = grouped[key]
        bucket["total_sales"] = round(bucket["total_sales"], 2)
        monthly_sales.append(bucket)

    approved_farmer_count = sum(1 for f in farmers if _clean(f.get("approval_status") or "approved").lower() == "approved")
    pending_farmer_count = len(farmers) - approved_farmer_count

    return {
        "centre_uid": centre_uid,
        "farmer_count": len(farmers),
        "approved_farmer_count": approved_farmer_count,
        "pending_farmer_count": pending_farmer_count,
        "farmers": farmers[:20],
        "transactions": all_rows[:10],
        "recent_activity": all_rows[:8],
        "monthly_sales": monthly_sales,
        "monthly_sales_total": round(business_value, 2),
        "monthly_purchase_total": round(sum(_num(r.get("value")) for r in month_rows if r.get("activity_type") == "produce_purchase"), 2),
        "business_this_month": round(business_value, 2),
        "my_earnings": round(earnings, 2),
        "current_input_commission_rate": float(current_commission.get("percentage") or 0),
        "current_input_commission_source": current_commission.get("source_label") or "",
        "input_bonus": round(input_earnings, 2),
        "output_bonus": round(produce_earnings, 2),
        "needs_action": int(pending_validations),
        "pending_validations": int(pending_validations),
        # Kept for existing app/web compatibility. Farmer order review is a UFC
        # Centre responsibility; it is not added to Mitra's Needs Action count.
        "centre_farmer_orders_pending": mongo.db.ufc_farmer_orders.count_documents({"centre_uid": centre_uid, "status": "requested"}) if centre_uid else 0,
    }
