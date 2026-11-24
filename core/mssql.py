"""Shared MSSQL (CustomerPOUL/CustomerPOULDetail) read helpers, used across several route domains as a fallback source alongside BigQuery."""
import re
import requests
from .http_clients import _gcp_api, _proxy_json

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
        "company_id": row.get("companyId"),
        "po_date": row.get("poDate"),
        "delivery_date": row.get("deliveryDate"),
        "cancellation_date": row.get("cancellationDate"),
        "remark": row.get("remark"),
        "created_at": row.get("createDate"),
        "create_by": row.get("createBy"),
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
