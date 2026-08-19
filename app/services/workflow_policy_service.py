"""Central workflow policy for UnnatFarm's simplified operating mode.

The MIS keeps all historical workflow states/routes for compatibility and audit,
but routine day-to-day work can be completed in one action when the streamlined
mode is enabled. High-risk controls (reversals, stock adjustments, financial-year
close/reopen and posted-document cancellation) remain explicit.
"""
from __future__ import annotations

import os

try:
    from flask import current_app, has_app_context
except Exception:  # pragma: no cover - Flask is present in production
    current_app = None
    has_app_context = lambda: False


ROUTINE_STREAMLINED_WORKFLOWS = {
    "avpl.purchase_order",
    "avpl.goods_receipt",
    "avpl.supplier_invoice_posting",
    "accounting.party_ledger",
    "accounting.gst_tax",
    "accounting.unit",
    "accounting.unit_conversion",
    "accounting.hsn",
    "accounting.product_mapping",
    "accounting.product_tracking",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def streamlined_workflows_enabled() -> bool:
    """Return whether simplified one-step routine workflows are enabled.

    Defaults to True for the refined MIS. It can be disabled with
    STREAMLINED_WORKFLOWS_ENABLED=false to restore the older maker/checker UI.
    """
    if has_app_context():
        return bool(current_app.config.get("STREAMLINED_WORKFLOWS_ENABLED", True))
    return _env_bool("STREAMLINED_WORKFLOWS_ENABLED", True)


def workflow_is_streamlined(workflow_key: str) -> bool:
    return streamlined_workflows_enabled() and workflow_key in ROUTINE_STREAMLINED_WORKFLOWS


def allow_same_actor_completion(workflow_key: str) -> bool:
    """Allow the same authorized user to complete a routine workflow.

    This does not remove permissions or audit history; it only removes the
    unnecessary second human click for workflows explicitly listed above.
    """
    return workflow_is_streamlined(workflow_key)
