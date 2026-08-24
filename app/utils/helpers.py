from bson import ObjectId

from app.utils.timezone import utc_now


def now_utc():
    return utc_now()


def to_obj_id(value):
    return ObjectId(value)


def safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default
