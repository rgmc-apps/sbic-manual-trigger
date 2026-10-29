import os
import re
import smtplib
import requests
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify, url_for
from markupsafe import escape as _esc
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
BQ_UI_ONLY_HEADER_FIELDS = ("_source", "in_mssql", "in_bc", "in_buffer", "bufferedSkus")

# BC company codes this UI knows about, for the company picker. MTC (Manila Taste
# Curators) removed 2026-10-08 — confirmed live that no "MTC" company exists in BC
# Production at all (GET /bc/companies lists SBIC, KW1, etc., no MTC), so every
# MTC-targeted action here would just 500. Re-add once BC Production actually has
# a company to route MTC to (its own, or an existing one it should post under).
RECONCILE_COMPANIES = ["SBIC"]

# Company routing rule mirrored from rgmc-gcp-api's CustomerPOUL.py (mssql_bc_mapping.txt
# §5) — duplicated here (small, static table) rather than round-tripping through another
# service call just to resolve a BigQuery row's free-text companyName to a BC company code.
# No MTC entry — see RECONCILE_COMPANIES above: BC Production has no "MTC" company, so
# routing a Manila Taste Curators row there just 500s every BC call made with it (e.g.
# GET /bc/custom/v2/sales-orders?company=MTC -> "Company 'MTC' not found in Business
# Central"). Leaving the keyword unmapped makes _resolve_bc_company return None for
# those rows instead, which every caller already handles as "could not resolve a BC
# company" (see _bq_report_row/_insert_and_buffer) rather than attempting a doomed call.
_COMPANY_KEYWORD_MAP = [
    ("SUNCOAST", "SBIC"),
    ("SBIC", "SBIC"),
]


def _resolve_bc_company(source_company_name: str | None) -> str | None:
    upper = (source_company_name or "").upper()
    for keyword, bc_company in _COMPANY_KEYWORD_MAP:
        if keyword in upper:
            return bc_company
    return None


# Known acceptable raw company-name text variants per BC company, for the BigQuery
# Lookup tab's Company filter (api_bigquery_document_ai_search's `company` param) —
# an explicit, human-maintained allowlist of exact variants seen in BigQuery/
# CustomerPOUL's free-text companyName field (abbreviations, "Intl" vs
# "International", with/without "Corp.", etc.).
#
# Deliberately a SEPARATE table from _COMPANY_KEYWORD_MAP above, even though the two
# overlap in spirit — that one drives auto-routing a header to a BC company during
# insert/quick-align, a correctness-critical path; this one only narrows what the
# Company filter shows. Keeping them apart means adding a filter variant here can
# never accidentally change where an order actually gets imported.
BQ_COMPANY_NAME_TERMS: dict[str, list[str]] = {
    "SBIC": [
        "SBIC",
        "SUNCOAST BRANDS",
        "SUNCOAST",
        "SUNCOAST BRANDS INTERNATIONAL CORP",
        "SUNCOAST BRANDS INTERNATIONAL CORPORATION",
        "SUNCOAST BRANDS INTL CORP",
        "SUNCOAST BRANDS INTL",
        "SUNCOAST BRANDS INTERNATIONAL",
    ],
    # MTC never routes to BC (see RECONCILE_COMPANIES above — BC Production has no MTC
    # company) but int_document_ai still tags plenty of rows with Manila Taste Curators'
    # own free-text company name, so the Upload Dashboard's company filter needs this
    # entry even though no BC-facing action will ever use it.
    "MTC": [
        "MTC",
        "MANILA TASTE CURATORS",
    ],
}


def _company_name_matches(raw_company_name: str | None, bc_company: str) -> bool:
    """True if a BigQuery/CustomerPOUL row's raw companyName text looks like it
    belongs to `bc_company`, per BQ_COMPANY_NAME_TERMS — case-insensitive substring
    match against any of that company's known terms. False (never matches anything)
    for a BC company with no terms registered yet, rather than silently falling back
    to a different matching rule."""
    terms = BQ_COMPANY_NAME_TERMS.get((bc_company or "").strip().upper())
    if not terms:
        return False
    upper = (raw_company_name or "").strip().upper()
    return any(term in upper for term in terms)

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


