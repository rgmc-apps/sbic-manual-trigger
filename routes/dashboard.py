"""Upload Monitoring Dashboard (/): browse uploaded files/refs, their MSSQL/BC status, bulk-queue missing items, and add-to-buffer."""
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify
from core.config import DASHBOARD_UPLOAD_TYPES, BQ_COMPANY_NAME_TERMS, BQ_PROXY_TIMEOUT, RECONCILE_COMPANIES
from core.http_clients import _bc_api, _gcp_api, _proxy_json
from core.mssql import _split_po_refs
from routes.missing_items import _find_missing_items
from routes.document_ai_search import _insert_and_buffer

bp = Blueprint("dashboard", __name__)


@bp.route("/api/dashboard/files")
def api_dashboard_files():
    """Distinct uploaded file names for one upload type, grouped with counts — the
    dashboard's top-level search result."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    if upload_type not in DASHBOARD_UPLOAD_TYPES:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400

    params = {"upload_type": upload_type}
    ref_values = (request.args.get("ref_values") or "").strip()
    if ref_values:
        refs = _split_po_refs(ref_values)
        if refs:
            params["ref_values"] = ",".join(refs)
    company = (request.args.get("company") or "").strip().upper()
    if company:
        if upload_type != "po":
            return jsonify({"error": "company filtering is only supported for upload_type=po"}), 400
        terms = BQ_COMPANY_NAME_TERMS.get(company)
        if not terms:
            return jsonify({"error": f"No known company-name terms for {company!r} yet."}), 400
        params["company_terms"] = ",".join(terms)
    for key in ("date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value

    # A search with no date range AND no ref_values is an unbounded GROUP BY scan over
    # the entire header table — confirmed live (2026-10-29) that this now exceeds even a
    # 150s timeout against int_document_ai's current size, while the identical search
    # scoped to a single day returns in ~2s. A ref_values search is already narrow/fast
    # regardless of date range (an IN UNNEST(...) lookup, not a full scan), so it's
    # exempted — only a truly unscoped "browse everything" search gets this default.
    defaulted_date_window = False
    if "date_from" not in params and "date_to" not in params and "ref_values" not in params:
        params["date_from"] = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00")
        defaulted_date_window = True

    try:
        resp = _gcp_api("GET", "/bigquery_routes/upload-dashboard/files", params=params, timeout=BQ_PROXY_TIMEOUT)
        body, status_code = _proxy_json(resp)
        if status_code == 200 and isinstance(body, dict):
            body["defaulted_date_window"] = defaulted_date_window
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/dashboard/file-records")
def api_dashboard_file_records():
    """Every header row (one per PO/RA ref) sharing one uploaded file_name — the
    Expand action's data."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    file_name = request.args.get("file_name") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400

    try:
        resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": config_["header_table"], "where_column": "file_name", "where_value": file_name,
        }, timeout=BQ_PROXY_TIMEOUT)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/dashboard/detail-records")
