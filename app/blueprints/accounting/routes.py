from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.services.accounting_configuration_service import (
    approve_accounting_policy,
    approve_entity_profile,
    ensure_accounting_configuration_indexes,
    get_configuration_option_catalog,
    get_configuration_overview,
    return_accounting_policy,
    return_entity_profile,
    save_accounting_policy_draft,
    save_entity_profile_draft,
    submit_accounting_policy,
    submit_entity_profile,
    withdraw_accounting_policy,
    withdraw_entity_profile,
)
from app.services.accounting_entity_service import (
    bootstrap_avpl_entity,
    get_avpl_entity,
    list_accessible_entities,
    serialize_accounting_entity,
)
from app.services.accounting_financial_year_service import (
    approve_financial_year,
    create_financial_year,
    ensure_financial_year_indexes,
    get_default_financial_year_values,
    list_financial_years,
    return_financial_year,
    submit_financial_year,
    update_financial_year,
    withdraw_financial_year,
)
from app.services.accounting_permission_service import (
    get_accounting_access,
    has_accounting_permission,
)
from app.services.accounting_user_access_service import (
    initialize_default_user_access_mappings,
    list_user_access_mappings,
    update_user_access_mapping,
)
from app.utils.decorators import (
    accounting_permission_required,
    login_required,
    roles_required,
)


accounting_bp = Blueprint(
    "accounting",
    __name__,
    url_prefix="/accounting",
)


def _redirect_dashboard(anchor="financial-years"):
    suffix = f"#{anchor}" if anchor else ""
    return redirect(url_for("accounting.dashboard") + suffix)


def _run_financial_year_action(action, success_message):
    try:
        action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(success_message, "success")

    return _redirect_dashboard()


def _run_configuration_action(action, anchor):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "Accounting configuration updated.", "success")

    return _redirect_dashboard(anchor)


def _configuration_capabilities(access):
    return {
        "entity_profile": {
            "can_view": has_accounting_permission(
                access, "accounting.entity_settings.view"
            ),
            "can_create": has_accounting_permission(
                access, "accounting.entity_settings.create"
            ),
            "can_edit": has_accounting_permission(
                access, "accounting.entity_settings.edit"
            ),
            "can_submit": has_accounting_permission(
                access, "accounting.entity_settings.submit"
            ),
            "can_withdraw": has_accounting_permission(
                access, "accounting.entity_settings.withdraw"
            ),
            "can_approve": has_accounting_permission(
                access, "accounting.entity_settings.approve"
            ),
            "can_return": has_accounting_permission(
                access, "accounting.entity_settings.return"
            ),
        },
        "accounting_policy": {
            "can_view": has_accounting_permission(
                access, "accounting.settings.view"
            ),
            "can_create": has_accounting_permission(
                access, "accounting.settings.create"
            ),
            "can_edit": has_accounting_permission(
                access, "accounting.settings.edit"
            ),
            "can_submit": has_accounting_permission(
                access, "accounting.settings.submit"
            ),
            "can_withdraw": has_accounting_permission(
                access, "accounting.settings.withdraw"
            ),
            "can_approve": has_accounting_permission(
                access, "accounting.settings.approve"
            ),
            "can_return": has_accounting_permission(
                access, "accounting.settings.return"
            ),
        },
    }


