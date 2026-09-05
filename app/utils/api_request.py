import re
import uuid
from flask import request

from app.utils.api_errors import ApiValidationError


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def resolve_request_id():
    supplied = str(request.headers.get("X-Request-ID") or "").strip()
    if supplied and _REQUEST_ID_PATTERN.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


def require_json_object():
    if not request.is_json:
        raise ApiValidationError(
            "Content-Type must be application/json.",
            code="JSON_REQUIRED",
        )
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiValidationError(
            "Request body must be a JSON object.",
            code="INVALID_JSON_BODY",
        )
    return data


def require_fields(data, *field_names):
    missing = []
    for field in field_names:
        value = data.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
    if missing:
        raise ApiValidationError(
            "Required fields are missing.",
            details={"fields": missing},
            code="MISSING_REQUIRED_FIELDS",
        )
    return data
