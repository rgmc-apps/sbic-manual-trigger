import os
import smtplib
import requests
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
                "key": branch_name, "po_refs": [], "buffer_ids": [],
            })
            g["po_refs"].append(po_ref)
            g["buffer_ids"].append(buffer_id)

        if customer_name:
            g = customer_groups.setdefault(customer_name.upper(), {
                "key": customer_name, "po_refs": [], "buffer_ids": [],
            })
            g["po_refs"].append(po_ref)
            g["buffer_ids"].append(buffer_id)

        for line in order.get("lines") or []:
            sku = (line.get("customerSKUCode") or "").strip()
            if not sku:
                continue
            g = sku_groups.setdefault(sku.upper(), {
                "key": sku,
                "description": line.get("customerSKUDesc") or "",
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


@app.route("/api/buffer")
def api_buffer():
    """Buffered orders for one company, pre-grouped by SKU / branch / customer,
    with any previously-saved manual links merged in."""
    company = (request.args.get("company") or "").strip().upper()
    if not company:
        return jsonify({"error": "company is required"}), 400
    try:
        orders_resp = _bc_api("GET", "/bc/custom/v2/so-buffer", params={"company": company})
        orders_body, orders_status = _proxy_json(orders_resp)
        if orders_status != 200:
            return jsonify(orders_body), orders_status

        overrides_resp = _bc_api("GET", "/bc/custom/v2/so-buffer/overrides")
        overrides_body, overrides_status = _proxy_json(overrides_resp)
        overrides = overrides_body.get("data", []) if overrides_status == 200 else []

        orders = orders_body.get("data", [])
        return jsonify({
            "company": company,
            "order_count": len(orders),
            "orders": orders,
            "groups": _group_buffer(orders, overrides),
        })
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502


@app.route("/api/suggest/shipto/<po_ref>")
def api_suggest_shipto(po_ref):
    """Fuzzy BC ship-to suggestions for one PO, via rgmc-gcp-api (Cloud SQL + BC)."""
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}/shipto-match")
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-gcp-api: {exc}"}), 502


@app.route("/api/suggest/item/<po_ref>")
def api_suggest_item(po_ref):
    """Fuzzy BC item suggestions for one PO's lines, via rgmc-gcp-api (Cloud SQL + BC)."""
    try:
        resp = _gcp_api("GET", f"/customerpoul/{po_ref}/item-match")
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
