"""BigQuery Lookup tab: Document-AI header/detail search, alignment reports, insert-into-buffer, and Quick Align."""
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify, render_template, url_for
from markupsafe import escape as _esc
from core.config import GCP_API_BASE, API_TIMEOUT, BQ_PROXY_TIMEOUT, BQ_COMPANY_NAME_TERMS, BQ_UI_ONLY_HEADER_FIELDS
from core.http_clients import _bc_api, _gcp_api, _proxy_json
from core.company import _resolve_bc_company, _company_name_matches, _merge_duplicate_headers
from core.mssql import _split_po_refs, _fetch_from_customerpoul

bp = Blueprint("document_ai_search", __name__)


def _check_mssql_exists(po_ref: str) -> bool | None:
    """True/False, or None if the check itself failed (unknown — never reported as
    "missing" on a mere network error, which would wrongly invite a duplicate insert)."""
    try:
        resp = requests.get(f"{GCP_API_BASE}/customerpoul", params={"po_ref_number": po_ref}, timeout=API_TIMEOUT)
        if resp.status_code != 200:
            return None
        return (resp.json().get("record_count") or 0) > 0
    except requests.RequestException:
        return None


def _enrich_document_ai_status(headers: list) -> None:
    """Attach in_mssql (bool|None) and in_bc (bool) to each header dict, in place."""
    po_refs = sorted({h.get("po_ref_number") for h in headers if h.get("po_ref_number")})
    if not po_refs:
        return

    in_mssql: dict = {}
    with ThreadPoolExecutor(max_workers=min(20, len(po_refs))) as ex:
        for po_ref, exists in zip(po_refs, ex.map(_check_mssql_exists, po_refs)):
            in_mssql[po_ref] = exists

    # One combined filter per BC company, OR-ing multiple VALUES of the same field
    # (externalDocumentNo) — fine; it's ORing across *distinct fields* that hits BC's
    # documented 501 limitation (see _multi_field_contains_search).
    in_bc = {ref: False for ref in po_refs}
    by_company: dict[str, list[str]] = {}
    for h in headers:
        ref = h.get("po_ref_number")
        company = _resolve_bc_company(h.get("company_name"), h.get("company_id"))
        if ref and company:
            by_company.setdefault(company, []).append(ref)

    for company, refs in by_company.items():
        try:
            safe_refs = [r.replace("'", "''") for r in set(refs)]
            odata_filter = " or ".join(f"externalDocumentNo eq '{r}'" for r in safe_refs)
            resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={"company": company, "filter": odata_filter})
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                for row in body.get("data", []):
                    ext = row.get("externalDocumentNo")
                    if ext in in_bc:
                        in_bc[ext] = True
        except requests.RequestException:
            pass  # best-effort — leave this company's refs as "not found" rather than failing the whole search

    for h in headers:
        ref = h.get("po_ref_number")
        h["in_mssql"] = in_mssql.get(ref)
        h["in_bc"] = in_bc.get(ref, False)


def _buffer_line_key(sku_code, sku_desc) -> str:
    """Same raw-SKU identity so_buffer_service._line_key uses on the rgmc-bc-api side
    (customerSKUCode, falling back to customerSKUDesc) — so a BigQuery detail line and
    an already-buffered line for the same item compare equal regardless of which side
    produced them."""
    sku = (sku_code or "").strip()
    return (sku or (sku_desc or "").strip() or "(no SKU code, no description)").upper()


def _enrich_buffer_status(headers: list) -> None:
    """Attach in_buffer (bool) and, when True, bufferedSkus (list[str]) to each header
    dict, in place — whether this PO is already sitting in rgmc-worker-pool's Firestore
    so_buffer_{env} (stuck pending manual reconciliation, or just not yet retried) and
    which of its lines are already represented there. Without this, a PO already
    queued for reconciliation reads the same as one Quick Align would be inserting
    fresh, when in fact _insert_and_buffer would just merge in whatever lines are
    still missing (see add_missing_lines_to_buffer) — see _bq_report_row's "not_in_bc"
    branch, which uses this to say so explicitly.

    One unfiltered GET /bc/custom/v2/so-buffer for the whole search (never per-PO or
    per-company) — list_buffered_orders's own docstring says the collection is small
    (tens of documents), so this is cheap regardless of how many headers were found.
    """
    if not headers:
        return
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer")
        body, status_code = _proxy_json(resp)
        docs = (body.get("data") or []) if status_code == 200 else []
    except requests.RequestException:
        docs = []

    by_po_ref: dict = {}
    for doc in docs:
        po_ref = (doc.get("header") or {}).get("poRefNumber")
        if po_ref:
            by_po_ref[po_ref] = doc

    for h in headers:
        doc = by_po_ref.get(h.get("po_ref_number"))
        h["in_buffer"] = doc is not None
        if doc is not None:
            h["bufferedSkus"] = sorted({
                _buffer_line_key(line.get("customerSKUCode"), line.get("customerSKUDesc"))
                for line in (doc.get("lines") or [])
            })


