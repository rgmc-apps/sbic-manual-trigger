"""Missing Items tab: compare each open BC Sales Order's current lines against Document-AI-expected lines, queue/apply item fixes."""
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from core.config import BQ_PROXY_TIMEOUT
from core.http_clients import _bc_api, _gcp_api, _proxy_json, _bc_proxy_get_json
from core.overrides_data import _fetch_overrides_by_type, _fetch_item_ref_map
from core.mssql import _split_po_refs

bp = Blueprint("missing_items", __name__)


def _build_missing_items_filter(po_ref_number: str, date_from: str, date_to: str) -> str:
    """OData $filter for GET /bc/custom/v2/sales-orders — Business Central's v2.0 custom
    API uses plain unquoted ISO date literals for Edm.Date fields (orderDate), the
    standard convention across Microsoft's BC APIs.

    Always scoped to status eq 'Open' — a Released order's lines are locked in BC, so a
    Transfer to BC attempt against one always fails. Confirmed live: an early version of
    this search had no status filter and surfaced Released POs whose "missing" items
    could never actually be added without first reopening the order in BC (out of scope
    here — reopening is a business decision, not something this tool should do silently).
    """
    conditions = ["status eq 'Open'"]
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
    return " and ".join(conditions)


def _sku_key_for_line(line: dict) -> str:
    """Grouping key for a Document-AI detail line — customerSKUCode if present, else a
    derived key from the description, same convention _group_buffer already uses for
    blank-SKU lines."""
    sku = (line.get("customerSKUCode") or "").strip()
    if sku:
        return sku
    desc = (line.get("customerSKUDesc") or "").strip()
    return f"__NOSKU__::{(desc or '(no SKU code, no description)').upper()}"


