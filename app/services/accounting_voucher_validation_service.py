from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256
import json

from bson import ObjectId
from bson.decimal128 import Decimal128

from app.extensions import mongo
from app.services.accounting_ledger_service import (
    ACCOUNT_GROUP_COLLECTION,
    LEDGER_COLLECTION,
    get_ledger_for_posting,
)


MONEY_QUANTUM = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_LINE_AMOUNT = Decimal("999999999999.99")

VALIDATION_STATUS_NOT_VALIDATED = "not_validated"
VALIDATION_STATUS_VALID = "valid"
VALIDATION_STATUS_INVALID = "invalid"


# ---------------------------------------------------------------------------
# Decimal, text and identifier helpers
# ---------------------------------------------------------------------------


def _to_object_id(value):
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _decimal_value(value):
    if isinstance(value, Decimal128):
        return value.to_decimal()
    if isinstance(value, Decimal):
        return value
    if value in (None, ""):
        return ZERO

    text = str(value).strip().replace(",", "")
    if not text:
        return ZERO
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Enter a valid financial amount.") from exc


def _money(value):
    parsed = _decimal_value(value)
    rounded = parsed.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    if parsed != rounded:
        raise ValueError("Financial amounts cannot contain more than two decimal places.")
    return rounded


def money_decimal128(value):
    return Decimal128(_money(value))


def money_string(value):
    return format(_money(value), ".2f")


def _clean_multiline(value, label, maximum=500):
    lines = [" ".join(line.strip().split()) for line in str(value or "").splitlines()]
    cleaned = "\n".join(line for line in lines if line).strip()
    if len(cleaned) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters.")
    return cleaned


def _line_amounts(raw_payload):
    try:
        debit = _money(raw_payload.get("debit_amount"))
    except ValueError as exc:
        raise ValueError(f"Debit amount: {exc}") from exc
    try:
        credit = _money(raw_payload.get("credit_amount"))
    except ValueError as exc:
        raise ValueError(f"Credit amount: {exc}") from exc

    if debit < ZERO or credit < ZERO:
        raise ValueError("Debit and credit amounts cannot be negative.")
    if debit > MAX_LINE_AMOUNT or credit > MAX_LINE_AMOUNT:
        raise ValueError("A voucher line amount exceeds the supported maximum.")
    if debit > ZERO and credit > ZERO:
        raise ValueError("A voucher line cannot contain both a debit and a credit amount.")
    if debit == ZERO and credit == ZERO:
        raise ValueError("Enter a positive debit or credit amount for the voucher line.")
    return debit, credit


# ---------------------------------------------------------------------------
# Ledger snapshots and draft-line construction
# ---------------------------------------------------------------------------


def _account_group_name(account_group_id):
    group_object_id = _to_object_id(account_group_id)
    if not group_object_id:
        return ""
    group = mongo.db[ACCOUNT_GROUP_COLLECTION].find_one(
        {"_id": group_object_id},
        {"name": 1},
    )
    return (group or {}).get("name") or ""


def ledger_snapshot(ledger):
    return {
        "ledger_id": ledger["_id"],
        "ledger_id_str": str(ledger["_id"]),
        "ledger_code": ledger.get("ledger_code") or "",
        "ledger_name": ledger.get("name") or "",
        "ledger_system_key": ledger.get("system_key") or "",
        "ledger_type": ledger.get("ledger_type") or "",
        "normal_balance": ledger.get("normal_balance") or "",
        "posting_policy": ledger.get("posting_policy") or "",
        "account_group_id": ledger.get("account_group_id"),
        "account_group_id_str": str(ledger.get("account_group_id") or ""),
        "account_group_system_key": ledger.get("account_group_system_key") or "",
        "account_group_name": _account_group_name(ledger.get("account_group_id")),
        "ledger_version_snapshot": int(ledger.get("version") or 1),
    }


