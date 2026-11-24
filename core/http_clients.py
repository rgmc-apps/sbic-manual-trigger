"""Thin proxy helpers to the two backend services this app talks to: rgmc-bc-api (BC/Firestore) and rgmc-gcp-api (MSSQL/BigQuery)."""
import requests
from flask import jsonify
from .config import BC_API_BASE, GCP_API_BASE, API_TIMEOUT

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


def _bc_proxy_get_json(path: str, params: dict | None = None):
    """GET path via _bc_api and hand the proxied JSON straight back as a Flask response
    tuple — the fetch+jsonify+network-error shape repeated across every list/search/
    history endpoint that just forwards BC's response with no extra processing."""
    try:
        resp = _bc_api("GET", path, params=params or {})
        body, status_code = _proxy_json(resp)
        return jsonify(body), status_code
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not reach rgmc-bc-api: {exc}"}), 502
