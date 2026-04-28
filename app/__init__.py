import os
from flask import Flask, app, session, request, redirect, url_for

from app.extensions import mongo
from app.utils.db import init_indexes
from app.utils.security import ensure_upload_folder

from app.blueprints.auth.routes import auth_bp
from app.blueprints.dashboard.routes import dashboard_bp
from app.blueprints.admin.routes import admin_bp
from app.blueprints.validations.routes import validation_bp
from app.blueprints.master_data.routes import master_bp
from app.blueprints.modules.routes import modules_bp
from app.blueprints.documents.routes import documents_bp


def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "unnatfarm-dev-secret")
    app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017/")
    app.config["MONGO_DB_NAME"] = os.getenv("MONGO_DB_NAME", "unnatfarm_mis")
    app.config["UPLOAD_FOLDER"] = os.getenv(
        "UPLOAD_FOLDER",
        os.path.join(app.root_path, "uploads"),
    )
    app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
    app.config["ALLOWED_EXTENSIONS"] = {
    "pdf",
    "png", "jpg", "jpeg", "webp",
    "doc", "docx",
    "mp4", "mov", "avi", "mkv", "webm"
}

    mongo.init_app(app)
    ensure_upload_folder(app)

    with app.app_context():
        init_indexes(mongo.db)

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(validation_bp)
    app.register_blueprint(master_bp)
    app.register_blueprint(modules_bp)
    app.register_blueprint(documents_bp)

    @app.before_request
    def enforce_profile_and_validation_gate():
        endpoint = request.endpoint or ""

        if not session.get("user_id"):
            return None

        if endpoint.startswith("static") or endpoint in {
            "auth.logout",
            "dashboard.pending_access",
        }:
            return None

        role = session.get("role")
        approval = session.get("approval_status")

        if role == "ufc_admin":
            if approval == "pending_profile":
                if endpoint != "auth.complete_ufc_admin":
                    return redirect(url_for("auth.complete_ufc_admin"))

            elif approval == "rejected":
                allowed = {
                    "auth.complete_ufc_admin",
                    "dashboard.pending_access",
                    "auth.logout",
                }

                if endpoint not in allowed:
                    return redirect(url_for("dashboard.pending_access"))

            elif approval != "approved":
                return redirect(url_for("dashboard.pending_access"))

        if role == "ufc_mitra":
            if approval == "pending_profile":
                if endpoint != "auth.complete_ufc_mitra":
                    return redirect(url_for("auth.complete_ufc_mitra"))

            elif approval == "rejected":
                allowed = {
                    "auth.complete_ufc_mitra",
                    "dashboard.pending_access",
                    "auth.logout",
                }

                if endpoint not in allowed:
                    return redirect(url_for("dashboard.pending_access"))

            elif approval != "approved":
                return redirect(url_for("dashboard.pending_access"))

        if role == "farmer":
            if approval != "approved" and endpoint != "dashboard.pending_access":
                return redirect(url_for("dashboard.pending_access"))

        return None

    return app