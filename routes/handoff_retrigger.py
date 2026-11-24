"""Handoff History tab retrigger actions: BC-only (resume/backfill an existing order), full MSSQL+BC (cleanup, re-fetch from BigQuery, re-insert), the single combined button, and batch/file-scoped/async variants."""
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from core.config import BQ_PROXY_TIMEOUT
from core.http_clients import _bc_api, _gcp_api, _proxy_json
from core.company import _resolve_bc_company, _merge_duplicate_headers
from core.mssql import _split_po_refs, _fetch_from_customerpoul
from core.reprocess_polling import _poll_reprocess_run, _trigger_bc_followup, _insert_mssql_and_log

bp = Blueprint("handoff_retrigger", __name__)


def _retrigger_bc_only_core(po_ref: str) -> tuple[dict, int]:
    """Business-Central-only retrigger for one PO — unlike the MSSQL-side retrigger,
    this never touches BigQuery or re-inserts into MSSQL. It reads the PO exactly
    as it already sits in CustomerPOUL/CustomerPOULDetail (_fetch_from_customerpoul)
    and runs the same check-exists/backfill-missing-lines-or-create-fresh logic
    _trigger_bc_followup fires elsewhere, but blocking on the result (via
    _poll_reprocess_run) instead of fire-and-forget, so the caller gets one real
    outcome instead of pretending it's instant.

    Useful when MSSQL already has good data for a PO but BC doesn't yet (a missing
    order, or one still short some lines) — sidesteps the slow BigQuery dbt-view read
    entirely for that common case. Returns (response_body, status_code).
    """
    header, _lines = _fetch_from_customerpoul(po_ref)
    if not header:
        return {"error": f"PO {po_ref!r} not found in MSSQL (CustomerPOUL) — nothing to hand off to BC."}, 404

    company = _resolve_bc_company(header.get("company_name"), header.get("company_id"))
    if not company:
        return {
            "error": f"Could not resolve a BC company from companyName={header.get('company_name')!r} "
                     f"companyId={header.get('company_id')!r} — not sent to BC.",
        }, 400

    # rgmc-worker-pool's fetch_customerpoul_by_create_by filters CustomerPOUL by
    # create_by FIRST, then narrows by po_ref_numbers — so passing the literal
    # "trigger-bc-only" (which is never actually stamped on any row; it's only ever
    # used as a Teams-notification gate on the worker-pool side) matched zero rows for
    # every real PO, silently no-opping while still reporting "done". Using this PO's
    # own stored createBy makes the row lookup actually match, at the cost of the
    # "Retrigger BC only" Teams notification (gated on the literal "trigger-bc-only"
    # value) no longer firing for this path — confirmed live 2026-11-23 on 4 POs stuck
    # with BC headers but zero lines.
    effective_create_by = header.get("create_by") or "trigger-bc-only"
    run_ids = []
    for path in ("/customerpoul/sync-inserted-orders", "/customerpoul/backfill-from-cloudsql"):
        try:
            resp = _gcp_api("POST", path, params={
                "companies": company, "create_by": effective_create_by, "po_ref_numbers": po_ref,
            }, timeout=15)
            body, status_code = _proxy_json(resp)
            if status_code == 200 and body.get("run_id"):
                run_ids.append(body["run_id"])
        except requests.RequestException as exc:
            return {"error": f"Could not reach rgmc-gcp-api: {exc}"}, 502

    if not run_ids:
        return {"error": "Could not start the BC retrigger — no run_id returned."}, 502

    # Polled concurrently, not sequentially — each poll can take up to its own
    # timeout_s, and sequential polling would double the worst-case wait for no reason
    # (same ThreadPoolExecutor fan-out pattern already used elsewhere in this file).
    with ThreadPoolExecutor(max_workers=len(run_ids)) as ex:
        results = list(ex.map(_poll_reprocess_run, run_ids))

    if any(r.get("status") == "error" for r in results):
        detail = "; ".join(r.get("error") or str(r.get("summary") or "") for r in results if r.get("status") == "error")
        return {"status": "error", "detail": detail or "BC retrigger failed.", "runs": results}, 200
    if all(r.get("status") == "done" for r in results):
        return {"status": "done", "detail": "Business Central checked/updated for this PO.", "runs": results}, 200
    if any(r.get("status") in (None, "unknown") for r in results):
        # The status endpoint never returned a usable response for at least one run —
        # a genuine failure (bad run_id, rgmc-bc-api unreachable), not legitimately
        # still processing. Reporting this as "pending" would tell the user to wait for
        # a retrigger that will never complete.
        return {
            "status": "error",
            "detail": "Could not confirm the BC retrigger's outcome — check Handoff History or try again.",
            "runs": results,
        }, 200
    return {
        "status": "pending",
        "detail": "Still processing in the background — check Handoff History shortly.",
        "runs": results,
    }, 200


