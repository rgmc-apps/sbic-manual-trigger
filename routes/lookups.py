"""Fuzzy ship-to/item suggestions and item/customer/UOM/ship-to search lookups, all proxied to rgmc-bc-api/rgmc-gcp-api."""
import requests
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, request, jsonify
from core.http_clients import _bc_api, _gcp_api, _proxy_json
from core.overrides_data import _fetch_overrides_by_type, _match_branch_override, _match_customer_override
from core.mssql import _fetch_from_customerpoul

bp = Blueprint("lookups", __name__)


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


@bp.route("/api/suggest/shipto/<po_ref>")
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


@bp.route("/api/suggest/item/<po_ref>")
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


@bp.route("/api/lookup/items")
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


@bp.route("/api/lookup/customers")
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


@bp.route("/api/customer-name-matches")
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


@bp.route("/api/lookup/uom")
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


@bp.route("/api/lookup/ship-to")
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