def api_dashboard_detail_records():
    """Detail lines for one PO/RA ref — the More Details action's data."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    ref_value = request.args.get("ref_value") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not ref_value:
        return jsonify({"error": "ref_value is required"}), 400

    try:
        resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": config_["detail_table"], "where_column": config_["ref_column"], "where_value": ref_value,
        }, timeout=BQ_PROXY_TIMEOUT)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@bp.route("/api/dashboard/mssql-status")
def api_dashboard_mssql_status():
    """Whether this PO/RA ref's header and detail rows have already been copied into
    the MSSQL _BQ staging tables (CustomerPOULBQ/CustomerPOULDetailBQ for PO,
    CustomerRABQ/CustomerRADetailBQ for RA and RA(xml)) — not the promoted
    CustomerPOUL/CustomerRA tables, per the dashboard's own spec."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    ref_value = request.args.get("ref_value") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not ref_value:
        return jsonify({"error": "ref_value is required"}), 400

    def _fetch(table_name):
        """Returns (rows, error) — error is None on success, or a short diagnostic
        string on failure, so a bad column/table name shows up in the response instead
        of a generic "lookup failed" (see api.py git history: a prior version of this
        reused the BigQuery snake_case ref_column here, 500ing every call with
        "Invalid column name 'po_ref_number'" — MSSQL's _BQ tables use camelCase).
        Returns full rows (not just a count) so the dashboard's variance checker modal
        can compare them against the BigQuery detail rows it already has."""
        try:
            resp = _gcp_api("GET", "/sbic/by_table/value", params={
                "table_name": table_name, "where_column": config_["mssql_ref_column"], "where_value": ref_value,
            }, timeout=30)
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                return body.get("data") or [], None
            return None, str(body.get("detail") or body.get("error") or f"HTTP {status_code}")[:200]
        except requests.RequestException as exc:
            return None, f"Could not reach rgmc-gcp-api: {exc}"

    with ThreadPoolExecutor(max_workers=3) as ex:
        header_future = ex.submit(_fetch, config_["mssql_header_table"])
        detail_future = ex.submit(_fetch, config_["mssql_detail_table"])
        # Checked alongside the staging-table lookups above (not gated behind them
        # being found first) so one round trip covers both — CustomerPOULBQ's own
        # AFTER INSERT trigger is what actually promotes a row into this table, a
        # separate, silent, server-side step a staging-table-only check can't see
        # (see bigquery_bridge.py's __verify_trigger_promotion for the same check on
        # the automated-bridge side). "ref_value not in staging at all" and "in
        # staging but never promoted" both come back as promoted_in_mssql=False here —
        # the caller tells them apart via header_in_mssql.
        promoted_future = ex.submit(_fetch, config_["mssql_promoted_table"])
        header_rows, header_error = header_future.result()
        detail_rows, detail_error = detail_future.result()
        promoted_rows, promoted_error = promoted_future.result()

    return jsonify({
        "header_in_mssql": header_rows is not None and len(header_rows) > 0,
        "detail_count_in_mssql": None if detail_rows is None else len(detail_rows),
        "detail_data": detail_rows or [],
        "promoted_in_mssql": None if promoted_rows is None else len(promoted_rows) > 0,
        # Retrigger (cleanup-staging + insert-from-bigquery) only exists for PO
        # uploads — rgmc-gcp-api has no RA equivalent of either endpoint.
        "retrigger_supported": upload_type == "po",
        "check_failed": header_rows is None or detail_rows is None,
        "error": header_error or detail_error or promoted_error,
    })