@bp.route("/api/handoff-history/retrigger-bc-only", methods=["POST"])
def api_handoff_history_retrigger_bc_only():
    data = request.get_json(silent=True) or {}
    po_ref = (data.get("po_ref_number") or "").strip()
    if not po_ref:
        return jsonify({"error": "po_ref_number is required"}), 400
    body, status_code = _retrigger_bc_only_core(po_ref)
    return jsonify(body), status_code


def _resolve_po_refs_for_file(file_name: str) -> list[str]:
    """Every distinct po_ref_number CustomerPOULBQ (MSSQL staging — carries fileName,
    unlike the promoted CustomerPOUL table) has under this exact file_name. MSSQL-only,
    no BigQuery read — this is what lets the file-scoped BC-only retrigger below skip
    BigQuery entirely, same as the single-PO version above."""
    try:
        resp = _gcp_api("GET", "/sbic/by_table/value", params={
            "table_name": "CustomerPOULBQ", "where_column": "fileName", "where_value": file_name,
        }, timeout=30)
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return []
    if status_code != 200:
        return []
    return sorted({r.get("poRefNumber") for r in (body.get("data") or []) if r.get("poRefNumber")})


def _resolve_file_refs_by_mssql_presence(file_name: str) -> tuple[dict, list, list] | None:
    """Every po_ref under file_name, split into present_refs_by_company (already in
    CustomerPOUL, grouped by resolved BC company), missing (not yet promoted/deleted),
    and company_unresolved. None if the file has no POs in CustomerPOULBQ at all."""
    po_refs = _resolve_po_refs_for_file(file_name)
    if not po_refs:
        return None
    with ThreadPoolExecutor(max_workers=min(20, len(po_refs))) as ex:
        headers = list(ex.map(lambda ref: _fetch_from_customerpoul(ref)[0], po_refs))
    present_refs_by_company: dict[str, list[str]] = {}
    missing: list[str] = []
    company_unresolved: list[str] = []
    for po_ref, header in zip(po_refs, headers):
        if not header:
            missing.append(po_ref)
            continue
        company = _resolve_bc_company(header.get("company_name"), header.get("company_id"))
        if not company:
            company_unresolved.append(po_ref)
            continue
        present_refs_by_company.setdefault(company, []).append(po_ref)
    return present_refs_by_company, missing, company_unresolved


