from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.services.accounting_account_group_service import (
    ensure_account_group_indexes,
    get_account_group_overview,
    seed_protected_account_groups,
)
from app.services.accounting_ledger_service import (
    ensure_ledger_indexes,
    get_ledger_overview,
    seed_default_avpl_ledgers,
)
from app.services.accounting_party_ledger_service import (
    approve_party_ledger,
    cancel_party_ledger,
    create_party_ledger,
    deactivate_party_ledger,
    ensure_party_ledger_indexes,
    get_party_ledger_option_catalog,
    get_party_ledger_overview,
    reactivate_party_ledger,
    return_party_ledger,
    submit_party_ledger,
    update_party_ledger,
    withdraw_party_ledger,
)
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
from app.services.accounting_entity_mapping_service import (
    ensure_accounting_entity_mapping_indexes,
    get_future_accounting_entity_mapping_overview,
    synchronize_future_accounting_entity_mappings,
)
from app.services.accounting_financial_year_control_service import (
    approve_financial_year_control_request,
    cancel_financial_year_control_request,
    create_financial_year_control_request,
    ensure_financial_year_control_indexes,
    get_financial_year_control_overview,
    return_financial_year_control_request,
    submit_financial_year_control_request,
    update_financial_year_control_request,
    withdraw_financial_year_control_request,
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
from app.services.accounting_number_series_service import (
    approve_number_series,
    bulk_submit_number_series,
    ensure_number_series_indexes,
    get_number_series_overview,
    initialize_missing_number_series_drafts,
    return_number_series,
    save_number_series_draft,
    submit_number_series,
    withdraw_number_series,
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



def _run_number_series_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "Number series updated.", "success")

    return _redirect_dashboard("number-series")


def _run_account_group_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        category = "warning" if result.get("repaired") else "success"
        flash(
            result.get("message") or "Protected account groups synchronized.",
            category,
        )

    return _redirect_dashboard("account-groups")


def _run_ledger_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        category = "warning" if result.get("repaired") else "success"
        flash(
            result.get("message") or "Default AVPL ledgers synchronized.",
            category,
        )

    return _redirect_dashboard("ledger-masters")


def _run_party_ledger_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "Party ledger updated.", "success")

    return _redirect_dashboard("party-ledgers")


