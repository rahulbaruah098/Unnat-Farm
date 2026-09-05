import hashlib
import json
from datetime import timedelta
from functools import wraps

from flask import current_app, g, jsonify, request
from pymongo.errors import DuplicateKeyError

from app.extensions import mongo
from app.utils.api_errors import ApiError, ApiValidationError
from app.utils.helpers import now_utc


def _fingerprint_request():
    raw_body = request.get_data(cache=True) or b""
    material = b"|".join([
        request.method.encode("utf-8"),
        request.path.encode("utf-8"),
        request.query_string or b"",
        raw_body,
    ])
    return hashlib.sha256(material).hexdigest()


def _idempotency_key():
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        raise ApiValidationError(
            "Idempotency-Key header is required for this operation.",
            code="IDEMPOTENCY_KEY_REQUIRED",
        )
    if len(key) > 128:
        raise ApiValidationError(
            "Idempotency-Key must be 128 characters or fewer.",
            code="INVALID_IDEMPOTENCY_KEY",
        )
    return key


def idempotent_write(view):
    """Protect a future mobile write endpoint from duplicate network retries.

    Apply this after mobile_auth_required/mobile_roles_required. Stored replay
    bodies are only for endpoints explicitly decorated with this helper; auth
    token endpoints intentionally do not use it so raw tokens are not persisted
    inside idempotency records.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        key = _idempotency_key()
        user = getattr(g, "api_user", None) or {}
        user_id = str(user.get("_id") or "anonymous")
        scope_key = f"{user_id}:{request.method}:{request.endpoint}:{key}"
        fingerprint = _fingerprint_request()
        now = now_utc()
        ttl_seconds = max(int(current_app.config.get("API_IDEMPOTENCY_TTL_SECONDS", 86400)), 300)

        record = {
            "scope_key": scope_key,
            "key": key,
            "fingerprint": fingerprint,
            "user_id": None if user_id == "anonymous" else user_id,
            "method": request.method,
            "endpoint": request.endpoint,
            "status": "processing",
            "created_at": now,
            "updated_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }

        try:
            insert_result = mongo.db.api_idempotency_keys.insert_one(record)
            record_id = insert_result.inserted_id
        except DuplicateKeyError:
            existing = mongo.db.api_idempotency_keys.find_one({"scope_key": scope_key}) or {}
            if existing.get("fingerprint") != fingerprint:
                raise ApiError(
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "This Idempotency-Key was already used for a different request.",
                    409,
                )
            if existing.get("status") == "completed" and isinstance(existing.get("response_json"), dict):
                response = jsonify(existing["response_json"])
                response.status_code = int(existing.get("status_code") or 200)
                response.headers["Idempotency-Replayed"] = "true"
                return response
            raise ApiError(
                "IDEMPOTENCY_REQUEST_IN_PROGRESS",
                "A request with this Idempotency-Key is already being processed.",
                409,
            )

        try:
            response = current_app.make_response(view(*args, **kwargs))
        except Exception:
            mongo.db.api_idempotency_keys.delete_one({"_id": record_id, "status": "processing"})
            raise

        response_json = response.get_json(silent=True)
        if response.status_code < 500 and isinstance(response_json, dict):
            mongo.db.api_idempotency_keys.update_one(
                {"_id": record_id},
                {"$set": {
                    "status": "completed",
                    "status_code": int(response.status_code),
                    "response_json": json.loads(json.dumps(response_json)),
                    "updated_at": now_utc(),
                }},
            )
        else:
            mongo.db.api_idempotency_keys.delete_one({"_id": record_id})
        return response
    return wrapped