# Matches the UOM-specific sentence inside rgmc-worker-pool's _describe_line_rejection
# output, e.g. "item 'MU01CH02' (from reconcile-page SKU link): Unit of Measure Code
# 'CS' does not exist in 'Item Unit of Measure' in BC — ...". Recovers (itemNo, badCode)
# from last_error text alone, for an order buffered BEFORE line.uomIssue existed (every
# failure prior to that fix shipping) — without this, those orders would never appear
# on the Unit of Measure tab until their next reprocess attempt re-fails the same way.
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

@app.route("/")
def dashboard_page():
    return render_template("dashboard.html", companies=sorted(BQ_COMPANY_NAME_TERMS.keys()))


@app.route("/trigger")
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


@app.route("/api/buffer/resolve-file-scope")
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
                                params={"po_ref_number": query}, timeout=30)
        search_body, search_status = _proxy_json(search_resp)
        if search_status == 200 and (search_body.get("data") or []):
            # query matched an exact PO ref — use ITS file, not the raw query text.
            file_name = search_body["data"][0].get("file_name") or query
    except requests.RequestException:
        pass  # fall through and try query as a literal file_name instead

    try:
        # timeout=90, not 60 — see api_dashboard_file_records' comment on this same
        # /bigquery_routes/by_table/value race against rgmc-gcp-api's internal 60s cap.
        file_resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": "int_document_ai", "where_column": "file_name", "where_value": file_name,
        }, timeout=90)
        file_body, file_status = _proxy_json(file_resp)
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
    if file_status != 200:
        return jsonify(file_body), file_status

    po_refs = sorted({r.get("po_ref_number") for r in (file_body.get("data") or []) if r.get("po_ref_number")})
    if not po_refs:
        return jsonify({"error": f"No PO found for {query!r} — not a known fileName or PO ref number."}), 404
    return jsonify({"file_name": file_name, "po_ref_numbers": po_refs, "count": len(po_refs)})


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


def _fetch_overrides_by_type(override_type: str) -> list:
    """Every current override of one type, as raw {id, type, key, resolved, ...} dicts
    — fetched once and reused in-memory by a caller that needs to match MANY POs
    against the same override set (e.g. api_apply_known_overrides), instead of each one
    doing its own round trip to rgmc-bc-api."""
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/overrides", params={"type": override_type})
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return []
    return (body.get("data") or []) if status_code == 200 else []


def _match_branch_override(branch_overrides: list, branch_name: str) -> dict | None:
    """The already-saved "branch" override's ship-to for this exact customerBranchName,
    if one exists in `branch_overrides` (see _fetch_overrides_by_type) — a human already
    vetted this exact branch via the Buffer tab, so it should outrank a fresh fuzzy
    guess, not just sit alongside it unused."""
    if not branch_name:
        return None
    target = branch_name.strip().upper()
    for ov in branch_overrides:
        if (ov.get("key") or "").strip().upper() != target:
            continue
        resolved = ov.get("resolved") or {}
        if resolved.get("shipToCode"):
            return {
                "code": resolved["shipToCode"], "name": resolved.get("name") or "",
                "customerNumber": resolved.get("customerNo") or "",
                "source": "saved branch override",
            }
    return None


def _match_customer_override(customer_overrides_by_key: dict, customer_name: str, branch_name: str, po_ref: str) -> dict | None:
    """The already-saved "customer" override's ship-to reference for this PO, if one
    exists in `customer_overrides_by_key` (key upper -> raw override doc, see
    _fetch_overrides_by_type) — checked PO-scoped > branch-scoped > flat, the exact
    precedence rgmc-worker-pool's _create_order uses (so_buffer_service._customer_key_parts).
    A "customer" override only carries shipToCode as reference metadata (the ship-to its
    customer link was captured from, not a guaranteed match for THIS PO's own branch),
    so it's checked only when no branch override matched."""
    if not customer_name:
        return None
    name_u = customer_name.strip().upper()
    candidate_keys = []
    if po_ref:
        candidate_keys.append(f"{name_u}::PO:{po_ref.strip().upper()}")
    if branch_name:
        candidate_keys.append(f"{name_u}::{branch_name.strip().upper()}")
    candidate_keys.append(name_u)
    for key in candidate_keys:
        ov = customer_overrides_by_key.get(key)
        if not ov:
            continue
        resolved = ov.get("resolved") or {}
        if resolved.get("shipToCode"):
            return {
                "code": resolved["shipToCode"], "name": resolved.get("shipToName") or "",
                "customerNumber": resolved.get("customerNo") or "",
                "source": "saved customer override",
            }
    return None


