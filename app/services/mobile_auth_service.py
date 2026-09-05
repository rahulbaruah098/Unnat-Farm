import hashlib
import secrets
from datetime import timedelta

from bson import ObjectId
from flask import current_app

from app.extensions import mongo
from app.services.user_service import get_user_for_login, update_last_login
from app.utils.api_errors import ApiAuthenticationError, ApiPermissionError
from app.utils.helpers import now_utc
from app.utils.security import verify_password


MOBILE_ROLES = {"ufc_admin", "ufc_mitra", "farmer"}
ACCESS_PREFIX = "ufa_"
REFRESH_PREFIX = "ufr_"


def _hash_token(raw_token):
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _new_raw_token(token_type):
    prefix = ACCESS_PREFIX if token_type == "access" else REFRESH_PREFIX
    return prefix + secrets.token_urlsafe(48)


def _token_ttl_seconds(token_type):
    key = "MOBILE_ACCESS_TOKEN_TTL_SECONDS" if token_type == "access" else "MOBILE_REFRESH_TOKEN_TTL_SECONDS"
    default = 900 if token_type == "access" else 30 * 24 * 60 * 60
    return max(int(current_app.config.get(key, default)), 60)


def _clean_device_metadata(metadata=None):
    metadata = metadata or {}
    return {
        "device_id": str(metadata.get("device_id") or "")[:128],
        "device_name": str(metadata.get("device_name") or "")[:128],
        "platform": str(metadata.get("platform") or "")[:64],
        "app_version": str(metadata.get("app_version") or "")[:64],
        "user_agent": str(metadata.get("user_agent") or "")[:500],
    }


def _insert_token(user_id, token_type, family_id, metadata=None):
    raw_token = _new_raw_token(token_type)
    now = now_utc()
    ttl_seconds = _token_ttl_seconds(token_type)
    document = {
        "token_hash": _hash_token(raw_token),
        "token_type": token_type,
        "user_id": str(user_id),
        "family_id": family_id,
        "issued_at": now,
        "expires_at": now + timedelta(seconds=ttl_seconds),
        "revoked_at": None,
        "revoked_reason": None,
        "last_used_at": None,
        "device": _clean_device_metadata(metadata),
    }
    mongo.db.mobile_auth_tokens.insert_one(document)
    return raw_token, document, ttl_seconds


def issue_token_pair(user, *, family_id=None, device_metadata=None):
    user_id = str(user["_id"])
    family_id = family_id or secrets.token_hex(16)
    access_token, access_doc, access_ttl = _insert_token(
        user_id, "access", family_id, device_metadata
    )
    refresh_token, refresh_doc, refresh_ttl = _insert_token(
        user_id, "refresh", family_id, device_metadata
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": access_ttl,
        "refresh_expires_in": refresh_ttl,
        "family_id": family_id,
        "access_expires_at": access_doc["expires_at"],
        "refresh_expires_at": refresh_doc["expires_at"],
    }


def _lookup_token(raw_token, expected_type=None, *, allow_expired=False, allow_revoked=False):
    raw_token = str(raw_token or "").strip()
    if not raw_token:
        raise ApiAuthenticationError("Authentication token is missing.", "TOKEN_MISSING")

    if expected_type == "access" and not raw_token.startswith(ACCESS_PREFIX):
        raise ApiAuthenticationError("Invalid access token.", "INVALID_ACCESS_TOKEN")
    if expected_type == "refresh" and not raw_token.startswith(REFRESH_PREFIX):
        raise ApiAuthenticationError("Invalid refresh token.", "INVALID_REFRESH_TOKEN")

    token_doc = mongo.db.mobile_auth_tokens.find_one({"token_hash": _hash_token(raw_token)})
    if not token_doc:
        raise ApiAuthenticationError("Invalid or expired token.", "INVALID_TOKEN")

    if expected_type and token_doc.get("token_type") != expected_type:
        raise ApiAuthenticationError("Invalid token type.", "INVALID_TOKEN_TYPE")

    if not allow_revoked and token_doc.get("revoked_at"):
        raise ApiAuthenticationError("Token has been revoked.", "TOKEN_REVOKED")

    if not allow_expired and token_doc.get("expires_at") and token_doc["expires_at"] <= now_utc():
        raise ApiAuthenticationError("Token has expired.", "TOKEN_EXPIRED")

    return token_doc


def get_user_for_token(raw_token, expected_type="access"):
    token_doc = _lookup_token(raw_token, expected_type)
    try:
        user = mongo.db.users.find_one({"_id": ObjectId(token_doc["user_id"])})
    except Exception:
        user = None

    if not user:
        raise ApiAuthenticationError("User account no longer exists.", "USER_NOT_FOUND")
    if not user.get("active", True):
        raise ApiPermissionError("Account is inactive.", "ACCOUNT_INACTIVE")

    role = str(user.get("role") or "").strip().lower()
    if role not in MOBILE_ROLES:
        raise ApiPermissionError("This account cannot use the mobile application.", "MOBILE_ROLE_NOT_ALLOWED")

    # Avoid a Mongo write on every request. Touch at most once every five minutes.
    last_used = token_doc.get("last_used_at")
    if not last_used or (now_utc() - last_used).total_seconds() >= 300:
        mongo.db.mobile_auth_tokens.update_one(
            {"_id": token_doc["_id"]},
            {"$set": {"last_used_at": now_utc()}},
        )
    return user, token_doc


