"""Missing Ship-To tab: find SBIC-AI-created BC orders with a blank ship-to, queue/apply fixes (including bulk-apply from already-saved overrides)."""
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from core.config import _SBIC_AI_SUBMITTED_BY
from core.http_clients import _bc_api, _gcp_api, _proxy_json, _bc_proxy_get_json
from core.email_utils import _employee_notify_params
from core.overrides_data import _fetch_overrides_by_type, _match_branch_override, _match_customer_override

bp = Blueprint("shipto_fixes", __name__)


def _find_orders_by_customer(company: str, customer_no: str) -> list:
    """Sales Orders under `customer_no`, restricted to submittedBy == _SBIC_AI_SUBMITTED_BY.

    Queries the RGMC custom v2 Sales Order API (the same entity set rgmc-worker-pool
    creates SO-import orders against) rather than the older /bc/sales-orders — that's
    the one confirmed to actually carry the submittedBy field.
    """
    safe_customer = customer_no.replace("'", "''")
    odata_filter = f"sellToCustomerNo eq '{safe_customer}' and submittedBy eq '{_SBIC_AI_SUBMITTED_BY}'"
    resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={"company": company, "filter": odata_filter})
    body, status_code = _proxy_json(resp)
    if status_code != 200:
        raise RuntimeError(body.get("detail") or body.get("error") or f"BC returned {status_code}")
    return body.get("data") or []


# Open Sales Orders with a blank Ship-to Code — the worker's customer-resolution
# fallback (a flat customer override, or the denormalized header.resolvedCustomer
# patch) can leave shipToCode unset when no scoped override supplied one, which the
# Customers tab's Split action doesn't catch after the fact since it only acts on
# buffered POs, not orders already created in BC. Restricted to submittedBy ==
# _SBIC_AI_SUBMITTED_BY and status == Open for the same reason as _find_orders_by_customer.
_SHIPTO_MISSING_FILTER = f"shipToCode eq '' and status eq 'Open' and submittedBy eq '{_SBIC_AI_SUBMITTED_BY}'"


def _find_orders_missing_shipto(company: str) -> list:
    """Open Sales Orders with no Ship-to Code, restricted to submittedBy == _SBIC_AI_SUBMITTED_BY.

    Same entity set as _find_orders_by_customer — see its docstring.
    """
    resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={"company": company, "filter": _SHIPTO_MISSING_FILTER})
    body, status_code = _proxy_json(resp)
    if status_code != 200:
        raise RuntimeError(body.get("detail") or body.get("error") or f"BC returned {status_code}")
    return body.get("data") or []


