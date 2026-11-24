"""Customer/Ship-To Mismatch tab and the Overrides tab's bulk customer-correction action."""
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify, current_app
from core.config import BQ_PROXY_TIMEOUT, _SBIC_AI_SUBMITTED_BY
from core.http_clients import _bc_api, _gcp_api, _proxy_json, _bc_proxy_get_json
from core.mssql import _split_po_refs
from core.email_utils import _html_table, send_email
from routes.shipto_fixes import _find_orders_by_customer

bp = Blueprint("customer_shipto", __name__)


def _find_orders_for_customer_shipto_mismatch(
    company: str, po_ref_number: str, date_from: str, date_to: str, customer_name: str, ship_to_search: str,
) -> list:
    """Open BC Sales Orders (submittedBy == "SBIC AI Uploading") for manual review/
    correction of a wrong Sell-to Customer and/or Ship-to Code — the Customer/Ship-To
    Mismatch tab's search. Unlike _find_orders_missing_shipto, this is NOT restricted to
    a blank Ship-to Code: the point here is an order whose customer/ship-to is already
    SET but wrong, which a human has to spot visually, not something this search can
    auto-detect (there's no "correct" value to compare against). status eq 'Open' is
    still enforced (same as every other sales-order fix tab) since BC rejects a PATCH to
    a Released order's header.

    customer_name filters by contains(sellToCustomerName, ...) directly (the field
    already lives on the order row). ship_to_search is a two-step filter: first finds
    every BC ship-to address whose name contains the term, then ORs the order search by
    THOSE shipToCode values — sales order rows only carry a bare shipToCode, not a
    readable address name, so there's nothing on the order itself to substring-match
    against directly.
    """
    conditions = [f"submittedBy eq '{_SBIC_AI_SUBMITTED_BY}'", "status eq 'Open'"]
    if po_ref_number:
        refs = _split_po_refs(po_ref_number)
        if len(refs) == 1:
            conditions.append(f"externalDocumentNo eq '{refs[0]}'")
        elif refs:
            conditions.append("(" + " or ".join(f"externalDocumentNo eq '{r}'" for r in refs) + ")")
    if date_from:
        conditions.append(f"orderDate ge {date_from}")
    if date_to:
        conditions.append(f"orderDate le {date_to}")
    if customer_name:
        # Not tolower() -- see _multi_field_contains_search's docstring: BC's custom API
        # pages return zero rows through tolower(), and RGMC/SBIC data entry is all caps
        # anyway, so uppercasing the search term reproduces case-insensitive matching.
        esc = customer_name.replace("'", "''").upper()
        conditions.append(f"contains(sellToCustomerName,'{esc}')")
    if ship_to_search:
        esc = ship_to_search.replace("'", "''").upper()
        resp = _bc_api("GET", "/bc/custom/v2/ship-to-addresses", params={
            "company": company, "filter": f"contains(name,'{esc}')",
        })
        body, status_code = _proxy_json(resp)
        if status_code != 200:
            raise RuntimeError(body.get("detail") or body.get("error") or f"BC returned {status_code} searching ship-to addresses")
        codes = sorted({(r.get("code") or "").strip() for r in (body.get("data") or []) if r.get("code")})
        if not codes:
            return []  # no BC ship-to address matched this text at all -- nothing to OR against
        esc_codes = [c.replace("'", "''") for c in codes]
        conditions.append("(" + " or ".join(f"shipToCode eq '{c}'" for c in esc_codes) + ")")

    odata_filter = " and ".join(conditions)
    resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={"company": company, "filter": odata_filter})
    body, status_code = _proxy_json(resp)
    if status_code != 200:
        raise RuntimeError(body.get("detail") or body.get("error") or f"BC returned {status_code}")
    return body.get("data") or []


