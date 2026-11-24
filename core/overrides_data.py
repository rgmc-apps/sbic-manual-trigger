"""Shared read-only lookups against rgmc-bc-api's saved overrides and BC item-reference tables."""
import requests
from .http_clients import _bc_api, _proxy_json

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


def _fetch_item_ref_map(company: str) -> dict:
    """Upper-cased customerSKUCode (referenceNo) -> {"itemNo", "description"}, from BC's
    own Item Reference table (Pag50349, referenceType="Customer" only) — same convention
    rgmc-worker-pool's so_import_worker.py._build_ref_map uses for the automated import/
    reprocess-buffer/retrigger paths. Filtered to "Customer" type there for the same
    reason: Item/Variant/Bar Code/Vendor reference rows can share a referenceNo value
    with an unrelated item's customer SKU code."""
    try:
        resp = _bc_api("GET", "/bc/custom/v2/item-references", params={
            "reference_type": "Customer", "company": company,
        })
        body, status_code = _proxy_json(resp)
    except requests.RequestException:
        return {}
    if status_code != 200:
        return {}
    return {
        (r.get("referenceNo") or "").strip().upper(): {
            "itemNo": r.get("itemNo"), "description": r.get("description") or "",
        }
        for r in (body.get("data") or [])
        if r.get("itemNo") and r.get("referenceNo")
    }


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
