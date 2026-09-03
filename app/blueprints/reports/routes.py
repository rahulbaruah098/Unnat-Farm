from __future__ import annotations

import csv
import io
from datetime import datetime
from urllib.parse import urlencode

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request, send_file, session, url_for


from app.services.operational_report_service import (
    OPERATIONAL_REPORT_ROLES,
    build_operational_report,
    export_rows as operational_export_rows,
    parse_operational_filters,
)
from app.services.report_export_service import build_pdf, build_xlsx
from app.services.management_report_service import (
    MANAGEMENT_REPORT_ROLES,
    build_management_report,
    export_rows as management_export_rows,
    parse_management_filters,
)
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
    if role not in (set(ALL_REPORT_ROLES) | OPERATIONAL_REPORT_ROLES):
        abort(403)
    return resolve_report_scope(
        session.get("user_id"),
        role=role,
        centre_uid_hint=session.get("centre_uid") or "",
        mitra_uid_hint=session.get("mitra_uid") or "",
    )


def _filters():
    return parse_report_filters(request.args)


def _operational_nav_query(filters):
    values = {
        "period": filters.get("period") or "this_month",
        "farmer": filters.get("farmer") or "",
        "mitra": filters.get("mitra") or "",
        "product": filters.get("product") or "",
    }
    if values["period"] == "custom":
        values["from"] = filters.get("from_text") or ""
        values["to"] = filters.get("to_text") or ""
    return urlencode({key: value for key, value in values.items() if value})




def _management_nav_query(filters):
    values = {
        "period": filters.get("period") or "this_month",
        "centre": filters.get("centre") or "",
        "farmer": filters.get("farmer") or "",
        "mitra": filters.get("mitra") or "",
        "product": filters.get("product") or "",
        "status": filters.get("status") or "all",
        "q": filters.get("q") or "",
    }
    if values["period"] == "custom":
        values["from"] = filters.get("from_text") or ""
        values["to"] = filters.get("to_text") or ""
    return urlencode({key: value for key, value in values.items() if value not in {"", None}})




def _control_export_payload(report_name, scope, filters):
    name = str(report_name or "").strip().lower()
    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    applied = []
    if filters.get("from_text"):
        applied.append(("From", filters.get("from_text")))
    if filters.get("to_text"):
        applied.append(("To", filters.get("to_text")))
    if filters.get("q"):
        applied.append(("Search", filters.get("q")))

    if name == "reconciliation":
        report = build_reconciliation_report(scope, filters)
        summary = report.get("summary") or {}
        return {
            "title": "UnnatFarm Reconciliation",
            "scope_label": scope.get("label") or scope.get("role") or "Management",
            "subtitle": "Stock, financial, payment and transaction-chain consistency checks.",
            "generated_on": generated,
            "applied_filters": applied,
            "kpis": [
                ("Total Issues", summary.get("issues", 0)),
                ("Critical", summary.get("critical", 0)),
                ("Warnings", summary.get("warnings", 0)),
                ("Financial Documents Checked", summary.get("financial_documents_checked", 0)),
                ("Transaction Chains Checked", summary.get("transaction_chains_checked", 0)),
                ("Stock Lots Checked", summary.get("stock_lots_checked", 0)),
                ("Orphan Payments", summary.get("orphan_payments", 0)),
            ],
            "notice": "Review recommendations before changing historical stock or finance records.",
            "tables": [{
                "title": "Reconciliation Issues",
                "headers": ["Severity", "Type", "Reference", "Issue", "Likely Cause", "Impact", "Recommended Action"],
                "rows": [[
                    x.get("severity", "").title(), x.get("type", ""), x.get("reference", ""), x.get("message", ""),
                    x.get("cause", ""), x.get("impact", ""), x.get("recommended_action", ""),
                ] for x in report.get("issues") or []],
            }],
        }

    if name == "system-health":
        report = build_system_health(scope, filters)
        summary = report.get("summary") or {}
        rec = (report.get("reconciliation") or {}).get("summary") or {}
        return {
            "title": "UnnatFarm System Health",
            "scope_label": scope.get("label") or scope.get("role") or "Management",
            "subtitle": "Operational and accounting readiness checks with recommended actions.",
            "generated_on": generated,
            "applied_filters": applied,
            "kpis": [
                ("Health Score", f"{summary.get('score', 0)}%"),
                ("Healthy Checks", f"{summary.get('healthy', 0)}/{summary.get('total', 0)}"),
                ("Needs Attention", summary.get("attention", 0)),
                ("Critical Blockers", summary.get("critical", 0)),
                ("Reconciliation Issues", rec.get("issues", 0)),
                ("Reconciliation Critical", rec.get("critical", 0)),
                ("Orphan Payments", rec.get("orphan_payments", 0)),
            ],
            "notice": "Health checks are diagnostic. Resolve the source issue rather than silently rewriting history.",
            "tables": [{
                "title": "Health Checks",
                "headers": ["Category", "Check", "Status", "Severity", "Detail", "Recommended Action"],
                "rows": [[
                    c.get("category", "System"), c.get("name", ""), "Healthy" if c.get("ok") else "Attention",
                    c.get("severity", "").title(), c.get("detail", ""), c.get("recommended_action", ""),
                ] for c in report.get("checks") or []],
            }],
        }
    raise ValueError("Unsupported control report")


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
@roles_required(*sorted(set(ALL_REPORT_ROLES) | OPERATIONAL_REPORT_ROLES))
def overview():
    role = session.get("role") or ""
    if role in OPERATIONAL_REPORT_ROLES:
        filters = parse_operational_filters(request.args)
        try:
            report = build_operational_report(
                session.get("user_id"),
                role,
                "overview",
                filters,
                centre_uid_hint=session.get("centre_uid") or "",
                mitra_uid_hint=session.get("mitra_uid") or "",
            )
        except PermissionError:
            abort(403)
        except ValueError as exc:
            return render_template("reports/operational.html", report={
                "title": "Reports", "subtitle": "", "scope_label": "UnnatFarm",
                "section": "overview", "nav": [("overview", "Overview")],
                "filters": filters, "filter_options": {"periods": [], "farmers": [], "mitras": [], "products": [], "statuses": []},
                "show_filters": {}, "kpis": [], "tables": [], "trend": None, "notice": str(exc), "exportable": False,
            }, navigation_query=_operational_nav_query(filters))
        return render_template("reports/operational.html", report=report, navigation_query=_operational_nav_query(filters))

    if role in MANAGEMENT_REPORT_ROLES:
        filters = parse_management_filters(request.args)
        try:
            report = build_management_report(session.get("user_id"), role, "overview", filters)
        except PermissionError:
            abort(403)
        except ValueError as exc:
            abort(400, description=str(exc))
        return render_template("reports/management_operational.html", report=report, navigation_query=_management_nav_query(filters))

    scope = _scope()
    filters = _filters()
    report = build_management_overview(scope, filters)
    return render_template("reports/overview.html", **_context("overview", scope=scope, filters=filters, report=report))