def _run_bc_only_for_refs_by_company(present_refs_by_company: dict) -> tuple[dict, int]:
    """BC-only check/create for every ref in present_refs_by_company (grouped by
    resolved BC company), one sync + one backfill call per company, polled to
    completion. Returns (response_body, status_code); response_body always carries
    "present" (the refs actually attempted)."""
    present = [ref for refs in present_refs_by_company.values() for ref in refs]
    if not present_refs_by_company:
        return {"status": "error", "detail": "No POs under this file could be sent to BC.", "present": present, "runs": []}, 200

    run_ids = []
    for company, refs in present_refs_by_company.items():
        refs_param = ",".join(refs)
        for path in ("/customerpoul/sync-inserted-orders", "/customerpoul/backfill-from-cloudsql"):
            try:
                resp = _gcp_api("POST", path, params={
                    "companies": company, "create_by": "trigger-bc-only", "po_ref_numbers": refs_param,
                }, timeout=15)
                body, status_code = _proxy_json(resp)
                if status_code == 200 and body.get("run_id"):
                    run_ids.append(body["run_id"])
            except requests.RequestException as exc:
                return {"error": f"Could not reach rgmc-gcp-api: {exc}"}, 502

    if not run_ids:
        return {"error": "Could not start the BC retrigger — no run_id returned."}, 502

    with ThreadPoolExecutor(max_workers=len(run_ids)) as ex:
        results = list(ex.map(_poll_reprocess_run, run_ids))

    if any(r.get("status") == "error" for r in results):
        detail = "; ".join(r.get("error") or str(r.get("summary") or "") for r in results if r.get("status") == "error")
        status, detail = "error", (detail or "BC retrigger failed.")
    elif all(r.get("status") == "done" for r in results):
        status, detail = "done", "Business Central checked/updated for these POs."
    elif any(r.get("status") in (None, "unknown") for r in results):
        status, detail = "error", "Could not confirm the BC retrigger's outcome — check Handoff History or try again."
    else:
        status, detail = "pending", "Still processing in the background — check Handoff History shortly."

    return {"status": status, "detail": detail, "present": present, "runs": results}, 200