@bp.route("/api/sales-orders/missing-shipto")
def api_sales_orders_missing_shipto():
    """Open BC Sales Orders with a blank Ship-to Code — surfaces SO-import orders that
    landed in BC without a ship-to address, for manual review/correction in BC. Read-only:
    nothing here ever changes an existing Sales Order. Restricted to
    submittedBy == "SBIC AI Uploading" and status == Open — see _find_orders_missing_shipto."""
    company = (request.args.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        orders = _find_orders_missing_shipto(company)
        return jsonify({"data": orders, "total": len(orders)})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/sales-orders/shipto-fixes", methods=["GET"])
def api_list_shipto_fixes():
    """Queued/attempted Ship-to Code fixes — lets the Missing Ship-To tab show which
    rows already have a fix queued (and its outcome, once an Apply run has tried it)
    without re-deriving that from the BC search itself."""
    params = {}
    company = (request.args.get("company") or "").strip()
    if company:
        params["company"] = company
    applied = (request.args.get("applied") or "").strip()
    if applied:
        params["applied"] = applied
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/shipto-fixes", params)


@bp.route("/api/sales-orders/shipto-fixes", methods=["POST"])
def api_queue_shipto_fix():
    """Queue a Ship-to Code fix for one already-created BC Sales Order — does NOT touch
    BC. Applying is a separate, explicit, batched step (POST /api/apply-shipto-fixes),
    same two-step shape as the Buffer tab's overrides + Reprocess Buffer: a saved link
    here is cheap and instant, writing to BC happens only on an explicit Apply trigger,
    processed by rgmc-worker-pool in batch.

    Re-queries Business Central fresh server-side via _find_orders_missing_shipto
    rather than trusting the client-supplied order_id for anything but WHICH result to
    act on, so the submittedBy/status/shipToCode-blank safety filter stays
    authoritative — an order_id that didn't come back from that fresh query (already
    fixed by someone else, or no longer Open) is rejected rather than silently queued.

    ship_to_customer_no (the ship-to address's OWN customerNumber in BC — every
    candidate shape in this app carries it) must match the order's existing
    sellToCustomerNo: BC's Ship-to Address table is scoped per customer, so patching in
    a ship-to that belongs to a DIFFERENT customer than the order's own Sell-to
    Customer No. is guaranteed to fail at Apply time with a 404
    Internal_InvalidTableRelation — rejected here instead, immediately, with a clear
    reason, rather than silently queuing something that can never actually apply. This
    mismatch is real: BC sometimes splits one raw customer name across more than one
    Customer record (e.g. ME001/ME002 both "METRO RETAIL STORES GROUP, INC."), so a
    branch/customer override saved against one of them can still fuzzy- or
    override-match a ship-to that belongs to the OTHER.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = (data.get("order_id") or "").strip()
    ship_to_code = (data.get("ship_to_code") or "").strip()
    ship_to_name = (data.get("ship_to_name") or "").strip()
    ship_to_customer_no = (data.get("ship_to_customer_no") or "").strip()
    source = (data.get("source") or "").strip()

    if not company:
        return jsonify({"error": "company is required"}), 400
    if not order_id:
        return jsonify({"error": "order_id is required"}), 400
    if not ship_to_code:
        return jsonify({"error": "ship_to_code is required"}), 400

    try:
        orders = _find_orders_missing_shipto(company)
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": f"Could not search Business Central: {exc}"}), 502

    order = next((o for o in orders if o.get("id") == order_id), None)
    if not order:
        return jsonify({
            "error": "This order no longer matches (it may already have a Ship-to Code, a "
                     "different status, or have changed since the lookup ran) — refresh and try again."
        }), 409

    order_customer_no = (order.get("sellToCustomerNo") or "").strip()
    if ship_to_customer_no and ship_to_customer_no.upper() != order_customer_no.upper():
        return jsonify({
            "error": f"Ship-to {ship_to_code} belongs to customer {ship_to_customer_no}, but this order's "
                     f"Sell-to Customer No. is {order_customer_no!r} — Business Central will reject this "
                     "(Ship-to Address is scoped per customer). Pick a ship-to that belongs to this order's "
                     "own customer instead."
        }), 409

    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/shipto-fixes", json={
            "company": company,
            "order_id": order_id,
            "order_no": order.get("number") or "",
            "po_ref": order.get("externalDocumentNo") or "",
            "ship_to_code": ship_to_code,
            "ship_to_name": ship_to_name,
            "ship_to_customer_no": ship_to_customer_no,
            "resolved_by": (data.get("resolved_by") or "").strip(),
            "source": source,
        })
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/shipto-fixes/<fix_id>", methods=["DELETE"])
def api_cancel_shipto_fix(fix_id):
    """Cancel a queued Ship-to Code fix before an Apply run has picked it up."""
    try:
        resp = _bc_api("DELETE", f"/bc/custom/v2/so-buffer/shipto-fixes/{fix_id}")
        if resp.status_code == 204:
            return "", 204
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/shipto-fix-history")
def api_shipto_fix_history():
    """Search the append-only Ship-to Code fix attempt history — every Apply Ship-To
    Fixes attempt (applied/skipped/failed), by PO ref or order number, so a human can
    find what was actually applied to a PO and when, to track down and correct a wrong
    link after the fact. All filters optional; omit everything to list the most recent
    attempts across every company."""
    params = {}
    for key in ("po_ref", "order_no", "company", "outcome"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/shipto-fix-history", params)


@bp.route("/api/apply-shipto-fixes", methods=["POST"])
def api_apply_shipto_fixes():
    """Trigger the same async pipeline as Reprocess Buffer (rgmc-gcp-api -> Pub/Sub ->
    rgmc-worker-pool), but for queued Ship-to Code fixes instead of buffered orders:
    the worker pulls every pending fix for the given company, re-validates each against
    BC fresh, and PATCHes shipToCode — see rgmc-worker-pool's
    _run_shipto_fixes_batch. Same run_id/status-polling shape as /api/reprocess
    (GET /api/reprocess-status/<run_id>, already generic across job types).
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
        resp = _gcp_api("POST", "/customerpoul/apply-shipto-fixes", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


def _fetch_customerpoul_branch_info(po_ref: str) -> tuple:
    """(customerName, customerBranchName) from CustomerPOUL's latest row for this PO
    ref, or (None, None) if not found / the lookup failed — header only, no detail
    lines, since api_apply_known_overrides calls this once per candidate order and
    doesn't need the lines."""
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}")
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return None, None
    rows = body.get("data") or []
    if status_code != 200 or not rows:
        return None, None
    row = rows[0]
    return row.get("customerName"), row.get("customerBranchName")


@bp.route("/api/sales-orders/apply-known-overrides", methods=["POST"])
def api_apply_known_overrides():
    """Bulk-queue a Ship-to Code fix for every Missing-Ship-To order (company-wide —
    not just whatever page of results the browser currently has rendered) that already
    has a saved branch/customer override covering its branch: an already-vetted human
    decision, reused here the same way rgmc-worker-pool already reuses it during a
    normal reprocess — just applied to orders already sitting in BC instead of the
    Firestore buffer. Orders with no matching override are left completely untouched,
    for manual review via each row's own Link Ship-To….

    Only QUEUES — never writes to BC directly. Apply Ship-To Fixes is still the one
    real write, same as a manually-picked candidate; this just saves the trouble of
    clicking Link Ship-To… one-by-one for every order whose answer is already known.

    Branch/customer overrides are each fetched ONCE (not per-order) and every
    candidate's CustomerPOUL branch lookup runs concurrently — see
    _fetch_overrides_by_type/_fetch_customerpoul_branch_info — since this can cover
    hundreds of orders in one call.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    resolved_by = (data.get("resolved_by") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400

    try:
        orders = _find_orders_missing_shipto(company)
    except (requests.RequestException, RuntimeError) as exc:
        return jsonify({"error": f"Could not search Business Central: {exc}"}), 502

    if not orders:
        return jsonify({"data": [], "total": 0, "queued": 0, "already_queued": 0, "no_override_match": 0})

    try:
        existing_resp = _bc_api("GET", "/bc/custom/v2/so-buffer/shipto-fixes", params={"company": company, "applied": "false"})
        existing_body, existing_status = _proxy_json(existing_resp)
        already_queued_ids = {f.get("order_id") for f in (existing_body.get("data") or [])} if existing_status == 200 else set()
    except requests.RequestException:
        already_queued_ids = set()

    branch_overrides = _fetch_overrides_by_type("branch")
    customer_overrides_by_key = {(ov.get("key") or "").strip().upper(): ov for ov in _fetch_overrides_by_type("customer")}
    candidates = [o for o in orders if o.get("id") not in already_queued_ids]

    def _resolve_one(order):
        po_ref = order.get("externalDocumentNo") or ""
        customer_name, branch_name = _fetch_customerpoul_branch_info(po_ref) if po_ref else (None, None)
        match = _match_branch_override(branch_overrides, branch_name)
        if not match:
            match = _match_customer_override(customer_overrides_by_key, customer_name, branch_name, po_ref)
        if not match:
            return order, None, None
        # BC's Ship-to Address table is scoped per customer — an override's ship-to is
        # only valid for THIS order if it belongs to the same customer BC already has on
        # the order's own header. BC sometimes splits one raw customer name across more
        # than one Customer record (e.g. ME001/ME002 both "METRO RETAIL STORES GROUP,
        # INC."), so a branch/customer override saved against one of them can still
        # match a branch name that another order — resolved to the OTHER record at
        # import time — also shares. Applying it anyway would 404 at Apply time
        # (Internal_InvalidTableRelation) — rejected here instead, before it's ever
        # queued.
        order_customer_no = (order.get("sellToCustomerNo") or "").strip().upper()
        match_customer_no = (match.get("customerNumber") or "").strip().upper()
        if match_customer_no and order_customer_no and match_customer_no != order_customer_no:
            return order, None, f"Matched override belongs to customer {match['customerNumber']}, not this order's {order.get('sellToCustomerNo')!r}"
        return order, match, None

    results = []
    mismatches = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(20, len(candidates))) as ex:
            resolved_triples = list(ex.map(_resolve_one, candidates))
        for order, match, mismatch_reason in resolved_triples:
            if mismatch_reason:
                mismatches.append({"number": order.get("number"), "reason": mismatch_reason})
                continue
            if not match:
                continue
            try:
                queue_resp = _bc_api("POST", "/bc/custom/v2/so-buffer/shipto-fixes", json={
                    "company": company,
                    "order_id": order.get("id"),
                    "order_no": order.get("number") or "",
                    "po_ref": order.get("externalDocumentNo") or "",
                    "ship_to_code": match["code"],
                    "ship_to_name": match.get("name") or "",
                    "ship_to_customer_no": match.get("customerNumber") or "",
                    "resolved_by": resolved_by or "Apply Known Overrides",
                    "source": match.get("source") or "saved override",
                })
                qbody, qstatus = _proxy_json(queue_resp)
                if qstatus in (200, 201):
                    results.append({"number": order.get("number"), "ship_to_code": match["code"], "ok": True})
                else:
                    results.append({
                        "number": order.get("number"), "ok": False,
                        "detail": str(qbody.get("detail") or qbody.get("error") or f"BC returned {qstatus}")[:300],
                    })
            except requests.RequestException as exc:
                results.append({"number": order.get("number"), "ok": False, "detail": f"Request failed: {exc}"})

    queued = sum(1 for r in results if r["ok"])
    return jsonify({
        "data": results,
        "mismatches": mismatches,
        "total": len(orders),
        "already_queued": len(already_queued_ids),
        "queued": queued,
        "customer_mismatch": len(mismatches),
        "no_override_match": len(candidates) - len(results) - len(mismatches),
    })
