from functools import wraps

from flask import g, request

from app.services.mobile_auth_service import get_user_for_token, resolve_identity_scope
from app.utils.api_errors import ApiAuthenticationError, ApiPermissionError


def get_bearer_token(required=True):
    authorization = str(request.headers.get("Authorization") or "").strip()
    if not authorization:
        if required:
            raise ApiAuthenticationError("Bearer access token is required.", "ACCESS_TOKEN_REQUIRED")
        return None

    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise ApiAuthenticationError(
            "Authorization header must use Bearer authentication.",
            "INVALID_AUTHORIZATION_HEADER",
        )
    return token.strip()


def mobile_auth_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        token = get_bearer_token(required=True)
        user, token_doc = get_user_for_token(token, "access")
        g.api_user = user
        g.api_token = token_doc
        g.api_identity = resolve_identity_scope(user)
        return view(*args, **kwargs)
    return wrapped


def mobile_roles_required(*allowed_roles):
    normalized = {str(role).strip().lower() for role in allowed_roles}

    def decorator(view):
        @wraps(view)
        @mobile_auth_required
        def wrapped(*args, **kwargs):
            role = str((getattr(g, "api_identity", {}) or {}).get("role") or "").lower()
            if role not in normalized:
                raise ApiPermissionError(
                    "You do not have permission to access this mobile resource.",
                    "ROLE_NOT_ALLOWED",
                )
            return view(*args, **kwargs)
        return wrapped
    return decorator


def require_scope_ownership(*, centre_uid=None, mitra_uid=None, farmer_user_id=None):
    identity = getattr(g, "api_identity", None) or {}
    if not identity:
        raise ApiAuthenticationError()

    checks = (
        ("centre_uid", centre_uid),
        ("mitra_uid", mitra_uid),
        ("farmer_user_id", farmer_user_id),
    )
    for field, requested in checks:
        if requested is None:
            continue
        authoritative = identity.get(field)
        if str(authoritative or "") != str(requested or ""):
            raise ApiPermissionError(
                "The requested resource is outside your authorized scope.",
                "OWNERSHIP_MISMATCH",
            )
    return identity


def ownership_required(scope_loader):
    """Future endpoint helper.

    scope_loader receives the route args/kwargs and must return a dict containing
    any of centre_uid, mitra_uid or farmer_user_id. The comparison is always
    made against the authenticated token identity, never a client identity claim.
    """
    def decorator(view):
        @wraps(view)
        @mobile_auth_required
        def wrapped(*args, **kwargs):
            requested_scope = scope_loader(*args, **kwargs) or {}
            require_scope_ownership(**requested_scope)
            return view(*args, **kwargs)
        return wrapped
    return decorator
