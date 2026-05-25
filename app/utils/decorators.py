from functools import wraps
from bson import ObjectId
from flask import session, redirect, url_for, flash, abort, request, jsonify
from app.extensions import mongo


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" in session:
            return view(*args, **kwargs)

        wants_json = (
            request.headers.get("Accept") == "application/json"
            or request.is_json
            or request.args.get("format") == "json"
        )

        if wants_json:
            data = request.get_json(silent=True) or {}

            user_id = (
                request.args.get("user_id")
                or request.form.get("user_id")
                or data.get("user_id")
                or ""
            ).strip()

            if not user_id:
                return jsonify({
                    "ok": False,
                    "message": "Login required."
                }), 401

            try:
                user = mongo.db.users.find_one({"_id": ObjectId(user_id)})
            except Exception:
                user = None

            if not user:
                return jsonify({
                    "ok": False,
                    "message": "Invalid user."
                }), 401

            session["user_id"] = str(user["_id"])
            session["role"] = user.get("role")
            session["name"] = user.get("name") or user.get("full_name") or user.get("username")
            session["username"] = user.get("username")
            session["centre_uid"] = (
                user.get("centre_uid")
                or user.get("mapped_centre_uid")
                or user.get("center_uid")
                or user.get("mapped_center_uid")
            )
            session["mitra_uid"] = (
                user.get("mitra_uid")
                or user.get("mapped_mitra_uid")
            )
            session["approval_status"] = user.get("approval_status")

            return view(*args, **kwargs)

        flash("Please login first.", "warning")
        return redirect(url_for("auth.login"))

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