def _find_shipto_override(po_ref: str, branch_name: str) -> dict | None:
    """An already-saved override that resolves this PO's ship-to, if any — surfaced
    ahead of fresh fuzzy matching so a human's already-vetted decision (made via the
    Buffer tab, or the Customers tab's Split action) is reused instead of asking them to
    re-decide the same branch/customer from scratch. Fetches each override type fresh —
    fine for this single-PO call site (api_suggest_shipto); a bulk caller fetches once
    via _fetch_overrides_by_type and calls _match_branch_override/_match_customer_override
    directly instead (see api_apply_known_overrides)."""
    branch_hit = _match_branch_override(_fetch_overrides_by_type("branch"), branch_name)
    if branch_hit:
        return branch_hit
    header, _ = _fetch_from_customerpoul(po_ref)
    customer_name = (header or {}).get("customer_name")
    customer_overrides_by_key = {(ov.get("key") or "").strip().upper(): ov for ov in _fetch_overrides_by_type("customer")}
    return _match_customer_override(customer_overrides_by_key, customer_name, branch_name, po_ref)


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
            override_match = _find_shipto_override(po_ref, body.get("customerBranchName"))
            if override_match:
                _enrich_customer_names([override_match], body.get("bcCompany"))
            body["overrideMatch"] = override_match
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


@app.route("/api/customer-name-matches")
def api_customer_name_matches():
    """Every BC Customer record sharing one EXACT name — used to detect when a raw
    customerName from a buffered PO has been split across more than one Customer
    record in BC (e.g. a retail chain set up as a separate customer per store/branch,
    all under the identical Name field). `name eq` (exact), not `contains` (substring)
    — the Overrides tab / branch-link flow uses this count, not the full-text one
    /api/lookup/customers already does, to decide whether a customer override needs to
    be scoped to one branch instead of applying to every buffered order sharing this
    customer name — see reconcile.js's isCustomerNameAmbiguous.
    """
    company = request.args.get("company", "")
    name = (request.args.get("name") or "").strip()
    if not name or not company:
        return jsonify({"data": []})
    try:
        esc = name.replace("'", "''").upper()
        resp = _bc_api("GET", "/bc/custom/v2/customers", params={"company": company, "filter": f"name eq '{esc}'"})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/lookup/uom")
def api_lookup_uom():
    """Valid units of measure for one item, via rgmc-bc-api's Item Unit of Measure
    lookup — unlike the sku/branch/customer lookups, this is scoped to one exact
    item_no (the group's own item), not a free-text search across a whole table."""
    company = request.args.get("company", "")
    item_no = (request.args.get("item_no") or "").strip()
    if not item_no:
        return jsonify({"data": []})
    try:
        resp = _bc_api("GET", f"/bc/custom/v2/items/{item_no}/unit-of-measure", params={"company": company})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
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


@app.route("/api/handoff-history")
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


@app.route("/api/sales-orders/missing-shipto")
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


@app.route("/api/sales-orders/shipto-fixes", methods=["GET"])
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
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/shipto-fixes", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/sales-orders/shipto-fixes", methods=["POST"])
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


@app.route("/api/sales-orders/shipto-fixes/<fix_id>", methods=["DELETE"])
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


@app.route("/api/sales-orders/shipto-fix-history")
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
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/shipto-fix-history", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/apply-shipto-fixes", methods=["POST"])
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


@app.route("/api/sales-orders/apply-known-overrides", methods=["POST"])
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
            }, timeout=90)
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
            sku_override = sku_overrides_by_key.get(sku_key.upper())
            resolved_item = sku_override.get("resolved") if sku_override else None
            resolved_item_no = ((resolved_item or {}).get("itemNo") or "").strip()
            raw_sku = (line.get("customerSKUCode") or "").strip()

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


@app.route("/api/sales-orders/missing-items")
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


@app.route("/api/sales-orders/missing-item-fixes", methods=["GET"])
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
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/missing-item-fixes", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/sales-orders/missing-item-fixes", methods=["POST"])
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


@app.route("/api/sales-orders/missing-item-fixes/<fix_id>", methods=["DELETE"])
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


