"""BC company resolution: map a raw company name/id to a BC company code, dedupe duplicate BigQuery headers, and filter by known company-name text variants."""
from .config import _COMPANY_KEYWORD_MAP, _COMPANY_ID_MAP, BQ_COMPANY_NAME_TERMS

def _resolve_bc_company(source_company_name: str | None, company_id: int | None = None) -> str | None:
    upper = (source_company_name or "").upper()
    for keyword, bc_company in _COMPANY_KEYWORD_MAP:
        if keyword in upper:
            return bc_company
    if company_id is not None:
        return _COMPANY_ID_MAP.get(company_id)
    return None


def _merge_duplicate_headers(headers: list, details: list) -> list:
    """Collapses duplicate BigQuery int_document_ai header rows sharing the same
    po_ref_number down to one per ref — same merge rule as rgmc-worker-pool's
    retrigger_client.merge_duplicate_headers / bigquery_bridge.py's
    __merge_duplicate_po_refs (duplicated here for the same reason the SUNCOAST/SBIC
    company map is duplicated across these repos, see _COMPANY_KEYWORD_MAP above).

    Confirmed live 2026-11-05/06: the same PO can land in int_document_ai more than
    once under a different company_name (e.g. a "SUNCOAST BRANDS" ghost-tag row with
    zero matching detail lines, alongside the real row with real lines) — without this,
    both the BigQuery Lookup tab's Insert action and the batch retrigger route would
    act on every duplicate independently instead of once.

    Tie-break, most-preferred first: company_name (+company_id, if present) resolves to
    a BC-postable company (via _resolve_bc_company), has >=1 matching row in details,
    has a non-blank company_name at all (even if not BC-routable — keeps this tier
    instead of dropping it when BC-routability was added), latest created_at. Returns
    one header per po_ref_number, in first-seen order.

    BC-routability deliberately outranks "has items": confirmed live 2026-11-06 that a
    po_ref's duplicates can split exactly this way — a BC-routable "SUNCOAST BRANDS"
    header with zero of its own matching detail rows, alongside a "MANILA TASTE
    CURATORS VENTURES" header (MTC has no BC company configured, so it can never reach
    BC at all) that DOES have the real detail rows. Picking the BC-routable header here
    is safe even though it looks item-less: every caller matches a po_ref's detail rows
    from the full `details` list independently of which header this function returns,
    so the real lines still attach regardless — this only decides which company_name
    attribution the resulting order gets.
    """
    detail_refs = {d.get("po_ref_number") for d in details if d.get("po_ref_number")}

    def _score(h: dict):
        is_bc_routable = _resolve_bc_company(h.get("company_name"), h.get("company_id")) is not None
        has_items = h.get("po_ref_number") in detail_refs
        has_company = bool((h.get("company_name") or "").strip())
        return (is_bc_routable, has_items, has_company, h.get("created_at") or "")

    best_by_ref: dict = {}
    order: list = []
    for h in headers:
        ref = h.get("po_ref_number")
        if not ref:
            continue
        current = best_by_ref.get(ref)
        if current is None:
            order.append(ref)
            best_by_ref[ref] = h
        elif _score(h) > _score(current):
            best_by_ref[ref] = h
    return [best_by_ref[ref] for ref in order]


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