def _run_financial_year_control_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(
            result.get("message") or "Financial Year lifecycle request updated.",
            "success",
        )

    return _redirect_dashboard("financial-years")


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

    financial_year_control_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.financial_year.control.view"
        ),
        "can_create": has_accounting_permission(
            access, "accounting.financial_year.control.create"
        ),
        "can_edit": has_accounting_permission(
            access, "accounting.financial_year.control.edit"
        ),
        "can_submit": has_accounting_permission(
            access, "accounting.financial_year.control.submit"
        ),
        "can_withdraw": has_accounting_permission(
            access, "accounting.financial_year.control.withdraw"
        ),
        "can_cancel": has_accounting_permission(
            access, "accounting.financial_year.control.cancel"
        ),
        "can_approve": has_accounting_permission(
            access, "accounting.financial_year.control.approve"
        ),
        "can_return": has_accounting_permission(
            access, "accounting.financial_year.control.return"
        ),
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
    closed_financial_years = [
        item for item in financial_years if item.get("status") == "closed"
    ]
    locked_financial_years = [
        item for item in financial_years
        if item.get("status") == "locked" or item.get("is_locked")
    ]

    financial_year_control_overview = {
        "by_financial_year": {},
        "pending_count": 0,
        "returned_count": 0,
        "active_count": 0,
        "history_count": 0,
    }
    financial_year_control_setup_error = ""

    if (
        avpl_entity_document
        and financial_years
        and financial_year_control_capabilities["can_view"]
    ):
        try:
            ensure_financial_year_control_indexes()
            financial_year_control_overview = get_financial_year_control_overview(
                avpl_entity_document["_id"],
                financial_years,
            )
        except (ValueError, RuntimeError) as exc:
            financial_year_control_setup_error = str(exc)

    control_by_year = financial_year_control_overview.get(
        "by_financial_year", {}
    )
    for financial_year in financial_years:
        financial_year["control"] = control_by_year.get(
            financial_year.get("id"),
            {"active_request": None, "history": [], "allowed_types": []},
        )

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

    number_series_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.number_series.view"
        ),
        "can_create": has_accounting_permission(
            access, "accounting.number_series.create"
        ),
        "can_edit": has_accounting_permission(
            access, "accounting.number_series.edit"
        ),
        "can_submit": has_accounting_permission(
            access, "accounting.number_series.submit"
        ),
        "can_withdraw": has_accounting_permission(
            access, "accounting.number_series.withdraw"
        ),
        "can_approve": has_accounting_permission(
            access, "accounting.number_series.approve"
        ),
        "can_return": has_accounting_permission(
            access, "accounting.number_series.return"
        ),
    }
    number_series_overview = {
        "financial_years": [],
        "groups": [],
        "active_count": 0,
        "working_count": 0,
        "pending_count": 0,
        "returned_count": 0,
        "required_count": 0,
        "configured_scope_count": 0,
        "catalog": {},
    }
    number_series_setup_error = ""

    if avpl_entity_document and number_series_capabilities["can_view"]:
        try:
            ensure_number_series_indexes()
            number_series_overview = get_number_series_overview(
                avpl_entity_document["_id"],
                financial_years=open_financial_years,
            )
        except (ValueError, RuntimeError) as exc:
            number_series_setup_error = str(exc)

    entity_mapping_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.entity_mapping.view"
        ),
        "can_sync": (
            session.get("role") == "super_admin"
            and has_accounting_permission(
                access, "accounting.entity_mapping.sync"
            )
        ),
    }
    entity_mapping_overview = {
        "source_counts": {"centres": 0, "mitras": 0, "farmers": 0},
        "mapping_counts": {
            "centres": 0,
            "mitras": 0,
            "farmers": 0,
            "total": 0,
            "expected_total": 0,
        },
        "entity_counts": {"centres": 0, "farmers": 0, "active_non_avpl": 0},
        "status_counts": {},
        "unresolved_count": 0,
        "stale_count": 0,
        "centre_rows": [],
        "latest_run": None,
        "is_complete": False,
        "avpl_only_active": True,
    }
    entity_mapping_setup_error = ""

    if avpl_entity_document and entity_mapping_capabilities["can_view"]:
        try:
            ensure_accounting_entity_mapping_indexes()
            entity_mapping_overview = get_future_accounting_entity_mapping_overview()
        except (ValueError, RuntimeError) as exc:
            entity_mapping_setup_error = str(exc)

    account_group_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.account_group.view"
        ),
        "can_bootstrap": (
            session.get("role") == "super_admin"
            and has_accounting_permission(
                access, "accounting.account_group.bootstrap"
            )
        ),
    }
    account_group_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "groups": [],
        "primary_groups": [],
        "child_groups": [],
        "group_count": 0,
        "active_count": 0,
        "system_count": 0,
        "protected_count": 0,
        "inactive_count": 0,
        "audit_recovery_count": 0,
        "health": {
            "required_count": 15,
            "present_count": 0,
            "missing": [],
            "missing_count": 15,
            "drifted": [],
            "drifted_count": 0,
            "is_complete": False,
        },
    }
    account_group_setup_error = ""

    if avpl_entity_document and account_group_capabilities["can_view"]:
        try:
            ensure_account_group_indexes()
            account_group_overview = get_account_group_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            account_group_setup_error = str(exc)

    ledger_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.ledger.view"
        ),
        "can_bootstrap": (
            session.get("role") == "super_admin"
            and has_accounting_permission(
                access, "accounting.ledger.bootstrap"
            )
        ),
    }
    ledger_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "ledgers": [],
        "sections": [],
        "ledger_count": 0,
        "active_count": 0,
        "system_count": 0,
        "protected_count": 0,
        "inactive_count": 0,
        "tax_ledger_count": 0,
        "clearing_ledger_count": 0,
        "audit_recovery_count": 0,
        "health": {
            "required_count": 14,
            "present_count": 0,
            "missing": [],
            "missing_count": 14,
            "drifted": [],
            "drifted_count": 0,
            "is_complete": False,
        },
    }
    ledger_setup_error = ""

    if avpl_entity_document and ledger_capabilities["can_view"]:
        try:
            ensure_ledger_indexes()
            ledger_overview = get_ledger_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            ledger_setup_error = str(exc)

    party_ledger_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.party_ledger.view"
        ),
        "can_create": has_accounting_permission(
            access, "accounting.party_ledger.create"
        ),
        "can_edit": has_accounting_permission(
            access, "accounting.party_ledger.edit"
        ),
        "can_submit": has_accounting_permission(
            access, "accounting.party_ledger.submit"
        ),
        "can_withdraw": has_accounting_permission(
            access, "accounting.party_ledger.withdraw"
        ),
        "can_cancel": has_accounting_permission(
            access, "accounting.party_ledger.cancel"
        ),
        "can_approve": has_accounting_permission(
            access, "accounting.party_ledger.approve"
        ),
        "can_return": has_accounting_permission(
            access, "accounting.party_ledger.return"
        ),
        "can_deactivate": has_accounting_permission(
            access, "accounting.party_ledger.deactivate"
        ),
        "can_reactivate": has_accounting_permission(
            access, "accounting.party_ledger.reactivate"
        ),
    }
    party_ledger_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "rows": [],
        "active_rows": [],
        "pending_rows": [],
        "working_rows": [],
        "inactive_rows": [],
        "cancelled_rows": [],
        "counts": {
            "draft": 0,
            "pending_approval": 0,
            "returned_for_correction": 0,
            "active": 0,
            "inactive": 0,
            "cancelled": 0,
        },
        "role_counts": {"supplier": 0, "customer": 0},
        "total_count": 0,
        "non_cancelled_count": 0,
        "audit_recovery_count": 0,
        "options": get_party_ledger_option_catalog(),
        "form_defaults": {
            "party_role": "supplier",
            "gst_registration_status": "unregistered",
            "state_name": "Assam",
            "state_code": "18",
            "credit_period_days": 0,
            "credit_limit": "0.00",
        },
    }
    party_ledger_setup_error = ""

    if avpl_entity_document and party_ledger_capabilities["can_view"]:
        try:
            ensure_party_ledger_indexes()
            party_ledger_overview = get_party_ledger_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            party_ledger_setup_error = str(exc)

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
        closed_financial_years=closed_financial_years,
        locked_financial_years=locked_financial_years,
        financial_year_control_capabilities=financial_year_control_capabilities,
        financial_year_control_overview=financial_year_control_overview,
        financial_year_control_setup_error=financial_year_control_setup_error,
        current_user_id=current_user_id,
        configuration_capabilities=configuration_capabilities,
        configuration_overview=configuration_overview,
        configuration_options=get_configuration_option_catalog(),
        configuration_setup_error=configuration_setup_error,
        number_series_capabilities=number_series_capabilities,
        number_series_overview=number_series_overview,
        number_series_setup_error=number_series_setup_error,
        entity_mapping_capabilities=entity_mapping_capabilities,
        entity_mapping_overview=entity_mapping_overview,
        entity_mapping_setup_error=entity_mapping_setup_error,
        account_group_capabilities=account_group_capabilities,
        account_group_overview=account_group_overview,
        account_group_setup_error=account_group_setup_error,
        ledger_capabilities=ledger_capabilities,
        ledger_overview=ledger_overview,
        ledger_setup_error=ledger_setup_error,
        party_ledger_capabilities=party_ledger_capabilities,
        party_ledger_overview=party_ledger_overview,
        party_ledger_setup_error=party_ledger_setup_error,
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
# Stage 2 Batch 7: Financial Year close, lock, unlock and reopen controls
# ---------------------------------------------------------------------------

