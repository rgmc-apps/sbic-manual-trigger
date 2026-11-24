"""BigQuery-bridge handoff history: one record per bridge run, with MSSQL + BC status, enriched with BigQuery insert timestamps."""
import requests
from flask import Blueprint, request, jsonify
from core.config import BQ_PROXY_TIMEOUT
from core.http_clients import _bc_api, _gcp_api, _proxy_json

bp = Blueprint("handoff_history", __name__)


@bp.route("/api/handoff-history")
def api_handoff_history():
    """BigQuery-bridge handoff history — one record per bridge run, listing only the
    PO ref numbers inserted into MSSQL per target table, plus date/time/status.

    All filters are optional; omit everything to list the whole log (most recent first).
    """
    params = {}
    for key in ("group_code", "mssql_status", "bc_status", "date", "date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/handoff-history", params=params)
        body, status_code = _proxy_json(resp)
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    if status_code == 200 and isinstance(body, dict):
        _enrich_handoff_history_with_bq_created_at(body.get("data") or [])
    return jsonify(body), status_code


def _enrich_handoff_history_with_bq_created_at(records: list) -> None:
    """Stamps each customerpoul record with bq_created_at — when its underlying row was
    actually inserted into BigQuery's int_document_ai, as opposed to `timestamp`
    (already on the record), which is when THIS handoff/retrigger was logged. Firestore
    never stores this itself (bridge_handoff_history.record_handoff/record_manual_handoff
    only ever log the handoff event's own timestamp), so it's looked up fresh here in one
    batched call — works for every historical record, not just ones logged after some
    future schema change.

    customerpoul only: its po_ref_number maps unambiguously to one BigQuery table
    (int_document_ai). customerra/onlinesalespo's po_ref_number field actually holds a
    RA ref or online-sales key against a *different* source table each, and group_code
    alone can't disambiguate RA from RA(xml) (both log as "customerra") — left as "—" in
    the UI rather than guessed at. Best-effort: any failure here just leaves
    bq_created_at unset, never breaks the history list itself.
    """
    po_refs = sorted({
        r.get("po_ref_number") for r in records
        if r.get("group_code") == "customerpoul" and r.get("po_ref_number")
    })
    if not po_refs:
        return
    try:
        resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                         params={"po_ref_number": ",".join(po_refs), "limit": 1000}, timeout=BQ_PROXY_TIMEOUT)
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return  # best-effort enrichment — the history list still renders without it
    if status_code != 200:
        return
    created_at_by_ref = {
        h.get("po_ref_number"): h.get("created_at")
        for h in (body.get("data") or []) if h.get("po_ref_number")
    }
    for r in records:
        if r.get("group_code") == "customerpoul":
            r["bq_created_at"] = created_at_by_ref.get(r.get("po_ref_number"))