def authenticate_mobile_credentials(identifier, password):
    identifier = str(identifier or "").strip()
    password = str(password or "")
    user = get_user_for_login(identifier) if identifier else None

    if not user or not verify_password(password, user.get("password_hash", "")):
        raise ApiAuthenticationError("Invalid credentials.", "INVALID_CREDENTIALS")
    if not user.get("active", True):
        raise ApiPermissionError("Account is inactive.", "ACCOUNT_INACTIVE")

    role = str(user.get("role") or "").strip().lower()
    if role not in MOBILE_ROLES:
        raise ApiPermissionError("This account cannot use the mobile application.", "MOBILE_ROLE_NOT_ALLOWED")

    update_last_login(str(user["_id"]))
    return user


def rotate_refresh_token(raw_refresh_token, *, device_metadata=None):
    token_doc = _lookup_token(raw_refresh_token, "refresh")
    now = now_utc()

    result = mongo.db.mobile_auth_tokens.update_one(
        {"_id": token_doc["_id"], "revoked_at": None},
        {"$set": {"revoked_at": now, "revoked_reason": "rotated"}},
    )
    if result.modified_count != 1:
        raise ApiAuthenticationError("Refresh token has already been used.", "REFRESH_TOKEN_REUSED")

    try:
        user = mongo.db.users.find_one({"_id": ObjectId(token_doc["user_id"])})
    except Exception:
        user = None
    if not user:
        raise ApiAuthenticationError("User account no longer exists.", "USER_NOT_FOUND")
    if not user.get("active", True):
        revoke_token_family(token_doc.get("family_id"), reason="account_inactive")
        raise ApiPermissionError("Account is inactive.", "ACCOUNT_INACTIVE")

    role = str(user.get("role") or "").strip().lower()
    if role not in MOBILE_ROLES:
        revoke_token_family(token_doc.get("family_id"), reason="role_not_allowed")
        raise ApiPermissionError("This account cannot use the mobile application.", "MOBILE_ROLE_NOT_ALLOWED")

    return user, issue_token_pair(
        user,
        family_id=token_doc.get("family_id") or secrets.token_hex(16),
        device_metadata=device_metadata,
    )


def revoke_token_family(family_id, *, reason="logout"):
    if not family_id:
        return 0
    result = mongo.db.mobile_auth_tokens.update_many(
        {"family_id": family_id, "revoked_at": None},
        {"$set": {"revoked_at": now_utc(), "revoked_reason": reason}},
    )
    return int(result.modified_count or 0)


def revoke_by_raw_token(raw_token, *, expected_type=None, reason="logout"):
    token_doc = _lookup_token(
        raw_token,
        expected_type,
        allow_expired=True,
        allow_revoked=True,
    )
    return revoke_token_family(token_doc.get("family_id"), reason=reason), token_doc


def resolve_identity_scope(user):
    role = str(user.get("role") or "").strip().lower()
    centre_uid = (
        user.get("centre_uid")
        or user.get("mapped_centre_uid")
        or user.get("center_uid")
        or user.get("mapped_center_uid")
    )
    mitra_uid = user.get("mitra_uid") or user.get("mapped_mitra_uid")

    return {
        "user_id": str(user.get("_id")),
        "role": role,
        "centre_uid": centre_uid,
        "mitra_uid": mitra_uid,
        "farmer_user_id": str(user.get("_id")) if role == "farmer" else None,
    }


def _latest_rejection_reason(user_id):
    latest = mongo.db.validations.find_one(
        {"entity_id": str(user_id)},
        sort=[("updated_at", -1), ("created_at", -1)],
    ) or {}
    return (
        latest.get("rejection_reason")
        or latest.get("action_remarks")
        or latest.get("remarks")
        or ""
    )


def serialize_mobile_user(user):
    role = str(user.get("role") or "").strip().lower()
    approval_status = str(user.get("approval_status") or "pending").strip().lower()
    scope = resolve_identity_scope(user)

    return {
        "id": str(user.get("_id")),
        "user_ref_id": user.get("user_ref_id"),
        "name": user.get("name") or user.get("full_name") or user.get("username") or "",
        "username": user.get("username"),
        "phone": user.get("phone"),
        "role": role,
        "active": bool(user.get("active", True)),
        "approval": {
            "status": approval_status,
            "is_approved": approval_status == "approved",
            "requires_profile_completion": approval_status == "pending_profile",
            "is_rejected": approval_status == "rejected",
            "rejection_reason": _latest_rejection_reason(user.get("_id")) if approval_status == "rejected" else "",
        },
        "scope": scope,
        "location": {
            "state": user.get("state") or "",
            "district": user.get("district") or "",
            "block": user.get("block") or "",
            "village": user.get("village") or "",
        },
    }