@reports_bp.route("/view/<section>")
@login_required
@roles_required(*sorted(OPERATIONAL_REPORT_ROLES))
def operational(section):
    filters = parse_operational_filters(request.args)
    try:
        report = build_operational_report(
            session.get("user_id"),
            session.get("role") or "",
            section,
            filters,
            centre_uid_hint=session.get("centre_uid") or "",
            mitra_uid_hint=session.get("mitra_uid") or "",
        )
    except PermissionError:
        abort(403)
    except ValueError as exc:
        abort(400, description=str(exc))
    if report.get("section") != section:
        abort(404)
    return render_template("reports/operational.html", report=report, navigation_query=_operational_nav_query(filters))


@reports_bp.route("/download/<section>.<file_type>")
@login_required
@roles_required(*sorted(OPERATIONAL_REPORT_ROLES))
def operational_export(section, file_type):
    file_type = (file_type or "").lower()
    if file_type not in {"xlsx", "pdf"}:
        abort(404)
    filters = parse_operational_filters(request.args)
    try:
        report = build_operational_report(
            session.get("user_id"),
            session.get("role") or "",
            section,
            filters,
            centre_uid_hint=session.get("centre_uid") or "",
            mitra_uid_hint=session.get("mitra_uid") or "",
        )
    except PermissionError:
        abort(403)
    except ValueError as exc:
        abort(400, description=str(exc))
    if report.get("section") != section:
        abort(404)

    payload = operational_export_rows(report)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_section = "".join(ch for ch in section.lower() if ch.isalnum() or ch in {"-", "_"}) or "report"
    if file_type == "xlsx":
        content = build_xlsx(payload)
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = build_pdf(payload)
        mimetype = "application/pdf"
    return send_file(
        io.BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=f"unnatfarm-{safe_section}-{timestamp}.{file_type}",
        max_age=0,
    )


@reports_bp.route("/management/<section>")
@login_required
@roles_required(*sorted(MANAGEMENT_REPORT_ROLES))
def management(section):
    filters = parse_management_filters(request.args)
    try:
        report = build_management_report(session.get("user_id"), session.get("role") or "", section, filters)
    except PermissionError:
        abort(403)
    except ValueError as exc:
        abort(404, description=str(exc))
    return render_template("reports/management_operational.html", report=report, navigation_query=_management_nav_query(filters))


@reports_bp.route("/management/download/<section>.<file_type>")
@login_required
@roles_required(*sorted(MANAGEMENT_REPORT_ROLES))
def management_export(section, file_type):
    file_type = (file_type or "").lower()
    if file_type not in {"xlsx", "pdf"}:
        abort(404)
    filters = parse_management_filters(request.args)
    try:
        report = build_management_report(session.get("user_id"), session.get("role") or "", section, filters)
    except PermissionError:
        abort(403)
    except ValueError as exc:
        abort(404, description=str(exc))
    payload = management_export_rows(report)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_section = "".join(ch for ch in section.lower() if ch.isalnum() or ch in {"-", "_"}) or "report"
    if file_type == "xlsx":
        content = build_xlsx(payload)
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = build_pdf(payload)
        mimetype = "application/pdf"
    return send_file(io.BytesIO(content), mimetype=mimetype, as_attachment=True, download_name=f"unnatfarm-management-{safe_section}-{timestamp}.{file_type}", max_age=0)


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




@reports_bp.route("/control/download/<report_name>.<file_type>")
@login_required
@roles_required(*sorted(MANAGEMENT_ROLES))
def control_export(report_name, file_type):
    file_type = (file_type or "").lower()
    if file_type not in {"xlsx", "pdf"}:
        abort(404)
    if report_name not in {"reconciliation", "system-health"}:
        abort(404)
    scope = _scope()
    filters = _filters()
    try:
        payload = _control_export_payload(report_name, scope, filters)
    except ValueError:
        abort(404)
    timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    if file_type == "xlsx":
        content = build_xlsx(payload)
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        content = build_pdf(payload)
        mimetype = "application/pdf"
    return send_file(
        io.BytesIO(content),
        mimetype=mimetype,
        as_attachment=True,
        download_name=f"unnatfarm-{report_name}-{timestamp}.{file_type}",
        max_age=0,
    )


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
