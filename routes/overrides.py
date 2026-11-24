"""Overrides tab CRUD, inactive-SKU marking, and the generic resolution-history/buffer-history lookups."""
import requests
from flask import Blueprint, request, jsonify
from core.http_clients import _bc_api, _proxy_json

bp = Blueprint("overrides", __name__)


@bp.route("/api/overrides", methods=["GET"])
def api_list_overrides():
    """List every saved SKU/branch/customer link — the Overrides tab's full registry,
    independent of any one company's currently-loaded buffer."""
    params = {}
    override_type = (request.args.get("type") or "").strip()
    if override_type:
        params["type"] = override_type
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/overrides", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/overrides", methods=["POST"])
def api_save_override():
    """Save the user's chosen BC link for one SKU code / branch name / customer name."""
    data = request.get_json(silent=True) or {}
    override_type = (data.get("type") or "").strip()
    key = (data.get("key") or "").strip()
    resolved = data.get("resolved") or {}
    resolved_by = (data.get("resolved_by") or "").strip()
    buffer_ids = data.get("buffer_ids") or []

    if override_type not in ("sku", "branch", "customer", "uom"):
        return jsonify({"error": "type must be sku, branch, customer, or uom"}), 400
    if not key:
        return jsonify({"error": "key is required"}), 400
    if not resolved:
        return jsonify({"error": "resolved is required"}), 400

    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/overrides", json={
            "type": override_type,
            "key": key,
            "resolved": resolved,
            "resolved_by": resolved_by,
            "buffer_ids": buffer_ids,
        })
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/overrides/<override_id>", methods=["DELETE"])
def api_delete_override(override_id):
    """Remove a previously-saved link (undo a mistaken resolution)."""
    try:
        resp = _bc_api("DELETE", f"/bc/custom/v2/so-buffer/overrides/{override_id}")
        if resp.status_code == 204:
            return "", 204
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/inactive-skus", methods=["POST"])
def api_mark_sku_inactive():
    """Mark a raw SKU code (or description, for a blank-SKU group) inactive.

    Pulls that SKU group out of the Items (SKU) tab and its resolved/total counts
    entirely, into its own Inactive Items tab, until reactivated via DELETE below.
    """
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    marked_by = (data.get("marked_by") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/inactive-skus", json={"key": key, "marked_by": marked_by})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/inactive-skus/<doc_id>", methods=["DELETE"])
def api_unmark_sku_inactive(doc_id):
    """Reactivate a SKU previously marked inactive (undo api_mark_sku_inactive)."""
    try:
        resp = _bc_api("DELETE", f"/bc/custom/v2/so-buffer/inactive-skus/{doc_id}")
        if resp.status_code == 204:
            return "", 204
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/reference")
def api_reference():
    """Full resolution history for one SKU code / branch name / customer name — every
    link ever saved for this key, not just the current one, for reference on future
    uploads that hit the same raw value again."""
    override_type = (request.args.get("type") or "").strip()
    key = (request.args.get("key") or "").strip()
    if not override_type or not key:
        return jsonify({"error": "type and key are required"}), 400
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/reference", params={"type": override_type, "key": key})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/history")
def api_history():
    """Buffer-reconciliation history — every PO a manual reprocess-buffer run touched,
    with its header/lines snapshot at that attempt, the outcome, and who triggered it.

    All filters are optional; omit everything to list the whole log (most recent first).
    """
    params = {}
    for key in ("company", "po_ref", "outcome", "run_id"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/history", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
