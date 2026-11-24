"""Shared reprocess-run helpers used by both the Dashboard and Handoff-History retrigger flows: MSSQL-insert-then-log, fire-and-forget BC follow-up, and synchronous polling of a reprocess run."""
import time
import requests
from flask import current_app
from .config import BQ_UI_ONLY_HEADER_FIELDS
from .http_clients import _bc_api, _gcp_api, _proxy_json
from .company import _resolve_bc_company

def _insert_mssql_and_log(header: dict, details: list, po_ref: str, file_name: str | None):
    """Shared MSSQL-insert-then-verify-then-log core of /api/handoff-history/
    retrigger-mssql, called after that route's own cleanup-staging step.
    Returns (response_body, status_code).

    Re-sends header/details into CustomerPOULBQ/CustomerPOULDetailBQ via
    insert-from-bigquery, then verifies CustomerPOULBQ's own AFTER INSERT trigger's
    ACTUAL result with a direct CustomerPOUL lookup — the insert call alone only
    confirms the staging-table write succeeded, never whether the trigger that
    follows it actually promoted the row (it runs server-side with no return value) —
    and logs both outcomes into bridge_handoff_history_{env} (via rgmc-bc-api),
    best-effort, so a logging failure never fails the already-decided retrigger result.
    """
    header = {k: v for k, v in header.items() if k not in BQ_UI_ONLY_HEADER_FIELDS}
    try:
        # insert-from-bigquery is rate_limit-gated in rgmc-gcp-api, keyed on
        # path+groupcode with NO groupcode given here defaulting every call to the
        # SAME "customerpoul" bucket (30s cooldown) — harmless for a single retrigger,
        # but the batch route below calls this once per PO back-to-back, so without a
        # per-PO groupcode every PO after the first in any 30s window gets a 429 AFTER
        # its staging rows were already deleted by cleanup-staging (confirmed live,
        # 2026-10-28 MTC batch: 1 of 54 POs succeeded, the other 53 lost their staging
        # rows for nothing). A distinct groupcode per po_ref gives each call its own
        # bucket — still only ever called once per po_ref here, so the actual
        # protection this rate limit exists for (repeated hammering of the SAME PO)
        # is untouched.
        resp = _gcp_api("POST", "/customerpoul/insert-from-bigquery",
                         params={"groupcode": f"retrigger-{po_ref}"},
                         json={"headers": [header], "details": details}, timeout=90)
        body, status_code = _proxy_json(resp)
    except requests.RequestException as exc:
        return {"error": f"Could not reach rgmc-gcp-api: {exc}"}, 502

    mssql_status = "inserted" if status_code == 200 and body.get("status") == "success" else "error"
    error_detail = None if mssql_status == "inserted" else (body.get("message") or f"HTTP {status_code}")

    trigger_status = "unknown"
    if mssql_status == "inserted" and po_ref:
        try:
            check_resp = _gcp_api("GET", "/sbic/by_table/value", params={
                "table_name": "CustomerPOUL", "where_column": "poRefNumber", "where_value": po_ref,
            }, timeout=30)
            check_body, check_status = _proxy_json(check_resp)
            if check_status == 200:
                trigger_status = "promoted" if check_body.get("data") else "not_promoted"
        except requests.RequestException:
            pass  # stays "unknown"

    try:
        _bc_api("POST", "/bc/custom/v2/so-buffer/handoff-history/manual-entry", json={
            "po_ref_number": po_ref, "file_name": file_name,
            "mssql_status": mssql_status, "error": error_detail,
            "trigger_status": trigger_status,
        })
    except requests.RequestException:
        pass  # best-effort — the retrigger result itself is unaffected

    if isinstance(body, dict):
        # insert-from-bigquery can return HTTP 200 with an internal
        # {"status": "error", ...} payload (a business-logic failure, not a transport
        # one) — mssql_status/error here are the only reliable signal of that; every
        # caller (single-PO route, batch route, their JS) must check mssql_status
        # instead of the raw HTTP status, so normalize status_code to match it too,
        # so a plain `if status_code != 200` check downstream is no longer fooled by a
        # masked-as-200 failure.
        body["mssql_status"] = mssql_status
        body["trigger_status"] = trigger_status
        if error_detail and not body.get("error"):
            body["error"] = error_detail
    if mssql_status == "error" and status_code == 200:
        status_code = 502
    return body, status_code


def _trigger_bc_followup(company_name: str | None, po_refs: list, company_id: int | None = None) -> None:
    """Automatic BC-exists + missing-items follow-up, fired right after a retrigger's
    MSSQL insert succeeds — publishes the existing sync-from-cloudsql (backfill missing
    lines onto a BC order that already exists) and backfill-from-cloudsql (create a
    fresh order for a ref that still has none) messages, scoped to exactly these
    po_refs, instead of leaving BC correct only when a human separately clicks Cloud SQL
    Sync/Backfill on /reconcile.

    Both endpoints just publish a Pub/Sub message and return a run_id immediately — the
    actual BC work happens moments later in rgmc-worker-pool — so this stays a quick,
    best-effort fire-and-forget call that never blocks or fails the retrigger response
    itself (same best-effort convention as the handoff-history logging call above it).
    """
    company = _resolve_bc_company(company_name, company_id)
    if not company or not po_refs:
        return
    refs_param = ",".join(po_refs)
    for path in ("/customerpoul/sync-inserted-orders", "/customerpoul/backfill-from-cloudsql"):
        try:
            _gcp_api("POST", path, params={
                "companies": company, "create_by": "trigger", "po_ref_numbers": refs_param,
            }, timeout=15)
        except requests.RequestException as exc:
            current_app.logger.warning(f"_trigger_bc_followup: {path} failed for po_refs={refs_param}: {exc}")


def _poll_reprocess_run(run_id: str, timeout_s: float = 20, interval_s: float = 1) -> dict:
    """Polls GET /bc/custom/v2/so-buffer/reprocess-status/{run_id} (same call
    /api/reprocess-status/<run_id> makes) until status is "done"/"error" or timeout_s
    runs out — returns the last known status dict either way (never raises).

    Used only by the "Retrigger BC only" action below: unlike a file-scoped retrigger,
    this path never touches BigQuery (sync-inserted-orders/backfill-from-cloudsql are
    Cloud-SQL/MSSQL + BC only), so it's expected to finish in low single-digit seconds —
    fast enough to poll server-side and hand the UI one synchronous result instead of
    adding a second frontend polling loop alongside the existing file-retrigger one.
    """
    deadline = time.monotonic() + timeout_s
    last_body: dict = {"status": "unknown"}
    while True:
        try:
            resp = _bc_api("GET", f"/bc/custom/v2/so-buffer/reprocess-status/{run_id}")
            body, status_code = _proxy_json(resp)
            if status_code == 200 and isinstance(body, dict):
                last_body = body
                if body.get("status") in ("done", "error"):
                    return last_body
        except requests.RequestException as exc:
            last_body = {"status": "unknown", "error": f"Could not reach rgmc-bc-api: {exc}"}
        if time.monotonic() >= deadline:
            return last_body
        time.sleep(interval_s)
