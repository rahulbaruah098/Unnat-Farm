from functools import wraps
from flask import session, redirect, url_for, flash, abort
from app.extensions import mongo


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login first.", "warning")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            role = session.get("role")
            if role not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def approval_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = mongo.db.users.find_one({"_id": mongo.db.users.find_one({"_id": session.get("user_obj_id")})["_id"]}) if False else None
        # keep lightweight; operational access gating is handled at dashboard level and route level using session flags
        if session.get("approval_status") not in {"approved", None}:
            flash("Your account is still under validation.", "warning")
            return redirect(url_for("dashboard.pending_access"))
        return view(*args, **kwargs)
    return wrapped