@bp.route("/api/bigquery/document-ai/search")
def api_bigquery_document_ai_search():
    params = {}
    for key in ("po_ref_number", "customer_name", "date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    # Not forwarded to rgmc-gcp-api — its BigQuery query has no companyName filter —
    # applied below instead, against company_name's free text, once the full result
    # set (BigQuery + any CustomerPOUL fallback) is assembled.
    company_filter = (request.args.get("company") or "").strip().upper()
    if company_filter and company_filter not in BQ_COMPANY_NAME_TERMS:
        return jsonify({
            "error": f"No known company-name terms for {company_filter!r} yet — add it to BQ_COMPANY_NAME_TERMS."
        }), 400
    try:
        # Two live BigQuery query executions happen server-side for one of these calls
        # (header, then detail) — slower and far more variable than every other call
        # this app makes, so it gets its own longer timeout instead of API_TIMEOUT's
        # 30s (already too short in practice — confirmed by a live read-timeout error).
        resp = _gcp_api("GET", "/bigquery_routes/document-ai/search", params=params, timeout=BQ_PROXY_TIMEOUT)
        body, status_code = _proxy_json(resp)
        if status_code != 200:
            return jsonify(body), status_code
        headers = body.get("data") or []
        details = body.get("detail_data") or []
        for h in headers:
            h["_source"] = "bigquery"

        # A PO searched for by exact ref that BigQuery doesn't have at all — e.g. one
        # manually encoded through SBIC's legacy app, which never goes through Document
        # AI/BigQuery — falls back to CustomerPOUL/CustomerPOULDetail instead of just
        # reporting "not found", so this one search box covers both sources without
        # the user needing to know or care which one actually has the data. Only makes
        # sense for an exact-ref search; customer_name/date searches have no PO ref to
        # fall back on and are left BigQuery-only.
        if params.get("po_ref_number"):
            requested_refs = _split_po_refs(params["po_ref_number"])
            found_refs = {h.get("po_ref_number") for h in headers}
            for po_ref in requested_refs:
                if po_ref in found_refs:
                    continue
                mssql_header, mssql_lines = _fetch_from_customerpoul(po_ref)
                if mssql_header:
                    mssql_header["_source"] = "mssql"
                    headers.append(mssql_header)
                    details.extend(mssql_lines)

        if company_filter:
            headers = [h for h in headers if _company_name_matches(h.get("company_name"), company_filter)]
            kept_refs = {h.get("po_ref_number") for h in headers}
            details = [d for d in details if d.get("po_ref_number") in kept_refs]

        _enrich_document_ai_status(headers)
        _enrich_buffer_status(headers)
        return jsonify({"headers": headers, "details": details})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


def _bq_lines_for_po(details: list, po_ref: str) -> list:
    """BigQuery int_document_ai_detail rows (snake_case) -> the camelCase shape a
    so_buffer_{env} doc's `lines` are expected to carry — same mapping
    _recover_lines_for_order already uses for BigQuery-sourced lines, extended with
    deliveryDate since a freshly-buffered order (not a line-recovery patch onto an
    existing one) needs it for header-level date fallbacks the worker reads."""
    return [{
        "customerSKUCode": d.get("customer_sku_code"),
        "customerSKUDesc": d.get("customer_sku_desc"),
        "poQty": d.get("po_qty"),
        "poQtyPcs": d.get("po_qty_pcs"),
        "unitOfMeasurement": d.get("unit_of_measurement"),
        "unitPrice": d.get("unit_price"),
        "unitPricePcs": d.get("unit_price_pcs"),
        "netPrice": d.get("net_price"),
        "deliveryDate": d.get("delivery_date"),
    } for d in details if d.get("po_ref_number") == po_ref]


def _bq_header_to_buffer_header(h: dict) -> dict:
    return {
        "poRefNumber": h.get("po_ref_number"),
        "customerName": h.get("customer_name"),
        "customerBranchName": h.get("customer_branch_name"),
        # No BigQuery equivalent — only ever populated for an MSSQL-sourced header
        # (_customerpoul_row_to_search_header); harmless None for a BigQuery one, same
        # as how the worker already reads this field with .get(...) or "".
        "customerBranchLookUpCode": h.get("customer_branch_lookup_code"),
        "companyName": h.get("company_name"),
        "poDate": h.get("po_date"),
        "deliveryDate": h.get("delivery_date"),
        "cancellationDate": h.get("cancellation_date"),
        "remark": h.get("remark"),
    }


def _fetch_existing_bc_order(company: str, po_ref: str) -> tuple:
    """(so_number, line_count) for a BC Sales Order already existing under this
    externalDocumentNo, or (None, 0) if none exists (or the check itself failed —
    treated the same as "not found" here, since this only gates an insert that's safe
    to attempt either way: a false negative just means a duplicate-looking order gets
    created, which the normal reconcile flow can still catch and merge by hand)."""
    try:
        safe_ref = po_ref.replace("'", "''")
        resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={
            "company": company,
            "filter": f"externalDocumentNo eq '{safe_ref}'",
            "expand": "salesOrderLines",
        })
        body, status_code = _proxy_json(resp)
        if status_code != 200:
            return None, 0
        rows = body.get("data") or []
        if not rows:
            return None, 0
        order = rows[0]
        return order.get("number"), len(order.get("salesOrderLines") or [])
    except requests.RequestException:
        return None, 0