@app.route("/api/sales-orders/missing-item-fix-history")
def api_missing_item_fix_history():
    """Search the append-only missing-item fix attempt history — every Apply run's
    outcome (applied/already_present/failed), by PO ref or order number."""
    params = {}
    for key in ("po_ref", "order_no", "company", "outcome"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/missing-item-fix-history", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/apply-missing-item-fixes", methods=["POST"])
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
            }, timeout=90)
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


@app.route("/api/sales-orders/customer-shipto-mismatch")
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


@app.route("/api/sales-orders/customer-shipto-fixes", methods=["GET"])
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
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/customer-shipto-fixes", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/sales-orders/customer-shipto-fixes", methods=["POST"])
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


@app.route("/api/sales-orders/customer-shipto-fixes/<fix_id>", methods=["DELETE"])
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


@app.route("/api/sales-orders/customer-shipto-fix-history")
def api_customer_shipto_fix_history():
    """Search the append-only customer/ship-to fix attempt history — every Apply run's
    outcome (applied/already_present/failed), by PO ref or order number."""
    params = {}
    for key in ("po_ref", "order_no", "company", "outcome"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value
    try:
        resp = _bc_api("GET", "/bc/custom/v2/so-buffer/customer-shipto-fix-history", params=params)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/apply-customer-shipto-fixes", methods=["POST"])
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
    company = _resolve_bc_company(h.get("company_name"))
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
            _resolve_bc_company(h.get("company_name")) for h in not_in_bc_headers
            if _resolve_bc_company(h.get("company_name"))
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


# ---------------------------------------------------------------------------
# Upload Monitoring Dashboard ("/") — browse every uploaded file across all three
# Document AI pipelines, drill into its PO/RA refs, their detail lines, MSSQL/BC
# status, and (PO + SBIC only) add a not-yet-in-BC PO to the buffer. See
# plans/upload_dashboard_plan.md for the full design.
# ---------------------------------------------------------------------------

# upload_type -> BigQuery header/detail table names + the detail table's join column —
# mirrors rgmc-gcp-api's own UPLOAD_TYPE_TABLES (bigquery_routes.py); duplicated here
# only so this proxy layer can validate upload_type itself rather than trusting
# whatever a client sends straight through to a generic table-name parameter.
DASHBOARD_UPLOAD_TYPES = {
    "po": {
        "header_table": "int_document_ai", "detail_table": "int_document_ai_detail",
        "ref_column": "po_ref_number", "mssql_header_table": "CustomerPOULBQ",
        "mssql_detail_table": "CustomerPOULDetailBQ",
        # MSSQL's _BQ staging tables use camelCase columns, not BigQuery's snake_case —
        # confirmed live (ProgrammingError: "Invalid column name 'po_ref_number'") that
        # reusing ref_column here 500s every MSSQL check. See rgmc-gcp-api's
        # mappings.py table_mappings['customerpoulbq']/['customerradetailbq'].
        "mssql_ref_column": "poRefNumber",
        # The table CustomerPOULBQ's own AFTER INSERT trigger promotes a matching row
        # into — see api_dashboard_mssql_status's "promoted" check.
        "mssql_promoted_table": "CustomerPOUL",
    },
    "ra": {
        "header_table": "int_document_ai_ra", "detail_table": "int_document_ai_ra_detail",
        "ref_column": "ra_ref_number", "mssql_header_table": "CustomerRABQ",
        "mssql_detail_table": "CustomerRADetailBQ", "mssql_ref_column": "raRefNumber",
        "mssql_promoted_table": "CustomerRA",
    },
    "ra_xml": {
        "header_table": "int_document_ai_ra_xml", "detail_table": "int_document_ai_ra_xml_detail",
        "ref_column": "ra_ref_number", "mssql_header_table": "CustomerRABQ",
        "mssql_detail_table": "CustomerRADetailBQ", "mssql_ref_column": "raRefNumber",
        "mssql_promoted_table": "CustomerRA",
    },
}


@app.route("/api/dashboard/files")
def api_dashboard_files():
    """Distinct uploaded file names for one upload type, grouped with counts — the
    dashboard's top-level search result."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    if upload_type not in DASHBOARD_UPLOAD_TYPES:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400

    params = {"upload_type": upload_type}
    ref_values = (request.args.get("ref_values") or "").strip()
    if ref_values:
        refs = _split_po_refs(ref_values)
        if refs:
            params["ref_values"] = ",".join(refs)
    company = (request.args.get("company") or "").strip().upper()
    if company:
        if upload_type != "po":
            return jsonify({"error": "company filtering is only supported for upload_type=po"}), 400
        terms = BQ_COMPANY_NAME_TERMS.get(company)
        if not terms:
            return jsonify({"error": f"No known company-name terms for {company!r} yet."}), 400
        params["company_terms"] = ",".join(terms)
    for key in ("date_from", "date_to"):
        value = (request.args.get(key) or "").strip()
        if value:
            params[key] = value

    try:
        resp = _gcp_api("GET", "/bigquery_routes/upload-dashboard/files", params=params, timeout=60)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/dashboard/file-records")
def api_dashboard_file_records():
    """Every header row (one per PO/RA ref) sharing one uploaded file_name — the
    Expand action's data."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    file_name = request.args.get("file_name") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400

    try:
        # timeout > 60s (not the usual API_TIMEOUT/60) — rgmc-gcp-api's own BigQuery read
        # has its own internal 60s cap (BQ_READ_TIMEOUT_SECONDS), so a client timeout of
        # exactly 60s races it: the client's socket can read-timeout right at (or just
        # before) the server would have returned its own clean 504, surfacing a raw
        # "Read timed out" instead. Matches /api/bigquery/document-ai/search's timeout=90.
        resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": config_["header_table"], "where_column": "file_name", "where_value": file_name,
        }, timeout=90)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/dashboard/detail-records")
