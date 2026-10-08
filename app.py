import os
import re
import smtplib
import requests
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, url_for
from datetime import datetime

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Backend services for the buffer reconciliation facility
# ---------------------------------------------------------------------------
# The browser only ever talks to this Flask app — it proxies to rgmc-bc-api (BC table
# lookups + the Firestore SO-import buffer) and rgmc-gcp-api (Cloud SQL lookups / fuzzy
# match suggestions / the reprocess-buffer trigger), matching how PROCESSES already
# calls rgmc-gcp-api server-side rather than from client-side JS.
BC_API_BASE = os.environ.get("BC_API_BASE", "https://rgmc-bc-api-prod-935246372408.asia-southeast1.run.app")
GCP_API_BASE = os.environ.get("GCP_API_BASE", "https://rgmc-gcp-api-935246372408.asia-southeast1.run.app")
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "30"))

# Display-only fields _enrich_document_ai_status/the BigQuery search route stamp onto
# each header for the UI — never real int_document_ai columns. Must be stripped before
# a header dict is sent on to rgmc-gcp-api's MSSQL insert (see api_bigquery_insert_and_buffer),
# since __rename_columns there only renames columns it recognizes and leaves everything
# else untouched, straight into the INSERT statement.
BQ_UI_ONLY_HEADER_FIELDS = ("_source", "in_mssql", "in_bc")

# BC company codes this UI knows about, for the company picker.
RECONCILE_COMPANIES = ["SBIC", "MTC"]

# Company routing rule mirrored from rgmc-gcp-api's CustomerPOUL.py (mssql_bc_mapping.txt
# §5) — duplicated here (small, static table) rather than round-tripping through another
# service call just to resolve a BigQuery row's free-text companyName to a BC company code.
_COMPANY_KEYWORD_MAP = [
    ("SUNCOAST", "SBIC"),
    ("SBIC", "SBIC"),
    ("MANILA", "MTC"),
    ("MTC", "MTC"),
]


def _resolve_bc_company(source_company_name: str | None) -> str | None:
    upper = (source_company_name or "").upper()
    for keyword, bc_company in _COMPANY_KEYWORD_MAP:
        if keyword in upper:
            return bc_company
    return None

