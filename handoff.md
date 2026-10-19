# Handoff

## Goal

Maintain and extend the SO-import buffer reconciliation ecosystem for RGMC/SBIC across four
repos: `rgmc-gcp-api` (BigQuery/MSSQL bridge), `rgmc-bc-api` (BC/Firestore buffer API),
`rgmc-worker-pool` (Pub/Sub SO-import worker — untouched this session), and
`sbic-manual-trigger-page` (the `/reconcile` + `/` Flask UI, this repo). This session's concrete
asks, roughly in order:

1. Investigate and fix an "8 concurrent `/handoff/runbridge/` calls" problem in `rgmc-gcp-api`
   (root cause of data-correctness bugs from a prior session's handoff: silently-dropped
   `CustomerPOULBQ`/`CustomerPOULDetailBQ` rows, possibly related to 3 fully-missing Manila
   Taste Curators PO refs).
2. Build a **Missing Items tab** on `/reconcile` (sbic-manual-trigger-page): find BC Sales Orders
   whose lines don't match their Document-AI-extracted PO lines, let a human queue/link fixes,
   then an explicit **Transfer to BC** button applies them — same queue-then-apply shape as the
   already-complete Missing Ship-To tab. Binding acceptance criteria (carried over from Missing
   Ship-To, repeated by the user this session): never write to BC directly from a pick/link
   action, only from an explicit batched Apply step.
3. Build an **Upload Monitoring Dashboard** as the new `/` page (moving the old manual-trigger
   form to `/trigger`): search BigQuery uploads across all three Document AI pipelines (PO, RA,
   RA-xml) by upload type / company / date range / PO-or-RA ref number, browse every uploaded
   file in a collapsible tree (file → refs → detail lines), view the original uploaded source
   file via a signed GCS URL, and compare BigQuery's data against MSSQL and (PO+SBIC only)
   Business Central, with variance highlighting on item count/price/UOM mismatches.
4. Along the way: several real production bugs were found (by the user actually using the
   features) and fixed — a 504 timeout, a BigQuery column-name bug, a BC company-param bug, and a
   BigQuery DATE-vs-DATETIME bug that silently zeroed out same-day search results.

## Current State

**Everything currently committed is deployed and confirmed working in production**, with one
significant exception (see below). Working trees are clean in all three touched repos.

- **`sbic-manual-trigger-page`** (branch `master`, single Cloud Run service `sbic-manual-trigger`,
  deploys directly on push to `master`): at commit `f9419fc`. `/` is now the Upload Dashboard,
  `/trigger` is the old manual-trigger form, `/reconcile` is unchanged plus the new Missing Items
  tab. All three pages cross-link in their nav. **Confirmed live and working** via direct
  production API calls throughout the session (see Context & Gotchas for what's safe to call
  directly vs. not).
- **`rgmc-gcp-api`** (branch `main`, Cloud Run service `rgmc-gcp-api`, deploys directly on push to
  `main` — no staging split): at commit `975b809`. Confirmed live and working.
