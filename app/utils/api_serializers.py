from datetime import date, datetime
from decimal import Decimal

from bson import ObjectId


def serialize_value(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat() + ("Z" if value.tzinfo is None else "")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [serialize_value(item) for item in value]
    return value


def serialize_document(document, *, exclude=None):
    if document is None:
        return None
    excluded = set(exclude or [])
    return {
        str(key): serialize_value(value)
        for key, value in dict(document).items()
        if key not in excluded
    }
