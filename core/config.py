"""Shared static configuration: env-based endpoints/timeouts, BC company maps, email/process registries."""
import os

BC_API_BASE = os.environ.get("BC_API_BASE", "https://rgmc-bc-api-prod-935246372408.asia-southeast1.run.app")
GCP_API_BASE = os.environ.get("GCP_API_BASE", "https://rgmc-gcp-api-935246372408.asia-southeast1.run.app")
API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "30"))

# Client-side timeout for every call to rgmc-gcp-api's two generic BigQuery-backed
# lookup routes (/bigquery_routes/by_table/value, /bigquery_routes/document-ai/search).
# Those routes' own internal BigQuery read has a 120s cap (bumped from 60s on
# 2026-10-29 — confirmed live that a multi-PO lookup across one large uploaded file,
# e.g. the "Retrigger Handoff (cleanup & re-insert all)" batch action, can genuinely
# take longer than 60s against int_document_ai_detail's current size). This must stay
# comfortably above that cap so a slow-but-successful query — or the backend's own
# clean 504 once ITS timeout fires — has time to actually come back, instead of this
# app's own socket read timing out first and surfacing a raw connection error instead.
BQ_PROXY_TIMEOUT = int(os.environ.get("BQ_PROXY_TIMEOUT", "150"))

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

# Fallback for rows whose companyName text is blank — confirmed live 2026-11-05/06 on
# several PO407xxxx refs: CustomerPOUL headers manually encoded through SBIC's legacy
# app carry a correct, non-blank companyId but an empty companyName string, which
# _resolve_bc_company used to reject outright ("could not resolve a BC company"),
# excluding otherwise-good MSSQL data from Handoff/buffering. companyId is a clean
# numeric key (see rgmc-gcp-api's CustomerPOUL.py: "companyId=6 on sbic_prod" etc.).
# Deliberately no entry for 12 (MTC) — see RECONCILE_COMPANIES above: BC Production has
# no MTC company, so this must never resolve an MTC row to a BC company either.
_COMPANY_ID_MAP = {6: "SBIC"}


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

# submittedBy tag rgmc-worker-pool stamps on every Sales Order it creates via the
# automated SO-import path — used to scope the Missing Ship-To and Customer/Ship-To
# Mismatch tabs' BC searches to only orders this app's own automation created, never a
# human-entered order in BC.
_SBIC_AI_SUBMITTED_BY = "SBIC AI Uploading"