def api_dashboard_detail_records():
    """Detail lines for one PO/RA ref — the More Details action's data."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    ref_value = request.args.get("ref_value") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not ref_value:
        return jsonify({"error": "ref_value is required"}), 400

    try:
        # See api_dashboard_file_records above — same race against rgmc-gcp-api's own
        # internal 60s BigQuery timeout.
        resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
            "table_name": config_["detail_table"], "where_column": config_["ref_column"], "where_value": ref_value,
        }, timeout=90)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/dashboard/mssql-status")
def api_dashboard_mssql_status():
    """Whether this PO/RA ref's header and detail rows have already been copied into
    the MSSQL _BQ staging tables (CustomerPOULBQ/CustomerPOULDetailBQ for PO,
    CustomerRABQ/CustomerRADetailBQ for RA and RA(xml)) — not the promoted
    CustomerPOUL/CustomerRA tables, per the dashboard's own spec."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    ref_value = request.args.get("ref_value") or ""
    config_ = DASHBOARD_UPLOAD_TYPES.get(upload_type)
    if not config_:
        return jsonify({"error": f"upload_type must be one of {sorted(DASHBOARD_UPLOAD_TYPES)}"}), 400
    if not ref_value:
        return jsonify({"error": "ref_value is required"}), 400

    def _fetch(table_name):
        """Returns (rows, error) — error is None on success, or a short diagnostic
        string on failure, so a bad column/table name shows up in the response instead
        of a generic "lookup failed" (see api.py git history: a prior version of this
        reused the BigQuery snake_case ref_column here, 500ing every call with
        "Invalid column name 'po_ref_number'" — MSSQL's _BQ tables use camelCase).
        Returns full rows (not just a count) so the dashboard's variance checker modal
        can compare them against the BigQuery detail rows it already has."""
        try:
            resp = _gcp_api("GET", "/sbic/by_table/value", params={
                "table_name": table_name, "where_column": config_["mssql_ref_column"], "where_value": ref_value,
            }, timeout=30)
            body, status_code = _proxy_json(resp)
            if status_code == 200:
                return body.get("data") or [], None
            return None, str(body.get("detail") or body.get("error") or f"HTTP {status_code}")[:200]
        except requests.RequestException as exc:
            return None, f"Could not reach rgmc-gcp-api: {exc}"

    with ThreadPoolExecutor(max_workers=3) as ex:
        header_future = ex.submit(_fetch, config_["mssql_header_table"])
        detail_future = ex.submit(_fetch, config_["mssql_detail_table"])
        # Checked alongside the staging-table lookups above (not gated behind them
        # being found first) so one round trip covers both — CustomerPOULBQ's own
        # AFTER INSERT trigger is what actually promotes a row into this table, a
        # separate, silent, server-side step a staging-table-only check can't see
        # (see bigquery_bridge.py's __verify_trigger_promotion for the same check on
        # the automated-bridge side). "ref_value not in staging at all" and "in
        # staging but never promoted" both come back as promoted_in_mssql=False here —
        # the caller tells them apart via header_in_mssql.
        promoted_future = ex.submit(_fetch, config_["mssql_promoted_table"])
        header_rows, header_error = header_future.result()
        detail_rows, detail_error = detail_future.result()
        promoted_rows, promoted_error = promoted_future.result()

    return jsonify({
        "header_in_mssql": header_rows is not None and len(header_rows) > 0,
        "detail_count_in_mssql": None if detail_rows is None else len(detail_rows),
        "detail_data": detail_rows or [],
        "promoted_in_mssql": None if promoted_rows is None else len(promoted_rows) > 0,
        # Retrigger (cleanup-staging + insert-from-bigquery) only exists for PO
        # uploads — rgmc-gcp-api has no RA equivalent of either endpoint.
        "retrigger_supported": upload_type == "po",
        "check_failed": header_rows is None or detail_rows is None,
        "error": header_error or detail_error or promoted_error,
    })


