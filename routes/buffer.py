"""Buffer reconciliation tab: grouped view of Firestore-buffered orders needing manual SKU/branch/customer/UOM links."""
import re
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from core.config import API_TIMEOUT, GCP_API_BASE, BQ_PROXY_TIMEOUT
from core.http_clients import _bc_api, _gcp_api, _proxy_json

bp = Blueprint("buffer", __name__)


_UOM_REJECTION_RE = re.compile(
    r"item '([^']+)' \(from [^)]+\): Unit of Measure Code '([^']+)' does not exist"
)


def _extract_uom_issues(order: dict) -> list[dict]:
    """Every Unit-of-Measure rejection this buffered order currently carries, deduped
    by item number — structurally tagged lines (line.uomIssue) first, then any rejection
    named only in last_error (see _UOM_REJECTION_RE) that isn't already covered by one.
    For a last_error-only match, the actual raw unit-of-measure text is recovered from
    the order's own matching line (by resolvedItem.itemNo) — last_error only ever names
    the REJECTED code (e.g. "CS"), never the original source text — so a uom override
    saved from this stays correctly matchable against that line later
    (so_buffer_service.apply_resolution_to_buffer compares against unitOfMeasurement
    directly, not against anything derived from last_error).
    Returns [{"itemNo", "rawUom", "badCode", "description"}, ...].
    """
    lines = order.get("lines") or []
    issues: dict[str, dict] = {}

    for line in lines:
        uom_issue = line.get("uomIssue") or {}
        item_no = (uom_issue.get("itemNo") or "").strip()
        if item_no:
            issues[item_no.upper()] = {
                "itemNo": item_no,
                "rawUom": uom_issue.get("rawUom") or "",
                "badCode": uom_issue.get("badCode") or "",
                "description": (line.get("resolvedItem") or {}).get("description")
                or (line.get("customerSKUDesc") or "").strip(),
            }

    for item_no, bad_code in _UOM_REJECTION_RE.findall(order.get("last_error") or ""):
        key = item_no.strip().upper()
        if key in issues:
            continue
        match_line = next(
            (l for l in lines if ((l.get("resolvedItem") or {}).get("itemNo") or "").strip().upper() == key),
            None,
        )
        issues[key] = {
            "itemNo": item_no,
            "rawUom": (match_line.get("unitOfMeasurement") or "") if match_line else "",
            "badCode": bad_code,
            "description": ((match_line.get("resolvedItem") or {}) if match_line else {}).get("description") or "",
        }

    return list(issues.values())


_NUMERIC_SKU_FLOAT_ARTIFACT_RE = re.compile(r"^(\d+)\.0$")


def _canonical_sku_code(sku: str) -> str:
    """Strips a numeric SKU's "ragged" trailing ".0" — a pandas/BigQuery float-
    coercion artifact (an all-digit SKU like "218720608619709" that passed through a
    pandas Series/DataFrame anywhere upstream, e.g. _recover_lines_for_order's
    BigQuery int_document_ai_detail fallback, serializes back out as
    "218720608619709.0") — so a line recovered that way groups with the same SKU's
    clean-string occurrences instead of showing up as a separate, duplicate-looking
    SKU group for the same item. Only strips when the whole value is digits plus
    exactly one trailing ".0" — never touches an alphanumeric SKU or a genuine
    decimal value (anything with other digits after the decimal point)."""
    m = _NUMERIC_SKU_FLOAT_ARTIFACT_RE.match(sku)
    return m.group(1) if m else sku


