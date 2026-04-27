from datetime import datetime
from bson import ObjectId


def now_utc():
    return datetime.utcnow()


def to_obj_id(value):
    return ObjectId(value)


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default