@bp.route("/api/handoff-history/retrigger-bc-only-file", methods=["POST"])
def api_handoff_history_retrigger_bc_only_file():
    """File-scoped "Retrigger BC only" — re-checks every PO under one uploaded file
    against Business Central in one pass, still without touching BigQuery.

    Resolves the file's po_refs from CustomerPOULBQ (staging), then checks each one's
    presence in the PROMOTED CustomerPOUL table via _fetch_from_customerpoul. A po_ref
    resolved here but missing from CustomerPOUL means it's stuck in staging (the AFTER
    INSERT trigger never promoted it) or was deleted — sync-inserted-orders/
    backfill-from-cloudsql both read CustomerPOUL, not the staging tables, so BC-only
    retrigger genuinely can't do anything for it; reported back as "missing" instead of
    silently skipped. Present refs are grouped by resolved BC company and sent as ONE
    sync + ONE backfill call per company (both already accept a comma-separated
    po_ref_numbers), not one call per PO.
    """
    data = request.get_json(silent=True) or {}
    file_name = (data.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400

    split = _resolve_file_refs_by_mssql_presence(file_name)
    if split is None:
        return jsonify({"error": f"No POs found in MSSQL (CustomerPOULBQ) for file {file_name!r}."}), 404
    present_refs_by_company, missing, company_unresolved = split

    body, status_code = _run_bc_only_for_refs_by_company(present_refs_by_company)
    if isinstance(body, dict):
        body["missing"] = missing
        body["company_unresolved"] = company_unresolved
    return jsonify(body), status_code


@bp.route("/api/handoff-history/retrigger-file", methods=["POST"])
def api_handoff_history_retrigger_file():
    """Single file-scoped retrigger action — splits this file's PO refs by whether
    each already has a row in CustomerPOUL and lets that decide the path per ref,
    instead of a person choosing between "Retrigger Handoff (cleanup & re-insert
    all)" and "Retrigger BC only": refs already in CustomerPOUL get the fast,
    synchronous BC-only check/create (_run_bc_only_for_refs_by_company, same as
    retrigger-bc-only-file); refs still missing get the full async cleanup+reinsert
    (POST /customerpoul/retrigger-file), scoped to just those refs via
    po_ref_numbers so already-good POs in the same file aren't redundantly re-fetched
    from BigQuery. The missing-side result is a run_id only — the caller polls
    GET /api/reprocess-status/<run_id> for it, same as the old whole-file retrigger.
    """
    data = request.get_json(silent=True) or {}
    file_name = (data.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400

    split = _resolve_file_refs_by_mssql_presence(file_name)
    if split is None:
        return jsonify({"error": f"No POs found in MSSQL (CustomerPOULBQ) for file {file_name!r}."}), 404
    present_refs_by_company, missing, company_unresolved = split

    bc_only_result = None
    if present_refs_by_company:
        bc_only_result, _bc_status_code = _run_bc_only_for_refs_by_company(present_refs_by_company)

    full_retrigger_run_id = None
    if missing:
        try:
            resp = _gcp_api("POST", "/customerpoul/retrigger-file",
                             params={"po_ref_numbers": ",".join(missing)}, timeout=15)
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                full_retrigger_run_id = body.get("run_id")
        except requests.RequestException as exc:
            return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502

    return jsonify({
        "bc_only_result": bc_only_result,
        "missing": missing,
        "company_unresolved": company_unresolved,
        "full_retrigger_run_id": full_retrigger_run_id,
    })


def _cleanup_and_reinsert_po(po_ref: str, header: dict, details: list, file_name: str | None):
    """Shared per-PO retrigger core — cleanup-staging then _insert_mssql_and_log —
    used by both the single-PO and batch retrigger routes below. Returns
    (response_body, status_code); response_body is always a dict."""
    try:
        cleanup_resp = _gcp_api("POST", "/customerpoul/cleanup-staging",
                                 params={"po_ref_number": po_ref}, timeout=30)
        cleanup_body, cleanup_status = _proxy_json(cleanup_resp)
        if cleanup_status != 200:
            detail = cleanup_body.get("detail") or cleanup_body.get("error") or f"HTTP {cleanup_status}"
            return {"error": f"Cleanup of staging rows failed, re-insert not attempted: {detail}"}, 502
    except requests.RequestException as exc:
        return {"error": f"Could not reach rgmc-gcp-api (cleanup): {exc}"}, 502

    body, status_code = _insert_mssql_and_log(header, details, po_ref, file_name)
    if isinstance(body, dict):
        body["cleanup"] = cleanup_body
        if status_code != 200:
            # Cleanup already committed by this point — the PO now has NO row in
            # CustomerPOULBQ/CustomerPOULDetailBQ at all (worse than the original
            # "not_promoted" state, though self-healing: a retry re-runs cleanup as a
            # harmless no-op and attempts a fresh insert). Said explicitly so this
            # doesn't read as an ordinary, already-recovered retry failure.
            body["error"] = (
                f"Staging rows were cleaned up, but the re-insert failed: "
                f"{body.get('message') or body.get('error') or body.get('detail') or f'HTTP {status_code}'}. "
                f"Retrigger again to retry the insert."
            )
    return body, status_code


def _retrigger_mssql_core(po_ref: str) -> tuple[dict, int]:
    """Full MSSQL-handoff retry for one PO, by po_ref_number alone — shared by both the
    Handoff History tab's own Retrigger button (a "not_promoted" record) and the
    Search tab's Check MSSQL result (either "not found in staging at all", or "in
    staging but never promoted"). Re-POSTing the same data straight to
    insert-from-bigquery would just hit a duplicate-key error on CustomerPOULBQ when a
    row is already sitting there from a prior attempt — the trigger only fires ON
    INSERT, never on a row that already exists — so this always cleans up first,
    harmlessly a no-op when nothing was there yet:

      1. Re-fetches this PO's current header/detail rows straight from BigQuery — the
         handoff-history record itself only ever stores a po_ref_number, never the
         actual rows.
      2. Deletes the stale CustomerPOULBQ/CustomerPOULDetailBQ rows via rgmc-gcp-api's
         POST /customerpoul/cleanup-staging (staging tables only — never the promoted
         CustomerPOUL/CustomerPOULDetail tables, which for a "not_promoted" record
         have no row for this PO yet anyway).
      3. Re-inserts fresh via the same insert-from-bigquery call + trigger-verification
         + history logging every other MSSQL handoff here uses (_insert_mssql_and_log),
         giving the trigger a brand-new INSERT to actually react to.

    On a successful insert, also fires the BC follow-up (fire-and-forget) so a fresh
    MSSQL row doesn't just sit there until someone separately checks BC.
    Returns (response_body, status_code).
    """
    try:
        search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                params={"po_ref_number": po_ref}, timeout=BQ_PROXY_TIMEOUT)
        search_body, search_status = _proxy_json(search_resp)
    except requests.RequestException as exc:
        return {"error": f"Could not reach rgmc-gcp-api: {exc}"}, 502
    if search_status != 200:
        return search_body, search_status
    headers = search_body.get("data") or []
    details = search_body.get("detail_data") or []
    if not headers:
        return {"error": f"No BigQuery int_document_ai row found for po_ref_number={po_ref!r} — nothing to re-insert."}, 404
    header = _merge_duplicate_headers(headers, details)[0]
    file_name = header.get("file_name")

    body, status_code = _cleanup_and_reinsert_po(po_ref, header, details, file_name)
    if isinstance(body, dict) and body.get("mssql_status") == "inserted":
        _trigger_bc_followup(header.get("company_name"), [po_ref])
    return body, status_code


@bp.route("/api/handoff-history/retrigger-mssql", methods=["POST"])
def api_handoff_history_retrigger_mssql():
    data = request.get_json(silent=True) or {}
    po_ref = (data.get("po_ref_number") or "").strip()
    if not po_ref:
        return jsonify({"error": "po_ref_number is required"}), 400
    body, status_code = _retrigger_mssql_core(po_ref)
    return jsonify(body), status_code


@bp.route("/api/handoff-history/retrigger", methods=["POST"])
def api_handoff_history_retrigger():
    """Single retrigger action for one PO — decides BC-only vs the full MSSQL+BC path
    itself (by checking whether CustomerPOUL already has this PO) instead of making
    the person using this tool choose between "Retrigger MSSQL" and "Retrigger BC
    only": a PO already in CustomerPOUL only needs the fast BC-only check/create
    (_retrigger_bc_only_core); one that isn't needs the full BigQuery refetch + MSSQL
    re-insert first (_retrigger_mssql_core, which already fires the BC follow-up once
    that succeeds). Response always carries "path" ("bc_only" or "mssql_full") so the
    UI can word its message correctly either way.
    """
    data = request.get_json(silent=True) or {}
    po_ref = (data.get("po_ref_number") or "").strip()
    if not po_ref:
        return jsonify({"error": "po_ref_number is required"}), 400

    header, _lines = _fetch_from_customerpoul(po_ref)
    if header:
        body, status_code = _retrigger_bc_only_core(po_ref)
        path = "bc_only"
    else:
        body, status_code = _retrigger_mssql_core(po_ref)
        path = "mssql_full"
    if isinstance(body, dict):
        body["path"] = path
    return jsonify(body), status_code


@bp.route("/api/handoff-history/retrigger-mssql-batch", methods=["POST"])
def api_handoff_history_retrigger_mssql_batch():
    """Same cleanup-then-reinsert retrigger as /api/handoff-history/retrigger-mssql,
    for every PO matching ONE of two selectors (exactly one must be given):

      po_ref_numbers — explicit list (comma/newline/whitespace separated)
      date_from/date_to — every PO with int_document_ai.created_at in this range
                           (inclusive), straight from /bigquery_routes/document-ai/search

    A file_name selector used to be a third option here, but a file-scoped batch can
    mean 100+ POs' worth of BigQuery lookups — confirmed live (2026-10-29) that this
    can exceed even a 150s HTTP request timeout. That case now goes through
    /api/handoff-history/retrigger-file-async (Pub/Sub -> rgmc-worker-pool, no request
    deadline) instead; see that route. po_ref_numbers/date_range batches here are
    small enough in practice that moving them too hasn't been needed.

    PO uploads only (group_code=customerpoul) — same BigQuery source table and
    insert-from-bigquery/cleanup-staging endpoints the single-PO route depends on,
    neither of which exist for RA or Online Sales PO.

    Resolves to a po_ref_number list first, then ONE document-ai/search call (it
    already accepts a comma-separated po_ref_number list) fetches every matching
    header/detail row in a single round trip — detail rows are then split per po_ref
    (search_document_ai's response mixes every matched PO's lines together) before
    each PO gets its own independent cleanup+reinsert, so one PO's failure never
    blocks the rest of the batch.
    """
    data = request.get_json(silent=True) or {}
    raw_refs = (data.get("po_ref_numbers") or "").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()
    selectors_given = sum(bool(x) for x in (raw_refs, (date_from or date_to)))
    if selectors_given != 1:
        return jsonify({"error": "Give exactly one of: po_ref_numbers or date_from/date_to"}), 400

    try:
        if raw_refs:
            po_refs = sorted(set(_split_po_refs(raw_refs)))
            search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                    params={"po_ref_number": ",".join(po_refs), "limit": 1000}, timeout=BQ_PROXY_TIMEOUT)
        else:
            search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                    params={"date_from": date_from, "date_to": date_to, "limit": 1000}, timeout=BQ_PROXY_TIMEOUT)
        search_body, search_status = _proxy_json(search_resp)
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
    if search_status != 200:
        return jsonify(search_body), search_status

    headers = search_body.get("data") or []
    details = search_body.get("detail_data") or []
    if not headers:
        return jsonify({"data": [], "total": 0, "ok_count": 0, "error_count": 0,
                         "message": "No matching BigQuery rows found — nothing to retrigger."})

    headers = _merge_duplicate_headers(headers, details)

    results = []
    successful_refs_by_company: dict = {}
    for header in headers:
        po_ref = header.get("po_ref_number")
        if not po_ref:
            continue
        po_details = [d for d in details if d.get("po_ref_number") == po_ref]
        body, status_code = _cleanup_and_reinsert_po(po_ref, header, po_details, header.get("file_name"))
        # mssql_status (not the raw HTTP status_code) is the authoritative signal —
        # insert-from-bigquery can return HTTP 200 with an internal {"status": "error"}
        # payload, which _insert_mssql_and_log now normalizes into mssql_status/error
        # (and status_code) consistently, but check mssql_status directly here anyway
        # rather than relying on that normalization alone. A successful insert whose
        # trigger never promoted the row is reported as its own "not_promoted" status,
        # not lumped in with either a clean success or an outright insert failure.
        inserted = body.get("mssql_status") == "inserted"
        trigger_status = body.get("trigger_status")
        if not inserted:
            po_status = "error"
        elif trigger_status == "not_promoted":
            po_status = "not_promoted"
        else:
            po_status = "success"
        results.append({
            "po_ref": po_ref, "status": po_status,
            "detail": body.get("error") or body.get("message") or (
                "Inserted and promoted." if trigger_status == "promoted" else
                "Inserted — trigger promotion status unknown." if trigger_status == "unknown" else
                "Re-inserted successfully."
            ),
            "trigger_status": trigger_status,
        })
        if po_status == "success":
            company = _resolve_bc_company(header.get("company_name"), header.get("company_id"))
            if company:
                successful_refs_by_company.setdefault(company, []).append(po_ref)

    for company, refs in successful_refs_by_company.items():
        _trigger_bc_followup(company, refs)

    return jsonify({
        "data": results, "total": len(results),
        "ok_count": sum(1 for r in results if r["status"] == "success"),
        "error_count": sum(1 for r in results if r["status"] in ("error", "not_promoted")),
    })