def _fetch_bq_customer_branch_names(po_refs: list) -> dict:
    """po_ref -> BigQuery int_document_ai's customer_branch_name (the raw Document-AI-
    extracted branch text, as originally uploaded) — shown alongside each Customer/
    Ship-To Mismatch result so a human has the ORIGINAL branch text to compare against
    BC's current (possibly wrong) ship-to, without leaving the page. Batched via the
    same document-ai/search IN UNNEST(...) endpoint _fetch_expected_lines_batch already
    uses for the Missing Items tab, chunked by 100 refs per call.

    Rows come back ordered by created_at DESC (search_document_ai's own ORDER BY), so
    the first row seen per po_ref is the most recent extraction — same "latest wins"
    convention _fetch_customerpoul_branch_info's Cloud SQL equivalent uses, just sourced
    from BigQuery directly instead of the promoted CustomerPOUL table, per this tab's
    own ask (BigQuery, not Cloud SQL).
    """
    unique_refs = sorted({r for r in po_refs if r})
    if not unique_refs:
        return {}
    chunks = [unique_refs[i:i + 100] for i in range(0, len(unique_refs), 100)]

    def _fetch_chunk(chunk):
        try:
            resp = _gcp_api("GET", "/bigquery_routes/document-ai/search", params={
                "po_ref_number": ",".join(chunk),
            }, timeout=BQ_PROXY_TIMEOUT)
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                return body.get("data") or []
        except requests.RequestException:
            pass
        return []

    by_ref: dict = {}
    with ThreadPoolExecutor(max_workers=min(5, len(chunks))) as ex:
        for rows in ex.map(_fetch_chunk, chunks):
            for r in rows:
                ref = r.get("po_ref_number")
                if ref and ref not in by_ref:
                    by_ref[ref] = r.get("customer_branch_name") or ""
    return by_ref