def _b(value) -> str:
    """HTML-escaped value wrapped in <strong> — for the one important word/number in a
    bullet of _bq_report_row's action_steps, never the surrounding sentence (so a
    bullet reads as one normal sentence with just the key term bolded, not shouted)."""
    return f"<strong>{_esc(value)}</strong>"


def _plain(step: str) -> str:
    """action_steps's HTML stripped back to plain text — for callers that need a bare
    string (e.g. /api/bigquery/quick-align's "no action taken" detail, shown via
    escapeHtml() client-side, which would otherwise print the literal <strong> tags)."""
    return re.sub(r"</?strong>", "", step)


_BUFFER_FOLLOWUP_STEP = (
    f"If any SKU/branch/customer still can't be auto-matched, resolve it on the {_b('Buffer')} tab's "
    f"Items/Branches/Customers lists, then click {_b('Reprocess Buffer')} again."
)


def _bq_report_row(h: dict, details: list) -> dict:
    """One alignment-report row for a header already in the BigQuery Lookup search
    results — BigQuery is the source of truth (see its search route), so the
    recommendation always points at the tool that brings MSSQL/BC up to what BigQuery
    already has, never the other way around. Runs one live BC lookup per call (same
    _fetch_existing_bc_order used by the insert-and-buffer blocker) to get the real
    current line count, not just the in_bc flag the search enrichment already attached.

    action_steps is a list of HTML-safe bullet strings (bold only on the tab/button
    names and key values — see _b) for the report's "Steps to align" column. action is
    the same recommendation as one plain-text-ish sentence, kept for callers that just
    need a short string (e.g. /api/bigquery/quick-align's "no action taken" rows).
    """
    po_ref = h.get("po_ref_number") or ""
    company = _resolve_bc_company(h.get("company_name"), h.get("company_id"))
    total_lines = len(_bq_lines_for_po(details, po_ref))
    in_bigquery = h.get("_source") != "mssql"
    in_mssql = h.get("in_mssql")

    so_number, bc_line_count = (None, 0)
    if company:
        so_number, bc_line_count = _fetch_existing_bc_order(company, po_ref)

    if not company:
        status = "failed"
        action_steps = [
            f"{_b('Could not resolve a BC company')} from companyName \"{_esc(h.get('company_name'))}\".",
            "Check this PO manually.",
        ]
    elif so_number and bc_line_count >= total_lines:
        status = "aligned"
        action_steps = [f"{_b('Already aligned')} — BC order {_b(so_number)} has all {total_lines} line(s). No action needed."]
    elif so_number:
        status = "lines_missing"
        action_steps = [
            f"BC order {_b(so_number)} is missing lines ({bc_line_count}/{total_lines} present).",
            f"Go to the {_b('Cloud SQL')} tab, select {_b(company)}, and click {_b('Sync from Cloud SQL')} to backfill the rest.",
            _BUFFER_FOLLOWUP_STEP,
        ]
    elif in_mssql is None:
        status = "unknown"
        action_steps = [f"{_b('Could not confirm MSSQL status')} — re-run the search in BigQuery Lookup before acting on this row."]
    elif h.get("in_buffer"):
        # Already sitting in the Firestore buffer (a prior import attempt failed, or it
        # was added here before) — Quick Align's insert-and-buffer step would just
        # merge in whatever lines are still missing (add_missing_lines_to_buffer),
        # never a fresh insert, so say so instead of implying this is untouched.
        status = "not_in_bc"
        action_steps = [
            f"{_b('Not yet in BC')} — but already sitting in the {_b('Buffer')} tab, waiting on reconciliation or a retry.",
            f"Go to the {_b('Buffer')} tab, select {_b(company)}, resolve any flagged SKU/branch/customer/UOM links, then click {_b('Reprocess Buffer')}.",
        ]
    else:
        status = "not_in_bc"
        buffer_note = "buffers it directly, no MSSQL insert needed" if (not in_bigquery or in_mssql) else "inserts it into MSSQL and buffers it"
        action_steps = [
            f"{_b('Not yet in BC')}.",
            f"Go to the {_b('BigQuery Lookup')} tab, select this PO, and click {_b('Add Selected to Buffer')} ({buffer_note}).",
            f"Go to the {_b('Buffer')} tab, select {_b(company)}, and click {_b('Reprocess Buffer')}.",
            _BUFFER_FOLLOWUP_STEP,
        ]

    return {
        "po_ref_number": po_ref,
        "customer_name": h.get("customer_name"),
        "customer_branch_name": h.get("customer_branch_name"),
        "company_name": h.get("company_name"),
        "company": company,
        "po_date": h.get("po_date"),
        "in_bigquery": in_bigquery,
        "in_mssql": in_mssql,
        "in_bc": bool(so_number),
        "in_buffer": bool(h.get("in_buffer")),
        "so_number": so_number,
        "lines_present": bc_line_count if so_number else None,
        "lines_total": total_lines,
        "status": status,
        "action": " ".join(_plain(step) for step in action_steps),
        "action_steps": action_steps,
        "buffer_url": f"/reconcile?tab=buffer&company={company}" if company else None,
    }


