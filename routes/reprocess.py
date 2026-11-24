"""Reprocess/sync/backfill triggers against rgmc-gcp-api, their run-status polling, and bridge-run monitoring."""
import requests
from flask import Blueprint, request, jsonify
from core.http_clients import _bc_api, _gcp_api, _proxy_json
from core.email_utils import _employee_notify_params

bp = Blueprint("reprocess", __name__)


@bp.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    """Trigger the existing POUL SO reprocess-buffer pass for one company.

    This re-runs the normal buffer retry (rgmc-gcp-api -> Pub/Sub -> rgmc-worker-pool).
    Manual links saved via /api/overrides are consulted: rgmc-bc-api's
    apply_resolution_to_buffer patches the resolved link directly onto the affected
    buffer doc(s) (header.resolvedShipTo/resolvedCustomer, line.resolvedItem), and
    rgmc-worker-pool's _create_order/_resolve_valid_lines read those fields first,
    ahead of their own automatic ship-to/item matching.

    employee_name/employee_company/employee_department/email identify who triggered
    this from the page (required client-side before the page is usable at all — see
    reconcile.js). Forwarded to rgmc-gcp-api as notify_* so rgmc-worker-pool can CC
    this person on the reprocess result emails.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400

    notify_params, err = _employee_notify_params(data)
    if err:
        return err

    params = {"companies": company, **notify_params}
    try:
        resp = _gcp_api("POST", "/customerpoul/reprocess-buffer", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/sync-inserted-orders", methods=["POST"])
def api_sync_inserted_orders():
    """Backfill lines onto BC sales orders already inserted for CustomerPOUL rows
    with a given createBy (default 'trigger' — the BigQuery bridge's automated
    inserts), using Cloud SQL (CustomerPOUL/CustomerPOULDetailBQ) as the source of
    truth. Finds each order by externalDocumentNo == poRefNumber, so it works even
    for orders whose Firestore buffer doc is already gone. Any line that still can't
    be resolved gets buffered (with the order's so_number) for manual reconciliation.

    Same employee-notify requirement and run_id/status tracking as /api/reprocess.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    create_by = (data.get("create_by") or "trigger").strip()

    notify_params, err = _employee_notify_params(data)
    if err:
        return err

    params = {"companies": company, "create_by": create_by, **notify_params}
    try:
        resp = _gcp_api("POST", "/customerpoul/sync-inserted-orders", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/backfill-from-cloudsql", methods=["POST"])
def api_backfill_from_cloudsql():
    """Create missing BC sales orders (header + lines) from Cloud SQL CustomerPOUL/
    CustomerPOULDetail for a given createBy (default 'trigger') and createDate range
    (when the row was inserted into CustomerPOUL, not poDate, the original PO date
    from the source ERP).

    Opposite skip condition from /api/sync-inserted-orders: a PO whose
    externalDocumentNo already matches an existing BC sales order is skipped
    untouched, never re-created. Anything that can't be fully resolved is buffered
    for manual reconciliation, same as every other import path.

    Same employee-notify requirement and run_id/status tracking as /api/reprocess.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    create_by = (data.get("create_by") or "trigger").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()

    notify_params, err = _employee_notify_params(data)
    if err:
        return err

    params = {"companies": company, "create_by": create_by, **notify_params}
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    try:
        resp = _gcp_api("POST", "/customerpoul/backfill-from-cloudsql", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/reprocess-status/<run_id>")
def api_reprocess_status(run_id):
    """Status of one reprocess-buffer run — queued / processing / done / error.

    run_id comes from /api/reprocess's response. Proxies rgmc-bc-api, which reads
    the Firestore doc rgmc-worker-pool writes as it processes the run.
    """
    try:
        resp = _bc_api("GET", f"/bc/custom/v2/so-buffer/reprocess-status/{run_id}")
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/bridge-run-locks")
def api_bridge_run_locks():
    """Status of every BigQuery-bridge automated-trigger lock (one per group_code —
    customerpoul/customerra/onlinesalespo): running/idle, since when, and whether it's
    stale (stuck "running" past rgmc-worker-pool's own 20-minute staleness threshold,
    silently blocking every automated trigger for that group_code). Proxies
    rgmc-bc-api, which reads the Firestore doc rgmc-worker-pool's bridge_run_lock.py
    owns. For the Monitoring page's trigger-status modal.
    """
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/bridge-run-locks")
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/bridge-run-history")
def api_bridge_run_history():
    """Past BigQuery-bridge runs (automated or manual), most recent first — one entry
    per run with its group_code/method/status/summary. Proxies rgmc-bc-api, which
    reads the append-only Firestore log rgmc-worker-pool's bridge_run_history.py
    writes. For the Monitoring page's trigger-status modal's "previous triggers" list.
    """
    params = {}
    for key in ("group_code", "date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    limit = (request.args.get("limit") or "").strip()
    if limit:
        params["limit"] = limit
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/bridge-run-history", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