@app.route("/api/dashboard/bc-status")
def api_dashboard_bc_status():
    """Whether a PO ref already has a Sales Order in Business Central — PO + SBIC only,
    same scope every other BC-facing action in this app uses (BC Production has no MTC
    company — see RECONCILE_COMPANIES)."""
    upload_type = (request.args.get("upload_type") or "").strip().lower()
    po_ref = request.args.get("ref_value") or ""
    company = (request.args.get("company") or "").strip().upper()
    if upload_type != "po":
        return jsonify({"error": "Business Central status is only available for upload_type=po"}), 400
    if company != "SBIC":
        return jsonify({"error": "Business Central status is only available for company=SBIC"}), 400
    if not po_ref:
        return jsonify({"error": "ref_value is required"}), 400

    try:
        safe_ref = po_ref.replace("'", "''")
        resp = _bc_api("GET", "/bc/custom/v2/sales-orders", params={
            "company": company, "filter": f"externalDocumentNo eq '{safe_ref}'",
            "expand": "salesOrderLines",
        }, timeout=30)
        body, status_code = _proxy_json(resp)
        if status_code != 200:
            return jsonify(body), status_code
        orders = body.get("data") or []
        return jsonify({"in_bc": len(orders) > 0, "orders": orders})
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/dashboard/queue-missing-items", methods=["POST"])
def api_dashboard_queue_missing_items():
    """Bulk-queue missing Sales Order lines for one or more PO refs that already have an
    Open order in Business Central — a one-click shortcut from the Upload Dashboard's BC
    comparison view straight into the same so_missing_item_fixes_{env} Firestore buffer
    the Missing Items tab reads from, so Transfer to BC there picks these up too. Reuses
    _find_missing_items (same matching rules as the Missing Items tab) instead of
    re-deriving the comparison from whatever the dashboard already has client-side, so an
    order/line only ever gets queued from a fresh, server-validated BC + BigQuery read.

    Still only ever QUEUES — see _find_missing_items/api_queue_missing_item_fix, the one
    real BC write stays behind Apply Missing Item Fixes on the Buffer Reconciliation page.
    Only a "ready" item (same readiness bar the Missing Items tab's own one-click Queue
    button requires — a resolved BC item AND a BC-valid Unit of Measure) gets queued;
    "needs_linking"/"needs_uom" items are reported back as skipped rather than queued
    with an item_no/UOM that Apply would just reject.

    po_ref_number may be a single ref or a comma-separated list (e.g. every PO ref under
    one uploaded file) — _build_missing_items_filter already ORs multiple refs into one
    BC search, so this naturally scopes to "every PO under this file that's both in BC
    and still has a missing line," with no separate per-PO looping needed here."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    po_ref_number = (data.get("po_ref_number") or "").strip()
    if company != "SBIC":
        return jsonify({"error": "Only company=SBIC is supported here"}), 400
    if not po_ref_number:
        return jsonify({"error": "po_ref_number is required"}), 400

    try:
        groups = _find_missing_items(company, po_ref_number, "", "")
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502

    queued, skipped, errors = [], [], []
    for group in groups:
        order = group["order"]
        po_ref = order.get("externalDocumentNo") or ""
        for item in group["missing_items"]:
            label = item.get("customer_sku_code") or item.get("customer_sku_desc") or item["sku_key"]
            if item["status"] != "ready":
                skipped.append({"po_ref": po_ref, "sku": label, "reason": item["status"]})
                continue
            resolved_item = item.get("resolved_item") or {}
            try:
                resp = _bc_api("POST", "/bc/custom/v2/so-buffer/missing-item-fixes", json={
                    "company": company, "order_id": order.get("id"), "order_no": order.get("number") or "",
                    "po_ref": po_ref, "sku_key": item["sku_key"],
                    "customer_sku_code": item.get("customer_sku_code") or "",
                    "customer_sku_desc": item.get("customer_sku_desc") or "",
                    "item_no": resolved_item.get("itemNo"), "item_description": resolved_item.get("description") or "",
                    "po_qty": item.get("po_qty") or 0, "unit_of_measurement": item.get("unit_of_measurement") or "",
                    "unit_price": item.get("unit_price") or 0, "resolved_by": "upload-dashboard",
                })
                body, status_code = _proxy_json(resp)
                if status_code == 201:
                    queued.append({"po_ref": po_ref, "item_no": resolved_item.get("itemNo"), "sku": label})
                else:
                    errors.append({"po_ref": po_ref, "sku": label, "detail": body.get("detail") or body.get("error") or f"HTTP {status_code}"})
            except requests.RequestException as exc:
                errors.append({"po_ref": po_ref, "sku": label, "detail": f"Could not reach rgmc-bc-api: {exc}"})

    return jsonify({
        "orders_with_missing_items": len(groups),
        "queued": queued, "queued_count": len(queued),
        "skipped": skipped, "skipped_count": len(skipped),
        "errors": errors, "errors_count": len(errors),
    })


@app.route("/api/dashboard/add-to-buffer", methods=["POST"])
def api_dashboard_add_to_buffer():
    """Add a PO (not yet in Business Central) to the SO-import buffer — thin wrapper
    around the same _insert_and_buffer the BigQuery Lookup tab's Quick Align already
    uses, given one already-fetched header + its detail lines straight from this
    dashboard's own file-records/detail-records calls."""
    data = request.get_json(silent=True) or {}
    header = data.get("header")
    details = data.get("details") or []
    employee = data.get("employee") or {}
    if not header:
        return jsonify({"error": "header is required"}), 400
    header = {**header, "_source": "bigquery"}
    return jsonify(_insert_and_buffer([header], details, employee))


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
        body["trigger_status"] = trigger_status
    return body, status_code


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