def _build_bq_report_rows(headers: list, details: list, requested_refs: list) -> list:
    """One row per header found, plus one "not_found" row for any requested PO ref the
    search came back with nothing for at all (neither BigQuery nor its MSSQL fallback)."""
    rows = [_bq_report_row(h, details) for h in headers]
    found_refs = {h.get("po_ref_number") for h in headers}
    for ref in requested_refs:
        if ref in found_refs:
            continue
        not_found_steps = [
            f"{_b('Not found')} in BigQuery or MSSQL.",
            "Verify the PO ref number, or wait for Document AI to process it.",
        ]
        rows.append({
            "po_ref_number": ref, "customer_name": None, "customer_branch_name": None,
            "company_name": None, "company": None, "po_date": None,
            "in_bigquery": False, "in_mssql": False, "in_bc": False, "in_buffer": False,
            "so_number": None, "lines_present": None, "lines_total": None,
            "status": "not_found", "buffer_url": None,
            "action": " ".join(_plain(step) for step in not_found_steps),
            "action_steps": not_found_steps,
        })
    return rows


@bp.route("/api/bigquery/report", methods=["POST"])
def api_bigquery_report():
    """Build and save a shareable alignment report for the current BigQuery Lookup
    search results — one row per PO with its BigQuery/MSSQL/BC status and a plain
    recommended next step naming the exact tab/button on this page that fixes it.
    BigQuery is always treated as the source of truth (see _bq_report_row). Saved to
    rgmc-bc-api, including the raw headers/details (not just the computed rows), so
    the report page's own Quick Align buttons can act directly from the shared link —
    generating the report itself never applies anything to MSSQL/BC.
    """
    data = request.get_json(silent=True) or {}
    headers = data.get("headers") or []
    details = data.get("details") or []
    criteria = data.get("criteria") or {}
    employee = data.get("employee") or {}
    requested_refs = _split_po_refs(criteria.get("po_ref_number") or "")
    if not headers and not requested_refs:
        return jsonify({"error": "Nothing to report — run a search first."}), 400

    rows = _build_bq_report_rows(headers, details, requested_refs)
    generated_by = {
        "name": employee.get("employee_name") or "",
        "company": employee.get("employee_company") or "",
        "department": employee.get("employee_department") or "",
        "email": employee.get("email") or "",
    }
    try:
        resp = _bc_api("POST", "/bc/custom/v2/bq-lookup-reports", json={
            "criteria": criteria, "rows": rows, "generated_by": generated_by,
            "headers": headers, "details": details,
        })
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    body, status_code = _proxy_json(resp)
    if status_code != 201:
        detail = body.get("detail") or body.get("error") or f"rgmc-bc-api returned {status_code}"
        return jsonify({"error": detail}), 502

    report_id = body.get("id")
    # Built from the Host/X-Forwarded-Proto headers rather than url_for(_external=True) —
    # Cloud Run terminates TLS upstream and forwards plain HTTP to this container, so
    # Flask's own scheme guessing (with no ProxyFix configured) would emit "http://"
    # for a service that's actually HTTPS-only.
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    report_url = f"{scheme}://{request.host}{url_for('document_ai_search.report_page', report_id=report_id)}"
    return jsonify({"report_id": report_id, "url": report_url})


