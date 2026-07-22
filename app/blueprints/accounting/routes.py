from datetime import date

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
from app.services.accounting_gst_tax_service import (
    approve_gst_tax_rate,
    cancel_gst_tax_rate,
    create_gst_tax_rate,
    ensure_gst_tax_indexes,
    get_gst_tax_option_catalog,
    get_gst_tax_overview,
    retire_gst_tax_rate,
    return_gst_tax_rate,
    seed_gst_tax_foundation,
    submit_gst_tax_rate,
    update_gst_tax_rate,
    withdraw_gst_tax_rate,
)
from app.services.accounting_unit_service import (
    approve_custom_unit,
    approve_unit_conversion,
    cancel_custom_unit,
    cancel_unit_conversion,
    create_custom_unit,
    create_unit_conversion,
    deactivate_custom_unit,
    deactivate_unit_conversion,
    ensure_unit_indexes,
    get_unit_option_catalog,
    get_unit_overview,
    reactivate_custom_unit,
    reactivate_unit_conversion,
    return_custom_unit,
    return_unit_conversion,
    seed_standard_units,
    submit_custom_unit,
    submit_unit_conversion,
    update_custom_unit,
    update_unit_conversion,
    withdraw_custom_unit,
    withdraw_unit_conversion,
)
from app.services.accounting_hsn_service import (
    approve_hsn_master,
    cancel_hsn_master,
    create_hsn_master,
    deactivate_hsn_master,
    ensure_hsn_indexes,
    get_hsn_option_catalog,
    get_hsn_overview,
    reactivate_hsn_master,
    return_hsn_master,
    submit_hsn_master,
    update_hsn_master,
    withdraw_hsn_master,
)
from app.services.accounting_product_mapping_service import (
    approve_product_mapping,
    cancel_product_mapping,
    create_product_mapping,
    deactivate_product_mapping,
    ensure_product_mapping_indexes,
    get_product_mapping_option_catalog,
    get_product_mapping_overview,
    reactivate_product_mapping,
    return_product_mapping,
    submit_product_mapping,
    update_product_mapping,
    withdraw_product_mapping,
)
from app.services.accounting_gst_determination_service import (
    get_gst_determination_overview,
    preview_gst_determination,
)
from app.services.accounting_product_tracking_service import (
    approve_product_tracking_profile,
    cancel_product_tracking_profile,
    create_product_tracking_profile,
    deactivate_product_tracking_profile,
    ensure_product_tracking_indexes,
    get_product_tracking_overview,
    preview_product_tracking_validation,
    reactivate_product_tracking_profile,
    return_product_tracking_profile,
    submit_product_tracking_profile,
    update_product_tracking_profile,
    withdraw_product_tracking_profile,
)
from app.services.accounting_voucher_service import (
    add_voucher_draft_line,
    create_voucher_draft,
    ensure_voucher_indexes,
    get_voucher_option_catalog,
    get_voucher_overview,
    remove_voucher_draft_line,
    update_voucher_draft,
    update_voucher_draft_line,
    validate_voucher_draft,
)

from app.services.accounting_voucher_posting_service import (
    ensure_voucher_posting_indexes,
    post_voucher_draft,
)


from app.services.accounting_voucher_reversal_service import (
    cancel_voucher_draft,
    reverse_posted_voucher,
)

from app.services.accounting_voucher_recovery_service import (
    ensure_voucher_recovery_indexes,
    get_voucher_recovery_overview,
    recover_voucher_posting,
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


def _run_gst_tax_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        category = "warning" if result.get("repaired") else "success"
        flash(result.get("message") or "GST tax master updated.", category)

    return _redirect_dashboard("gst-tax-master")


def _run_unit_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        category = "warning" if result.get("repaired") else "success"
        flash(result.get("message") or "Units master updated.", category)

    return _redirect_dashboard("units-master")


def _run_hsn_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "HSN master updated.", "success")

    return _redirect_dashboard("hsn-master")


def _run_product_mapping_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "Product Accounting mapping updated.", "success")

    return _redirect_dashboard("product-accounting-mapping")


def _run_product_tracking_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        flash(result.get("message") or "Product tracking controls updated.", "success")

    return _redirect_dashboard("product-tracking-controls")


