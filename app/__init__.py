from bson import ObjectId
import os
from flask import Flask, app, session, request, redirect, url_for

from app.extensions import mongo
from app.utils.db import init_indexes
from app.utils.security import ensure_upload_folder
from app.utils.timezone import (
    APP_TIMEZONE_NAME,
    business_today,
    format_ist_date,
    format_ist_datetime,
)

from app.blueprints.auth.routes import auth_bp
from app.blueprints.dashboard.routes import dashboard_bp
from app.blueprints.admin.routes import admin_bp
from app.blueprints.validations.routes import validation_bp
from app.blueprints.master_data.routes import master_bp
from app.blueprints.modules.routes import modules_bp
from app.blueprints.documents.routes import documents_bp
from app.blueprints.accounting.routes import accounting_bp
from app.blueprints.reports.routes import reports_bp

from flask_cors import CORS


def _env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def create_app():
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    CORS(app, resources={r"/api/*": {"origins": "*"}})

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

    # AVPL workflow feature controls. Stages 1-10 are part of the current
    # baseline, so completed modules default to enabled. Environment variables
    # can still explicitly disable a module for emergency rollback.
    app.config.update(
        AVPL_PRODUCT_MASTER_V2_ENABLED=_env_bool("AVPL_PRODUCT_MASTER_V2_ENABLED", True),
        AVPL_PURCHASE_WORKFLOW_ENABLED=_env_bool("AVPL_PURCHASE_WORKFLOW_ENABLED", True),
        AVPL_INVENTORY_LEDGER_ENABLED=_env_bool("AVPL_INVENTORY_LEDGER_ENABLED", True),
        AVPL_LISTING_WORKFLOW_ENABLED=_env_bool("AVPL_LISTING_WORKFLOW_ENABLED", True),
        AVPL_SALES_ACCOUNTING_ENABLED=_env_bool("AVPL_SALES_ACCOUNTING_ENABLED", True),
        AVPL_REPORTS_ENABLED=_env_bool("AVPL_REPORTS_ENABLED", True),
        LEGACY_PRODUCT_RESTOCK_ENABLED=_env_bool("LEGACY_PRODUCT_RESTOCK_ENABLED", True),
        LEGACY_DIRECT_AVPL_ORDER_ENABLED=_env_bool("LEGACY_DIRECT_AVPL_ORDER_ENABLED", True),
        # Refined operating mode: routine records can be completed in one step.
        # High-risk reversals/financial-year controls remain explicit.
        STREAMLINED_WORKFLOWS_ENABLED=_env_bool("STREAMLINED_WORKFLOWS_ENABLED", True),
    )

    mongo.init_app(app)
    ensure_upload_folder(app)

    # One timezone policy for the entire MIS: UTC in storage, IST in UI/business dates.
    app.config["APP_TIMEZONE"] = os.getenv("APP_TIMEZONE", APP_TIMEZONE_NAME)
    app.jinja_env.filters["ist_datetime"] = format_ist_datetime
    app.jinja_env.filters["ist_date"] = format_ist_date

    with app.app_context():
        init_indexes(mongo.db)

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(validation_bp)
    app.register_blueprint(master_bp)
    app.register_blueprint(modules_bp)
    app.register_blueprint(documents_bp)
    app.register_blueprint(accounting_bp)
    app.register_blueprint(reports_bp)

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
            allowed_farmer_endpoints = {
                "dashboard.pending_access",
                "auth.complete_farmer",
                "auth.logout",
                "auth.api_states",
                "auth.api_districts",
                "auth.api_blocks",
                "auth.api_villages",
                "auth.api_centre",
                "auth.api_mitra",
                "documents.serve",
            }

            if approval != "approved" and endpoint not in allowed_farmer_endpoints:
                return redirect(url_for("dashboard.pending_access"))

        return None

    @app.context_processor
    def inject_current_user_name():
        display_name = (
            session.get("name")
            or session.get("username")
            or session.get("user_name")
            or ""
        )

        if not display_name and session.get("user_id"):
            try:
                user = mongo.db.users.find_one({"_id": ObjectId(session["user_id"])}) or {}
                display_name = (
                    user.get("name")
                    or user.get("username")
                    or user.get("phone")
                    or ""
                )
            except Exception:
                display_name = ""

        if not display_name:
            display_name = (
                session.get("role", "User")
                .replace("_", " ")
                .title()
            )

        from app.services.workflow_policy_service import streamlined_workflows_enabled
        return {
            "current_display_name": display_name,
            "streamlined_workflows_enabled": streamlined_workflows_enabled(),
            "app_timezone": app.config.get("APP_TIMEZONE", APP_TIMEZONE_NAME),
            "business_today_iso": business_today().isoformat(),
        }

    return app