@bp.route("/report/<report_id>")
def report_page(report_id):
    """Standalone, read-only alignment report — no employee gate, since the whole
    point is a person-in-charge who has no reason to be logged into /reconcile can
    open this link directly."""
    try:
        resp = _bc_api("GET", f"/bc/custom/v2/bq-lookup-reports/{report_id}")
    except requests.RequestException:
        return render_template("report.html", report=None, error="Could not reach the report service. Try again later."), 502
    if resp.status_code == 404:
        return render_template("report.html", report=None, error="This report link doesn't exist or has expired."), 404
    if resp.status_code != 200:
        return render_template("report.html", report=None, error=f"Could not load this report (status {resp.status_code})."), 502
    return render_template("report.html", report=resp.json(), error=None)


def _insert_and_buffer(headers: list, details: list, employee: dict) -> dict:
    """Add caller-selected PO(s) to the SO-import buffer — source-aware per header via
    each one's "_source" tag (set by /api/bigquery/document-ai/search):

      "bigquery" (or untagged, for safety) — MSSQL is always inserted into first
        (CustomerPOULBQ/CustomerPOULDetailBQ — promoted into the clean CustomerPOUL/
        CustomerPOULDetail tables by BC's own AFTER INSERT trigger), regardless of
        whether the PO already has a BC Sales Order. Keeping MSSQL in sync with what
        BigQuery actually has is treated as independent of BC's state.

      "mssql" — found only in CustomerPOUL/CustomerPOULDetail (e.g. manually encoded
        through SBIC's legacy app, which never reaches BigQuery at all) — no MSSQL
        insert happens; there's nothing to insert, it's already there.

    BLOCKER (buffer only, never MSSQL) applies identically to both: a fresh check
    against BC by externalDocumentNo runs before anything is allowed near the
    Firestore buffer — if a Sales Order already exists there, buffering it too would
    be pointless (a future reprocess would just skip header creation and look it up
    again) and would leave a confusing duplicate-looking trail. Instead it's recorded
    straight to so_buffer_history_{env} (so it still shows up in /reconcile's History
    tab rather than silently vanishing), after also checking whether the EXISTING
    order's line count already covers what's available for it — if not, the history
    entry says so and points at "Sync from Cloud SQL" as the existing way to backfill
    missing lines. Everything else proceeds into the buffer exactly as before, never
    attempting an automatic BC import itself.

    If the PO isn't blocked by BC but is already sitting in the buffer under its own
    right (e.g. the worker already failed to import it), this never overwrites that
    doc — it merges in only the detail lines not already present there (status
    "merged" in the response), so attempt_count/last_error/resolved* already on it
    survive untouched. See add_missing_lines_to_buffer in rgmc-bc-api.

    Shared by both the /api/bigquery/insert-and-buffer route (caller-checked selection)
    and /api/bigquery/quick-align (the "not yet in BC" branch) — called directly, not
    over HTTP, so quick-align doesn't round-trip through this app's own routing.
    """
    triggered_by = {
        "name": employee.get("employee_name") or "",
        "company": employee.get("employee_company") or "",
        "department": employee.get("employee_department") or "",
        "email": employee.get("email") or "",
    } if employee else None

    bq_headers = _merge_duplicate_headers(
        [h for h in headers if h.get("_source") != "mssql"], details,
    )
    mssql_only_headers = [h for h in headers if h.get("_source") == "mssql"]
    results = []

    if bq_headers:
        bq_refs = {h.get("po_ref_number") for h in bq_headers}
        bq_details = [d for d in details if d.get("po_ref_number") in bq_refs]
        bq_headers_for_insert = [
            {k: v for k, v in h.items() if k not in BQ_UI_ONLY_HEADER_FIELDS} for h in bq_headers
        ]
        try:
            # Same reasoning as the search route above — an MSSQL insert (then
            # whatever the AFTER INSERT trigger does) is slower and less predictable
            # than this app's usual calls.
            resp = _gcp_api("POST", "/customerpoul/insert-from-bigquery",
                             json={"headers": bq_headers_for_insert, "details": bq_details}, timeout=90)
            body, status_code = _proxy_json(resp)
            if status_code != 200 or body.get("status") != "success":
                detail = body.get("message") or f"MSSQL insert failed ({status_code}): {body}"
                for h in bq_headers:
                    results.append({"po_ref": h.get("po_ref_number"), "status": "failed", "detail": detail})
                bq_headers = []  # don't also try to buffer ones whose MSSQL insert failed
        except requests.RequestException as exc:
            detail = f"Could not reach rgmc-gcp-api: {exc}"
            for h in bq_headers:
                results.append({"po_ref": h.get("po_ref_number"), "status": "failed", "detail": detail})
            bq_headers = []

    for h in bq_headers + mssql_only_headers:
        po_ref = h.get("po_ref_number")
        company = _resolve_bc_company(h.get("company_name"), h.get("company_id"))
        already_in_mssql = h.get("_source") == "mssql"
        if not company:
            results.append({
                "po_ref": po_ref, "status": "failed",
                "detail": f"Could not resolve a BC company from companyName={h.get('company_name')!r} companyId={h.get('company_id')!r} — not added to any buffer.",
            })
            continue

        so_number, bc_line_count = _fetch_existing_bc_order(company, po_ref)
        if so_number:
            bq_lines = _bq_lines_for_po(details, po_ref)
            lines_complete = bc_line_count >= len(bq_lines)
            outcome = "resolved" if lines_complete else "still_buffered"
            prefix = "Already in MSSQL. " if already_in_mssql else "Inserted into MSSQL. "
            note = (
                f"{prefix}Already exists in BC as {so_number} — not added to the buffer. "
                f"Lines: {bc_line_count}/{len(bq_lines)} present."
                if lines_complete else
                f"{prefix}Already exists in BC as {so_number} — not added to the buffer, but only "
                f"{bc_line_count}/{len(bq_lines)} lines are present. Use \"Sync from Cloud SQL\" to backfill the missing ones."
            )
            try:
                _bc_api("POST", "/bc/custom/v2/so-buffer/history/manual-entry", json={
                    "header": _bq_header_to_buffer_header(h), "lines": bq_lines, "company": company,
                    "outcome": outcome, "triggered_by": triggered_by, "so_number": so_number,
                    "detail": f"Blocked from the buffer by BigQuery-lookup insert — {note}",
                })
            except requests.RequestException:
                pass  # best-effort — the block itself still applies even if logging it fails
            results.append({"po_ref": po_ref, "status": "blocked", "detail": note})
            continue

        bq_lines = _bq_lines_for_po(details, po_ref)
        prefix = "Already in MSSQL" if already_in_mssql else "Inserted into MSSQL"

        # The PO may already be sitting in the buffer under its own right — e.g. the
        # worker already tried and failed to import it, and save_failed_order beat this
        # lookup to the same doc. Merging missing lines into that existing doc (never
        # overwriting it — see add_missing_lines_to_buffer) preserves attempt_count/
        # last_error/resolvedShipTo/resolvedCustomer/resolvedItem already on it; only a
        # PO with no existing buffer doc at all falls through to a fresh create below.
        try:
            merge_resp = _bc_api("POST", "/bc/custom/v2/so-buffer/merge-lines", json={
                "po_ref_number": po_ref, "lines": bq_lines,
            })
        except requests.RequestException as exc:
            results.append({"po_ref": po_ref, "status": "failed", "detail": f"Buffer merge check failed: {exc}"})
            continue

        if merge_resp.status_code == 200:
            merge_body, _ = _proxy_json(merge_resp)
            added = merge_body.get("lines_added", 0)
            detail = (
                f"{prefix}. Already in the {company} buffer — added {added} missing line(s)."
                if added else
                f"{prefix}. Already in the {company} buffer — all {len(bq_lines)} line(s) were already present, nothing added."
            )
            results.append({"po_ref": po_ref, "status": "merged", "detail": detail})
            continue
        elif merge_resp.status_code != 404:
            merge_body, _ = _proxy_json(merge_resp)
            detail = merge_body.get("detail") or merge_body.get("error") or f"BC-API returned {merge_resp.status_code}"
            results.append({"po_ref": po_ref, "status": "failed", "detail": f"Buffer merge check failed: {detail}"})
            continue
        # else 404 — not yet buffered at all, fall through to a fresh create.

        try:
            buf_resp = _bc_api("POST", "/bc/custom/v2/so-buffer/manual-entry", json={
                "header": _bq_header_to_buffer_header(h),
                "lines": bq_lines,
                "company": company,
                "src_company": f"{'manual-mssql-lookup' if already_in_mssql else 'manual-bq-lookup'}:{company}",
                "created_by": triggered_by.get("name") if triggered_by else "",
            })
            buf_body, buf_status = _proxy_json(buf_resp)
            if buf_status == 201:
                results.append({"po_ref": po_ref, "status": "inserted", "detail": f"{prefix} — added to the {company} buffer."})
            else:
                detail = buf_body.get("detail") or buf_body.get("error") or f"BC-API returned {buf_status}"
                results.append({"po_ref": po_ref, "status": "failed", "detail": f"Buffer create failed: {detail}"})
        except requests.RequestException as exc:
            results.append({"po_ref": po_ref, "status": "failed", "detail": f"Buffer create request failed: {exc}"})

    return {
        "data": results, "total": len(results),
        "ok_count": sum(1 for r in results if r["status"] in ("inserted", "merged")),
        "blocked_count": sum(1 for r in results if r["status"] == "blocked"),
    }