@accounting_bp.route(
    "/financial-years/<financial_year_id>/controls/create",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.control.create")
def financial_year_control_create(financial_year_id):
    return _run_financial_year_control_action(
        lambda: create_financial_year_control_request(
            financial_year_id=financial_year_id,
            actor_user_id=session["user_id"],
            request_type=request.form.get("request_type"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/edit",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.control.edit")
def financial_year_control_edit(control_request_id):
    return _run_financial_year_control_action(
        lambda: update_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
            correction_response=request.form.get("correction_response"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/submit",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.control.submit")
def financial_year_control_submit(control_request_id):
    return _run_financial_year_control_action(
        lambda: submit_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            submission_note=request.form.get("submission_note"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/withdraw",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.control.withdraw")
def financial_year_control_withdraw(control_request_id):
    return _run_financial_year_control_action(
        lambda: withdraw_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/cancel",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin")
@accounting_permission_required("accounting.financial_year.control.cancel")
def financial_year_control_cancel(control_request_id):
    return _run_financial_year_control_action(
        lambda: cancel_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/approve",
    methods=["POST"],
)
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.financial_year.control.approve")
def financial_year_control_approve(control_request_id):
    return _run_financial_year_control_action(
        lambda: approve_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            approval_note=request.form.get("approval_note"),
        )
    )


@accounting_bp.route(
    "/financial-year-controls/<control_request_id>/return",
    methods=["POST"],
)
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.financial_year.control.return")
def financial_year_control_return(control_request_id):
    return _run_financial_year_control_action(
        lambda: return_financial_year_control_request(
            request_id=control_request_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
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


# ---------------------------------------------------------------------------
# Stage 2 Batch 6: Entity- and Financial-Year-wise document number series
# ---------------------------------------------------------------------------

@accounting_bp.route("/number-series/initialize", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.number_series.create")
def number_series_initialize():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("number-series")

    return _run_number_series_action(
        lambda: initialize_missing_number_series_drafts(
            entity_id=entity["_id"],
            financial_year_id=request.form.get("financial_year_id"),
            actor_user_id=session["user_id"],
        )
    )


@accounting_bp.route("/number-series/save", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.number_series.view")
def number_series_save():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("number-series")

    return _run_number_series_action(
        lambda: save_number_series_draft(
            entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route(
    "/number-series/<category>/<series_id>/submit",
    methods=["POST"],
)
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.number_series.submit")
def number_series_submit(category, series_id):
    return _run_number_series_action(
        lambda: submit_number_series(
            category=category,
            series_id=series_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            note=request.form.get("submission_note"),
        )
    )


@accounting_bp.route("/number-series/bulk-submit", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.number_series.submit")
def number_series_bulk_submit():
    raw_selections = request.form.getlist("selected_series")
    selections = []

    for raw_value in raw_selections:
        parts = str(raw_value or "").split("|", 2)
        if len(parts) != 3:
            flash("One selected number-series record is invalid. Refresh and try again.", "danger")
            return _redirect_dashboard("number-series")

        category, series_id, version = (part.strip() for part in parts)
        selections.append({
            "category": category,
            "series_id": series_id,
            "expected_version": version,
        })

    try:
        result = bulk_submit_number_series(
            selections=selections,
            actor_user_id=session["user_id"],
            note=request.form.get("bulk_submission_note"),
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(
            result.get("message") or "Selected number series submitted.",
            "warning" if result.get("failed_count") else "success",
        )

    return _redirect_dashboard("number-series")


@accounting_bp.route(
    "/number-series/<category>/<series_id>/withdraw",
    methods=["POST"],
)
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.number_series.withdraw")
def number_series_withdraw(category, series_id):
    return _run_number_series_action(
        lambda: withdraw_number_series(
            category=category,
            series_id=series_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route(
    "/number-series/<category>/<series_id>/approve",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.number_series.approve")
def number_series_approve(category, series_id):
    return _run_number_series_action(
        lambda: approve_number_series(
            category=category,
            series_id=series_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            approval_note=request.form.get("approval_note"),
        )
    )


@accounting_bp.route(
    "/number-series/<category>/<series_id>/return",
    methods=["POST"],
)
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.number_series.return")
def number_series_return(category, series_id):
    return _run_number_series_action(
        lambda: return_number_series(
            category=category,
            series_id=series_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
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


# ---------------------------------------------------------------------------
# Stage 2 Batch 8: disabled Centre, Mitra and Farmer hierarchy pre-mapping
# ---------------------------------------------------------------------------

@accounting_bp.route("/entity-mappings/synchronize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.entity_mapping.sync")
def entity_mappings_synchronize():
    try:
        result = synchronize_future_accounting_entity_mappings(
            actor_user_id=session["user_id"]
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        warning_count = len(result.get("warnings") or [])
        flash(
            result.get("message") or "Future Accounting hierarchy synchronized.",
            "warning" if warning_count else "success",
        )

    return _redirect_dashboard("future-entity-mapping")

# ---------------------------------------------------------------------------
# Stage 3 Batch 1: protected AVPL account-group foundation
# ---------------------------------------------------------------------------

@accounting_bp.route("/account-groups/synchronize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.account_group.bootstrap")
def account_groups_synchronize():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the active AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("account-groups")

    return _run_account_group_action(
        lambda: seed_protected_account_groups(
            actor_user_id=session["user_id"],
            accounting_entity_id=entity["_id"],
        )
    )

# ---------------------------------------------------------------------------
# Stage 3 Batch 2: protected default AVPL ledger masters
# ---------------------------------------------------------------------------

@accounting_bp.route("/ledgers/synchronize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.ledger.bootstrap")
def ledgers_synchronize():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the active AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("ledger-masters")

    return _run_ledger_action(
        lambda: seed_default_avpl_ledgers(
            actor_user_id=session["user_id"],
            accounting_entity_id=entity["_id"],
        )
    )

# ---------------------------------------------------------------------------
# Stage 3 Batch 3: supplier and customer party-ledger masters
# ---------------------------------------------------------------------------

@accounting_bp.route("/party-ledgers/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.party_ledger.create")
def party_ledger_create():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the active AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("party-ledgers")

    return _run_party_ledger_action(
        lambda: create_party_ledger(
            accounting_entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.party_ledger.edit")
def party_ledger_edit(ledger_id):
    return _run_party_ledger_action(
        lambda: update_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.party_ledger.submit")
def party_ledger_submit(ledger_id):
    return _run_party_ledger_action(
        lambda: submit_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            submission_note=request.form.get("submission_note"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.party_ledger.withdraw")
def party_ledger_withdraw(ledger_id):
    return _run_party_ledger_action(
        lambda: withdraw_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.party_ledger.cancel")
def party_ledger_cancel(ledger_id):
    return _run_party_ledger_action(
        lambda: cancel_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.party_ledger.approve")
def party_ledger_approve(ledger_id):
    return _run_party_ledger_action(
        lambda: approve_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            approval_note=request.form.get("approval_note"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.party_ledger.return")
def party_ledger_return(ledger_id):
    return _run_party_ledger_action(
        lambda: return_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            return_reason=request.form.get("return_reason"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.party_ledger.deactivate")
def party_ledger_deactivate(ledger_id):
    return _run_party_ledger_action(
        lambda: deactivate_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/party-ledgers/<ledger_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.party_ledger.reactivate")
def party_ledger_reactivate(ledger_id):
    return _run_party_ledger_action(
        lambda: reactivate_party_ledger(
            ledger_id=ledger_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )

