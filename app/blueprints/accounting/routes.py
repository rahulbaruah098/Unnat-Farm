from flask import Blueprint, flash, redirect, render_template, session, url_for

from app.services.accounting_entity_service import (
    bootstrap_avpl_entity,
    get_avpl_entity,
    list_accessible_entities,
    serialize_accounting_entity,
)
from app.services.accounting_permission_service import get_accounting_access
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
    avpl_entity = serialize_accounting_entity(
        get_avpl_entity(include_inactive=True)
    )

    return render_template(
        "accounting/dashboard.html",
        accounting_access=access,
        accessible_entities=accessible_entities,
        accounting_entity=avpl_entity,
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