@app.route("/api/handoff-history/retrigger-mssql", methods=["POST"])
def api_handoff_history_retrigger_mssql():
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
    """
    data = request.get_json(silent=True) or {}
    po_ref = (data.get("po_ref_number") or "").strip()
    if not po_ref:
        return jsonify({"error": "po_ref_number is required"}), 400

    try:
        search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                params={"po_ref_number": po_ref}, timeout=60)
        search_body, search_status = _proxy_json(search_resp)
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502
    if search_status != 200:
        return jsonify(search_body), search_status
    headers = search_body.get("data") or []
    details = search_body.get("detail_data") or []
    if not headers:
        return jsonify({"error": f"No BigQuery int_document_ai row found for po_ref_number={po_ref!r} — nothing to re-insert."}), 404
    header = headers[0]
    file_name = header.get("file_name")

    body, status_code = _cleanup_and_reinsert_po(po_ref, header, details, file_name)
    return jsonify(body), status_code


@app.route("/api/handoff-history/retrigger-mssql-batch", methods=["POST"])
def api_handoff_history_retrigger_mssql_batch():
    """Same cleanup-then-reinsert retrigger as /api/handoff-history/retrigger-mssql,
    for every PO matching ONE of three selectors (exactly one must be given):

      po_ref_numbers — explicit list (comma/newline/whitespace separated)
      file_name      — every PO BigQuery's int_document_ai has under this exact
                        fileName (same lookup /api/dashboard/file-records uses)
      date_from/date_to — every PO with int_document_ai.created_at in this range
                           (inclusive), straight from /bigquery_routes/document-ai/search

    PO uploads only (group_code=customerpoul) — same BigQuery source table and
    insert-from-bigquery/cleanup-staging endpoints the single-PO route depends on,
    neither of which exist for RA or Online Sales PO.

    Resolves to a po_ref_number list first, then ONE document-ai/search call (it
    already accepts a comma-separated po_ref_number list) fetches every matching
    header/detail row in a single round trip — detail rows are then split per po_ref
    (search_document_ai's response mixes every matched PO's lines together) before
    each PO gets its own independent cleanup+reinsert, so one PO's failure never
    blocks the rest of the batch.

    Headers are deliberately re-fetched by po_ref_number (not read straight off the
    file-scoped lookup used to build that po_ref list) — confirmed live (2026-10-28)
    that BigQuery's int_document_ai rows for a given PO can get re-stamped under a
    brand new file_name by an upstream dbt/export-pipeline rematerialization (the
    same known watermark/created_at-stamping quirk already documented elsewhere),
    sometimes while this call is still running — the OLD file_name can drop to zero
    rows entirely while the SAME data now lives under a new one. Searching by
    po_ref_number finds the current data regardless of which file_name it's
    currently filed under; resolving by the original file_name a second time would
    just 404 once that rename has happened.
    """
    data = request.get_json(silent=True) or {}
    raw_refs = (data.get("po_ref_numbers") or "").strip()
    file_name = (data.get("file_name") or "").strip()
    date_from = (data.get("date_from") or "").strip()
    date_to = (data.get("date_to") or "").strip()
    selectors_given = sum(bool(x) for x in (raw_refs, file_name, (date_from or date_to)))
    if selectors_given != 1:
        return jsonify({"error": "Give exactly one of: po_ref_numbers, file_name, or date_from/date_to"}), 400

    try:
        if raw_refs:
            po_refs = sorted(set(_split_po_refs(raw_refs)))
            search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                    params={"po_ref_number": ",".join(po_refs), "limit": 1000}, timeout=90)
        elif file_name:
            # timeout=90 — see api_dashboard_file_records' comment on this same
            # /bigquery_routes/by_table/value race against rgmc-gcp-api's internal 60s cap.
            file_resp = _gcp_api("GET", "/bigquery_routes/by_table/value", params={
                "table_name": "int_document_ai", "where_column": "file_name", "where_value": file_name,
            }, timeout=90)
            file_body, file_status = _proxy_json(file_resp)
            if file_status != 200:
                return jsonify(file_body), file_status
            po_refs = sorted({r.get("po_ref_number") for r in (file_body.get("data") or []) if r.get("po_ref_number")})
            if not po_refs:
                return jsonify({"error": f"No BigQuery int_document_ai rows found for file_name={file_name!r}."}), 404
            search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                    params={"po_ref_number": ",".join(po_refs), "limit": 1000}, timeout=90)
        else:
            search_resp = _gcp_api("GET", "/bigquery_routes/document-ai/search",
                                    params={"date_from": date_from, "date_to": date_to, "limit": 1000}, timeout=90)
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

    results = []
    for header in headers:
        po_ref = header.get("po_ref_number")
        if not po_ref:
            continue
        po_details = [d for d in details if d.get("po_ref_number") == po_ref]
        body, status_code = _cleanup_and_reinsert_po(po_ref, header, po_details, header.get("file_name"))
        results.append({
            "po_ref": po_ref, "status": "success" if status_code == 200 else "error",
            "detail": body.get("error") or body.get("message") or "Re-inserted successfully.",
            "trigger_status": body.get("trigger_status"),
        })

    return jsonify({
        "data": results, "total": len(results),
        "ok_count": sum(1 for r in results if r["status"] == "success"),
        "error_count": sum(1 for r in results if r["status"] == "error"),
    })


@app.route("/api/dashboard/source-file")
def api_dashboard_source_file():
    """Signed URL to the original uploaded file behind a Document AI file_name — the
    View File action."""
    upload_type = (request.args.get("upload_type") or "po").strip().lower()
    file_name = request.args.get("file_name") or ""
    if not file_name:
        return jsonify({"error": "file_name is required"}), 400
    try:
        resp = _gcp_api("GET", "/bigquery_routes/document-ai/source-file", params={
            "file_name": file_name, "upload_type": upload_type,
        }, timeout=30)
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=False)
