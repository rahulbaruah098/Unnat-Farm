from datetime import timedelta

from flask import current_app, g, request

from app.extensions import mongo
from app.services.audit_service import log_action
from app.utils.helpers import now_utc


def _safe_ip_address():
    forwarded = str(request.headers.get("X-Forwarded-For") or "").strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return str(request.remote_addr or "")[:64]


def log_api_action(action, *, user_id=None, entity_type="mobile_api", entity_id=None, remarks=None, metadata=None):
    try:
        log_action(
            actor_user_id=str(user_id) if user_id else None,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            remarks=remarks,
            metadata={
                "request_id": getattr(g, "request_id", None),
                **(metadata or {}),
            },
        )
    except Exception:
        current_app.logger.exception("Failed to write API audit action")


def log_api_request(status_code, duration_ms):
    if not current_app.config.get("API_REQUEST_LOGGING_ENABLED", True):
        return

    user = getattr(g, "api_user", None) or {}
    now = now_utc()
    retention_days = int(current_app.config.get("API_REQUEST_LOG_RETENTION_DAYS", 30))

    doc = {
        "request_id": getattr(g, "request_id", None),
        "method": request.method,
        "path": request.path,
        "endpoint": request.endpoint,
        "status_code": int(status_code),
        "duration_ms": round(float(duration_ms), 2),
        "actor_user_id": str(user.get("_id")) if user.get("_id") else None,
        "actor_role": user.get("role"),
        "ip_address": _safe_ip_address(),
        "user_agent": str(request.user_agent or "")[:500],
        "has_idempotency_key": bool(request.headers.get("Idempotency-Key")),
        "created_at": now,
        "expires_at": now + timedelta(days=max(retention_days, 1)),
    }
    try:
        mongo.db.api_request_logs.insert_one(doc)
    except Exception:
        current_app.logger.exception("Failed to write API request log")
