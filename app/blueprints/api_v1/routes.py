from time import perf_counter

from flask import Blueprint, current_app, g, request
from werkzeug.exceptions import HTTPException

from app.extensions import mongo
from app.services.api_audit_service import log_api_action, log_api_request
from app.services.mobile_auth_service import (
    authenticate_mobile_credentials,
    issue_token_pair,
    revoke_by_raw_token,
    revoke_token_family,
    rotate_refresh_token,
    serialize_mobile_user,
)
from app.utils.api_auth import get_bearer_token, mobile_auth_required
from app.utils.api_errors import ApiError
from app.utils.api_request import require_fields, require_json_object, resolve_request_id
from app.utils.api_response import api_error, api_success
from app.utils.api_serializers import serialize_value


api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _device_metadata(data=None):
    data = data or {}
    return {
        "device_id": request.headers.get("X-Device-ID") or data.get("device_id"),
        "device_name": request.headers.get("X-Device-Name") or data.get("device_name"),
        "platform": request.headers.get("X-Platform") or data.get("platform"),
        "app_version": request.headers.get("X-App-Version") or data.get("app_version"),
        "user_agent": str(request.user_agent or ""),
    }


@api_v1_bp.before_request
def api_before_request():
    g.request_id = resolve_request_id()
    g.api_started_at = perf_counter()


@api_v1_bp.after_request
def api_after_request(response):
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id

    started = getattr(g, "api_started_at", None)
    duration_ms = (perf_counter() - started) * 1000 if started is not None else 0
    log_api_request(response.status_code, duration_ms)
    return response


@api_v1_bp.errorhandler(ApiError)
def handle_api_error(error):
    return api_error(error.code, error.message, error.status, error.details)


@api_v1_bp.errorhandler(HTTPException)
def handle_http_error(error):
    code = str(error.name or "HTTP_ERROR").upper().replace(" ", "_")
    return api_error(code, error.description or error.name, error.code or 500)


@api_v1_bp.errorhandler(Exception)
def handle_unexpected_error(error):
    current_app.logger.exception(
        "Unhandled Mobile API error request_id=%s",
        getattr(g, "request_id", None),
    )
    return api_error(
        "INTERNAL_SERVER_ERROR",
        "The server could not complete this request.",
        500,
    )


@api_v1_bp.get("/health")
def health():
    database_ok = True
    database_message = "connected"
    try:
        mongo.db.command("ping")
    except Exception:
        database_ok = False
        database_message = "unavailable"

    status = 200 if database_ok else 503
    return api_success({
        "service": "unnatfarm-mobile-api",
        "version": "v1",
        "status": "ok" if database_ok else "degraded",
        "database": database_message,
    }, status=status)


@api_v1_bp.post("/auth/login")
def login():
    data = require_fields(require_json_object(), "identifier", "password")
    user = authenticate_mobile_credentials(data.get("identifier"), data.get("password"))
    g.api_user = user
    tokens = issue_token_pair(user, device_metadata=_device_metadata(data))

    log_api_action(
        "mobile_login",
        user_id=user.get("_id"),
        metadata={"role": user.get("role")},
    )

    return api_success({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "expires_in": tokens["expires_in"],
        "refresh_expires_in": tokens["refresh_expires_in"],
        "access_expires_at": serialize_value(tokens["access_expires_at"]),
        "refresh_expires_at": serialize_value(tokens["refresh_expires_at"]),
        "user": serialize_mobile_user(user),
    })


@api_v1_bp.post("/auth/refresh")
def refresh():
    data = require_fields(require_json_object(), "refresh_token")
    user, tokens = rotate_refresh_token(
        data.get("refresh_token"),
        device_metadata=_device_metadata(data),
    )

    g.api_user = user
    log_api_action(
        "mobile_token_refresh",
        user_id=user.get("_id"),
        metadata={"role": user.get("role")},
    )

    return api_success({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": tokens["token_type"],
        "expires_in": tokens["expires_in"],
        "refresh_expires_in": tokens["refresh_expires_in"],
        "access_expires_at": serialize_value(tokens["access_expires_at"]),
        "refresh_expires_at": serialize_value(tokens["refresh_expires_at"]),
    })


@api_v1_bp.post("/auth/logout")
def logout():
    data = request.get_json(silent=True) if request.is_json else {}
    data = data if isinstance(data, dict) else {}
    refresh_token = str(data.get("refresh_token") or "").strip()
    access_token = get_bearer_token(required=False)

    if not access_token and not refresh_token:
        raise ApiError(
            "TOKEN_REQUIRED",
            "Provide a Bearer access token or refresh_token to log out.",
            401,
        )

    revoked = 0
    user_id = None
    family_ids = set()

    if access_token:
        count, token_doc = revoke_by_raw_token(
            access_token,
            expected_type="access",
            reason="logout",
        )
        revoked += count
        user_id = token_doc.get("user_id") or user_id
        if token_doc.get("family_id"):
            family_ids.add(token_doc["family_id"])

    if refresh_token:
        count, token_doc = revoke_by_raw_token(
            refresh_token,
            expected_type="refresh",
            reason="logout",
        )
        if token_doc.get("family_id") not in family_ids:
            revoked += count
        user_id = token_doc.get("user_id") or user_id

    if user_id:
        g.api_user = {"_id": user_id}

    log_api_action(
        "mobile_logout",
        user_id=user_id,
        metadata={"revoked_tokens": revoked},
    )
    return api_success({"logged_out": True})


@api_v1_bp.get("/me")
@mobile_auth_required
def me():
    return api_success({"user": serialize_mobile_user(g.api_user)})