def _run_voucher_action(action):
    try:
        result = action()
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        category = result.get("category")
        if not category:
            category = "info" if result.get("idempotent_replay") else "success"
        flash(result.get("message") or "Accounting voucher updated.", category)

    return _redirect_dashboard("voucher-engine")


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

    gst_tax_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.gst_tax.view"
        ),
        "can_bootstrap": (
            session.get("role") == "super_admin"
            and has_accounting_permission(
                access, "accounting.gst_tax.bootstrap"
            )
        ),
        "can_create": has_accounting_permission(
            access, "accounting.gst_tax.create"
        ),
        "can_edit": has_accounting_permission(
            access, "accounting.gst_tax.edit"
        ),
        "can_submit": has_accounting_permission(
            access, "accounting.gst_tax.submit"
        ),
        "can_withdraw": has_accounting_permission(
            access, "accounting.gst_tax.withdraw"
        ),
        "can_cancel": has_accounting_permission(
            access, "accounting.gst_tax.cancel"
        ),
        "can_approve": has_accounting_permission(
            access, "accounting.gst_tax.approve"
        ),
        "can_return": has_accounting_permission(
            access, "accounting.gst_tax.return"
        ),
        "can_retire": has_accounting_permission(
            access, "accounting.gst_tax.retire"
        ),
    }
    gst_tax_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "foundation": {
            "components": [],
            "taxabilities": [],
            "required_component_count": 3,
            "present_component_count": 0,
            "missing_components": ["cgst", "sgst", "igst"],
            "drifted_components": [],
            "required_taxability_count": 4,
            "present_taxability_count": 0,
            "missing_taxabilities": ["taxable", "exempt", "nil_rated", "non_gst"],
            "drifted_taxabilities": [],
            "is_complete": False,
            "audit_recovery_count": 0,
        },
        "rows": [],
        "working_rows": [],
        "pending_rows": [],
        "active_rows": [],
        "retired_rows": [],
        "cancelled_rows": [],
        "current_active_rows": [],
        "future_active_rows": [],
        "counts": {
            "draft": 0,
            "pending_approval": 0,
            "returned_for_correction": 0,
            "active": 0,
            "retired": 0,
            "cancelled": 0,
        },
        "total_count": 0,
        "audit_recovery_count": 0,
        "options": get_gst_tax_option_catalog(),
        "form_defaults": {
            "effective_from": "",
            "effective_to": "",
            "taxability_code": "TAXABLE",
        },
    }
    gst_tax_setup_error = ""

    if avpl_entity_document and gst_tax_capabilities["can_view"]:
        try:
            ensure_gst_tax_indexes()
            gst_tax_overview = get_gst_tax_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            gst_tax_setup_error = str(exc)

    unit_capabilities = {
        "can_view": has_accounting_permission(access, "accounting.unit.view"),
        "can_bootstrap": (
            session.get("role") == "super_admin"
            and has_accounting_permission(access, "accounting.unit.bootstrap")
        ),
        "can_create": has_accounting_permission(access, "accounting.unit.create"),
        "can_edit": has_accounting_permission(access, "accounting.unit.edit"),
        "can_submit": has_accounting_permission(access, "accounting.unit.submit"),
        "can_withdraw": has_accounting_permission(access, "accounting.unit.withdraw"),
        "can_cancel": has_accounting_permission(access, "accounting.unit.cancel"),
        "can_approve": has_accounting_permission(access, "accounting.unit.approve"),
        "can_return": has_accounting_permission(access, "accounting.unit.return"),
        "can_deactivate": has_accounting_permission(access, "accounting.unit.deactivate"),
        "can_reactivate": has_accounting_permission(access, "accounting.unit.reactivate"),
    }
    unit_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "foundation": {
            "required_count": 45,
            "present_count": 0,
            "missing": [],
            "drifted": [],
            "is_complete": False,
        },
        "units": [],
        "standard_units": [],
        "custom_units": [],
        "custom_working": [],
        "custom_pending": [],
        "custom_active": [],
        "custom_inactive": [],
        "unit_counts": {},
        "conversions": [],
        "conversion_working": [],
        "conversion_pending": [],
        "conversion_active": [],
        "conversion_inactive": [],
        "conversion_counts": {},
        "audit_recovery_count": 0,
        "options": get_unit_option_catalog(),
    }
    unit_setup_error = ""

    if avpl_entity_document and unit_capabilities["can_view"]:
        try:
            ensure_unit_indexes()
            unit_overview = get_unit_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            unit_setup_error = str(exc)

    hsn_capabilities = {
        "can_view": has_accounting_permission(access, "accounting.hsn.view"),
        "can_create": has_accounting_permission(access, "accounting.hsn.create"),
        "can_edit": has_accounting_permission(access, "accounting.hsn.edit"),
        "can_submit": has_accounting_permission(access, "accounting.hsn.submit"),
        "can_withdraw": has_accounting_permission(access, "accounting.hsn.withdraw"),
        "can_cancel": has_accounting_permission(access, "accounting.hsn.cancel"),
        "can_approve": has_accounting_permission(access, "accounting.hsn.approve"),
        "can_return": has_accounting_permission(access, "accounting.hsn.return"),
        "can_deactivate": has_accounting_permission(access, "accounting.hsn.deactivate"),
        "can_reactivate": has_accounting_permission(access, "accounting.hsn.reactivate"),
    }
    hsn_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "rows": [],
        "working_rows": [],
        "pending_rows": [],
        "active_rows": [],
        "inactive_rows": [],
        "cancelled_rows": [],
        "counts": {},
        "taxability_counts": {},
        "total_count": 0,
        "audit_recovery_count": 0,
        "options": get_hsn_option_catalog(),
    }
    hsn_setup_error = ""

    if avpl_entity_document and hsn_capabilities["can_view"]:
        try:
            ensure_hsn_indexes()
            hsn_overview = get_hsn_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            hsn_setup_error = str(exc)

    product_mapping_capabilities = {
        "can_view": has_accounting_permission(access, "accounting.product_mapping.view"),
        "can_create": has_accounting_permission(access, "accounting.product_mapping.create"),
        "can_edit": has_accounting_permission(access, "accounting.product_mapping.edit"),
        "can_submit": has_accounting_permission(access, "accounting.product_mapping.submit"),
        "can_withdraw": has_accounting_permission(access, "accounting.product_mapping.withdraw"),
        "can_cancel": has_accounting_permission(access, "accounting.product_mapping.cancel"),
        "can_approve": has_accounting_permission(access, "accounting.product_mapping.approve"),
        "can_return": has_accounting_permission(access, "accounting.product_mapping.return"),
        "can_deactivate": has_accounting_permission(access, "accounting.product_mapping.deactivate"),
        "can_reactivate": has_accounting_permission(access, "accounting.product_mapping.reactivate"),
    }
    product_mapping_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "rows": [],
        "working_rows": [],
        "pending_rows": [],
        "active_rows": [],
        "inactive_rows": [],
        "cancelled_rows": [],
        "counts": {},
        "total_mapping_count": 0,
        "active_operational_product_count": 0,
        "active_mapped_product_count": 0,
        "unmapped_active_product_count": 0,
        "audit_recovery_count": 0,
        "options": get_product_mapping_option_catalog(),
        "prerequisites": {
            "has_active_hsn": False,
            "has_active_units": False,
            "has_purchase_ledger": False,
            "has_sales_ledger": False,
            "has_inventory_ledger": False,
            "is_ready": False,
        },
    }
    product_mapping_setup_error = ""

    if avpl_entity_document and product_mapping_capabilities["can_view"]:
        try:
            ensure_product_mapping_indexes()
            product_mapping_overview = get_product_mapping_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            product_mapping_setup_error = str(exc)

    gst_determination_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.gst_determination.view"
        ),
        "can_preview": has_accounting_permission(
            access, "accounting.gst_determination.preview"
        ),
    }
    gst_determination_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "seller": {},
        "default_place_of_supply": {},
        "parties": [],
        "suppliers": [],
        "customers": [],
        "product_mappings": [],
        "states": [],
        "transaction_types": [],
        "today": "",
        "counts": {"suppliers": 0, "customers": 0, "product_mappings": 0},
        "prerequisites": {
            "seller_state_ready": False,
            "seller_gst_profile_ready": False,
            "has_policy": False,
            "has_parties": False,
            "has_suppliers": False,
            "has_customers": False,
            "has_product_mappings": False,
            "is_ready": False,
        },
    }
    gst_determination_setup_error = ""
    gst_determination_preview = session.pop(
        "accounting_gst_determination_preview", None
    )

    if avpl_entity_document and gst_determination_capabilities["can_view"]:
        try:
            gst_determination_overview = get_gst_determination_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            gst_determination_setup_error = str(exc)

    product_tracking_capabilities = {
        "can_view": has_accounting_permission(
            access, "accounting.product_tracking.view"
        ),
        "can_create": has_accounting_permission(
            access, "accounting.product_tracking.create"
        ),
        "can_edit": has_accounting_permission(
            access, "accounting.product_tracking.edit"
        ),
        "can_submit": has_accounting_permission(
            access, "accounting.product_tracking.submit"
        ),
        "can_withdraw": has_accounting_permission(
            access, "accounting.product_tracking.withdraw"
        ),
        "can_cancel": has_accounting_permission(
            access, "accounting.product_tracking.cancel"
        ),
        "can_approve": has_accounting_permission(
            access, "accounting.product_tracking.approve"
        ),
        "can_return": has_accounting_permission(
            access, "accounting.product_tracking.return"
        ),
        "can_deactivate": has_accounting_permission(
            access, "accounting.product_tracking.deactivate"
        ),
        "can_reactivate": has_accounting_permission(
            access, "accounting.product_tracking.reactivate"
        ),
        "can_validate": has_accounting_permission(
            access, "accounting.product_tracking.validate"
        ),
    }
    product_tracking_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "rows": [],
        "working_rows": [],
        "pending_rows": [],
        "active_rows": [],
        "inactive_rows": [],
        "cancelled_rows": [],
        "counts": {},
        "total_profile_count": 0,
        "active_profile_count": 0,
        "barcode_profile_count": 0,
        "batch_profile_count": 0,
        "expiry_profile_count": 0,
        "unconfigured_mapping_count": 0,
        "audit_recovery_count": 0,
        "options": {
            "eligible_mappings": [],
            "all_active_mappings": [],
            "barcode_types": [],
            "movement_types": [],
            "status_labels": {},
        },
        "prerequisites": {
            "has_accounting_ready_products": False,
            "has_unconfigured_products": False,
            "is_ready": False,
        },
    }
    product_tracking_setup_error = ""
    product_tracking_preview = session.pop(
        "accounting_product_tracking_preview", None
    )

    if avpl_entity_document and product_tracking_capabilities["can_view"]:
        try:
            ensure_product_tracking_indexes()
            product_tracking_overview = get_product_tracking_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
        except (PermissionError, ValueError, RuntimeError) as exc:
            product_tracking_setup_error = str(exc)

    voucher_capabilities = {
        "can_view": has_accounting_permission(access, "accounting.voucher.view"),
        "can_create": has_accounting_permission(access, "accounting.voucher.create"),
        "can_edit": has_accounting_permission(access, "accounting.voucher.edit"),
        "can_validate": has_accounting_permission(access, "accounting.voucher.validate"),
        "can_cancel": has_accounting_permission(access, "accounting.voucher.cancel"),
        "can_post": has_accounting_permission(access, "accounting.voucher.post"),
        "can_reverse": has_accounting_permission(access, "accounting.voucher.reverse"),
        "can_audit_view": has_accounting_permission(
            access, "accounting.voucher.audit.view"
        ),
        "can_recover": has_accounting_permission(
            access, "accounting.voucher.recovery"
        ),
    }
    voucher_overview = {
        "entity_id": "",
        "entity_code": "AVPL",
        "entity_name": "AVPL",
        "rows": [],
        "draft_rows": [],
        "posted_rows": [],
        "cancelled_rows": [],
        "reversed_rows": [],
        "counts": {"draft": 0, "posted": 0, "cancelled": 0, "reversed": 0},
        "voucher_type_counts": {},
        "validation_counts": {"not_validated": 0, "valid": 0, "invalid": 0},
        "ledger_options": [],
        "active_ledger_count": 0,
        "total_count": 0,
        "audit_recovery_count": 0,
        "voucher_line_count": 0,
        "index_health": {
            "required_count": 8,
            "present_count": 0,
            "missing_count": 8,
            "present": [],
            "missing": [],
            "is_complete": False,
        },
        "open_financial_years": [],
        "options": get_voucher_option_catalog(),
        "form_defaults": {
            "voucher_type": "journal_voucher",
            "financial_year_id": "",
            "transaction_date": "",
            "reference_date": "",
            "voucher_role": "primary",
            "idempotency_key": "",
        },
        "prerequisites": {
            "has_open_financial_year": False,
            "indexes_ready": False,
            "is_ready_for_draft_headers": False,
            "has_active_ledgers": False,
            "is_ready_for_line_entry": False,
            "is_ready_for_posting": False,
        },
    }
    voucher_setup_error = ""
    voucher_recovery_overview = {
        "rows": [],
        "count": 0,
        "recovery_required_count": 0,
        "stale_lock_count": 0,
    }

    if avpl_entity_document and voucher_capabilities["can_view"]:
        try:
            ensure_voucher_indexes()
            ensure_voucher_posting_indexes()
            voucher_overview = get_voucher_overview(
                avpl_entity_document["_id"],
                session["user_id"],
            )
            if voucher_capabilities["can_recover"] and session.get("role") == "super_admin":
                ensure_voucher_recovery_indexes()
                voucher_recovery_overview = get_voucher_recovery_overview(
                    avpl_entity_document["_id"],
                    session["user_id"],
                )
        except (PermissionError, ValueError, RuntimeError) as exc:
            voucher_setup_error = str(exc)

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
        gst_tax_capabilities=gst_tax_capabilities,
        gst_tax_overview=gst_tax_overview,
        gst_tax_setup_error=gst_tax_setup_error,
        unit_capabilities=unit_capabilities,
        unit_overview=unit_overview,
        unit_setup_error=unit_setup_error,
        hsn_capabilities=hsn_capabilities,
        hsn_overview=hsn_overview,
        hsn_setup_error=hsn_setup_error,
        product_mapping_capabilities=product_mapping_capabilities,
        product_mapping_overview=product_mapping_overview,
        product_mapping_setup_error=product_mapping_setup_error,
        gst_determination_capabilities=gst_determination_capabilities,
        gst_determination_overview=gst_determination_overview,
        gst_determination_setup_error=gst_determination_setup_error,
        gst_determination_preview=gst_determination_preview,
        product_tracking_capabilities=product_tracking_capabilities,
        product_tracking_overview=product_tracking_overview,
        product_tracking_setup_error=product_tracking_setup_error,
        product_tracking_preview=product_tracking_preview,
        product_tracking_today=date.today().isoformat(),
        voucher_capabilities=voucher_capabilities,
        voucher_overview=voucher_overview,
        voucher_recovery_overview=voucher_recovery_overview,
        voucher_setup_error=voucher_setup_error,
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