def _fetch_expected_lines_batch(po_refs: list) -> dict:
    """po_ref -> list of Document-AI-extracted lines (camelCase, same shape
    _recover_lines_for_order's BigQuery branch returns), for MANY POs in a handful of
    BigQuery calls instead of one call per PO.

    _find_missing_items originally called _recover_lines_for_order (Cloud SQL, then
    BigQuery fallback) once per matched BC order via a 20-worker thread pool — fine for
    the handful of orders the Buffer tab recovers lines for, but a broad company+date
    search here can match hundreds of orders with no per-order concurrency cap on the
    network itself, which is exactly what produced a real 504 in production (Cloud
    Run's own 300s request timeout, confirmed via Cloud Logging: the underlying BC
    search took 4s, the ~300 fanned-out recovery calls took the rest). Chunking into
    batched int_document_ai_detail queries (the same IN UNNEST(...) search
    search_document_ai already does for multiple po_refs in one call) turns that into
    O(refs/100) BigQuery calls total regardless of how many orders matched, at the cost
    of skipping the Cloud SQL-first preference — acceptable here since every PO in this
    list already exists in BC, meaning it already made it through BigQuery upstream of
    that, unlike the Buffer tab's buffered-and-failed orders this helper doesn't touch.

    Deduplicates per ref by _sku_key_for_line, the same composite key the BigQuery
    bridge itself already uses to decide a detail row is a duplicate (po_ref_number +
    customer_name + customer_sku_code, keep='first' — see mappings.required_columns in
    rgmc-gcp-api). Without this, a PO whose Document AI extraction genuinely has a line
    duplicated would hand _find_missing_items two "missing" candidates for the same SKU,
    which api_dashboard_queue_missing_items would then queue as two separate Sales Order
    lines for the same item on Transfer to BC.
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
                return body.get("detail_data") or []
        except requests.RequestException:
            pass
        return []

    by_ref: dict = {}
    seen_keys_by_ref: dict = {}
    with ThreadPoolExecutor(max_workers=min(5, len(chunks))) as ex:
        for rows in ex.map(_fetch_chunk, chunks):
            for r in rows:
                ref = r.get("po_ref_number")
                if not ref:
                    continue
                line = {
                    "customerSKUCode": r.get("customer_sku_code"),
                    "customerSKUDesc": r.get("customer_sku_desc"),
                    "poQty": r.get("po_qty"),
                    "poQtyPcs": r.get("po_qty_pcs"),
                    "unitOfMeasurement": r.get("unit_of_measurement"),
                    "unitPrice": r.get("unit_price"),
                    "netPrice": r.get("net_price"),
                }
                sku_key = _sku_key_for_line(line).upper()
                seen_keys = seen_keys_by_ref.setdefault(ref, set())
                if sku_key in seen_keys:
                    continue
                seen_keys.add(sku_key)
                by_ref.setdefault(ref, []).append(line)
    return by_ref


def _fetch_valid_uoms_batch(item_nos: set, company: str) -> dict:
    """item_no -> raw list of that item's valid BC Units of Measure ([{code, description,
    qtyPerUnitOfMeasure}, ...]), fetched once per UNIQUE item instead of once per missing
    line across every order — same N+1 avoidance as _fetch_expected_lines_batch, since
    many orders typically share the same handful of catalog items."""
    if not item_nos:
        return {}

    def _fetch_one(item_no):
        try:
            resp = _bc_api("GET", f"/bc/custom/v2/items/{item_no}/unit-of-measure", params={"company": company}, timeout=20)
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                return item_no, body.get("data") or []
        except requests.RequestException:
            pass
        return item_no, []

    result = {}
    with ThreadPoolExecutor(max_workers=min(10, len(item_nos))) as ex:
        for item_no, rows in ex.map(_fetch_one, item_nos):
            result[item_no] = rows
    return result


def _find_missing_items(company: str, po_ref_number: str, date_from: str, date_to: str) -> list:
    """For every BC Sales Order matching the given filters, compare its current lines
    against the Document-AI-extracted lines for the same PO (via _fetch_expected_lines_batch)
    and report any expected line with no matching current BC line.

    "Missing" determination (known limitation, accepted for v1 — see
    missing_items_plan.md): a Document-AI line counts as present unless a current BC
    line's `number` equals either (a) a saved SKU override's resolved itemNo for this
    line's key, or (b) the line's own raw customer_sku_code (handles the common case
    where the customer's SKU code literally is the BC item number). This does not
    replicate rgmc-worker-pool's BC item-reference ("ref_map") matching from automated
    import — a line BC resolved that way, but never saved as our own override, shows
    here as "needs linking" even though BC could already auto-resolve it on its own.
    Harmless false positive (a redundant link), not a false negative (never hides a
    line that's genuinely missing).

    A resolved line also needs a BC-valid Unit of Measure before it can actually be
    added — confirmed live: Transfer to BC failing with a UOM rejection for items whose
    Document-AI-extracted unit code (e.g. "CS") isn't one of that item's own valid units
    in BC (same class of issue as the Buffer tab's existing Unit of Measure overrides —
    see _extract_uom_issues above). Reuses that exact same override shape
    (type="uom", key="{itemNo}::{rawUom}", resolved={uomCode, description}) so a UOM
    link saved here also benefits the Buffer tab's automated import path, and vice
    versa. A line whose resolved item's raw UOM doesn't validate, and has no saved UOM
    override either, is reported as "needs_uom" instead of "ready".
    """
    odata_filter = _build_missing_items_filter(po_ref_number, date_from, date_to)
    params = {"company": company, "expand": "salesOrderLines"}
    if odata_filter:
        params["filter"] = odata_filter
    resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params=params, timeout=60)
    body, status_code = _proxy_json(resp)
    if status_code != 200:
        raise RuntimeError(body.get("detail") or body.get("error") or f"BC returned {status_code}")
    orders = body.get("data") or []
    if not orders:
        return []

    sku_overrides_by_key = {
        (ov.get("key") or "").strip().upper(): ov for ov in _fetch_overrides_by_type("sku")
    }
    uom_overrides_by_key = {
        (ov.get("key") or "").strip().upper(): ov for ov in _fetch_overrides_by_type("uom")
    }
    # Same BC Item Reference ("Customer" type, Pag50349) matching rgmc-worker-pool's own
    # automated import already does via _build_ref_map — a raw customerSKUCode BC itself
    # already resolves to an item, with no saved override needed. Previously this
    # function only recognized a saved SKU override or a raw-SKU-equals-item-number
    # coincidence, so an item BC could already auto-resolve showed here as "needs
    # linking" even though Transfer to BC (via the automated path) would have resolved
    # it fine — a harmless-but-noisy false positive now eliminated.
    item_ref_map_by_key = _fetch_item_ref_map(company)

    po_refs = [order.get("externalDocumentNo") for order in orders if order.get("externalDocumentNo")]
    expected_by_ref = _fetch_expected_lines_batch(po_refs)

    # Pass 1: find every missing line and figure out which resolved items' UOM validity
    # still needs a live BC check (no saved override already covers it) — collected
    # across ALL orders first, so the actual BC calls in pass 2 are batched per unique
    # item, not repeated for every order that happens to be missing the same item.
    order_candidates = []
    pending_uom_checks: set = set()
    for order in orders:
        expected_lines = expected_by_ref.get(order.get("externalDocumentNo") or "", [])
        current_numbers = {
            (l.get("number") or "").strip().upper()
            for l in (order.get("salesOrderLines") or [])
        }
        candidates = []
        for line in expected_lines:
            sku_key = _sku_key_for_line(line)
            raw_sku = (line.get("customerSKUCode") or "").strip()
            sku_override = sku_overrides_by_key.get(sku_key.upper())
            resolved_item = sku_override.get("resolved") if sku_override else None
            if not resolved_item and raw_sku:
                resolved_item = item_ref_map_by_key.get(raw_sku.upper())
            resolved_item_no = ((resolved_item or {}).get("itemNo") or "").strip()

            already_present = bool(
                (resolved_item_no and resolved_item_no.upper() in current_numbers)
                or (raw_sku and raw_sku.upper() in current_numbers)
            )
            if already_present:
                continue

            raw_uom = (line.get("unitOfMeasurement") or "").strip()
            uom_key = f"{resolved_item_no}::{raw_uom}".upper() if (resolved_item_no and raw_uom) else ""
            needs_uom_check = bool(resolved_item_no and raw_uom and uom_key not in uom_overrides_by_key)
            if needs_uom_check:
                pending_uom_checks.add(resolved_item_no)

            candidates.append({
                "sku_key": sku_key,
                "customer_sku_code": raw_sku,
                "customer_sku_desc": (line.get("customerSKUDesc") or "").strip(),
                "po_qty": line.get("poQty"),
                "unit_of_measurement": raw_uom,
                # Net price (post-discount) is the real basis for the line — unit price
                # alone ignores whatever discount Document AI also extracted. Falls back
                # to unit price when net price is 0 (not every PO has one extracted).
                "unit_price": line.get("netPrice") or line.get("unitPrice"),
                "resolved_item": resolved_item,
                "resolved_item_no": resolved_item_no,
                "uom_key": uom_key,
                "needs_uom_check": needs_uom_check,
            })
        if candidates:
            order_candidates.append((order, candidates))

    valid_uoms_by_item = _fetch_valid_uoms_batch(pending_uom_checks, company) if pending_uom_checks else {}

    # Pass 2: finalize status now that every item's valid-UOM list (if needed) is known.
    results = []
    for order, candidates in order_candidates:
        missing = []
        for c in candidates:
            resolved_item = c["resolved_item"]
            uom_options = None
            if not resolved_item:
                item_status = "needs_linking"
            elif not c["unit_of_measurement"]:
                item_status = "ready"  # nothing to validate
            elif c["uom_key"] in uom_overrides_by_key:
                item_status = "ready"  # already-saved UOM link covers this item+raw-unit combo
            else:
                valid_rows = valid_uoms_by_item.get(c["resolved_item_no"], [])
                valid_codes = {(row.get("code") or "").strip().upper() for row in valid_rows}
                if c["unit_of_measurement"].upper() in valid_codes:
                    item_status = "ready"
                else:
                    item_status = "needs_uom"
                    uom_options = valid_rows

            missing.append({
                "sku_key": c["sku_key"],
                "customer_sku_code": c["customer_sku_code"],
                "customer_sku_desc": c["customer_sku_desc"],
                "po_qty": c["po_qty"],
                "unit_of_measurement": c["unit_of_measurement"],
                "unit_price": c["unit_price"],
                "status": item_status,
                "resolved_item": resolved_item,
                "uom_options": uom_options,
            })

        if missing:
            results.append({"order": order, "missing_items": missing})

    return results


@bp.route("/api/sales-orders/missing-items")
def api_sales_orders_missing_items():
    """BC Sales Orders (matching the given PO/company/date filters) whose current lines
    are missing one or more items the original Document AI extraction says should be
    there. Read-only — nothing here ever changes an existing Sales Order; see
    _find_missing_items for the comparison logic and its known limitations."""
    company = (request.args.get("company") or "").strip()
    po_ref_number = (request.args.get("po_ref_number") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        groups = _find_missing_items(company, po_ref_number, date_from, date_to)
        return jsonify({"data": groups, "total": len(groups)})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/sales-orders/missing-item-fixes", methods=["GET"])
def api_list_missing_item_fixes():
    """Queued/attempted missing-item fixes — lets the Missing Items tab show which
    rows already have a fix queued without re-deriving that from the BC search itself."""
    params = {}
    company = (request.args.get("company") or "").strip()
    if company:
        params["company"] = company
    applied = request.args.get("applied")
    if applied is not None:
        params["applied"] = applied
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/missing-item-fixes", params)


@bp.route("/api/sales-orders/missing-item-fixes", methods=["POST"])
def api_queue_missing_item_fix():
    """Queue one missing-item fix — picking an item (whether auto-resolved via a saved
    SKU override, or freshly linked through /api/overrides) only ever writes here. Apply
    Missing Item Fixes is the one real write to BC, same two-step shape as every other
    link-then-apply flow in this app."""
    data = request.get_json(silent=True) or {}
    required = ("company", "order_id", "sku_key", "item_no")

    def _is_blank(value):
        return not value.strip() if isinstance(value, str) else value in (None, "")

    missing = [f for f in required if _is_blank(data.get(f))]
    if missing:
        return jsonify({"error": f"{', '.join(missing)} required"}), 400
    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/missing-item-fixes", json={
            "company": data.get("company"),
            "order_id": data.get("order_id"),
            "order_no": data.get("order_no") or "",
            "po_ref": data.get("po_ref") or "",
            "sku_key": data.get("sku_key"),
            "customer_sku_code": data.get("customer_sku_code") or "",
            "customer_sku_desc": data.get("customer_sku_desc") or "",
            "item_no": data.get("item_no"),
            "item_description": data.get("item_description") or "",
            "po_qty": data.get("po_qty") or 0,
            "unit_of_measurement": data.get("unit_of_measurement") or "",
            "unit_price": data.get("unit_price") or 0,
            "resolved_by": data.get("resolved_by") or "",
        })
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/missing-item-fixes/<fix_id>", methods=["DELETE"])
def api_cancel_missing_item_fix(fix_id):
    """Cancel a queued missing-item fix before an Apply run has picked it up."""
    try:
        resp = _bc_api("DELETE", f"/bc/custom/v2/so-buffer/missing-item-fixes/{fix_id}")
        if resp.status_code == 204:
            return "", 204
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/sales-orders/missing-item-fix-history")
def api_missing_item_fix_history():
    """Search the append-only missing-item fix attempt history — every Apply run's
    outcome (applied/already_present/failed), by PO ref or order number."""
    params = {}
    for key in ("po_ref", "order_no", "company", "outcome"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    return _bc_proxy_get_json("/bc/custom/v2/so-buffer/missing-item-fix-history", params)


@bp.route("/api/apply-missing-item-fixes", methods=["POST"])
def api_apply_missing_item_fixes():
    """Apply every pending queued missing-item fix for a company — synchronous (unlike
    Apply Ship-To Fixes, this skips Pub/Sub + rgmc-worker-pool entirely: adding a Sales
    Order Line is already a single existing BC call, re-validated fresh against BC
    immediately before each write). Optionally scoped to specific order_ids."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    if not company:
        return jsonify({"error": "company is required"}), 400
    payload = {"company": company, "applied_by": data.get("applied_by") or ""}
    if data.get("order_ids"):
        payload["order_ids"] = data["order_ids"]
    try:
        resp = _bc_api("POST", "/bc/custom/v2/so-buffer/missing-item-fixes/apply", json=payload, timeout=90)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