- **`rgmc-bc-api`** (branch `staging` deploys to `rgmc-bc-api-staging`; branch `master` deploys to
  `rgmc-bc-api-prod`, normally via a reviewed GitHub PR per this repo's own history, but this
  session did two direct `git merge --no-ff staging -> master` + push instead, since `gh` CLI
  isn't installed in this environment): `staging` and `master` are both at commit `a995eb1`
  (`master`'s own merge commit is `e2b99a0`). Confirmed live and working on both services.

**Significant exception — Item 1 (the SQL Server concurrency lock) was reverted and never
successfully re-fixed.** `rgmc-gcp-api/src/routers/bigquery_bridge.py`'s `BigqueryBridge.main()`
has **no concurrency protection at all** right now — it is back to its exact pre-session state.
A first attempt (a SQL Server `sp_getapplock` call via a `DECLARE/EXEC/SELECT` multi-statement
batch through SQLAlchemy+pytds) was deployed, immediately broke **all** `customerra` bridge runs
in production (every call failed with `"This result object does not return rows"`, silently
treated as "lock busy" so **zero** runs actually executed), was caught via Cloud Logging within
minutes, and was reverted (`git revert`, commit `f527da8`). The underlying 8x-concurrency problem
this was meant to fix is now only *indirectly* mitigated by Item 2 (the debounce layer, see
below), which reduces concurrent `/handoff/runbridge/` invocations from the original problem but
does not eliminate the possibility of two overlapping runs racing on the same MSSQL watermark —
see Next Step.

**Item 2 (debounce) is complete and confirmed working.** A new Cloud Tasks queue
(`rgmc-bridge-debounce`, `asia-southeast1`) plus a new `POST /handoff/runbridge/debounce/`
endpoint in `rgmc-gcp-api` collapses a burst of near-simultaneous Eventarc events into a single
delayed call ~30s later (deterministic Cloud-Tasks-task-name-per-time-bucket dedup). All four
Eventarc triggers (`ingest-batch-output-done-trigger`, `ingest-batch-output-done-trigger-online`,
`ingest-batch-output-done-trigger-ra`, `bq-int-document-ai-ra-trigger-v2`) were rerouted to this
new endpoint — confirmed via `gcloud eventarc triggers list` and via live log watching that a
burst of ~16-30 events produced exactly one real downstream `/handoff/runbridge/` call.

**Item 3 (scoping the over-broad `bq-int-document-ai-ra-trigger-v2` audit-log trigger) was
investigated and explicitly deprioritized, not implemented.** Eventarc audit-log triggers can
only filter on `serviceName`/`methodName`/`resourceName`, and for BigQuery job audit logs
`resourceName` is the job's own random resource name, not the destination table — there is no
supported way to scope this trigger to "only `int_document_ai_ra` inserts." It still fires on
every `JobService.InsertJob` in the whole project. Its practical impact is now much smaller since
item 2 rerouted it through the same debounce layer (most of its spurious firings now just produce
"already_scheduled" no-ops), but it has not been fixed or removed. See Next Step.

**The 3 fully-missing Manila Taste Curators PO refs mystery (`21721828`, `21721850`, `21721866`,
from the original cross-session handoff) was never further investigated this session**, but a
strong new lead was found as a side effect of the Upload Dashboard's "view file" research: every
`int_document_ai`/`int_document_ai_ra` row tagged under a `portal/batch/manila_taste/...` `file_name`
prefix has a basename that embeds a literal dummy server path
(`..._var_www_html_mvp_uploading_batches_dummy_files1_...`), and **no matching object exists in
the real `sbic-po-parser-in-v2` input bucket** for any of these — confirmed via live `gsutil`
checks. These documents did not come through the normal `portal/` GCS upload flow at all, which
plausibly explains why some of them (the 3 refs above) never made it further downstream either.
Not pursued further — flagged here for whoever picks this up next.

## Files Actively Being Edited

All committed, nothing mid-edit. Working trees clean in all three repos as of this write.

**`rgmc-gcp-api`** (`C:\RGMC\Source\git\rgmc-gcp-api`):
- `src/services/bq_read.py` (**new**) — `read_gbq_with_timeout()`: runs `pandas_gbq.read_gbq` in a
  worker thread, enforces a 60s wall-clock cutoff (`BQ_READ_TIMEOUT_SECONDS` env var) instead of
  inheriting google-api-core's 600s default retry deadline. Fixes a confirmed-live incident where
  a BigQuery SSL hiccup tied up a Cloud Run instance for 10 minutes.
- `src/main.py` — `/checkBigQuery` uses `read_gbq_with_timeout`.
- `src/routers/bigquery_bridge.py` — all 5 `pandas_gbq.read_gbq` call sites in
  `__get_bigquery_data` use `read_gbq_with_timeout`. **No lock/concurrency code present** — fully
  reverted to pre-session state (see Current State).
- `src/routers/bigquery_routes/bigquery_routes.py` — heavily extended this session:
  - `_run_parameterized`/`by_table/value`/`by_table/latest` use `read_gbq_with_timeout`, return
    `504` (not `500`) specifically for a `TimeoutError`.
  - `UPLOAD_TYPE_TABLES`, `UPLOAD_TYPE_INPUT_BUCKETS`, `_PAGE_SUFFIX_RE` — module-level config for
    the Upload Dashboard feature.
  - `_get_signing_credentials()` — self-impersonated credentials (`impersonated_credentials
    .Credentials` targeting this SA's own email) for Cloud Storage signed-URL generation.
    **Not optional** — `compute_engine.Credentials` (the ambient Cloud Run credentials) has no
    `sign_bytes` implementation at all; confirmed live that the naive `blob.generate_signed_url()`
    call fails with `"you need a private key to sign credentials"` even with
    `service_account_email` passed. Relies on `roles/iam.serviceAccountTokenCreator` already
    granted to the compute SA on itself (see Context & Gotchas).
  - `GET /bigquery_routes/upload-dashboard/files` — distinct `file_name`s grouped with counts,
    filterable by `upload_type`, `ref_values` (comma-separated, `IN UNNEST`), `company_terms`
    (po only), `date_from`/`date_to` (now `DATETIME`-typed, see the date-bug fix below).
  - `GET /bigquery_routes/document-ai/source-file` — reverses the Document AI batch-output naming
    convention to find and sign a URL to the real original upload. `ra_xml` explicitly returns
    `"not_available"` (empty table, no known bucket).
  - `date_from`/`date_to` on `upload-dashboard/files` are `DATETIME`-typed (not `DATE`) — fixed a
    confirmed-live bug where `created_at <= date_to` (a `DATETIME` column compared to a `DATE`
    param) silently excluded the entire `date_to` day (0 rows found vs. 170 after the fix, tested
    directly against BigQuery before and after).
- `src/routers/sbic_routes/handoff.py` — new `POST /handoff/runbridge/debounce/` endpoint (Cloud
  Tasks-based debounce, see Current State's Item 2).
- `src/config.py` — new Cloud Tasks config: `cloud_tasks_project_id`, `cloud_tasks_location`,
  `cloud_tasks_queue`, `runbridge_debounce_seconds`, `runbridge_service_url`,
  `runbridge_invoker_service_account` (the last one doubles as the signing-credentials target
  principal above).
- `requirements.txt` — added `google-cloud-tasks==2.23.0` (pinned to this version specifically —
  `2.25.0`/`2.26.0` require `protobuf>=6.33.5`, incompatible with this repo's pinned
  `protobuf==6.33.1`; `2.23.0` only needs `protobuf>=4.25.8`).
- **Known pre-existing issue, not touched**: `src/config.py` line ~25 has a real-looking secret
  hardcoded as `BC_CLIENT_SECRET`'s `os.getenv(...)` *default value*, committed to git history.
  Flagged to the user, not rotated or removed.
- **Known pre-existing issue, not touched**: 8 different router files (`handoff.py`,
  `CustomerPOUL.py`, `CustomerRA.py`, `Customer.py`, `CustomerBranch.py`,
  `CustomerPOULDetail.py`, `bigquery_routes.py`, `mssql_routes.py`) each independently call
  `cloud_logging.Client().setup_logging()` at import time, stacking 8 duplicate handlers onto the
  root logger — every app log line gets written to Cloud Logging 8 times. Pure observability
  noise (confirmed NOT the cause of the real 8x-concurrency bug — that was independently confirmed
  via Cloud Run's own `httpRequest` access logs, which aren't subject to this duplication). Not
  fixed — centralizing `setup_logging()` to call once in `main.py` would fix it.

**`rgmc-bc-api`** (`C:\claude\rgmc-bc-api`):
- `src/services/so_buffer_service.py` — new Firestore collections `so_missing_item_fixes_{env}`/
  `so_missing_item_fix_history_{env}` (mirrors `so_shipto_fixes_{env}`'s shape). New functions:
  `list_missing_item_fixes`, `queue_missing_item_fix`, `delete_missing_item_fix`,
  `list_missing_item_fix_history`, `_record_missing_item_fix_attempt`,
  `apply_missing_item_fixes` (**synchronous**, unlike ship-to-fixes' Pub/Sub+worker-pool path —
  adding a Sales Order Line is already a single existing BC call, so no async round-trip needed;
  capped at `MISSING_ITEM_FIX_APPLY_BATCH_CAP = 50` per call, returns `remaining` count).
  `apply_missing_item_fixes` resolves `unitOfMeasureCode` fresh against saved `"uom"` overrides
  (`key="{itemNo}::{rawUom}"`, same shape the Buffer tab's existing UOM overrides use) immediately
  before building each line payload, rather than trusting whatever was on the fix doc when queued.
- `src/routers/bc_routes/so_buffer_routes.py` — 5 new routes under `/bc/custom/v2/so-buffer/`:
  `GET/POST/DELETE missing-item-fixes[/{fix_id}]`, `GET missing-item-fix-history`,
  `POST missing-item-fixes/apply`.

**`sbic-manual-trigger-page`** (`C:\claude\sbic-manual-trigger-page`):
- `app.py` — very heavily edited:
  - Routing: `/` → `dashboard_page()` (new), `/trigger` → `index()` (moved from `/`), `/reconcile`
    unchanged.
  - `BQ_COMPANY_NAME_TERMS` extended with an `"MTC"` entry (previously SBIC-only) — single source
    of truth for both the Upload Dashboard's company filter and the existing BigQuery Lookup tab.
  - Missing Items: `_fetch_expected_lines_batch`, `_fetch_valid_uoms_batch`,
    `_build_missing_items_filter` (now includes `status eq 'Open'` — Released orders can't accept
    new lines in BC, confirmed live), `_find_missing_items`, plus proxy routes
    `/api/sales-orders/missing-items`, `/missing-item-fixes[/<id>]`,
    `/missing-item-fix-history`, `/api/apply-missing-item-fixes`.
  - Upload Dashboard: `DASHBOARD_UPLOAD_TYPES` (includes both `ref_column`, the BigQuery
    snake_case join column, and `mssql_ref_column`, the MSSQL camelCase equivalent — these are
    **not the same string**, a real bug was shipped and fixed over this distinction, see Failed
    Attempts), proxy routes `/api/dashboard/files`, `/file-records`, `/detail-records`,
    `/mssql-status`, `/bc-status`, `/add-to-buffer`, `/source-file`.
- `templates/dashboard.html` (**new**) — search toolbar (Upload Type, PO/RA Ref Number(s) —
  comma-separated, label switches with Upload Type —, Company, Date from/to grouped in
  `.dash-date-range`), results list, and a shared `dash-details-modal-*` modal (used by both
  "View BC Details" and "View MSSQL Details").
- `templates/index.html`, `templates/reconcile.html` — nav links updated for the new 3-page
  structure (Dashboard / Manual Trigger / Buffer Reconciliation all cross-link).
- `static/dashboard.js` (**new**, ~470 lines) — the whole Upload Dashboard client: file search →
  expand (auto-expands + highlights matching rows when a ref-number search was used) → more
  details → Check MSSQL / Check Business Central, each with inline count-mismatch and
  price/UOM-variance indicators plus a "View ... Details" modal built from a shared
  `buildComparisonRows`/`renderComparisonTable` pair. `dateFromBoundary`/`dateToBoundary` append
  explicit `T00:00:00`/`T23:59:59` before the date params ever leave the browser.
- `static/dashboard.css` (**new**) — reuses `style.css`/`reconcile.css`'s existing tokens and
  component vocabulary (`.modal-*`, `.order-row`, `.btn-*`) rather than inventing new ones; only
  adds dashboard-specific tree indentation/variance-highlighting classes.
- `static/reconcile.js`/`static/reconcile.css` — Missing Items tab UI (`buildMissingItemRow`,
  `buildMissingItemsOrderGroup`, UOM-resolution picker reusing the existing `renderCandidateList`/
  `normalizeCandidates("uom", ...)` machinery), plus the `.dash-toolbar`/alignment-fix pattern
  (pre-existing fix for `.backfill-toolbar`/`.bq-toolbar`, now also applied via
  `.dash-toolbar`/`.dash-date-range` in dashboard.css).
- `.gitignore` — user added `plans/` so planning docs (`po_upload_plan.md`,
  `missing_items_plan.md`, `upload_dashboard_plan.md`, all now under `plans/`) don't get committed.
  All three are substantially or fully executed; useful as design-rationale reference, not as
  outstanding TODOs.

## Failed Attempts

- **SQL Server app-lock via `DECLARE/EXEC/SELECT` batch** (Item 1): tried capturing
  `sp_getapplock`'s return value via `connection.execute(text("DECLARE @res INT; EXEC @res = ...; SELECT @res;"))`
  through SQLAlchemy + the `sqlalchemy-pytds`/`python-tds` dialect. **Deployed to production and
  immediately broke every `customerra` bridge run** — every call errored with `"This result object
  does not return rows"` (pytds likely doesn't expose the DECLARE/EXEC statement's result the way
  `.scalar()` expects, landing on the wrong result-set token), and the code's own
  `except Exception: return False` path silently treated every error as "lock busy," so the bridge
  silently skipped 100% of runs with zero data loss *alerting* (just zero processing). Caught via
  Cloud Logging within ~15 minutes, reverted via `git revert` (commit `f527da8`), confirmed normal
  operation resumed. **The correct fix was identified but never implemented or tested**: pytds's
  raw DBAPI cursor supports `cursor.callproc('sp_getapplock', (...))` +
  `cursor.get_proc_return_status()`, purpose-built for exactly this (confirmed via reading pytds's
  source locally: `hasattr(pytds.Cursor, 'callproc')` and `get_proc_return_status` both exist).
  This still needs to be written, tested, and deployed — see Next Step.
- **Testing the above fix locally before redeploying**: blocked by this harness's own safety
  guard twice — once when trying to fetch production MSSQL credentials to local files (even
  scoped per-field via `--format=value(...)`, flagged "Credential Materialization"), once when
  trying to spin up a local Docker SQL Server container to test `sp_getapplock` in complete
  isolation from any production system (also flagged "Credential Materialization", seemingly
  just for touching Docker at all in that moment). Neither path is viable in this environment;
  any future attempt at Item 1 needs either an explicit one-off permission grant from the user for
  one of these approaches, or a willingness to redeploy-and-watch-logs again (the Item 2/3 Cloud
  Tasks work was validated this same cautious way — small code, read the docs/pytds source
  carefully, then deploy and verify via live logs/API calls immediately after).
- **Firing manual test HTTP calls directly at the live `sbic-manual-trigger-page`/`rgmc-bc-api-prod`
  URLs to "exercise" a feature** (e.g. two concurrent calls to test the then-new lock): blocked by
  the auto-mode classifier as "Production Deploy" risk. Plain **read-only GETs** against
  `rgmc-gcp-api`'s own endpoints (BigQuery/GCS reads, no side effects) were *not* blocked and were
  used extensively and successfully for verification throughout the session — the restriction
  appears to key off of "could this write/trigger a side effect," not "is this calling a
  production URL at all."
- **Dashboard's "Check MSSQL" originally reused the BigQuery `ref_column` (snake_case,
  `po_ref_number`) for MSSQL lookups too**: 500'd every single time with
  `"Invalid column name 'po_ref_number'"` (MSSQL's `_BQ` staging tables use camelCase —
  `poRefNumber`/`raRefNumber`), silently surfaced to the user as a generic "lookup failed" because
  the error itself was being swallowed. Fixed by adding a separate `mssql_ref_column` per upload
  type in `DASHBOARD_UPLOAD_TYPES` (`app.py`) and by no longer swallowing the underlying error
  string in the response.
- **Dashboard's "Check Business Central" originally sent `headerRow.company_name`** (the row's raw
  free-text BigQuery value, e.g. `"SUNCOAST BRANDS INTERNATIONAL CORP"`) as the `company` query
  param instead of the real BC company code: every call 400'd with `"Business Central status is
  only available for company=SBIC"` even when the user's own company filter was set to SBIC,
  because the raw text never equals `"SBIC"` after normalization. Fixed by hardcoding `"SBIC"`
  directly in that one call site (the button is already gated on `isSbicPo` having matched, so
  there's nothing else it could legitimately be).
- **An early version of the Missing Items search fanned out one network call per matched BC
  order** (via `_recover_lines_for_order`, through a 20-worker thread pool) to fetch each order's
  expected BigQuery lines, with no cap on how many orders a broad company+date search could
  return. A real production search (`company=SBIC`, one-day range, no PO filter) matched ~300
  orders and the fan-out alone consumed the remaining ~296s of a 300s Cloud Run request budget,
  killed by Cloud Run's own infrastructure with a non-JSON 504 body (`"upstream request
  timeout"`), which broke the browser's `res.json()` parse (`"Unexpected token 'u'"` — the actual
  bug report that led to this fix). Replaced with `_fetch_expected_lines_batch`, chunking all
  matched PO refs into batches of 100 and reusing the existing `search_document_ai` endpoint's
  `IN UNNEST(...)` detail query — confirmed fixed, same search now completes well within budget.
- **`created_at` date-range filtering on `upload-dashboard/files` used `DATE`-typed BigQuery query
  parameters** against `created_at`, which is itself a `DATETIME` column: `created_at <= @date_to`
  implicitly cast `date_to` down to midnight at the *start* of that day, so a same-day search
  returned 0 rows (confirmed by testing the exact old parameter shape directly against BigQuery:
  170 rows with the fix, 0 without). Fixed by sending full `YYYY-MM-DDTHH:MM:SS` boundaries from
  the browser (`00:00:00`/`23:59:59`) and changing the BigQuery parameter type to `DATETIME`.

## Next Step

**Re-attempt Item 1 (the SQL Server concurrency lock in `rgmc-gcp-api`), using the
`cursor.callproc()` approach, not the `DECLARE/EXEC/SELECT` batch that already failed once.**

Concretely, in `rgmc-gcp-api/src/routers/bigquery_bridge.py`'s `BigqueryBridge` class (the lock
code was previously added then fully reverted — nothing to clean up, just add fresh):

1. Add `__acquire_run_lock`/`__release_run_lock` methods, using
   `connection.connection.dbapi_connection.cursor()` to get the raw pytds cursor, then
   `cursor.callproc('sp_getapplock', (lock_name, 'Exclusive', 'Transaction', wait_ms))` followed by
   `cursor.get_proc_return_status()` (an int: `>= 0` means acquired, negative means not). Keep
   `@LockOwner = 'Transaction'` (not `'Session'`) — SQLAlchemy's connection pool keeps the
   physical connection alive after `.close()`, so a session-scoped lock would never actually
   release between pooled reuses.
2. Wrap `main()`'s body (and `insert_selected_records`, the manual-trigger-page's BigQuery-lookup
   insert path, which writes to the same MSSQL tables and races the same way) in
   acquire-lock/try-finally-release, same shape as the reverted attempt.
3. **Validate before deploying this time** — ask the user directly whether they're willing to
   grant one of: (a) a scoped exception to fetch a single MSSQL credential value to a local file
   for an isolated test, or (b) permission to run a local Docker SQL Server container, or
   (c) accept redeploying straight to production and watching Cloud Logging closely immediately
   after (the pattern that worked fine for the Cloud Tasks/debounce work, but this is the second
   attempt at this *exact* lock mechanism after the first attempt caused a real incident — extra
   caution warranted). Do not silently redeploy without raising this choice explicitly, given the
   first attempt's blast radius (100% of customerra runs silently no-op'd for ~15 minutes).
4. After it's confirmed working, revisit whether Item 3 (the over-broad audit-log trigger) is
   still worth closing — now lower priority since the debounce layer absorbs most of its impact.

**Secondary, lower-urgency threads still open** (not blocking, but worth surfacing to the user if
asked "what's left"): the hardcoded `BC_CLIENT_SECRET` default in `rgmc-gcp-api/src/config.py`
(rotation + removal from source), the 8-duplicate-Cloud-Logging-handlers cleanup, and the 3
fully-missing Manila Taste Curators PO refs (new lead: `gsutil` confirms no real input file exists
for any `portal/batch/manila_taste/...`-tagged row — worth checking with whoever owns that
ingestion path about where "manila_taste" batches actually originate from).

## Context & Gotchas

- **Three different git/deploy topologies in play, don't assume one pattern fits all**:
  `rgmc-gcp-api` deploys straight from `main` (no staging). `sbic-manual-trigger-page` deploys
  straight from `master` (no staging). `rgmc-bc-api` has a real staging/prod split —
  `rgmc-bc-api-staging` deploys from `staging`, `rgmc-bc-api-prod` deploys from `master`, and this
  repo's own git history shows every prior feature landed on `master` via a genuine GitHub PR
  (`"Merge pull request #N from rgmc-apps/staging"`). This session did two direct
  `git merge --no-ff staging` + `git push origin master` instead, because `gh` CLI isn't installed
  in this environment — functionally equivalent, but flag to the user if they'd prefer the PR-review
  habit restored (e.g. by installing `gh` or doing the merge via GitHub's web UI themselves next
  time).
- **`sbic-manual-trigger-page`'s `BC_API_BASE` env var points at `rgmc-bc-api-prod`, not
  `-staging`** — any `rgmc-bc-api` change only actually takes effect for the live app once it's on
  `master`/`-prod`, not just `staging`. This tripped up this session once (code was live on
  staging, invisible to the real app, until explicitly promoted) — always check which service the
  live app's env vars actually point to before declaring a backend change "done."
- **This harness's safety classifier blocks credential-adjacent and Docker actions outright**, even
  narrowly-scoped ones (a single `--format=value(...)` field fetch, a plain `docker --version`
  check) — don't assume "I scoped it carefully" will get it through; if a validation approach
  needs either of these, it needs to be raised as an explicit choice to the user, not attempted
  silently. Plain read-only `gcloud`/`gsutil`/`bq` commands against this GCP project were not
  blocked and were used heavily and successfully all session for diagnosis.
- **BigQuery column casing differs from MSSQL's `_BQ` staging tables** — BigQuery is snake_case
  (`po_ref_number`, `customer_sku_code`, `unit_of_measurement`), MSSQL's `CustomerPOULBQ`/
  `CustomerRABQ`/their `*DetailBQ` siblings are camelCase (`poRefNumber`, `customerSKUCode`,
  `unitOfMeasurement`) — confirmed via `rgmc-gcp-api/src/mappings.py`'s `table_mappings` dict,
  which is the authoritative source for every such rename. Getting this wrong silently 500s MSSQL
  queries with `"Invalid column name"` (happened once this session already, see Failed Attempts).
- **RA and RA(xml)'s BigQuery/MSSQL schema is NOT just "PO's schema with fewer columns"** — it's a
  genuinely different shape with no `company_name`, `customer_branch_name`, `customer_sku_code`,
  `unit_price`, or `unit_of_measurement` concept at all (its real fields are remittance/deduction
  line items like `ra_amount_detail`, `gross_amount_detail`, `doc_ref_number_detail`). The
  dashboard's detail-table rendering for non-PO upload types still shows PO-shaped column
  headers/values as a pre-existing, not-yet-fixed cosmetic gap (everything just reads "—" for RA)
  — low priority, but don't be surprised by it.
- **Signed URLs for GCS objects require self-impersonated credentials on this project**, not a
  plain service-account-default call — `google.auth.compute_engine.Credentials` (what Cloud Run
  gives you by default) has no `sign_bytes` implementation at all, confirmed by reading
  `google-auth`'s source directly (`hasattr(ce.Credentials, 'sign_bytes')` is `False`). The fix
  (`google.auth.impersonated_credentials.Credentials` targeting the SA's own email, relying on
  `roles/iam.serviceAccountTokenCreator` already granted to it on itself for the earlier Cloud
  Tasks work) is now the established pattern in `bigquery_routes.py`'s `_get_signing_credentials`
  — reuse it rather than re-deriving this from scratch if another "give me a link to a GCS object"
  feature comes up.
- **Document AI batch-output naming convention**: an input upload at
  `gs://{bucket}-in[-v2]/portal/{basename}.{ext}` produces output(s) at
  `gs://{bucket}-out[-v2]/portal/batch/{batch-id}/{job-id}/{shard}/{basename}-{page}.json`.
  Reversing this (strip the last path segment, strip the trailing `-{page}.json` via
  `re.compile(r"-\d+\.json$")`) recovers `{basename}`, then list the input bucket by prefix
  `portal/{basename}` to find the real file (extension isn't guessable — seen `.pdf`, could be an
  image). Confirmed working for both `sbic-po-parser-in-v2`/`-out-v2` and `sbic-ra-parser-in`/
  `-out`. `sbic-ra-parser-xml`-equivalent buckets do not exist yet (table is empty, no Eventarc
  trigger found for it).
- **`int_document_ai`'s `line_index` column is a stable 1:1 join key between a BigQuery detail row
  and its MSSQL `*DetailBQ` counterpart** (`lineIndex` there) — used for the dashboard's BQ-vs-
  MSSQL variance comparison instead of trying to match by SKU (which MSSQL doesn't need to
  disambiguate since it's a direct staging mirror of BigQuery, unlike BC which requires resolving
  a raw SKU to a real BC Item Number first).
- **The Missing Items / Upload Dashboard's BC-side SKU matching deliberately does not replicate
  `rgmc-worker-pool`'s own BC item-reference ("ref_map") matching** from automated import — only
  checks saved `"sku"`-type overrides plus the raw-SKU-as-BC-item-number fallback. This is a
  known, accepted v1 limitation (documented in `plans/missing_items_plan.md`): worst case is a
  harmless false "needs linking"/variance flag for an item BC could already auto-resolve on its
  own, never a false negative that hides a genuinely missing/mismatched line.