@bp.route("/api/dashboard/bc-status")
def api_dashboard_bc_status():
    """Whether a PO ref already has a Sales Order in Business Central — PO + SBIC only,
    same scope every other BC-facing action in this app uses (BC Production has no MTC
    company — see RECONCILE_COMPANIES)."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    po_ref = request.args.get("ref_value") or ""
    company = (request.args.get("company") or "").strip().upper()
    if upload_type != "po":
        return jsonify({"error": "Business Central status is only available for upload_type=po"}), 400
    if company not in RECONCILE_COMPANIES:
        return jsonify({"error": "Business Central status is only available for company=SBIC"}), 400
    if not po_ref:
        return jsonify({"error": "ref_value is required"}), 400

    try:
        safe_ref = po_ref.replace("'", "''")
        resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={
            "company": company, "filter": f"externalDocumentNo eq '{safe_ref}'",
            "expand": "salesOrderLines",
        }, timeout=30)
        body, status_code = _proxy_json(resp)
        if status_code != 200:
            return jsonify(body), status_code
        orders = body.get("data") or []
        return jsonify({"in_bc": len(orders) > 0, "orders": orders})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/dashboard/queue-missing-items", methods=["POST"])
def api_dashboard_queue_missing_items():
    """Bulk-queue missing Sales Order lines for one or more PO refs that already have an
    Open order in Business Central — a one-click shortcut from the Upload Dashboard's BC
    comparison view straight into the same so_missing_item_fixes_{env} Firestore buffer
    the Missing Items tab reads from, so Transfer to BC there picks these up too. Reuses
    _find_missing_items (same matching rules as the Missing Items tab) instead of
    re-deriving the comparison from whatever the dashboard already has client-side, so an
    order/line only ever gets queued from a fresh, server-validated BC + BigQuery read.

    Still only ever QUEUES — see _find_missing_items/api_queue_missing_item_fix, the one
    real BC write stays behind Apply Missing Item Fixes on the Buffer Reconciliation page.
    Only a "ready" item (same readiness bar the Missing Items tab's own one-click Queue
    button requires — a resolved BC item AND a BC-valid Unit of Measure) gets queued;
    "needs_linking"/"needs_uom" items are reported back as skipped rather than queued
    with an item_no/UOM that Apply would just reject.

    po_ref_number may be a single ref or a comma-separated list (e.g. every PO ref under
    one uploaded file) — _build_missing_items_filter already ORs multiple refs into one
    BC search, so this naturally scopes to "every PO under this file that's both in BC
    and still has a missing line," with no separate per-PO looping needed here."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    po_ref_number = (data.get("po_ref_number") or "").strip()
    if company not in RECONCILE_COMPANIES:
        return jsonify({"error": "Only company=SBIC is supported here"}), 400
    if not po_ref_number:
        return jsonify({"error": "po_ref_number is required"}), 400

    try:
        groups = _find_missing_items(company, po_ref_number, "", "")
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    queued, skipped, errors = [], [], []
    for group in groups:
        order = group["order"]
        po_ref = order.get("externalDocumentNo") or ""
        for item in group["missing_items"]:
            label = item.get("customer_sku_code") or item.get("customer_sku_desc") or item["sku_key"]
            if item["status"] != "ready":
                skipped.append({"po_ref": po_ref, "sku": label, "reason": item["status"]})
                continue
            resolved_item = item.get("resolved_item") or {}
            try:
                resp = _bc_api("POST", "/bc/custom/v2/so-buffer/missing-item-fixes", json={
                    "company": company, "order_id": order.get("id"), "order_no": order.get("number") or "",
                    "po_ref": po_ref, "sku_key": item["sku_key"],
                    "customer_sku_code": item.get("customer_sku_code") or "",
                    "customer_sku_desc": item.get("customer_sku_desc") or "",
                    "item_no": resolved_item.get("itemNo"), "item_description": resolved_item.get("description") or "",
                    "po_qty": item.get("po_qty") or 0, "unit_of_measurement": item.get("unit_of_measurement") or "",
                    "unit_price": item.get("unit_price") or 0, "resolved_by": "upload-dashboard",
                })
                body, status_code = _proxy_json(resp)
                if status_code == 201:
                    queued.append({"po_ref": po_ref, "item_no": resolved_item.get("itemNo"), "sku": label})
                else:
                    errors.append({"po_ref": po_ref, "sku": label, "detail": body.get("detail") or body.get("error") or f"HTTP {status_code}"})
            except requests.RequestException as exc:
                errors.append({"po_ref": po_ref, "sku": label, "detail": f"Could not reach rgmc-bc-api: {exc}"})

    return jsonify({
        "orders_with_missing_items": len(groups),
        "queued": queued, "queued_count": len(queued),
        "skipped": skipped, "skipped_count": len(skipped),
        "errors": errors, "errors_count": len(errors),
    })


@bp.route("/api/dashboard/add-to-buffer", methods=["POST"])
def api_dashboard_add_to_buffer():
    """Add a PO (not yet in Business Central) to the SO-import buffer — thin wrapper
    around the same _insert_and_buffer the BigQuery Lookup tab's Quick Align already
    uses, given one already-fetched header + its detail lines straight from this
    dashboard's own file-records/detail-records calls."""
    data = request.get_json(silent=True) or {}
    header = data.get("header")
    details = data.get("details") or []
    employee = data.get("employee") or {}
    if not header:
        return jsonify({"error": "header is required"}), 400
    header = {**header, "_source": "bigquery"}
    return jsonify(_insert_and_buffer([header], details, employee))


@bp.route("/api/dashboard/source-file")
def api_dashboard_source_file():
    """Signed URL to the original uploaded file behind a Document AI file_name — the
    View File action."""
    upload_type = (request.args.get("upload_type") or "po").strip().lower()
    file_name = request.args.get("file_name") or ""
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400
    try:
        resp = _gcp_api("GET", "/bigquery_routes/document-ai/source-file", params={
            "file_name": file_name, "upload_type": upload_type,
        }, timeout=30)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