def _group_buffer(orders: list, overrides: list, inactive_skus: list | None = None) -> dict:
    """Group buffered orders by shared SKU code / customer branch name / customer name.

    Each group is resolvable once and applies to every buffered PO sharing that exact
    raw value — this is the "consolidate the POs with the same item codes / customer
    branch name / customer name" behavior. overrides is the flat list from
    GET /bc/custom/v2/so-buffer/overrides.

    inactive_skus is the flat list from GET /bc/custom/v2/so-buffer/inactive-skus — any
    SKU group whose key matches one is pulled out of "sku" and returned under
    "sku_inactive" instead, so it's excluded from the Items (SKU) tab and its
    resolved/total counts entirely rather than just displayed differently.
    """
    overrides_by_key: dict[str, dict] = {}
    for ov in overrides:
        overrides_by_key[f"{ov.get('type')}::{(ov.get('key') or '').strip().upper()}"] = ov

    inactive_by_key: dict[str, dict] = {}
    for row in (inactive_skus or []):
        inactive_by_key[(row.get("key") or "").strip().upper()] = row

    sku_groups: dict[str, dict] = {}
    branch_groups: dict[str, dict] = {}
    customer_groups: dict[str, dict] = {}
    uom_groups: dict[str, dict] = {}

    for order in orders:
        header = order.get("header") or {}
        buffer_id = order.get("id")
        po_ref = header.get("poRefNumber") or buffer_id
        branch_name = (header.get("customerBranchName") or "").strip()
        customer_name = (header.get("customerName") or "").strip()

        if branch_name:
            g = branch_groups.setdefault(branch_name.upper(), {
                "key": branch_name,
                # Display-only context (not part of the consolidation key) — the
                # frontend renders "<branch> (<customer> - <company>)" from these.
                "customer_name": customer_name,
                "company_name": (header.get("companyName") or "").strip(),
                "po_refs": [], "buffer_ids": [],
            })
            g["po_refs"].append(po_ref)
            g["buffer_ids"].append(buffer_id)

        if customer_name:
            g = customer_groups.setdefault(customer_name.upper(), {
                "key": customer_name, "po_refs": [], "buffer_ids": [],
            })
            g["po_refs"].append(po_ref)
            g["buffer_ids"].append(buffer_id)

        lines = order.get("lines") or []
        if not lines:
            continue
        for line in lines:
            sku = (line.get("customerSKUCode") or "").strip()
            desc = (line.get("customerSKUDesc") or "").strip()

            def _positive(v):
                try:
                    return float(v) > 0
                except (TypeError, ValueError):
                    return False

            # Approximation of the worker's pcs/non-pcs unit check — good enough for
            # an informational badge, not used for any actual reprocessing decision.
            qty_ok = _positive(line.get("poQtyPcs")) or _positive(line.get("poQty"))

            if sku:
                sku = _canonical_sku_code(sku)
                dict_key = sku.upper()
                display_key = sku
            else:
                # Blank SKU code — these are exactly the lines that fail with
                # "missing SKU/item reference or non-positive quantity" and must
                # still surface here, not be silently dropped. Group by
                # description (the only distinguishing raw text available), or a
                # fixed bucket if that's blank too.
                display_key = desc or "(no SKU code, no description)"
                dict_key = f"__NOSKU__::{display_key.upper()}"

            g = sku_groups.setdefault(dict_key, {
                "key": display_key,
                "description": desc,
                "missing_sku": not sku,
                "has_nonpositive_qty": False,
                "po_refs": [], "buffer_ids": [],
            })
            if not qty_ok:
                g["has_nonpositive_qty"] = True
            if po_ref not in g["po_refs"]:
                g["po_refs"].append(po_ref)
            if buffer_id not in g["buffer_ids"]:
                g["buffer_ids"].append(buffer_id)

        for uom_issue in _extract_uom_issues(order):
            item_no = uom_issue["itemNo"]
            raw_uom = uom_issue["rawUom"]
            uom_key = f"{item_no}::{raw_uom}"
            g = uom_groups.setdefault(uom_key.upper(), {
                "key": uom_key,
                "item_no": item_no,
                "raw_uom": raw_uom,
                "bad_code": uom_issue["badCode"],
                "description": uom_issue["description"],
                "po_refs": [], "buffer_ids": [],
            })
            if po_ref not in g["po_refs"]:
                g["po_refs"].append(po_ref)
            if buffer_id not in g["buffer_ids"]:
                g["buffer_ids"].append(buffer_id)

    def _finalize(groups: dict, override_type: str) -> list:
        result = []
        for key_upper, g in groups.items():
            override = overrides_by_key.get(f"{override_type}::{key_upper}")
            result.append({
                **g,
                "po_count": len(set(g["po_refs"])),
                "resolved": override.get("resolved") if override else None,
                "resolved_by": override.get("resolved_by") if override else None,
                "resolved_at": override.get("resolved_at") if override else None,
                "override_id": override.get("id") if override else None,
            })
        result.sort(key=lambda g: (-g["po_count"], g["key"]))
        return result

    sku_active, sku_inactive = [], []
    for g in _finalize(sku_groups, "sku"):
        inactive_row = inactive_by_key.get(g["key"].strip().upper())
        if inactive_row:
            g["inactive_id"] = inactive_row.get("id")
            g["inactive_marked_by"] = inactive_row.get("marked_by")
            g["inactive_marked_at"] = inactive_row.get("marked_at")
            sku_inactive.append(g)
        else:
            sku_active.append(g)

    return {
        "sku": sku_active,
        "sku_inactive": sku_inactive,
        "branch": _finalize(branch_groups, "branch"),
        "customer": _finalize(customer_groups, "customer"),
        "uom": _finalize(uom_groups, "uom"),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _recover_lines_for_order(po_ref: str) -> tuple:
    """Best-effort recovery of an order's lines when the buffer doc has none.

    The buffer doc only has what was in the Pub/Sub message at the time the order
    failed — a bug fixed 2026-09-24 in rgmc-gcp-api's SO-import bridge means orders
    buffered before that fix have `lines: []` even though the source data exists.
    Tries Cloud SQL (CustomerPOULDetail, via rgmc-gcp-api) first, since it's the
    same table the bridge itself reads from, then falls back to BigQuery
    (int_document_ai_detail, the Document AI-parsed record of the same PO) if Cloud
    SQL has nothing. Returns (lines, source) where source is "cloudsql", "bigquery",
    or None if neither had anything -- never raises, so one bad lookup can't break
    the whole buffer listing.
    """
    # Shorter, dedicated timeout: this runs once per empty-lines order, in parallel,
    # on every buffer page load -- a single slow Cloud SQL call shouldn't be allowed
    # to eat the full general-purpose API_TIMEOUT before falling back to BigQuery.
    recovery_timeout = min(API_TIMEOUT, 12)
    try:
        resp = requests.get(f"{GCP_API_BASE}/customerpouldetail/{po_ref}", timeout=recovery_timeout)
        if resp.status_code == 200:
            rows = resp.json().get("data", [])
            if rows:
                return rows, "cloudsql"
    except requests.RequestException:
        pass

    try:
        resp = requests.get(f"{GCP_API_BASE}/bigquery_routes/by_table/value", params={
            "table_name": "int_document_ai_detail",
            "where_column": "po_ref_number",
            "where_value": po_ref,
        }, timeout=recovery_timeout)
        if resp.status_code == 200:
            rows = resp.json().get("data", [])
            if rows:
                # BigQuery's dbt-built table uses snake_case; normalize to the same
                # camelCase shape CustomerPOULDetail (and _group_buffer) expect.
                lines = [{
                    "customerSKUCode": r.get("customer_sku_code"),
                    "customerSKUDesc": r.get("customer_sku_desc"),
                    "poQty": r.get("po_qty"),
                    "poQtyPcs": r.get("po_qty_pcs"),
                    "unitOfMeasurement": r.get("unit_of_measurement"),
                    "unitPrice": r.get("unit_price"),
                    "netPrice": r.get("net_price"),
                } for r in rows]
                return lines, "bigquery"
    except requests.RequestException:
        pass

    return [], None


def _recover_missing_lines(orders: list) -> None:
    """Fill in `lines` (in place) for any order whose buffer doc has none, in parallel."""
    targets = [o for o in orders if not o.get("lines")]
    if not targets:
        return

    def _po_ref(order):
        return (order.get("header") or {}).get("poRefNumber") or order.get("id")

    with ThreadPoolExecutor(max_workers=min(20, len(targets))) as ex:
        futures = {ex.submit(_recover_lines_for_order, _po_ref(o)): o for o in targets}
        for future, order in futures.items():
            try:
                lines, source = future.result()
            except Exception:
                continue  # best-effort — leave this one order's lines empty
            if lines:
                order["lines"] = lines
                order["_lines_recovered_from"] = source


def _build_buffer_response(company: str, recover: bool):
    orders_resp = _bc_api("GET", "/bc/custom/v2/so-buffer", params={"company": company})
    orders_body, orders_status = _proxy_json(orders_resp)
    if orders_status != 200:
        return orders_body, orders_status

    overrides_resp = _bc_api("GET", "/bc/custom/v2/so-buffer/overrides")
    overrides_body, overrides_status = _proxy_json(overrides_resp)
    overrides = overrides_body.get("data", []) if overrides_status == 200 else []

    inactive_resp = _bc_api("GET", "/bc/custom/v2/so-buffer/inactive-skus")
    inactive_body, inactive_status = _proxy_json(inactive_resp)
    inactive_skus = inactive_body.get("data", []) if inactive_status == 200 else []

    orders = orders_body.get("data", [])
    if recover:
        _recover_missing_lines(orders)
    return {
        "company": company,
        "order_count": len(orders),
        "orders": orders,
        "groups": _group_buffer(orders, overrides, inactive_skus),
    }, 200


@bp.route("/api/buffer")
def api_buffer():
    """Buffered orders for one company, pre-grouped by SKU / branch / customer,
    with any previously-saved manual links merged in.

    Loads fast by default (no line recovery) -- see /api/buffer/lines for the
    slower pass that also fills in missing lines from Cloud SQL/BigQuery.
    """
    company = (request.args.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        body, status_code = _build_buffer_response(company, recover=False)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/buffer/lines")
def api_buffer_lines():
    """Same as /api/buffer, but also recovers missing lines from Cloud SQL/BigQuery.

    This is slow (one lookup per empty-lines order, parallelized, but each Cloud SQL
    call can take seconds under load) -- called by the page as a background follow-up
    after the fast /api/buffer response has already rendered, not on initial load.
    """
    company = (request.args.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        body, status_code = _build_buffer_response(company, recover=True)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@bp.route("/api/buffer/resolve-file-scope")
def api_buffer_resolve_file_scope():
    """Resolves the Buffer tab's "filter to one file" search box: `query` is either an
    exact BigQuery fileName, or a sample PO ref number from that file — either way,
    returns every po_ref_number BigQuery's int_document_ai has under that file, so the
    frontend can narrow the already-loaded buffer groups down to just those POs
    without a second full buffer fetch.

    PO uploads only (int_document_ai) — this is the Buffer tab's own SBIC/SUNCOAST
    PO-import scope, same as every other BigQuery lookup already in this file.
    """
    query = (request.args.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400

    file_name = query
    try:
        search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                params={"po_ref_number": query}, timeout=BQ_PROXY_TIMEOUT)
        search_body, search_status = _proxy_json(search_resp)
        if search_status == 200 and (search_body.get("data") or []):
            # query matched an exact PO ref — use ITS file, not the raw query text.
            file_name = search_body["data"][0].get("file_name") or query
    except requests.RequestException:
        pass  # fall through and try query as a literal file_name instead

    try:
        file_resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": "int_document_ai", "where_column": "file_name", "where_value": file_name,
        }, timeout=BQ_PROXY_TIMEOUT)
        file_body, file_status = _proxy_json(file_resp)
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
    if file_status != 200:
        return jsonify(file_body), file_status

    po_refs = sorted({r.get("po_ref_number") for r in (file_body.get("data") or []) if r.get("po_ref_number")})
    if not po_refs:
        return jsonify({"error": f"No PO found for {query!r} — not a known fileName or PO ref number."}), 404
    return jsonify({"file_name": file_name, "po_ref_numbers": po_refs, "count": len(po_refs)})