@bp.route("/api/sales-orders/customer-shipto-mismatch")
def api_sales_orders_customer_shipto_mismatch():
    """Open BC Sales Orders (submittedBy == "SBIC AI Uploading") for the Customer/Ship-To
    Mismatch tab — see _find_orders_for_customer_shipto_mismatch. Read-only: nothing here
    ever changes an existing Sales Order. Each order is enriched with bqCustomerBranchName
    (see _fetch_bq_customer_branch_names) so the UI can show the original BigQuery-
    extracted branch alongside BC's current customer/ship-to."""
    company = (request.args.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    po_ref_number = (request.args.get("po_ref_number") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    customer_name = (request.args.get("customer_name") or "").strip()
    ship_to_search = (request.args.get("ship_to_search") or "").strip()
    try:
        orders = _find_orders_for_customer_shipto_mismatch(
            company, po_ref_number, date_from, date_to, customer_name, ship_to_search,
        )
        branch_by_ref = _fetch_bq_customer_branch_names([o.get("externalDocumentNo") for o in orders])
        for o in orders:
            o["bqCustomerBranchName"] = branch_by_ref.get(o.get("externalDocumentNo") or "", "")
        return jsonify({"data": orders, "total": len(orders)})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/sales-orders/customer-shipto-fixes", methods=["GET"])
def api_list_customer_shipto_fixes():
    """Queued/attempted customer+ship-to fixes — lets the Customer/Ship-To Mismatch tab
    show which rows already have a fix queued without re-deriving that from the BC
    search itself."""
    params = {}
    company = (request.args.get("company") or "").strip()
    if company:
        params["company"] = company
    applied = request.args.get("applied")
    if applied is not None:
        params["applied"] = applied
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/customer-shipto-fixes", params)


@bp.route("/api/sales-orders/customer-shipto-fixes", methods=["POST"])
def api_queue_customer_shipto_fix():
    """Queue one customer+ship-to correction — picking a ship-to address (whether it
    belongs to the order's current customer or a different one) only ever writes here.
    Apply Customer/Ship-To Fixes is the one real write to BC, same two-step shape as
    every other link-then-apply flow in this app.

    Re-queries Business Central fresh server-side via _find_orders_for_customer_shipto_mismatch
    rather than trusting the client-supplied order_id for anything but WHICH result to
    act on, same safety principle api_queue_shipto_fix already uses -- an order_id that
    didn't come back from that fresh query (already fixed by someone else, no longer
    Open, or no longer submittedBy "SBIC AI Uploading") is rejected rather than silently
    queued. Unlike api_queue_shipto_fix, a ship-to belonging to a DIFFERENT customer than
    the order's current one is explicitly ALLOWED here -- that's this tab's whole reason
    to exist.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = (data.get("order_id") or "").strip()
    new_ship_to_code = (data.get("new_ship_to_code") or "").strip()
    new_ship_to_name = (data.get("new_ship_to_name") or "").strip()
    new_customer_no = (data.get("new_customer_no") or "").strip()
    new_customer_name = (data.get("new_customer_name") or "").strip()
    source = (data.get("source") or "").strip()

    if not company:
        return jsonify({"error": "company is required"}), 400
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    if not new_ship_to_code:
        return jsonify({"error": "new_ship_to_code is required"}), 400
    if not new_customer_no:
        return jsonify({"error": "new_customer_no is required"}), 400

    try:
        orders = _find_orders_for_customer_shipto_mismatch(company, "", "", "", "", "")
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": f"Could not search Business Central: {exc}"}), 502

    order = next((o for o in orders if o.get("id") == order_id), None)
    if not order:
        return jsonify({
            "error": "This order no longer matches (it may have changed status, company, or submittedBy "
                     "since the lookup ran) — refresh and try again."
        }), 409

    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/customer-shipto-fixes", json={
            "company": company,
            "order_id": order_id,
            "order_no": order.get("number") or "",
            "po_ref": order.get("externalDocumentNo") or "",
            "current_customer_no": order.get("sellToCustomerNo") or "",
            "current_customer_name": order.get("sellToCustomerName") or "",
            "current_ship_to_code": order.get("shipToCode") or "",
            "new_customer_no": new_customer_no,
            "new_customer_name": new_customer_name,
            "new_ship_to_code": new_ship_to_code,
            "new_ship_to_name": new_ship_to_name,
            "resolved_by": (data.get("resolved_by") or "").strip(),
            "source": source,
        })
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/customer-shipto-fixes/<fix_id>", methods=["DELETE"])
def api_cancel_customer_shipto_fix(fix_id):
    """Cancel a queued customer/ship-to fix before an Apply run has picked it up."""
    try:
        resp = _bc_api("DELETE", f"/bc/custom/v2/so-buffer/customer-shipto-fixes/{fix_id}")
        if resp.status_code == 204:
            return "", 204
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/customer-shipto-fix-history")
def api_customer_shipto_fix_history():
    """Search the append-only customer/ship-to fix attempt history — every Apply run's
    outcome (applied/already_present/failed), by PO ref or order number."""
    params = {}
    for key in ("po_ref", "order_no", "company", "outcome"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/customer-shipto-fix-history", params)


@bp.route("/api/apply-customer-shipto-fixes", methods=["POST"])
def api_apply_customer_shipto_fixes():
    """Apply every pending queued customer/ship-to fix for a company — synchronous (like
    Apply Missing Item Fixes, unlike Apply Ship-To Fixes): re-pointing a header's
    Sell-to Customer No. and/or Ship-to Code is one existing BC PATCH call, re-validated
    fresh against BC immediately before each write, so no Pub/Sub + rgmc-worker-pool
    round-trip is needed. Optionally scoped to specific order_ids."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    payload = {"company": company, "applied_by": data.get("applied_by") or ""}
    if data.get("order_ids"):
        payload["order_ids"] = data["order_ids"]
    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/customer-shipto-fixes/apply", json=payload, timeout=90)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders")
def api_sales_orders():
    """Look up existing BC Sales Orders by customer number — used by the Overrides
    tab's "Find previous orders" action to surface orders already created under a
    branch/customer link's OLD value, for manual review/correction in BC. Read-only:
    nothing here ever changes an existing Sales Order. Restricted to
    submittedBy == "SBIC AI Uploading" — see _find_orders_by_customer."""
    company = (request.args.get("company") or "").strip()
    customer_no = (request.args.get("customer_no") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    if not customer_no:
        return jsonify({"error": "customer_no is required"}), 400
    try:
        orders = _find_orders_by_customer(company, customer_no)
        return jsonify({"data": orders, "total": len(orders)})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


def _create_followup_override(old_customer_no: str, new_resolved: dict, override_key: str,
                               override_type: str, failed_results: list, requested_by: str) -> None:
    """When a batch correction only partially succeeds, auto-create ONE override entry
    tracking the still-stuck orders — reusing the existing overrides collection/endpoint
    (so it shows up in the Overrides tab like any other saved link) rather than letting
    that information disappear once the result email is read.

    Keyed with descriptive text that will never match any real PO's customerName/
    customerBranchName, so it's purely a human-facing follow-up marker — it can never
    accidentally get "consumed" by the worker's own resolution logic. "Remove link" on
    it once the stuck orders are sorted out directly in BC.

    Hardcoded to type="customer" regardless of whether the original override that
    triggered this was branch or customer — both resolve to a customerNo, and this is
    fundamentally tracking a customer-level correction that didn't fully land.
    """
    new_customer_no = new_resolved.get("customerNo", "—")
    new_customer_desc = new_customer_no
    if new_resolved.get("shipToCode"):
        new_customer_desc += f" (ship-to {new_resolved['shipToCode']})"
    key = f'NEEDS MANUAL CORRECTION — {old_customer_no} still stuck (from "{override_key}" update)'
    # Deliberately {customerNo, displayName} ONLY — no shipToCode key — to match the
    # "customer" type's shape exactly. reconcile.js's resolvedDisplay() branches on
    # resolved.shipToCode being present to mean a BRANCH-shaped override (expects a
    # .name field, not .displayName); including shipToCode here would make it take
    # that branch and render "undefined" for the name.
    resolved = {
        "customerNo": new_customer_no,
        "displayName": f"{len(failed_results)} order(s) still under {old_customer_no}, should be {new_customer_desc}",
        "stillStuckUnder": old_customer_no,
        "sourceOverrideKey": override_key,
        "sourceOverrideType": override_type,
        "stuckOrders": [{"number": r.get("number"), "detail": r.get("detail")} for r in failed_results],
    }
    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/overrides", json={
            "type": "customer",
            "key": key,
            "resolved": resolved,
            "resolved_by": f"Auto-tracked after {requested_by or 'a'} Sales Order correction",
            "buffer_ids": [],
        })
        if resp.status_code not in (200, 201):
            current_app.logger.error("Follow-up override save returned %s: %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        current_app.logger.error("Could not create follow-up override for stuck orders: %s", exc)


def _sales_order_correction_email_body(company: str, old_customer_no: str, new_resolved: dict,
                                        results: list, requested_by: str, followup_created: bool) -> str:
    new_customer_desc = new_resolved.get("customerNo", "—")
    if new_resolved.get("shipToCode"):
        new_customer_desc += f" (ship-to {new_resolved['shipToCode']})"
    rows = [
        (r["number"] or "—", ("✅ Updated" if r["ok"] else f"❌ Failed — {r['detail']}"))
        for r in results
    ]
    table = _html_table(rows) if rows else "<p>No matching Sales Orders were found.</p>"
    ok_count = sum(1 for r in results if r["ok"])
    return f"""
    <html><body style='font-family:sans-serif;color:#1a1a2e'>
      <h2 style='color:#16213e'>🔁 Sales Order Customer Correction</h2>
      {_html_table([
          ("Company", company),
          ("Old customer", old_customer_no),
          ("New customer", new_customer_desc),
          ("Requested by", requested_by or "—"),
          ("Result", f"{ok_count} of {len(results)} order(s) updated"),
      ])}
      <h3 style='color:#16213e;margin-top:20px'>Per-order result</h3>
      {table}
      <p style='color:#888;font-size:12px;margin-top:24px'>
        Only the header (Sell-to Customer No. / Ship-to Code) was changed — line pricing and
        discounts were NOT recalculated and may still reflect the old customer. Review pricing
        on updated orders directly in Business Central. A Sales Order already Released is
        reported as a failure above; reopen it in BC before retrying.
      </p>
      {"<p style='color:#92400e;font-size:12px'><strong>A follow-up override was created</strong> in the Overrides tab tracking the order(s) above that are still stuck under the old customer — remove it once they're corrected in BC.</p>" if followup_created else ""}
      <p style='color:#888;font-size:12px'>SBIC Buffer Reconciliation</p>
    </body></html>
    """


@bp.route("/api/sales-orders/update-customer", methods=["POST"])
def api_update_sales_orders_customer():
    """Correct a user-picked subset of already-created BC Sales Orders still under a
    branch/customer override's OLD value — PATCHes only each selected order's header
    (Sell-to Customer No. / Ship-to Code), never line pricing/discounts (BC's own
    "update prices?" confirmation can't be answered over a headless API call, so
    existing line pricing is left as-is and flagged for manual review in the result
    email instead).

    Re-queries Business Central fresh server-side via _find_orders_by_customer rather
    than trusting the client-supplied order list for anything but WHICH of those
    results to act on — so the submittedBy=="SBIC AI Uploading" safety filter is always
    authoritative (an id that didn't come back from that fresh query is silently
    dropped, never PATCHed) even though the browser's confirmation modal is what
    actually picks the subset via its per-row checkboxes.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    old_customer_no = (data.get("old_customer_no") or "").strip()
    new_resolved = data.get("new_resolved") or {}
    new_customer_no = (new_resolved.get("customerNo") or "").strip()
    requested_by_email = (data.get("requested_by_email") or "").strip()
    requested_by_name = (data.get("requested_by_name") or "").strip()
    selected_ids = [s for s in (data.get("selected_ids") or []) if s]
    override_key = (data.get("override_key") or old_customer_no).strip()
    override_type = (data.get("override_type") or "customer").strip()

    if not company:
        return jsonify({"error": "company is required"}), 400
    if not old_customer_no:
        return jsonify({"error": "old_customer_no is required"}), 400
    if not new_customer_no:
        return jsonify({"error": "new_resolved.customerNo is required"}), 400
    if not selected_ids:
        return jsonify({"error": "selected_ids is required — select at least one order to update"}), 400

    try:
        orders = _find_orders_by_customer(company, old_customer_no)
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": f"Could not search Business Central: {exc}"}), 502

    selected_id_set = set(selected_ids)
    orders = [o for o in orders if o.get("id") in selected_id_set]
    if not orders:
        return jsonify({
            "error": "None of the selected orders matched Business Central on re-check "
                     "(they may have changed since the lookup ran) — try the lookup again."
        }), 409

    patch_body = {"sellToCustomerNo": new_customer_no}
    if new_resolved.get("shipToCode"):
        patch_body["shipToCode"] = new_resolved["shipToCode"]

    results = []
    for order in orders:
        order_id = order.get("id")
        number = order.get("number") or order.get("externalDocumentNo") or order_id
        try:
            resp = _bc_api(
                "PATCH", f"/bc/custom/v2/sales-orders/{order_id}",
                params={"company": company}, json=patch_body,
            )
            body, status_code = _proxy_json(resp)
            if status_code in (200, 201):
                results.append({"ok": True, "number": number, "detail": "Updated"})
            else:
                detail = body.get("detail") or body.get("error") or f"BC returned {status_code}"
                results.append({"ok": False, "number": number, "detail": str(detail)[:300]})
        except requests.RequestException as exc:
            results.append({"ok": False, "number": number, "detail": f"Request failed: {exc}"})

    updated = sum(1 for r in results if r["ok"])
    failed_results = [r for r in results if not r["ok"]]

    # Only the orders actually ATTEMPTED and rejected by BC count here — one the user
    # deliberately left unchecked in the modal was never attempted, so it's not a
    # "failure" needing a follow-up marker, just a choice the user already made.
    followup_created = False
    if failed_results:
        _create_followup_override(
            old_customer_no, new_resolved, override_key, override_type,
            failed_results, requested_by_name or requested_by_email,
        )
        followup_created = True

    if requested_by_email:
        subject = f"Sales Order customer correction — {company} ({updated}/{len(results)} updated)"
        html_body = _sales_order_correction_email_body(
            company, old_customer_no, new_resolved, results,
            requested_by_name or requested_by_email, followup_created,
        )
        send_email(subject, html_body, to_addr=requested_by_email)

    return jsonify({
        "data": results, "total": len(results), "updated": updated,
        "followup_created": followup_created,
    })