def _line_fingerprint(line):
    source = {
        "line_id": line.get("line_id") or "",
        "ledger_id": str(line.get("ledger_id") or ""),
        "debit_amount": money_string(line.get("debit_amount")),
        "credit_amount": money_string(line.get("credit_amount")),
        "line_narration": line.get("line_narration") or "",
    }
    encoded = json.dumps(source, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def build_draft_line(
    accounting_entity_id,
    raw_payload,
    *,
    line_id,
    sequence,
    existing_line=None,
):
    entity_object_id = _to_object_id(accounting_entity_id)
    if not entity_object_id:
        raise ValueError("Invalid Accounting entity for voucher line.")

    ledger = get_ledger_for_posting(entity_object_id, raw_payload.get("ledger_id"))
    debit, credit = _line_amounts(raw_payload)
    narration = _clean_multiline(
        raw_payload.get("line_narration"),
        "Line narration",
        maximum=500,
    )

    line = {
        "line_id": str(line_id),
        "sequence": int(sequence),
        **ledger_snapshot(ledger),
        "debit_amount": Decimal128(debit),
        "credit_amount": Decimal128(credit),
        "entry_side": "debit" if debit > ZERO else "credit",
        "amount": Decimal128(debit if debit > ZERO else credit),
        "line_narration": narration,
    }
    if existing_line:
        line["created_at"] = existing_line.get("created_at")
        line["created_by"] = existing_line.get("created_by")
        line["created_by_str"] = existing_line.get("created_by_str") or ""
        line["created_by_name"] = existing_line.get("created_by_name") or ""
    line["line_fingerprint"] = _line_fingerprint(line)
    return line


def normalize_line_sequences(lines):
    normalized = []
    for index, line in enumerate(lines, start=1):
        item = dict(line)
        item["sequence"] = index
        item["line_fingerprint"] = _line_fingerprint(item)
        normalized.append(item)
    return normalized


def calculate_draft_totals(lines):
    debit_total = ZERO
    credit_total = ZERO
    for line in lines or []:
        debit_total += _money(line.get("debit_amount"))
        credit_total += _money(line.get("credit_amount"))
    debit_total = debit_total.quantize(MONEY_QUANTUM)
    credit_total = credit_total.quantize(MONEY_QUANTUM)
    difference = (debit_total - credit_total).quantize(MONEY_QUANTUM)
    return {
        "debit_total": debit_total,
        "credit_total": credit_total,
        "difference": difference,
        "absolute_difference": abs(difference).quantize(MONEY_QUANTUM),
        "is_balanced": debit_total > ZERO and debit_total == credit_total,
    }


def draft_lines_fingerprint(lines):
    payload = [
        {
            "sequence": int(line.get("sequence") or 0),
            "line_fingerprint": line.get("line_fingerprint") or _line_fingerprint(line),
        }
        for line in lines or []
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Validation engine
# ---------------------------------------------------------------------------


def _validation_error(code, message, line=None):
    result = {"code": code, "message": message}
    if line:
        result["line_id"] = line.get("line_id") or ""
        result["line_number"] = int(line.get("sequence") or 0)
    return result


def validate_draft_lines(accounting_entity_id, lines):
    entity_object_id = _to_object_id(accounting_entity_id)
    if not entity_object_id:
        raise ValueError("Invalid Accounting entity for voucher validation.")

    lines = normalize_line_sequences(lines or [])
    errors = []
    warnings = []
    seen_line_ids = set()
    live_snapshots = []

    if len(lines) < 2:
        errors.append(
            _validation_error(
                "minimum_two_lines",
                "A voucher requires at least two debit/credit lines.",
            )
        )

    for line in lines:
        line_id = str(line.get("line_id") or "").strip()
        if not line_id:
            errors.append(_validation_error("missing_line_id", "A voucher line has no internal line ID.", line))
        elif line_id in seen_line_ids:
            errors.append(_validation_error("duplicate_line_id", "Duplicate voucher line ID detected.", line))
        else:
            seen_line_ids.add(line_id)

        try:
            debit = _money(line.get("debit_amount"))
            credit = _money(line.get("credit_amount"))
        except ValueError as exc:
            errors.append(_validation_error("invalid_amount", str(exc), line))
            continue

        if debit < ZERO or credit < ZERO:
            errors.append(_validation_error("negative_amount", "Debit and credit amounts cannot be negative.", line))
        if debit > ZERO and credit > ZERO:
            errors.append(_validation_error("both_sides_used", "A line cannot contain both debit and credit.", line))
        if debit == ZERO and credit == ZERO:
            errors.append(_validation_error("zero_line", "A line must contain a positive debit or credit amount.", line))
        if debit > MAX_LINE_AMOUNT or credit > MAX_LINE_AMOUNT:
            errors.append(_validation_error("amount_too_large", "The line amount exceeds the supported maximum.", line))

        try:
            ledger = get_ledger_for_posting(entity_object_id, line.get("ledger_id"))
        except (ValueError, RuntimeError) as exc:
            errors.append(_validation_error("ledger_not_postable", str(exc), line))
            continue

        live_snapshot = ledger_snapshot(ledger)
        live_snapshot.update(
            {
                "line_id": line_id,
                "sequence": int(line.get("sequence") or 0),
                "debit_amount": Decimal128(debit),
                "credit_amount": Decimal128(credit),
                "line_narration": line.get("line_narration") or "",
            }
        )
        live_snapshots.append(live_snapshot)

        if str(line.get("accounting_entity_id") or entity_object_id) != str(entity_object_id):
            errors.append(_validation_error("wrong_entity", "A voucher line belongs to another Accounting entity.", line))

        if (
            line.get("ledger_code") != live_snapshot.get("ledger_code")
            or line.get("ledger_name") != live_snapshot.get("ledger_name")
            or int(line.get("ledger_version_snapshot") or 0)
            != int(live_snapshot.get("ledger_version_snapshot") or 0)
        ):
            warnings.append(
                _validation_error(
                    "ledger_snapshot_refreshed",
                    "The live ledger details changed after this line was saved. Current ledger details were used for validation.",
                    line,
                )
            )

    totals = calculate_draft_totals(lines)
    if totals["debit_total"] == ZERO and totals["credit_total"] == ZERO:
        errors.append(_validation_error("zero_voucher", "Voucher totals must be greater than zero."))
    if totals["debit_total"] != totals["credit_total"]:
        errors.append(
            _validation_error(
                "unbalanced_voucher",
                "Total Debit must equal Total Credit before posting can be allowed.",
            )
        )

    is_valid = not errors
    return {
        "is_valid": is_valid,
        "status": VALIDATION_STATUS_VALID if is_valid else VALIDATION_STATUS_INVALID,
        "line_count": len(lines),
        "debit_total": Decimal128(totals["debit_total"]),
        "credit_total": Decimal128(totals["credit_total"]),
        "difference": Decimal128(totals["difference"]),
        "absolute_difference": Decimal128(totals["absolute_difference"]),
        "is_balanced": totals["is_balanced"],
        "errors": errors,
        "warnings": warnings,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "draft_lines_fingerprint": draft_lines_fingerprint(lines),
        "live_ledger_snapshots": live_snapshots,
    }


# ---------------------------------------------------------------------------
# Read-model helpers
# ---------------------------------------------------------------------------


def list_active_ledger_options(accounting_entity_id):
    entity_object_id = _to_object_id(accounting_entity_id)
    if not entity_object_id:
        return []

    ledgers = list(
        mongo.db[LEDGER_COLLECTION]
        .find(
            {
                "accounting_entity_id": entity_object_id,
                "is_deleted": False,
                "status": "active",
                "is_active": True,
            }
        )
        .sort([("name", 1), ("ledger_code", 1)])
    )
    group_ids = [ledger.get("account_group_id") for ledger in ledgers if ledger.get("account_group_id")]
    group_names = {
        group["_id"]: group.get("name") or ""
        for group in mongo.db[ACCOUNT_GROUP_COLLECTION].find(
            {"_id": {"$in": group_ids}},
            {"name": 1},
        )
    } if group_ids else {}

    return [
        {
            "id": str(ledger["_id"]),
            "ledger_code": ledger.get("ledger_code") or "",
            "name": ledger.get("name") or "",
            "ledger_type": ledger.get("ledger_type") or "",
            "normal_balance": ledger.get("normal_balance") or "",
            "posting_policy": ledger.get("posting_policy") or "",
            "account_group_name": group_names.get(ledger.get("account_group_id"), ""),
            "display_name": " · ".join(
                value
                for value in [
                    ledger.get("name") or "",
                    ledger.get("ledger_code") or "",
                    group_names.get(ledger.get("account_group_id"), ""),
                ]
                if value
            ),
        }
        for ledger in ledgers
    ]


def serialize_draft_line(line):
    return {
        "line_id": line.get("line_id") or "",
        "sequence": int(line.get("sequence") or 0),
        "ledger_id": str(line.get("ledger_id") or ""),
        "ledger_code": line.get("ledger_code") or "",
        "ledger_name": line.get("ledger_name") or "",
        "ledger_type": line.get("ledger_type") or "",
        "normal_balance": line.get("normal_balance") or "",
        "posting_policy": line.get("posting_policy") or "",
        "account_group_id": str(line.get("account_group_id") or ""),
        "account_group_system_key": line.get("account_group_system_key") or "",
        "account_group_name": line.get("account_group_name") or "",
        "debit_amount": money_string(line.get("debit_amount")),
        "credit_amount": money_string(line.get("credit_amount")),
        "entry_side": line.get("entry_side") or ("debit" if _money(line.get("debit_amount")) > ZERO else "credit"),
        "amount": money_string(line.get("amount") or max(_money(line.get("debit_amount")), _money(line.get("credit_amount")))),
        "line_narration": line.get("line_narration") or "",
        "created_by_name": line.get("created_by_name") or "",
        "updated_by_name": line.get("updated_by_name") or "",
        "created_at": line.get("created_at"),
        "updated_at": line.get("updated_at"),
    }


def serialize_validation_result(result):
    if not result:
        return None
    return {
        "is_valid": result.get("is_valid") is True,
        "status": result.get("status") or VALIDATION_STATUS_NOT_VALIDATED,
        "line_count": int(result.get("line_count") or 0),
        "debit_total": money_string(result.get("debit_total")),
        "credit_total": money_string(result.get("credit_total")),
        "difference": money_string(result.get("difference")),
        "absolute_difference": money_string(result.get("absolute_difference")),
        "is_balanced": result.get("is_balanced") is True,
        "errors": result.get("errors") or [],
        "warnings": result.get("warnings") or [],
        "error_count": int(result.get("error_count") or 0),
        "warning_count": int(result.get("warning_count") or 0),
        "draft_lines_fingerprint": result.get("draft_lines_fingerprint") or "",
        "validated_by_name": result.get("validated_by_name") or "",
        "validated_at": result.get("validated_at"),
        "voucher_version": int(result.get("voucher_version") or 0),
    }
