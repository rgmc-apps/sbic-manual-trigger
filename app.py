import os
import smtplib
import requests
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify
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

# BC company codes this UI knows about, for the company picker.
RECONCILE_COMPANIES = ["SBIC", "MTC"]

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


def send_email(subject: str, html_body: str) -> bool:
    if not EMAIL_CONFIG["smtp_user"] or not EMAIL_CONFIG["smtp_password"]:
        app.logger.warning("Email credentials not set — skipping send")
        return False

    to_addr = EMAIL_CONFIG["notification_email"]
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
    resp = requests.request(method, f"{BC_API_BASE}{path}", timeout=API_TIMEOUT, **kwargs)
    return resp


def _gcp_api(method: str, path: str, **kwargs):
    resp = requests.request(method, f"{GCP_API_BASE}{path}", timeout=API_TIMEOUT, **kwargs)
    return resp


def _proxy_json(resp: requests.Response):
    """Return (body, status) for a proxied response, tolerating a non-JSON error body."""
    try:
        body = resp.json()
    except Exception:
        body = {"error": resp.text}
    return body, resp.status_code


def _group_buffer(orders: list, overrides: list) -> dict:
    """Group buffered orders by shared SKU code / customer branch name / customer name.

    Each group is resolvable once and applies to every buffered PO sharing that exact
    raw value — this is the "consolidate the POs with the same item codes / customer
    branch name / customer name" behavior. overrides is the flat list from
    GET /bc/custom/v2/so-buffer/overrides.
    """
    overrides_by_key: dict[str, dict] = {}
    for ov in overrides:
        overrides_by_key[f"{ov.get('type')}::{(ov.get('key') or '').strip().upper()}"] = ov

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

    return {
        "sku": _finalize(sku_groups, "sku"),
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

    orders = orders_body.get("data", [])
    if recover:
        _recover_missing_lines(orders)
    return {
        "company": company,
        "order_count": len(orders),
        "orders": orders,
        "groups": _group_buffer(orders, overrides),
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
    """Search existing BC items by description, via rgmc-bc-api."""
    company = request.args.get("company", "")
    search = (request.args.get("search") or "").strip()
    if not search:
        return jsonify({"data": []})
    esc = search.replace("'", "''")
    try:
        resp = _bc_api("GET", "/bc/custom/v2/items", params={
            "company": company,
            # RGMC's custom items page (Pag50310) names this field "description",
            # not BC standard's "displayName".
            "filter": f"contains(description,'{esc}')",
        })
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/lookup/customers")
def api_lookup_customers():
    """Search existing BC customers by name, via rgmc-bc-api."""
    company = request.args.get("company", "")
    search = (request.args.get("search") or "").strip()
    if not search:
        return jsonify({"data": []})
    esc = search.replace("'", "''")
    try:
        resp = _bc_api("GET", "/bc/custom/v2/customers", params={
            "company": company,
            # RGMC's custom customers page names these fields "name" and "customerNo",
            # not BC standard's "displayName"/"number".
            "filter": f"contains(name,'{esc}')",
        })
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


@app.route("/api/overrides", methods=["POST"])
def api_save_override():
    """Save the user's chosen BC link for one SKU code / branch name / customer name."""
    data = request.get_json(silent=True) or {}
    override_type = (data.get("type") or "").strip()
    key = (data.get("key") or "").strip()
    resolved = data.get("resolved") or {}
    resolved_by = (data.get("resolved_by") or "").strip()

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


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    """Trigger the existing POUL SO reprocess-buffer pass for one company.

    NOTE: this re-runs the normal buffer retry (rgmc-gcp-api -> Pub/Sub ->
    rgmc-worker-pool), unchanged by this feature. Manual links saved via
    /api/overrides are persisted for reference but are not yet consulted by
    rgmc-worker-pool's order-creation logic, so a PO whose broken SKU/branch/customer
    caused the original failure will likely fail again the same way until that
    follow-up ships. This still re-triggers correctly for orders that were buffered
    for an unrelated, since-resolved reason.
    """
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        resp = _gcp_api("POST", "/customerpoul/reprocess-buffer", params={"companies": company})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


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
