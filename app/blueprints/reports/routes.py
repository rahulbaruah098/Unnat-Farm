from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, session, url_for

from app.services.stage10_reporting_service import (
    ALL_REPORT_ROLES,
    MANAGEMENT_ROLES,
    build_financial_report,
    build_gst_report,
    build_inventory_report,
    build_management_overview,
    build_reconciliation_report,
    build_system_health,
    build_transaction_chains,
    build_uat_checklist,
    parse_report_filters,
    report_rows_for_csv,
    resolve_report_scope,
)
from app.utils.decorators import login_required, roles_required


reports_bp = Blueprint("reports", __name__, url_prefix="/reports")


def _scope():
    if not current_app.config.get("AVPL_REPORTS_ENABLED", True):
        abort(404)
    role = session.get("role") or ""
    if role not in ALL_REPORT_ROLES:
        abort(403)
    return resolve_report_scope(
        session.get("user_id"),
        role=role,
        centre_uid_hint=session.get("centre_uid") or "",
        mitra_uid_hint=session.get("mitra_uid") or "",
    )


def _filters():
    return parse_report_filters(request.args)




def _paginate(report, key="rows", default_per_page=50):
    rows = list(report.get(key) or [])
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(max(int(request.args.get("per_page") or default_per_page), 10), 100)
    except (TypeError, ValueError):
        per_page = default_per_page
    total = len(rows)
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    report[key] = rows[start:start + per_page]
    args = request.args.to_dict(flat=True)
    args.pop("page", None)
    endpoint = request.endpoint
    prev_url = url_for(endpoint, **args, page=page - 1) if page > 1 else ""
    next_url = url_for(endpoint, **args, page=page + 1) if page < total_pages else ""
    report["pagination"] = {
        "page": page, "per_page": per_page, "total": total, "total_pages": total_pages,
        "previous_url": prev_url, "next_url": next_url,
        "start": start + 1 if total else 0, "end": min(start + per_page, total),
    }
    return report

def _context(page, **kwargs):
    scope = kwargs.pop("scope", None) or _scope()
    filters = kwargs.pop("filters", None) or _filters()
    return {
        "stage10_page": page,
        "scope": scope,
        "filters": filters,
        **kwargs,
    }


@reports_bp.route("/")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def overview():
    scope = _scope()
    filters = _filters()
    report = build_management_overview(scope, filters)
    return render_template("reports/overview.html", **_context("overview", scope=scope, filters=filters, report=report))


@reports_bp.route("/inventory")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def inventory():
    scope = _scope()
    filters = _filters()
    report = _paginate(build_inventory_report(scope, filters))
    return render_template("reports/inventory.html", **_context("inventory", scope=scope, filters=filters, report=report))


@reports_bp.route("/financial")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def financial():
    scope = _scope()
    filters = _filters()
    report = _paginate(build_financial_report(scope, filters))
    return render_template("reports/financial.html", **_context("financial", scope=scope, filters=filters, report=report))


@reports_bp.route("/transactions")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def transactions():
    scope = _scope()
    filters = _filters()
    report = _paginate(build_transaction_chains(scope, filters))
    return render_template("reports/transactions.html", **_context("transactions", scope=scope, filters=filters, report=report))


@reports_bp.route("/reconciliation")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def reconciliation():
    scope = _scope()
    filters = _filters()
    report = _paginate(build_reconciliation_report(scope, filters), key="issues")
    return render_template("reports/reconciliation.html", **_context("reconciliation", scope=scope, filters=filters, report=report))


@reports_bp.route("/gst")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def gst():
    scope = _scope()
    filters = _filters()
    report = _paginate(build_gst_report(scope, filters))
    return render_template("reports/gst.html", **_context("gst", scope=scope, filters=filters, report=report))


@reports_bp.route("/system-health")
@login_required
@roles_required(*sorted(MANAGEMENT_ROLES))
def system_health():
    scope = _scope()
    filters = _filters()
    report = build_system_health(scope, filters)
    return render_template("reports/system_health.html", **_context("system_health", scope=scope, filters=filters, report=report))


@reports_bp.route("/uat")
@login_required
@roles_required("super_admin", "avpl_admin")
def uat():
    scope = _scope()
    report = build_uat_checklist(scope)
    return render_template("reports/uat.html", **_context("uat", scope=scope, report=report))


@reports_bp.route("/api/health")
@login_required
@roles_required(*sorted(MANAGEMENT_ROLES))
def api_health():
    scope = _scope()
    filters = _filters()
    health = build_system_health(scope, filters)
    return jsonify({
        "ok": health["summary"]["critical"] == 0,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "summary": health["summary"],
        "reconciliation": health["reconciliation"]["summary"],
        "checks": health["checks"],
    })


@reports_bp.route("/export/<report_name>.csv")
@login_required
@roles_required(*sorted(ALL_REPORT_ROLES))
def export_csv(report_name):
    scope = _scope()
    filters = _filters()
    allowed = {"inventory", "financial", "transactions", "gst", "reconciliation"}
    if report_name not in allowed:
        abort(404)
    headers, rows = report_rows_for_csv(report_name, scope, filters)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    filename = f"unnatfarm-{report_name}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        "\ufeff" + buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