@accounting_bp.route("/")
@login_required
@roles_required("super_admin", "avpl_admin", "accounts")
@accounting_permission_required("accounting.dashboard.view")
def dashboard():
    access = get_accounting_access(
        user_id=session["user_id"],
        session_role=session.get("role"),
    )

    accessible_entities = list_accessible_entities(access.get("entity_ids"))
    accessible_entity_ids = {item.get("id") for item in accessible_entities}

    avpl_entity_candidate = get_avpl_entity(include_inactive=True)
    entity_access_missing = bool(
        avpl_entity_candidate
        and str(avpl_entity_candidate.get("_id")) not in accessible_entity_ids
    )
    avpl_entity_document = None if entity_access_missing else avpl_entity_candidate
    avpl_entity = serialize_accounting_entity(avpl_entity_document)

    financial_year_capabilities = {
        "can_view": has_accounting_permission(access, "accounting.financial_year.view"),
        "can_create": has_accounting_permission(access, "accounting.financial_year.create"),
        "can_edit": has_accounting_permission(access, "accounting.financial_year.edit"),
        "can_submit": has_accounting_permission(access, "accounting.financial_year.submit"),
        "can_withdraw": has_accounting_permission(access, "accounting.financial_year.withdraw"),
        "can_approve": has_accounting_permission(access, "accounting.financial_year.approve"),
        "can_return": has_accounting_permission(access, "accounting.financial_year.return"),
        "can_use": has_accounting_permission(access, "accounting.financial_year.use"),
    }

    financial_years = []
    financial_year_setup_error = ""

    if avpl_entity_document and financial_year_capabilities["can_view"]:
        try:
            ensure_financial_year_indexes()
            financial_years = list_financial_years(avpl_entity_document["_id"])
        except RuntimeError as exc:
            financial_year_setup_error = str(exc)

    current_user_id = str(session.get("user_id") or "")
    open_financial_years = [
        item for item in financial_years if item.get("usable_for_posting")
    ]
    pending_financial_years = [
        item for item in financial_years if item.get("status") == "pending_approval"
    ]

    configuration_capabilities = _configuration_capabilities(access)
    configuration_overview = {
        "entity_profile": {"active": None, "working": None, "form": {}},
        "accounting_policy": {"active": None, "working": None, "form": {}},
    }
    configuration_setup_error = ""

    if avpl_entity_document and (
        configuration_capabilities["entity_profile"]["can_view"]
        or configuration_capabilities["accounting_policy"]["can_view"]
    ):
        try:
            ensure_accounting_configuration_indexes()
            configuration_overview = get_configuration_overview(
                avpl_entity_document["_id"],
                open_financial_years=open_financial_years,
            )
        except (ValueError, RuntimeError) as exc:
            configuration_setup_error = str(exc)

    user_access_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.user_access.view"
        ),
        "can_initialize": (
            session.get("role") == "super_admin"
            and has_accounting_permission(
                access, "accounting.user_access.bootstrap"
            )
        ),
        "can_manage_accounts": has_accounting_permission(
            access, "accounting.user_access.manage_accounts"
        ),
    }
    user_access_summary = {
        "rows": [],
        "eligible_count": 0,
        "explicit_count": 0,
        "fallback_count": 0,
        "enabled_count": 0,
        "schema_outdated_count": 0,
        "permission_schema_version": 1,
        "permission_catalog_by_role": {},
    }
    user_access_setup_error = ""

    if user_access_capabilities["can_view"]:
        try:
            user_access_summary = list_user_access_mappings(
                session["user_id"]
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            user_access_setup_error = str(exc)

    return render_template(
        "accounting/dashboard.html",
        accounting_access=access,
        accessible_entities=accessible_entities,
        accounting_entity=avpl_entity,
        financial_years=financial_years,
        financial_year_defaults=get_default_financial_year_values(),
        financial_year_capabilities=financial_year_capabilities,
        financial_year_setup_error=financial_year_setup_error,
        entity_access_missing=entity_access_missing,
        open_financial_years=open_financial_years,
        pending_financial_years=pending_financial_years,
        current_user_id=current_user_id,
        configuration_capabilities=configuration_capabilities,
        configuration_overview=configuration_overview,
        configuration_options=get_configuration_option_catalog(),
        configuration_setup_error=configuration_setup_error,
        user_access_capabilities=user_access_capabilities,
        user_access_summary=user_access_summary,
        user_access_setup_error=user_access_setup_error,
    )


@accounting_bp.route("/setup/avpl-entity", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.entity.bootstrap")
def setup_avpl_entity():
    try:
        result = bootstrap_avpl_entity(session["user_id"])
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(
            result["message"],
            "success" if result["created"] else "info",
        )

    return redirect(url_for("accounting.dashboard"))


@accounting_bp.route("/financial-years/create", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.create")
def financial_year_create():
    entity = get_avpl_entity()

    if not entity:
        flash("Initialize the AVPL Accounting entity before creating a Financial Year.", "danger")
        return _redirect_dashboard()

    return _run_financial_year_action(
        lambda: create_financial_year(
            entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            start_date=request.form.get("start_date"),
            end_date=request.form.get("end_date"),
        ),
        "Financial Year draft created successfully.",
    )


@accounting_bp.route("/financial-years/<financial_year_id>/edit", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.edit")
def financial_year_edit(financial_year_id):
    return _run_financial_year_action(
        lambda: update_financial_year(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            start_date=request.form.get("start_date"),
            end_date=request.form.get("end_date"),
            expected_version=request.form.get("version"),
            correction_note=request.form.get("correction_note"),
        ),
        "Financial Year draft updated successfully.",
    )


@accounting_bp.route("/financial-years/<financial_year_id>/submit", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.submit")
def financial_year_submit(financial_year_id):
    return _run_financial_year_action(
        lambda: submit_financial_year(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            submission_note=request.form.get("submission_note"),
        ),
        "Financial Year submitted to Super Admin for approval.",
    )


@accounting_bp.route("/financial-years/<financial_year_id>/withdraw", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.withdraw")
def financial_year_withdraw(financial_year_id):
    return _run_financial_year_action(
        lambda: withdraw_financial_year(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "Financial Year withdrawn to draft for correction.",
    )


@accounting_bp.route("/financial-years/<financial_year_id>/approve", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.financial_year.approve")
def financial_year_approve(financial_year_id):
    return _run_financial_year_action(
        lambda: approve_financial_year(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        ),
        "Financial Year approved and opened successfully.",
    )


@accounting_bp.route("/financial-years/<financial_year_id>/return", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.financial_year.return")
def financial_year_return(financial_year_id):
    return _run_financial_year_action(
        lambda: return_financial_year(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "Financial Year sent back to AVPL Admin for correction.",
    )


# ---------------------------------------------------------------------------
# Stage 2 Batch 5: AVPL entity profile configuration
# ---------------------------------------------------------------------------

@accounting_bp.route("/entity-settings/save", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.entity_settings.view")
def entity_settings_save():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("entity-configuration")

    return _run_configuration_action(
        lambda: save_entity_profile_draft(
            entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        ),
        "entity-configuration",
    )


@accounting_bp.route("/entity-settings/<configuration_id>/submit", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.entity_settings.submit")
def entity_settings_submit(configuration_id):
    return _run_configuration_action(
        lambda: submit_entity_profile(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            note=request.form.get("submission_note"),
        ),
        "entity-configuration",
    )


@accounting_bp.route("/entity-settings/<configuration_id>/withdraw", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.entity_settings.withdraw")
def entity_settings_withdraw(configuration_id):
    return _run_configuration_action(
        lambda: withdraw_entity_profile(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "entity-configuration",
    )


@accounting_bp.route("/entity-settings/<configuration_id>/approve", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.entity_settings.approve")
def entity_settings_approve(configuration_id):
    return _run_configuration_action(
        lambda: approve_entity_profile(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        ),
        "entity-configuration",
    )


@accounting_bp.route("/entity-settings/<configuration_id>/return", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.entity_settings.return")
def entity_settings_return(configuration_id):
    return _run_configuration_action(
        lambda: return_entity_profile(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "entity-configuration",
    )


# ---------------------------------------------------------------------------
# Stage 2 Batch 5: AVPL Accounting policy settings
# ---------------------------------------------------------------------------

@accounting_bp.route("/settings/save", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.settings.view")
def accounting_settings_save():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("accounting-settings")

    return _run_configuration_action(
        lambda: save_accounting_policy_draft(
            entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        ),
        "accounting-settings",
    )


@accounting_bp.route("/settings/<configuration_id>/submit", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.settings.submit")
def accounting_settings_submit(configuration_id):
    return _run_configuration_action(
        lambda: submit_accounting_policy(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            note=request.form.get("submission_note"),
        ),
        "accounting-settings",
    )


@accounting_bp.route("/settings/<configuration_id>/withdraw", methods=["POST"])
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.settings.withdraw")
def accounting_settings_withdraw(configuration_id):
    return _run_configuration_action(
        lambda: withdraw_accounting_policy(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "accounting-settings",
    )


@accounting_bp.route("/settings/<configuration_id>/approve", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.settings.approve")
def accounting_settings_approve(configuration_id):
    return _run_configuration_action(
        lambda: approve_accounting_policy(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        ),
        "accounting-settings",
    )


@accounting_bp.route("/settings/<configuration_id>/return", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.settings.return")
def accounting_settings_return(configuration_id):
    return _run_configuration_action(
        lambda: return_accounting_policy(
            configuration_id=configuration_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        ),
        "accounting-settings",
    )


@accounting_bp.route("/user-access/initialize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.user_access.bootstrap")
def user_access_initialize():
    try:
        result = initialize_default_user_access_mappings(
            session["user_id"]
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result["message"], "success")

    return _redirect_dashboard("user-access")


@accounting_bp.route(
    "/user-access/users/<target_user_id>/save",
    methods=["POST"],
)
@login_required
@roles_required("super_admin", "avpl_admin")
@accounting_permission_required("accounting.user_access.manage_accounts")
def user_access_save(target_user_id):
    try:
        result = update_user_access_mapping(
            actor_user_id=session["user_id"],
            target_user_id=target_user_id,
            accounting_enabled=(
                request.form.get("accounting_enabled") == "1"
            ),
            entity_ids=request.form.getlist("entity_ids"),
            permissions=request.form.getlist("permissions"),
            expected_version=request.form.get("version"),
            change_note=request.form.get("change_note"),
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result["message"], "success")

    return _redirect_dashboard("user-access")