@bp.route("/api/handoff-history/retrigger-file-async", methods=["POST"])
def api_handoff_history_retrigger_file_async():
    """Starts an async, worker-pool-driven cleanup+reinsert retrigger for every PO
    under one BigQuery file_name — the file-scoped sibling of
    /api/handoff-history/retrigger-mssql-batch, moved off this app's (and
    rgmc-gcp-api's) own request path entirely. Confirmed live (2026-10-29) that a file
    with 100+ POs' worth of BigQuery lookups can take several minutes — far past any
    reasonable HTTP request deadline, even after raising it to 150s.

    Publishes a poul-so-retrigger-file message (via rgmc-gcp-api's POST
    /customerpoul/retrigger-file) and returns immediately with a run_id. The caller
    polls GET /api/reprocess-status/<run_id> (already generic — shared with Reprocess
    Buffer/Sync) for progress and the final per-PO results, in the finished doc's
    `summary.results` (same {po_ref, status, trigger_status, detail} shape
    /api/handoff-history/retrigger-mssql-batch already returns).
    """
    data = request.get_json(silent=True) or {}
    file_name = (data.get("file_name") or "").strip()
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400
    try:
        resp = _gcp_api("POST", "/customerpoul/retrigger-file", params={"file_name": file_name}, timeout=30)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