# ---------------------------------------------------------------------------
# Stage 4 Batch 1: GST components, taxability and effective-dated tax rates
# ---------------------------------------------------------------------------

@accounting_bp.route("/gst-tax-master/synchronize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.gst_tax.bootstrap")
def gst_tax_master_synchronize():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the active AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("gst-tax-master")

    return _run_gst_tax_action(
        lambda: seed_gst_tax_foundation(
            actor_user_id=session["user_id"],
            accounting_entity_id=entity["_id"],
        )
    )


@accounting_bp.route("/gst-tax-rates/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.gst_tax.create")
def gst_tax_rate_create():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the active AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("gst-tax-master")

    return _run_gst_tax_action(
        lambda: create_gst_tax_rate(
            accounting_entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.gst_tax.edit")
def gst_tax_rate_edit(rate_id):
    return _run_gst_tax_action(
        lambda: update_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.gst_tax.submit")
def gst_tax_rate_submit(rate_id):
    return _run_gst_tax_action(
        lambda: submit_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            submission_note=request.form.get("submission_note"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.gst_tax.withdraw")
def gst_tax_rate_withdraw(rate_id):
    return _run_gst_tax_action(
        lambda: withdraw_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.gst_tax.cancel")
def gst_tax_rate_cancel(rate_id):
    return _run_gst_tax_action(
        lambda: cancel_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.gst_tax.approve")
def gst_tax_rate_approve(rate_id):
    return _run_gst_tax_action(
        lambda: approve_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            approval_note=request.form.get("approval_note"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.gst_tax.return")
def gst_tax_rate_return(rate_id):
    return _run_gst_tax_action(
        lambda: return_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            return_reason=request.form.get("return_reason"),
        )
    )


@accounting_bp.route("/gst-tax-rates/<rate_id>/retire", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.gst_tax.retire")
def gst_tax_rate_retire(rate_id):
    return _run_gst_tax_action(
        lambda: retire_gst_tax_rate(
            rate_id=rate_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            effective_to=request.form.get("effective_to"),
            reason=request.form.get("reason"),
        )
    )

# ---------------------------------------------------------------------------
# Stage 4 Batch 2 — Units, conversions and HSN masters
# ---------------------------------------------------------------------------

@accounting_bp.route("/units/synchronize", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.unit.bootstrap")
def units_synchronize():
    entity = get_avpl_entity()
    if not entity:
        flash("Initialize the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("units-master")
    return _run_unit_action(lambda: seed_standard_units(entity["_id"], session["user_id"]))


@accounting_bp.route("/units/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.create")
def custom_unit_create():
    entity = get_avpl_entity()
    if not entity:
        flash("The active AVPL Accounting entity is not available.", "danger")
        return _redirect_dashboard("units-master")
    return _run_unit_action(lambda: create_custom_unit(entity["_id"], session["user_id"], request.form))


@accounting_bp.route("/units/<unit_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.edit")
def custom_unit_edit(unit_id):
    return _run_unit_action(lambda: update_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form))


@accounting_bp.route("/units/<unit_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.submit")
def custom_unit_submit(unit_id):
    return _run_unit_action(lambda: submit_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("submission_note")))


@accounting_bp.route("/units/<unit_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.withdraw")
def custom_unit_withdraw(unit_id):
    return _run_unit_action(lambda: withdraw_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/units/<unit_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.cancel")
def custom_unit_cancel(unit_id):
    return _run_unit_action(lambda: cancel_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/units/<unit_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.approve")
def custom_unit_approve(unit_id):
    return _run_unit_action(lambda: approve_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("approval_note")))


@accounting_bp.route("/units/<unit_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.return")
def custom_unit_return(unit_id):
    return _run_unit_action(lambda: return_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("return_reason")))


@accounting_bp.route("/units/<unit_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.deactivate")
def custom_unit_deactivate(unit_id):
    return _run_unit_action(lambda: deactivate_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/units/<unit_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.reactivate")
def custom_unit_reactivate(unit_id):
    return _run_unit_action(lambda: reactivate_custom_unit(unit_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/unit-conversions/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.create")
def unit_conversion_create():
    entity = get_avpl_entity()
    if not entity:
        flash("The active AVPL Accounting entity is not available.", "danger")
        return _redirect_dashboard("units-master")
    return _run_unit_action(lambda: create_unit_conversion(entity["_id"], session["user_id"], request.form))


@accounting_bp.route("/unit-conversions/<conversion_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.edit")
def unit_conversion_edit(conversion_id):
    return _run_unit_action(lambda: update_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form))


@accounting_bp.route("/unit-conversions/<conversion_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.submit")
def unit_conversion_submit(conversion_id):
    return _run_unit_action(lambda: submit_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("submission_note")))


@accounting_bp.route("/unit-conversions/<conversion_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.withdraw")
def unit_conversion_withdraw(conversion_id):
    return _run_unit_action(lambda: withdraw_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/unit-conversions/<conversion_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.unit.cancel")
def unit_conversion_cancel(conversion_id):
    return _run_unit_action(lambda: cancel_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/unit-conversions/<conversion_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.approve")
def unit_conversion_approve(conversion_id):
    return _run_unit_action(lambda: approve_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("approval_note")))


@accounting_bp.route("/unit-conversions/<conversion_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.return")
def unit_conversion_return(conversion_id):
    return _run_unit_action(lambda: return_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("return_reason")))


@accounting_bp.route("/unit-conversions/<conversion_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.deactivate")
def unit_conversion_deactivate(conversion_id):
    return _run_unit_action(lambda: deactivate_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/unit-conversions/<conversion_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.unit.reactivate")
def unit_conversion_reactivate(conversion_id):
    return _run_unit_action(lambda: reactivate_unit_conversion(conversion_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/hsn-masters/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.hsn.create")
def hsn_master_create():
    entity = get_avpl_entity()
    if not entity:
        flash("The active AVPL Accounting entity is not available.", "danger")
        return _redirect_dashboard("hsn-master")
    return _run_hsn_action(lambda: create_hsn_master(entity["_id"], session["user_id"], request.form))


@accounting_bp.route("/hsn-masters/<hsn_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.hsn.edit")
def hsn_master_edit(hsn_id):
    return _run_hsn_action(lambda: update_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form))


@accounting_bp.route("/hsn-masters/<hsn_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.hsn.submit")
def hsn_master_submit(hsn_id):
    return _run_hsn_action(lambda: submit_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("submission_note")))


@accounting_bp.route("/hsn-masters/<hsn_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.hsn.withdraw")
def hsn_master_withdraw(hsn_id):
    return _run_hsn_action(lambda: withdraw_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/hsn-masters/<hsn_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.hsn.cancel")
def hsn_master_cancel(hsn_id):
    return _run_hsn_action(lambda: cancel_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/hsn-masters/<hsn_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.hsn.approve")
def hsn_master_approve(hsn_id):
    return _run_hsn_action(lambda: approve_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("approval_note")))


@accounting_bp.route("/hsn-masters/<hsn_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.hsn.return")
def hsn_master_return(hsn_id):
    return _run_hsn_action(lambda: return_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("return_reason")))


@accounting_bp.route("/hsn-masters/<hsn_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.hsn.deactivate")
def hsn_master_deactivate(hsn_id):
    return _run_hsn_action(lambda: deactivate_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("reason")))


@accounting_bp.route("/hsn-masters/<hsn_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.hsn.reactivate")
def hsn_master_reactivate(hsn_id):
    return _run_hsn_action(lambda: reactivate_hsn_master(hsn_id, session["user_id"], request.form.get("version"), request.form.get("reason")))

# ---------------------------------------------------------------------------
# Stage 4 · Batch 3 — Product Accounting mapping
# ---------------------------------------------------------------------------


@accounting_bp.route("/product-mappings/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_mapping.create")
def product_mapping_create():
    entity = get_avpl_entity()
    if not entity:
        flash("The active AVPL Accounting entity is not available.", "danger")
        return _redirect_dashboard("product-accounting-mapping")
    return _run_product_mapping_action(
        lambda: create_product_mapping(entity["_id"], session["user_id"], request.form)
    )


@accounting_bp.route("/product-mappings/<mapping_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_mapping.edit")
def product_mapping_edit(mapping_id):
    return _run_product_mapping_action(
        lambda: update_product_mapping(
            mapping_id, session["user_id"], request.form.get("version"), request.form
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_mapping.submit")
def product_mapping_submit(mapping_id):
    return _run_product_mapping_action(
        lambda: submit_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("submission_note"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_mapping.withdraw")
def product_mapping_withdraw(mapping_id):
    return _run_product_mapping_action(
        lambda: withdraw_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_mapping.cancel")
def product_mapping_cancel(mapping_id):
    return _run_product_mapping_action(
        lambda: cancel_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_mapping.approve")
def product_mapping_approve(mapping_id):
    return _run_product_mapping_action(
        lambda: approve_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("approval_note"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_mapping.return")
def product_mapping_return(mapping_id):
    return _run_product_mapping_action(
        lambda: return_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("return_reason"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_mapping.deactivate")
def product_mapping_deactivate(mapping_id):
    return _run_product_mapping_action(
        lambda: deactivate_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-mappings/<mapping_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_mapping.reactivate")
def product_mapping_reactivate(mapping_id):
    return _run_product_mapping_action(
        lambda: reactivate_product_mapping(
            mapping_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )

@accounting_bp.route("/gst-determination/preview", methods=["POST"])
@login_required
@roles_required("super_admin", "avpl_admin", "accounts")
@accounting_permission_required("accounting.gst_determination.preview")
def gst_determination_preview_route():
    entity = get_avpl_entity(include_inactive=False)
    if not entity:
        flash("Initialize and activate the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("gst-determination")

    try:
        result = preview_gst_determination(
            entity["_id"],
            session["user_id"],
            request.form,
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        session["accounting_gst_determination_preview"] = result
        flash(
            f"GST preview resolved as {result['supply_type_label']}.",
            "success",
        )

    return _redirect_dashboard("gst-determination")

# ---------------------------------------------------------------------------
# Stage 4 · Batch 5 — Barcode, batch and expiry controls
# ---------------------------------------------------------------------------


@accounting_bp.route("/product-tracking/create", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_tracking.create")
def product_tracking_create():
    entity = get_avpl_entity(include_inactive=False)
    if not entity:
        flash("The active AVPL Accounting entity is not available.", "danger")
        return _redirect_dashboard("product-tracking-controls")
    return _run_product_tracking_action(
        lambda: create_product_tracking_profile(
            entity["_id"], session["user_id"], request.form
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_tracking.edit")
def product_tracking_edit(profile_id):
    return _run_product_tracking_action(
        lambda: update_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form,
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/submit", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_tracking.submit")
def product_tracking_submit(profile_id):
    return _run_product_tracking_action(
        lambda: submit_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("submission_note"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/withdraw", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_tracking.withdraw")
def product_tracking_withdraw(profile_id):
    return _run_product_tracking_action(
        lambda: withdraw_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts")
@accounting_permission_required("accounting.product_tracking.cancel")
def product_tracking_cancel(profile_id):
    return _run_product_tracking_action(
        lambda: cancel_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/approve", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_tracking.approve")
def product_tracking_approve(profile_id):
    return _run_product_tracking_action(
        lambda: approve_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("approval_note"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/return", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_tracking.return")
def product_tracking_return(profile_id):
    return _run_product_tracking_action(
        lambda: return_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("return_reason"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/deactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_tracking.deactivate")
def product_tracking_deactivate(profile_id):
    return _run_product_tracking_action(
        lambda: deactivate_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-tracking/<profile_id>/reactivate", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.product_tracking.reactivate")
def product_tracking_reactivate(profile_id):
    return _run_product_tracking_action(
        lambda: reactivate_product_tracking_profile(
            profile_id,
            session["user_id"],
            request.form.get("version"),
            request.form.get("reason"),
        )
    )


@accounting_bp.route("/product-tracking/validate", methods=["POST"])
@login_required
@roles_required("super_admin", "avpl_admin", "accounts")
@accounting_permission_required("accounting.product_tracking.validate")
def product_tracking_validate():
    entity = get_avpl_entity(include_inactive=False)
    if not entity:
        flash("Initialize and activate the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("product-tracking-controls")

    try:
        result = preview_product_tracking_validation(
            entity["_id"],
            session["user_id"],
            request.form,
        )
    except PermissionError as exc:
        flash(str(exc), "danger")
    except (ValueError, RuntimeError) as exc:
        flash(str(exc), "danger")
    else:
        session["accounting_product_tracking_preview"] = result
        flash("Product tracking controls validated successfully.", "success")

    return _redirect_dashboard("product-tracking-controls")

# ---------------------------------------------------------------------------
# Stage 5 · Batches 1–2 — Voucher headers, draft lines and validation
# ---------------------------------------------------------------------------


@accounting_bp.route("/vouchers/create", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.create")
def voucher_create():
    entity = get_avpl_entity(include_inactive=False)
    if not entity:
        flash("Initialize and activate the AVPL Accounting entity first.", "danger")
        return _redirect_dashboard("voucher-engine")

    return _run_voucher_action(
        lambda: create_voucher_draft(
            accounting_entity_id=entity["_id"],
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/edit", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.edit")
def voucher_edit(voucher_id):
    return _run_voucher_action(
        lambda: update_voucher_draft(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/lines/add", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.edit")
def voucher_line_add(voucher_id):
    return _run_voucher_action(
        lambda: add_voucher_draft_line(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route(
    "/vouchers/<voucher_id>/lines/<line_id>/edit",
    methods=["POST"],
)
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.edit")
def voucher_line_edit(voucher_id, line_id):
    return _run_voucher_action(
        lambda: update_voucher_draft_line(
            voucher_id=voucher_id,
            line_id=line_id,
            actor_user_id=session["user_id"],
            raw_payload=request.form.to_dict(flat=True),
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route(
    "/vouchers/<voucher_id>/lines/<line_id>/remove",
    methods=["POST"],
)
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.edit")
def voucher_line_remove(voucher_id, line_id):
    return _run_voucher_action(
        lambda: remove_voucher_draft_line(
            voucher_id=voucher_id,
            line_id=line_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/validate", methods=["POST"])
@login_required
@roles_required("accounts", "avpl_admin", "super_admin")
@accounting_permission_required("accounting.voucher.validate")
def voucher_validate(voucher_id):
    return _run_voucher_action(
        lambda: validate_voucher_draft(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        )
    )



@accounting_bp.route("/vouchers/<voucher_id>/post", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.voucher.post")
def voucher_post(voucher_id):
    return _run_voucher_action(
        lambda: post_voucher_draft(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/cancel", methods=["POST"])
@login_required
@roles_required("accounts", "super_admin")
@accounting_permission_required("accounting.voucher.cancel")
def voucher_cancel(voucher_id):
    return _run_voucher_action(
        lambda: cancel_voucher_draft(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/reverse", methods=["POST"])
@login_required
@roles_required("avpl_admin", "super_admin")
@accounting_permission_required("accounting.voucher.reverse")
def voucher_reverse(voucher_id):
    return _run_voucher_action(
        lambda: reverse_posted_voucher(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
            expected_version=request.form.get("version"),
            financial_year_id=request.form.get("financial_year_id"),
            reversal_date=request.form.get("reversal_date"),
            reason=request.form.get("reason"),
        )
    )


@accounting_bp.route("/vouchers/<voucher_id>/recover", methods=["POST"])
@login_required
@roles_required("super_admin")
@accounting_permission_required("accounting.voucher.recovery")
def voucher_recover(voucher_id):
    return _run_voucher_action(
        lambda: recover_voucher_posting(
            voucher_id=voucher_id,
            actor_user_id=session["user_id"],
        )
    )