@bp.route("/api/bigquery/insert-and-buffer", methods=["POST"])
def api_bigquery_insert_and_buffer():
    data = request.get_json(silent=True) or {}
    headers = data.get("headers") or []
    details = data.get("details") or []
    employee = data.get("employee") or {}
    if not headers:
        return jsonify({"error": "headers must not be empty"}), 400
    return jsonify(_insert_and_buffer(headers, details, employee))


@bp.route("/api/bigquery/quick-align", methods=["POST"])
def api_bigquery_quick_align():
    """One-click alignment for one or more BigQuery Lookup rows. BigQuery is always
    the source of truth (see _bq_report_row's status logic) — this only ever brings
    MSSQL/BC up to what BigQuery already has, never the reverse, and every action
    here is an existing, already-vetted tool; nothing new talks to BC directly:

      status "not_in_bc"     -> insert into MSSQL (if needed) + add to the buffer
                                 (_insert_and_buffer, same as Add Selected to Buffer),
                                 then trigger Reprocess Buffer with every distinct
                                 company touched batched into one call (same tool as
                                 clicking it manually, just one companies=A,B,C call
                                 instead of one call per company).
      status "lines_missing" -> trigger Sync from Cloud SQL, scoped to exactly these
                                 PO refs via po_ref_numbers, with every company
                                 similarly batched into one call.
      anything else           -> reported back with no action taken (already aligned,
                                 or there's genuinely nothing safe to automate yet).

    Both triggers are each a single HTTP call regardless of how many companies are
    involved — /customerpoul/reprocess-buffer and /customerpoul/sync-inserted-orders
    are rate-limited per endpoint path (30s cooldown, not per company — see
    rgmc-gcp-api's rate_limiter.py), so looping per company here would 429 ("Endpoint
    called too recently") as soon as a Quick Align batch spanned 2+ companies. Both
    endpoints already accept a comma-separated companies list and publish one Pub/Sub
    message that rgmc-worker-pool fans out per company itself, so batching here just
    means one message doing in the worker pool what used to be several API calls.
    """
    data = request.get_json(silent=True) or {}
    headers = data.get("headers") or []
    details = data.get("details") or []
    employee = data.get("employee") or {}
    if not headers:
        return jsonify({"error": "headers must not be empty"}), 400

    notify_params = {
        "notify_name": employee.get("employee_name") or "",
        "notify_company": employee.get("employee_company") or "",
        "notify_department": employee.get("employee_department") or "",
        "notify_email": employee.get("email") or "",
    }
    rows = [_bq_report_row(h, details) for h in headers]
    po_ref_to_company = {r["po_ref_number"]: r["company"] for r in rows}
    results = []
    run_ids = []  # [{"company", "action", "run_id"}, ...] — so the caller can watch each
    # triggered reprocess/sync through to completion (same polling the Buffer tab's own
    # Reprocess/Sync buttons use) instead of guessing when it's safe to refresh.

    not_in_bc_headers = [h for h, r in zip(headers, rows) if r["status"] == "not_in_bc"]
    if not_in_bc_headers:
        buffer_outcome = _insert_and_buffer(not_in_bc_headers, details, employee)
        for r in buffer_outcome["data"]:
            results.append({
                "po_ref": r["po_ref"], "company": po_ref_to_company.get(r["po_ref"]),
                "action": "buffer", "status": r["status"], "detail": r["detail"],
            })

        companies_touched = sorted({
            _resolve_bc_company(h.get("company_name"), h.get("company_id")) for h in not_in_bc_headers
            if _resolve_bc_company(h.get("company_name"), h.get("company_id"))
        })
        if companies_touched:
            # One call covering every company touched, not one call per company —
            # /customerpoul/reprocess-buffer is rate-limited per endpoint PATH (30s
            # cooldown, see rgmc-gcp-api's rate_limiter.py), not per company, so a
            # per-company loop here 429s ("called too recently") as soon as a Quick
            # Align batch spans 2+ companies. "companies" already accepts a
            # comma-separated list and the worker pool fans it out per company itself
            # (so_import_worker.py's poul-so-reprocess-buffer handler), so one call —
            # and one Pub/Sub message — batches the whole run safely.
            try:
                resp = _gcp_api("POST", "/customerpoul/reprocess-buffer", params={
                    "companies": ",".join(companies_touched), **notify_params,
                })
                body, status_code = _proxy_json(resp)
                ok = status_code == 200
                detail = f"Reprocess Buffer triggered for {', '.join(companies_touched)}." if ok else (
                    f"Reprocess Buffer failed: {body.get('detail') or body}")
                if ok and body.get("run_id"):
                    for company in companies_touched:
                        run_ids.append({"company": company, "action": "reprocess", "run_id": body["run_id"]})
            except requests.RequestException as exc:
                ok, detail = False, f"Reprocess Buffer request failed: {exc}"
            for company in companies_touched:
                results.append({
                    "po_ref": None, "company": company,
                    "action": "reprocess", "status": "triggered" if ok else "failed", "detail": detail,
                })

    lines_missing_by_company: dict = {}
    for r in rows:
        if r["status"] == "lines_missing" and r["company"]:
            lines_missing_by_company.setdefault(r["company"], []).append(r["po_ref_number"])
    if lines_missing_by_company:
        # Same single-call batching as reprocess above, for the same reason —
        # /customerpoul/sync-inserted-orders shares the identical per-path rate
        # limiter. po_ref_numbers is a flat, cross-company filter that the worker
        # pool re-scopes to each company internally (_run_sync_from_cloudsql builds
        # company_headers by resolving each row's own companyName), so mixing refs
        # from different companies into one po_ref_numbers list is safe.
        all_companies = sorted(lines_missing_by_company)
        all_po_refs = [ref for refs in lines_missing_by_company.values() for ref in refs]
        try:
            resp = _gcp_api("POST", "/customerpoul/sync-inserted-orders", params={
                "companies": ",".join(all_companies), "po_ref_numbers": ",".join(all_po_refs), **notify_params,
            })
            body, status_code = _proxy_json(resp)
            ok = status_code == 200
            detail = f"Sync from Cloud SQL triggered for {len(all_po_refs)} PO(s) across {', '.join(all_companies)}." if ok else (
                f"Sync from Cloud SQL failed: {body.get('detail') or body}")
            if ok and body.get("run_id"):
                for company in all_companies:
                    run_ids.append({"company": company, "action": "sync", "run_id": body["run_id"]})
        except requests.RequestException as exc:
            ok, detail = False, f"Sync from Cloud SQL request failed: {exc}"
        for company, po_refs in lines_missing_by_company.items():
            for po_ref in po_refs:
                results.append({
                    "po_ref": po_ref, "company": company,
                    "action": "sync", "status": "triggered" if ok else "failed", "detail": detail,
                })

    for r in rows:
        if r["status"] not in ("not_in_bc", "lines_missing"):
            results.append({
                "po_ref": r["po_ref_number"], "company": r["company"],
                "action": "none", "status": r["status"], "detail": r["action"],
            })

    return jsonify({"data": results, "total": len(results), "run_ids": run_ids})