# ---------------------------------------------------------------------------
# Email configuration — override via environment variables
# ---------------------------------------------------------------------------
EMAIL_CONFIG = {
    "smtp_host":         os.environ.get("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port":         int(os.environ.get("SMTP_PORT", "587")),
    "smtp_user":         os.environ.get("SMTP_USER", ""),
    "smtp_password":     os.environ.get("SMTP_PASSWORD", ""),
    "sender_email":      os.environ.get("SENDER_EMAIL", ""),
    "notification_email": os.environ.get("NOTIFICATION_EMAIL", "it.arellanoerwin@gmail.com"),
}

# ---------------------------------------------------------------------------
# Process registry — add / remove processes here
# ---------------------------------------------------------------------------
PROCESSES = {
    "po": {
        "name": "Purchase Orders (PO)",
        "endpoint": "https://rgmc-gcp-api-935246372408.asia-southeast1.run.app/customerpoul/runbridge/?method=manual",
    },
    "po_online": {
        "name": "Purchase Orders - Online Sales",
        "endpoint": "https://rgmc-gcp-api-935246372408.asia-southeast1.run.app/customerpoul/runbridge/onlinesalespo/?method=manual",
    },
    "remittance": {
        "name": "Remittance Advice",
        "endpoint": "https://rgmc-gcp-api-935246372408.asia-southeast1.run.app/customerra/runbridge/?method=manual",
    },
}

TIMEOUT_SECONDS = int(os.environ.get("ENDPOINT_TIMEOUT", "300"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_table(rows: list[tuple]) -> str:
    cells = "".join(
        f"<tr><td style='padding:6px 12px;font-weight:600;white-space:nowrap'>{k}</td>"
        f"<td style='padding:6px 12px'>{v}</td></tr>"
        for k, v in rows
    )
    return f"<table style='border-collapse:collapse;font-family:sans-serif'>{cells}</table>"


def send_email(subject: str, html_body: str, to_addr: str | None = None) -> bool:
    if not EMAIL_CONFIG["smtp_user"] or not EMAIL_CONFIG["smtp_password"]:
        app.logger.warning("Email credentials not set — skipping send")
        return False

    to_addr = to_addr or EMAIL_CONFIG["notification_email"]
    from_addr = EMAIL_CONFIG["sender_email"] or EMAIL_CONFIG["smtp_user"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(EMAIL_CONFIG["smtp_host"], EMAIL_CONFIG["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_CONFIG["smtp_user"], EMAIL_CONFIG["smtp_password"])
            server.sendmail(from_addr, [to_addr], msg.as_string())
        app.logger.info("Email sent: %s → %s", subject, to_addr)
        return True
    except Exception as exc:
        app.logger.error("Email send failed: %s", exc)
        return False


def _trigger_email_body(process: dict, name: str, department: str, timestamp: str) -> str:
    table = _html_table([
        ("Process", process["name"]),
        ("Triggered by", name),
        ("Department", department),
        ("Timestamp", timestamp),
        ("Endpoint", f"<code style='font-size:12px'>{process['endpoint']}</code>"),
    ])
    return f"""
    <html><body style='font-family:sans-serif;color:#1a1a2e'>
      <h2 style='color:#16213e'>⚡ Process Manually Triggered</h2>
      {table}
      <p style='color:#888;font-size:12px;margin-top:24px'>SBIC AI Uploading Manual Trigger</p>
    </body></html>
    """


def _error_email_body(process: dict, name: str, department: str, timestamp: str,
                      status_code: int | str, response_text: str) -> str:
    table = _html_table([
        ("Process", process["name"]),
        ("Triggered by", name),
        ("Department", department),
        ("Timestamp", timestamp),
        ("HTTP Status", f"<b style='color:red'>{status_code}</b>"),
        ("Response", f"<pre style='background:#f5f5f5;padding:8px;border-radius:4px;max-width:600px;overflow:auto'>{response_text[:2000]}</pre>"),
    ])
    return f"""
    <html><body style='font-family:sans-serif;color:#1a1a2e'>
      <h2 style='color:#c0392b'>🚨 Process Error Detected</h2>
      {table}
      <p style='color:#888;font-size:12px;margin-top:24px'>SBIC AI Uploading Manual Trigger</p>
    </body></html>
    """


# ---------------------------------------------------------------------------
# Backend proxy helpers — buffer reconciliation facility
# ---------------------------------------------------------------------------

def _bc_api(method: str, path: str, **kwargs):
    # setdefault, not a hardcoded kwarg — a caller passing its own timeout= used to
    # collide with this one ("got multiple values for keyword argument 'timeout'"),
    # which is exactly why every route was stuck at API_TIMEOUT with no way to ask for
    # longer on a call that's known to need it (e.g. BigQuery queries — see _gcp_api's
    # document-ai/search caller).
    kwargs.setdefault("timeout", API_TIMEOUT)
    resp = requests.request(method, f"{BC_API_BASE}{path}", **kwargs)
    return resp


def _gcp_api(method: str, path: str, **kwargs):
    kwargs.setdefault("timeout", API_TIMEOUT)
    resp = requests.request(method, f"{GCP_API_BASE}{path}", **kwargs)
    return resp


def _proxy_json(resp: requests.Response):
    """Return (body, status) for a proxied response, tolerating a non-JSON error body."""
    try:
        body = resp.json()
    except Exception:
        body = {"error": resp.text}
    return body, resp.status_code


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
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", processes=PROCESSES)


@app.route("/reconcile")
def reconcile_page():
    return render_template("reconcile.html", companies=RECONCILE_COMPANIES)


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


@app.route("/api/buffer")
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


@app.route("/api/buffer/lines")
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


def _multi_field_contains_search(path: str, company: str, search: str, fields: list) -> list:
    """contains(field, search) against each of `fields` in parallel, merged/deduped by id.

    BC's OData implementation rejects an `or` across distinct fields in one $filter
    ("BadRequest_MethodNotImplemented" / the documented /food/customers 501 limitation —
    see rgmc_ship_to_v2_routes.py's docstring in rgmc-bc-api for the same workaround).
    Querying one field per request and merging client-side is how ship-to-addresses
    already searches name/code/lookupCode together; this generalizes that pattern so a
    search matches ANY of `fields` containing the term, not just the first one checked.
    """
    # NOT tolower() — confirmed live (2026-10-06) that wrapping the field in tolower()
    # makes BC's contains() always return zero rows on these custom API pages,
    # regardless of search term (e.g. contains(tolower(name),'abby') -> 0 even though
    # contains(name,'ABBY') -> 1 for the exact same row). tolower() is simply
    # non-functional on this BC environment's custom pages, not just unexpectedly
    # case-sensitive. RGMC/SBIC's own data entry convention stores these fields in ALL
    # CAPS, so uppercasing the search term and comparing against the raw field
    # reliably reproduces case-insensitive matching without relying on the broken
    # BC function — this is what was silently making every non-exact-case search
    # (i.e. almost every real search) return no results.
    esc = search.replace("'", "''").upper()
    errors: list = []

    def _one(field: str) -> list:
        try:
            resp = _bc_api("GET", path, params={"company": company, "filter": f"contains({field},'{esc}')"})
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                return body.get("data", [])
            errors.append(f"{field}: {status_code} {body}")
        except requests.RequestException as exc:
            errors.append(f"{field}: {exc}")
        return []

    merged: dict = {}
    with ThreadPoolExecutor(max_workers=len(fields)) as ex:
        for rows in ex.map(_one, fields):
            for row in rows:
                merged[row.get("id") or id(row)] = row

    if not merged and errors:
        # Every field failed — surface it as a connectivity/BC error instead of a
        # silent "no matches", same as rgmc_ship_to_v2_routes.py's equivalent check.
        raise requests.RequestException("; ".join(errors))
    return list(merged.values())


def _enrich_customer_names(candidates: list, company: str) -> None:
    """Attach a readable customerName to each ship-to candidate, in place.

    Ship-to candidates (from either rgmc-gcp-api's shipto-match suggestions or
    rgmc-bc-api's ship-to-addresses search) only carry the BC customerNumber
    (e.g. "DS001") — not enough to tell candidates apart at a glance, or to know
    which BC customer to auto-link alongside a chosen branch. Resolves every
    distinct customerNumber in one batched rgmc-bc-api call.
    """
    numbers = sorted({c.get("customerNumber") for c in candidates if c.get("customerNumber")})
    if not company or not numbers:
        return
    try:
        esc_numbers = [n.replace("'", "''") for n in numbers]
        odata_filter = " or ".join(f"customerNo eq '{n}'" for n in esc_numbers)
        resp = _bc_api("GET", "/bc/custom/v2/customers", params={"company": company, "filter": odata_filter})
        cust_body, status_code = _proxy_json(resp)
        if status_code != 200:
            return
        name_by_no = {c.get("customerNo"): c.get("name") for c in cust_body.get("data", [])}
    except requests.RequestException:
        return  # Candidates still work without names if this lookup fails.

    for c in candidates:
        no = c.get("customerNumber")
        if no in name_by_no:
            c["customerName"] = name_by_no[no]


@app.route("/api/suggest/shipto/<po_ref>")
def api_suggest_shipto(po_ref):
    """Fuzzy BC ship-to suggestions for one PO, via rgmc-gcp-api (Cloud SQL + BC).

    Passes the page's selected company through explicitly: manually-encoded orders
    (manualEncoded: true) often have a blank header.companyName, which gcp-api needs
    to derive the BC company from when ?company= isn't given -- without this override
    those orders 400 with "Could not resolve a BC company from companyName=''" even
    though we already know the company from the buffer view the user is looking at.
    """
    company = (request.args.get("company") or "").strip()
    params = {"company": company} if company else {}
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}/shipto-match", params=params)
        body, status_code = _proxy_json(resp)
        if status_code == 200:
            candidates = list(body.get("fuzzyMatches") or [])
            if body.get("exactCodeMatch"):
                candidates.append(body["exactCodeMatch"])
            _enrich_customer_names(candidates, body.get("bcCompany"))
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/suggest/item/<po_ref>")
def api_suggest_item(po_ref):
    """Fuzzy BC item suggestions for one PO's lines, via rgmc-gcp-api (Cloud SQL + BC).

    See api_suggest_shipto for why ?company= is passed through explicitly.
    """
    company = (request.args.get("company") or "").strip()
    params = {"company": company} if company else {}
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}/item-match", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/lookup/items")
def api_lookup_items():
    """Search existing BC items by description OR item number, via rgmc-bc-api.

    RGMC's custom items page (Pag50310) names these fields "description" and "number"
    (not BC standard's "displayName"). A term is matched against either — e.g. the user
    might recognize the item by its code just as easily as by a fragment of its name.
    """
    company = request.args.get("company", "")
    search = (request.args.get("search") or "").strip()
    if not search:
        return jsonify({"data": []})
    try:
        rows = _multi_field_contains_search("/bc/custom/v2/items", company, search, ["description", "number"])
        return jsonify({"data": rows})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/lookup/customers")
def api_lookup_customers():
    """Search existing BC customers by name OR customer number, via rgmc-bc-api.

    RGMC's custom customers page names these fields "name" and "customerNo" (not BC
    standard's "displayName"/"number"). A term is matched against either.
    """
    company = request.args.get("company", "")
    search = (request.args.get("search") or "").strip()
    if not search:
        return jsonify({"data": []})
    try:
        rows = _multi_field_contains_search("/bc/custom/v2/customers", company, search, ["name", "customerNo"])
        return jsonify({"data": rows})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/lookup/ship-to")
def api_lookup_ship_to():
    """Search existing BC ship-to addresses by name, via rgmc-bc-api."""
    company = request.args.get("company", "")
    search = (request.args.get("search") or "").strip()
    if not search:
        return jsonify({"data": []})
    try:
        resp = _bc_api("GET", "/bc/custom/v2/ship-to-addresses", params={
            "company": company,
            "search": search,
        })
        body, status_code = _proxy_json(resp)
        if status_code == 200:
            _enrich_customer_names(body.get("data") or [], company)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/overrides", methods=["GET"])
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


@app.route("/api/overrides", methods=["POST"])
def api_save_override():
    """Save the user's chosen BC link for one SKU code / branch name / customer name."""
    data = request.get_json(silent=True) or {}
    override_type = (data.get("type") or "").strip()
    key = (data.get("key") or "").strip()
    resolved = data.get("resolved") or {}
    resolved_by = (data.get("resolved_by") or "").strip()
    buffer_ids = data.get("buffer_ids") or []

    if override_type not in ("sku", "branch", "customer"):
        return jsonify({"error": "type must be sku, branch, or customer"}), 400
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


@app.route("/api/overrides/<override_id>", methods=["DELETE"])
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


@app.route("/api/inactive-skus", methods=["POST"])
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


@app.route("/api/inactive-skus/<doc_id>", methods=["DELETE"])
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


@app.route("/api/reference")
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


@app.route("/api/history")
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


# Sales Orders eligible for the Overrides tab's "Find previous orders"/"Update these
# orders" actions are restricted to this exact value — this app's own automated
# SO-import pipeline stamps every order it creates with submittedBy="SBIC AI Uploading"
# (rgmc-worker-pool's so_import_worker.py, _SUBMITTED_BY). Hardcoded as a server-side
# OData filter condition, not a UI toggle, so a human-created BC order that happens to
# share the same (wrong) customer number can never be matched or touched by this feature.
_SBIC_AI_SUBMITTED_BY = "SBIC AI Uploading"


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


@app.route("/api/sales-orders")
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
            app.logger.error("Follow-up override save returned %s: %s", resp.status_code, resp.text)
    except requests.RequestException as exc:
        app.logger.error("Could not create follow-up override for stuck orders: %s", exc)


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


@app.route("/api/sales-orders/update-customer", methods=["POST"])
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


# ---------------------------------------------------------------------------
# BigQuery (int_document_ai / int_document_ai_detail) lookup
# ---------------------------------------------------------------------------
# Finds POs Document AI parsed from BigQuery that may never have made it into MSSQL
# (sbic_int) at all — separate from the normal automated BigQuery-bridge path, for a
# human to find and manually push through when the bridge missed something. Per-PO
# status (BigQuery / MSSQL / Business Central) tells the user exactly how far along
# each one already is before they act on it.

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
        company = _resolve_bc_company(h.get("company_name"))
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


def _split_po_refs(raw: str) -> list:
    return [p for p in re.split(r"[,\s]+", (raw or "").strip()) if p]


def _customerpoul_row_to_search_header(row: dict) -> dict:
    """CustomerPOUL (camelCase) -> the same snake_case shape BigQuery's int_document_ai
    returns, so a PO found only here (never staged into BigQuery at all — e.g. one
    manually encoded through SBIC's legacy app) can sit in the exact same `headers`
    list as a BigQuery-sourced one and flow through every downstream function
    (_enrich_document_ai_status, _bq_header_to_buffer_header, the browser's own
    rendering) completely unmodified. customer_branch_lookup_code has no BigQuery
    equivalent at all — carried along anyway since _bq_header_to_buffer_header reads it
    when present, and it genuinely helps the worker's ship-to matching for this source."""
    return {
        "po_ref_number": row.get("poRefNumber"),
        "customer_name": row.get("customerName"),
        "customer_branch_name": row.get("customerBranchName"),
        "customer_branch_lookup_code": row.get("customerBranchLookUpCode"),
        "company_name": row.get("companyName"),
        "po_date": row.get("poDate"),
        "delivery_date": row.get("deliveryDate"),
        "cancellation_date": row.get("cancellationDate"),
        "remark": row.get("remark"),
        "created_at": row.get("createDate"),
    }


def _customerpouldetail_rows_to_search_lines(rows: list) -> list:
    """CustomerPOULDetail (camelCase, a reduced-column mirror of CustomerPOULDetailBQ)
    -> the same snake_case shape BigQuery's int_document_ai_detail returns. Fields
    CustomerPOULDetail doesn't carry (e.g. unitOfMeasurement) just come back None —
    there's no richer source to fall back to for a PO that was never staged into the
    BigQuery tables at all."""
    return [{
        "po_ref_number": r.get("poRefNumber"),
        "customer_sku_code": r.get("customerSKUCode"),
        "customer_sku_desc": r.get("customerSKUDesc"),
        "po_qty": r.get("poQty"),
        "po_qty_pcs": r.get("poQtyPcs"),
        "unit_of_measurement": r.get("unitOfMeasurement"),
        "unit_price": r.get("unitPrice"),
        "unit_price_pcs": r.get("unitPricePcs"),
        "net_price": r.get("netPrice"),
        "delivery_date": r.get("deliveryDate"),
    } for r in rows]


def _fetch_from_customerpoul(po_ref: str) -> tuple:
    """(header, lines) in the common snake_case search shape for one PO ref, read
    straight from CustomerPOUL/CustomerPOULDetail — or (None, []) if MSSQL doesn't have
    it either. Used only as a fallback for a PO ref the BigQuery search didn't find."""
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}")
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return None, []
    if status_code != 200 or not (body.get("data") or []):
        return None, []
    header = _customerpoul_row_to_search_header(body["data"][0])

    lines = []
    try:
        lresp = _gcp_api("GET", f"/customerpouldetail/{po_ref}")
        lbody, lstatus = _proxy_json(lresp)
        if lstatus == 200:
            lines = _customerpouldetail_rows_to_search_lines(lbody.get("data") or [])
    except requests.RequestException:
        pass
    return header, lines


@app.route("/api/bigquery/document-ai/search")
def api_bigquery_document_ai_search():
    params = {}
    for key in ("po_ref_number", "customer_name", "date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    try:
        # Two live BigQuery query executions happen server-side for one of these calls
        # (header, then detail) — slower and far more variable than every other call
        # this app makes, so it gets its own longer timeout instead of API_TIMEOUT's
        # 30s (already too short in practice — confirmed by a live read-timeout error).
        resp = _gcp_api("GET", "/bigquery_routes/document-ai/search", params=params, timeout=90)
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

        _enrich_document_ai_status(headers)
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


def _bq_report_row(h: dict, details: list) -> dict:
    """One alignment-report row for a header already in the BigQuery Lookup search
    results — BigQuery is the source of truth (see its search route), so the
    recommendation always points at the tool that brings MSSQL/BC up to what BigQuery
    already has, never the other way around. Runs one live BC lookup per call (same
    _fetch_existing_bc_order used by the insert-and-buffer blocker) to get the real
    current line count, not just the in_bc flag the search enrichment already attached.
    """
    po_ref = h.get("po_ref_number") or ""
    company = _resolve_bc_company(h.get("company_name"))
    total_lines = len(_bq_lines_for_po(details, po_ref))
    in_bigquery = h.get("_source") != "mssql"
    in_mssql = h.get("in_mssql")

    so_number, bc_line_count = (None, 0)
    if company:
        so_number, bc_line_count = _fetch_existing_bc_order(company, po_ref)

    buffer_followup = (
        "If any SKU/branch/customer still can't be auto-matched, it lands (or stays) in the buffer — "
        "resolve it on the Buffer tab's Items/Branches/Customers lists, then Reprocess Buffer again."
    )

    if not company:
        status, action = "failed", f"Could not resolve a BC company from companyName={h.get('company_name')!r} — check this PO manually."
    elif so_number and bc_line_count >= total_lines:
        status, action = "aligned", f"Already aligned — BC order {so_number} has all {total_lines} line(s). No action needed."
    elif so_number:
        status = "lines_missing"
        action = (
            f"BC order {so_number} is missing lines ({bc_line_count}/{total_lines} present). Go to the "
            f"Cloud SQL tab, select {company}, and click \"Sync from Cloud SQL\" to backfill the rest. {buffer_followup}"
        )
    elif in_mssql is None:
        status, action = "unknown", "Could not confirm MSSQL status — re-run the search in BigQuery Lookup before acting on this row."
    else:
        status = "not_in_bc"
        buffer_note = "buffers it directly, no MSSQL insert needed" if (not in_bigquery or in_mssql) else "inserts it into MSSQL and buffers it"
        action = (
            f"Not yet in BC. In the BigQuery Lookup tab, select this PO and click \"Add Selected to Buffer\" "
            f"({buffer_note}), then go to the Buffer tab, select {company}, and click \"Reprocess Buffer\". {buffer_followup}"
        )

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
        "so_number": so_number,
        "lines_present": bc_line_count if so_number else None,
        "lines_total": total_lines,
        "status": status,
        "action": action,
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
        rows.append({
            "po_ref_number": ref, "customer_name": None, "customer_branch_name": None,
            "company_name": None, "company": None, "po_date": None,
            "in_bigquery": False, "in_mssql": False, "in_bc": False,
            "so_number": None, "lines_present": None, "lines_total": None,
            "status": "not_found", "buffer_url": None,
            "action": "Not found in BigQuery or MSSQL — verify the PO ref number, or wait for Document AI to process it.",
        })
    return rows


@app.route("/api/bigquery/report", methods=["POST"])
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
    report_url = f"{scheme}://{request.host}{url_for('report_page', report_id=report_id)}"
    return jsonify({"report_id": report_id, "url": report_url})


@app.route("/report/<report_id>")
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

    bq_headers = [h for h in headers if h.get("_source") != "mssql"]
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
        company = _resolve_bc_company(h.get("company_name"))
        already_in_mssql = h.get("_source") == "mssql"
        if not company:
            results.append({
                "po_ref": po_ref, "status": "failed",
                "detail": f"Could not resolve a BC company from companyName={h.get('company_name')!r} — not added to any buffer.",
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


@app.route("/api/bigquery/insert-and-buffer", methods=["POST"])
def api_bigquery_insert_and_buffer():
    data = request.get_json(silent=True) or {}
    headers = data.get("headers") or []
    details = data.get("details") or []
    employee = data.get("employee") or {}
    if not headers:
        return jsonify({"error": "headers must not be empty"}), 400
    return jsonify(_insert_and_buffer(headers, details, employee))


@app.route("/api/bigquery/quick-align", methods=["POST"])
def api_bigquery_quick_align():
    """One-click alignment for one or more BigQuery Lookup rows. BigQuery is always
    the source of truth (see _bq_report_row's status logic) — this only ever brings
    MSSQL/BC up to what BigQuery already has, never the reverse, and every action
    here is an existing, already-vetted tool; nothing new talks to BC directly:

      status "not_in_bc"     -> insert into MSSQL (if needed) + add to the buffer
                                 (_insert_and_buffer, same as Add Selected to Buffer),
                                 then trigger Reprocess Buffer once per distinct
                                 company touched (same as clicking it manually).
      status "lines_missing" -> trigger Sync from Cloud SQL, scoped to exactly these
                                 PO refs via po_ref_numbers (one call per company, so
                                 a multi-row align doesn't trip the 30s-per-path rate
                                 limiter on /customerpoul/sync-inserted-orders).
      anything else           -> reported back with no action taken (already aligned,
                                 or there's genuinely nothing safe to automate yet).
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
    results = []

    not_in_bc_headers = [h for h, r in zip(headers, rows) if r["status"] == "not_in_bc"]
    if not_in_bc_headers:
        buffer_outcome = _insert_and_buffer(not_in_bc_headers, details, employee)
        for r in buffer_outcome["data"]:
            results.append({"po_ref": r["po_ref"], "action": "buffer", "status": r["status"], "detail": r["detail"]})

        companies_touched = sorted({
            _resolve_bc_company(h.get("company_name")) for h in not_in_bc_headers
            if _resolve_bc_company(h.get("company_name"))
        })
        for company in companies_touched:
            try:
                resp = _gcp_api("POST", "/customerpoul/reprocess-buffer", params={"companies": company, **notify_params})
                body, status_code = _proxy_json(resp)
                ok = status_code == 200
                detail = f"Reprocess Buffer triggered for {company}." if ok else (
                    f"Reprocess Buffer failed for {company}: {body.get('detail') or body}")
            except requests.RequestException as exc:
                ok, detail = False, f"Reprocess Buffer request failed for {company}: {exc}"
            results.append({"po_ref": None, "action": "reprocess", "status": "triggered" if ok else "failed", "detail": detail})

    lines_missing_by_company: dict = {}
    for r in rows:
        if r["status"] == "lines_missing" and r["company"]:
            lines_missing_by_company.setdefault(r["company"], []).append(r["po_ref_number"])
    for company, po_refs in lines_missing_by_company.items():
        try:
            resp = _gcp_api("POST", "/customerpoul/sync-inserted-orders", params={
                "companies": company, "po_ref_numbers": ",".join(po_refs), **notify_params,
            })
            body, status_code = _proxy_json(resp)
            ok = status_code == 200
            detail = f"Sync from Cloud SQL triggered for {len(po_refs)} PO(s) in {company}." if ok else (
                f"Sync from Cloud SQL failed for {company}: {body.get('detail') or body}")
        except requests.RequestException as exc:
            ok, detail = False, f"Sync from Cloud SQL request failed for {company}: {exc}"
        for po_ref in po_refs:
            results.append({"po_ref": po_ref, "action": "sync", "status": "triggered" if ok else "failed", "detail": detail})

    for r in rows:
        if r["status"] not in ("not_in_bc", "lines_missing"):
            results.append({"po_ref": r["po_ref_number"], "action": "none", "status": r["status"], "detail": r["action"]})

    return jsonify({"data": results, "total": len(results)})


def _employee_notify_params(data: dict):
    """Validate the 4 required employee fields and return them as rgmc-gcp-api's
    notify_* query params, or (None, error_response) if any are missing."""
    employee_name = (data.get("employee_name") or "").strip()
    employee_company = (data.get("employee_company") or "").strip()
    employee_department = (data.get("employee_department") or "").strip()
    email = (data.get("email") or "").strip()
    if not employee_name or not employee_company or not employee_department or not email:
        return None, (jsonify({"error": "employee_name, employee_company, employee_department, and email are required"}), 400)
    return {
        "notify_name": employee_name,
        "notify_company": employee_company,
        "notify_department": employee_department,
        "notify_email": email,
    }, None


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    """Trigger the existing POUL SO reprocess-buffer pass for one company.

    This re-runs the normal buffer retry (rgmc-gcp-api -> Pub/Sub -> rgmc-worker-pool).
    Manual links saved via /api/overrides are consulted: rgmc-bc-api's
    apply_resolution_to_buffer patches the resolved link directly onto the affected
    buffer doc(s) (header.resolvedShipTo/resolvedCustomer, line.resolvedItem), and
    rgmc-worker-pool's _create_order/_resolve_valid_lines read those fields first,
    ahead of their own automatic ship-to/item matching.

    employee_name/employee_company/employee_department/email identify who triggered
    this from the page (required client-side before the page is usable at all — see
    reconcile.js). Forwarded to rgmc-gcp-api as notify_* so rgmc-worker-pool can CC
    this person on the reprocess result emails.
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
        resp = _gcp_api("POST", "/customerpoul/reprocess-buffer", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/sync-inserted-orders", methods=["POST"])
def api_sync_inserted_orders():
    """Backfill lines onto BC sales orders already inserted for CustomerPOUL rows
    with a given createBy (default 'trigger' — the BigQuery bridge's automated
    inserts), using Cloud SQL (CustomerPOUL/CustomerPOULDetailBQ) as the source of
    truth. Finds each order by externalDocumentNo == poRefNumber, so it works even
    for orders whose Firestore buffer doc is already gone. Any line that still can't
    be resolved gets buffered (with the order's so_number) for manual reconciliation.

    Same employee-notify requirement and run_id/status tracking as /api/reprocess.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    create_by = (data.get("create_by") or "trigger").strip()

    notify_params, err = _employee_notify_params(data)
    if err:
        return err

    params = {"companies": company, "create_by": create_by, **notify_params}
    try:
        resp = _gcp_api("POST", "/customerpoul/sync-inserted-orders", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/backfill-from-cloudsql", methods=["POST"])
def api_backfill_from_cloudsql():
    """Create missing BC sales orders (header + lines) from Cloud SQL CustomerPOUL/
    CustomerPOULDetail for a given createBy (default 'trigger') and createDate range
    (when the row was inserted into CustomerPOUL, not poDate, the original PO date
    from the source ERP).

    Opposite skip condition from /api/sync-inserted-orders: a PO whose
    externalDocumentNo already matches an existing BC sales order is skipped
    untouched, never re-created. Anything that can't be fully resolved is buffered
    for manual reconciliation, same as every other import path.

    Same employee-notify requirement and run_id/status tracking as /api/reprocess.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    create_by = (data.get("create_by") or "trigger").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()

    notify_params, err = _employee_notify_params(data)
    if err:
        return err

    params = {"companies": company, "create_by": create_by, **notify_params}
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to
    try:
        resp = _gcp_api("POST", "/customerpoul/backfill-from-cloudsql", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/reprocess-status/<run_id>")
def api_reprocess_status(run_id):
    """Status of one reprocess-buffer run — queued / processing / done / error.

    run_id comes from /api/reprocess's response. Proxies rgmc-bc-api, which reads
    the Firestore doc rgmc-worker-pool writes as it processes the run.
    """
    try:
        resp = _bc_api("GET", f"/bc/custom/v2/so-buffer/reprocess-status/{run_id}")
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/trigger", methods=["POST"])
def trigger():
    data = request.get_json(silent=True) or {}
    name         = (data.get("name") or "").strip()
    department   = (data.get("department") or "").strip()
    process_key  = (data.get("process") or "").strip()

    if not name or not department or not process_key:
        return jsonify({"error": "Name, department, and process are required."}), 400

    if process_key not in PROCESSES:
        return jsonify({"error": "Invalid process selected."}), 400

    process   = PROCESSES[process_key]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Notify on every trigger
    send_email(
        subject=f"[SBIC AI] Triggered: {process['name']}",
        html_body=_trigger_email_body(process, name, department, timestamp),
    )

    # Call the external endpoint
    try:
        resp = requests.post(process["endpoint"], timeout=TIMEOUT_SECONDS)

        try:
            response_body = resp.json()
        except Exception:
            response_body = resp.text

        result = {
            "ok":           resp.ok,
            "status_code":  resp.status_code,
            "process_name": process["name"],
            "timestamp":    timestamp,
            "response":     response_body,
        }

        if not resp.ok:
            send_email(
                subject=f"[SBIC AI] ERROR: {process['name']} — HTTP {resp.status_code}",
                html_body=_error_email_body(
                    process, name, department, timestamp,
                    resp.status_code,
                    str(response_body),
                ),
            )

        return jsonify(result), 200

    except requests.Timeout:
        msg = f"Request timed out after {TIMEOUT_SECONDS} seconds."
        send_email(
            subject=f"[SBIC AI] TIMEOUT: {process['name']}",
            html_body=_error_email_body(process, name, department, timestamp, "Timeout", msg),
        )
        return jsonify({"ok": False, "error": msg, "process_name": process["name"], "timestamp": timestamp}), 504

    except Exception as exc:
        msg = str(exc)
        send_email(
            subject=f"[SBIC AI] EXCEPTION: {process['name']}",
            html_body=_error_email_body(process, name, department, timestamp, "Exception", msg),
        )
        return jsonify({"ok": False, "error": msg, "process_name": process["name"], "timestamp": timestamp}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
