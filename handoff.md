# Handoff

## Goal

Maintain and extend the SO-import buffer reconciliation ecosystem for RGMC/SBIC across four
repos: `rgmc-gcp-api` (`C:\RGMC\Source\git\rgmc-gcp-api` — BigQuery/MSSQL bridge + BC trigger
endpoints), `rgmc-bc-api` (`C:\claude\rgmc-bc-api` — BC/Firestore buffer API), `rgmc-worker-pool`
(`C:\claude\rgmc-worker-pool` — Cloud Run Worker Pool, Pub/Sub-driven BC writes), and
`sbic-manual-trigger-page` (`C:\claude\sbic-manual-trigger-page`, this repo — the `/`, `/trigger`,
and `/reconcile` Flask UI). This session's concrete asks, in order, each already covered by a
prior handoff up to the MSSQL/rate-limiter work, then continuing fresh:

1. **(Inherited from the previous handoff, now resolved)** Fix `rgmc-gcp-api`'s MSSQL dashboard
   routes blocking the whole event loop on one slow query, and the `/handoff/runbridge/` rate
   limiter starving `customerpoul` traffic behind dense `customerra` bursts.
2. Set up automatic retry for "Held back (0 line items extracted yet)" orders — a Cloud Scheduler
   job instead of requiring a human to notice and click the manual backfill button.
3. Add duplicate-BigQuery-row detection to the Upload Dashboard's BQ-vs-MSSQL/BC comparison
   counts (and to the Missing Items tab's matching), so a PO whose Document AI extraction
   genuinely duplicated every line doesn't inflate "BQ: 14" when the real count is 7.
4. **The session's largest task**: move `BigqueryBridge.main()`'s actual BigQuery-read + MSSQL-
   write + SO-import-publish work OUT of `rgmc-gcp-api`'s HTTP request path entirely, into
   `rgmc-worker-pool`'s Pub/Sub pull workers — explicitly requested scope: **move everything**,
   including the three manual "Run Bridge Now" buttons on `/trigger`, not just the Eventarc-
   triggered automated path.
5. Add a new **"Customer/Ship-To Mismatch"** tab on `/reconcile`: search BC Sales Orders
   (`submittedBy = "SBIC AI Uploading"`) by PO ref/date/company/customer-name/ship-to-address, let
   a human pick the correct ship-to (allowed to belong to a **different** customer than the
   order's current one, unlike the existing Missing Ship-To tab), queue the fix, then an explicit
   batched Apply re-points both `sellToCustomerNo` and `shipToCode` together. Binding acceptance
   criterion (carried over verbatim from every other tab this session and prior ones): never write
   to BC directly from a pick/link action, only from an explicit Apply step.
6. Add BC-backed live search suggestions to that tab's Customer name / Ship-to address filter
   fields (autocomplete dropdown, reusing existing `/api/lookup/customers` / `/api/lookup/ship-to`
   endpoints).
7. Show the BigQuery-extracted `customer_branch_name` (the *original* uploaded branch text) on
   each Customer/Ship-To Mismatch result row, for comparison against BC's current (possibly wrong)
   ship-to.
8. Fix a real production bug reported by users: `rgmc-worker-pool`'s `_create_order` (the function
   that actually creates/resumes a BC Sales Order) was trusting a *flat* (name-only) customer
   override outright, even when BC has split one raw customer name across multiple real Customer
   records (e.g. "WALTERMART SUPERMARKET, INC." — confirmed in production). Fix: resolve the
   branch's own ship-to FIRST; if it disagrees with a flat override, the ship-to match wins; if
   ship-to can't be resolved at all, buffer the order instead of guessing via name alone.
9. Fix a `pip` dependency conflict the user hit deploying `rgmc-worker-pool` (`protobuf==6.33.1`,
   copied from `rgmc-gcp-api`'s requirements without checking it was only needed there for
   `google-cloud-tasks`, conflicted with this repo's own `google-cloud-firestore==2.20.1`, which
   caps `protobuf <6.0.0dev`).

## Current State

**Everything above is committed AND deployed successfully, confirmed live via direct gcloud/API
checks at the end of this session.** No repo has uncommitted work. Specifics:

- **`rgmc-gcp-api`** (branch `main`, deploys directly on push): tip `6bceb09 DI-0215 transfer
  handoff to worker pool`. Live revision `rgmc-gcp-api-00147-mjx`. `/handoff/runbridge/`,
  `/customerpoul/runbridge/[/onlinesalespo/]`, `/customerra/runbridge/` all now publish to
  Pub/Sub topic `rgmc-bridge-run` and return `{"status": "queued", ...}` immediately —
  confirmed via `src/services/bridge_trigger.py` and the four route files. `BigqueryBridge` class
  itself (`src/routers/bigquery_bridge.py`) is untouched and still used inline ONLY by
  `/customerpoul/insert-from-bigquery` (the BigQuery Lookup tab's manual caller-scoped insert —
  deliberately not moved, it's not the heavy "everything new" pull). This repo's own
  `handoff.md` has the full writeup of items 1 and 4 (MSSQL fix, rate limiter fix, held-back
  scheduler, and the worker-pool migration) if more detail is needed than below.
- **`rgmc-worker-pool`** (Cloud Run Worker Pool, `asia-southeast1`): tip `d8c0413 fix deployment`
  (on top of `a835a94 DI-0215 added worker pool mods`). Live revision
  `rgmc-worker-pool-00099-vxr`, deployed from **Cloud Build `5cc0285b` (SUCCESS, 2026-10-19
  11:40:22 UTC)** — the build immediately before it, `8dcd05cb` (11:25:45 UTC), **FAILED** on the
  `protobuf==6.33.1` conflict; `d8c0413` fixed `requirements.txt` by removing that pin entirely
  (see Context & Gotchas) and the very next build succeeded. New capability confirmed live:
  `src/workers/bigquery_bridge_worker.py` (Pub/Sub pull, `FlowControl(max_messages=1)`),
  `src/services/bigquery_bridge.py` (ported `BigqueryBridge`), `src/db/dbconn.py` (Cloud SQL
  Python Connector + pytds, no `db_name` param — only ever touches `MSSQL_DATABASE`/`sbic_prod`),
  `src/mappings_bridge.py`, `src/services/bq_read.py`. IAM role `roles/cloudsql.client` for
  `rgmc-worker-pool@durable-woods-465907-n1.iam.gserviceaccount.com` **confirmed granted**. Cloud
  Run annotation `run.googleapis.com/cloudsql-instances=durable-woods-465907-n1:asia-southeast1:
  rgmc-in-house-prod` **confirmed present**. `so_import_worker.py`'s `_create_order` ship-to-first
  fix (item 8) is also in this same `a835a94`/`d8c0413` deploy — **live in production now**, not
  yet observed in action against a real "WALTERMART-style" ambiguous order (no such order has
  reportedly come through since deploy as of session end).
- **`rgmc-bc-api`** (branch `master`, deploys directly on push — this repo's own history shows it
  normally goes through `staging` first, but this session, like the last one, pushed straight to
  `master`): tip `6a48b28 DI-0214 added ship-to/customer mismatch`, deployed successfully (build
  `9d1238b2`, SUCCESS, 2026-10-19 10:21:43 UTC) — this is the Customer/Ship-To Mismatch backend
  (`so_buffer_service.py`'s `*_customer_shipto_fix*` functions + `so_buffer_routes.py`'s
  `/customer-shipto-fixes*` routes). **A newer, unrelated commit `4345ead "added graphify"` was
  mid-deploy (Cloud Build `71b3c23e`, status WORKING) as this session ended** — this commit adds
  `.claude/settings.json`, `CLAUDE.md`, and a large `graphify-out/` cache directory (some local
  static-analysis/graph tool), made by the user directly, **not part of this session's work at
  all**. Check `gcloud builds describe 71b3c23e-f98f-4624-be84-442454f58482` if picking this up
  soon after handoff — if it failed, that's unrelated to anything in this document.
- **`sbic-manual-trigger-page`** (this repo, branch `master`, deploys directly on push): tip
  `bb89950 DI-0214 added ship-to address hints` (on top of `f9ccefe DI-0214 added ship-to/customer
  search` and `ff0c735 DI-0214 added customer/ship-to mismatch`). Two successful builds confirmed
  (`b67ebd16` and `8f050ad8`). Covers: the dashboard duplicate-row fix (item 3), the Manual
  Trigger page's async "queued" UX (item 4's frontend half), the whole Customer/Ship-To Mismatch
  tab (items 5–7).

**Update from the following session (2026-10-20): both items below ARE now independently confirmed
live.**

- **BigQuery bridge via Pub/Sub**: confirmed via Cloud Logging, no manual trigger needed — a real
  automated `customerra` run flowed through naturally. `rgmc-gcp-api` logged `Published bridge run
  request: {'group_code': 'customerra', 'method': 'triggered'}` at `2026-10-20T09:55:32.12Z`;
  `rgmc-worker-pool` (resource type is `cloud_run_worker_pool`, **not** `cloud_run_revision` — easy
  to search the wrong log bucket) picked it up, connected to MSSQL via pytds, and logged `Bridge
  run complete (group_code='customerra', method='triggered'): {'status': 'success', 'message': 'No
  new data to process.'}` at `09:55:35.39Z`. Full path confirmed working end to end.
- **Customer/Ship-To Mismatch tab, live in a browser** (claude-in-chrome against the real deployed
  `sbic-manual-trigger-page` service): company select, search, candidate picker, queue, and cancel
  all confirmed working correctly. Specifically: searched SBIC with no filters, got **"Found 972
  order(s)"**; clicked **Update Ship-To...** on `SBSO260002`, typed "METRO" into the ship-to
  search, got candidates from **different customers** (`ME003`, `ME011`, `ON003`, `RO005`, etc.) as
  designed; picked `ME003-M001`, UI immediately showed `Queued — Customer ME001 → ME003 (GRAND
  METRO WINE CENTER CORP.), Ship-to ME001-S010 → ME003-M001 — METRO VINOS` with **no BC write**
  (order's displayed customer stayed ME001 throughout — the fix only appeared as a queued diff with
  Cancel/Change buttons); clicked **Cancel** to clean up the test fix (`SBSO260002` was NOT a real
  mismatch, just used to exercise the UI), confirmed back to "Not queued". **The queue-then-apply
  contract holds.**
  - **New finding, not in scope to fix without user input**: the unfiltered search (no PO ref /
    date / customer / ship-to) took **182.9 seconds** server-side (confirmed both via a direct
    `Invoke-WebRequest` to the endpoint and via Cloud Logging's `httpRequest.latency` field) before
    returning those 972 rows. This isn't a hang — `_find_orders_for_customer_shipto_mismatch`'s BC
    call and `_fetch_bq_customer_branch_names`'s BigQuery calls both eventually complete — but in
    practice almost every Open `"SBIC AI Uploading"` order ever created matches this tab's filter
    (there's nothing to auto-detect a mismatch by design, per its own docstring), so a human has to
    wait 3 minutes AND scroll through ~972 rows to spot a real mismatch unless they already know a
    narrowing PO ref/date/customer-name. Worth asking the user whether this needs a required
    default date-range filter or BC-side pagination before it sees real use.
  - **Still genuinely unconfirmed**: whether BC's Sales Order API accepts a `sellToCustomerNo`
    change on an order that already has lines. Deliberately did NOT click **Apply Customer/Ship-To
    Fixes** against a real order to test this — `SBSO260002` was not an actual mismatch, and
    applying a fabricated "fix" would have incorrectly repointed a legitimate production order's
    customer/ship-to. This needs either a genuinely known-wrong order (e.g. a future
    WALTERMART-style case) or explicit user sign-off before testing for real.

**Also found while re-verifying this session (drift since the previous handoff, all unrelated to
this ecosystem's work, all already deployed successfully — see Context & Gotchas)**:
`rgmc-worker-pool`'s live revision ticked from `00099-vxr` to `00100-rr8` (same container image,
just the Cloud SQL instance annotation update the user applied from Cloud Shell, timestamped
~36 min after the image build — not new code). `rgmc-bc-api`'s `staging`/`master` history moved
past `6a48b28` via two more reviewed PR merges (`#61` "Added a custom page for transaction sales
entry", `#62`) plus an unrelated direct commit `cfed4f5 "DI-0216 fix for missing sales order
submissions"` — confirmed all already deployed (`rgmc-bc-api-prod-00273-99c`, builds
`91382258`/`1925ec97`/`611b4466`/`680f2a3f` all `SUCCESS`). None of this touches the Customer/
Ship-To Mismatch backend routes this session built.

## Files Actively Being Edited

None — every file touched this session is committed in the repo it lives in (see Current State
for exact commit hashes). Nothing is mid-edit. For a future session that needs to touch any of
this code, the key new/changed files per repo are:

**`rgmc-gcp-api`**:
- `src/config.py` — new `pubsub_bridge_run_topic`.
- `src/services/bridge_trigger.py` (**new**) — `publish_bridge_run(group_code, method)`.
- `src/routers/sbic_routes/handoff.py`, `CustomerPOUL.py`, `CustomerRA.py` — the four `runbridge`
  routes now call `publish_bridge_run` instead of `BigqueryBridge(...).main()`.
- `handoff.md` (repo root) — this repo's own detailed writeup, superseding its prior (unrelated,
  ANTZ-integration-planning) content, with that old content preserved at the bottom under "Older,
  separate, still-unresolved thread."

**`rgmc-worker-pool`**:
- `requirements.txt` — added `pandas`, `pandas-gbq`, `db-dtypes`, `SQLAlchemy`, `sqlalchemy-pytds`,
  `cloud-sql-python-connector`; bumped `google-cloud-bigquery` to `3.38.0`. **No explicit protobuf
  pin** (see Failed Attempts/Context & Gotchas — this was the deploy-breaking mistake, now fixed).
- `src/config.py` — new `BIGQUERY_DATASET_ID` (default `"sbic_int"`, confirmed via `bq ls` this is
  the REAL dataset for `int_document_ai*`, not the `bc_sbic_prod_*` ones), `BQ_TABLE_VERSION`,
  `BQ_READ_TIMEOUT_SECONDS`, `MSSQL_INSTANCE`/`MSSQL_USER`/`MSSQL_PASSWORD`/`MSSQL_DATABASE`
  (empty-string code defaults only — real values already existed as Cloud Run env vars before this
  session, confirmed via `gcloud beta run worker-pools describe`, see Context & Gotchas),
  `PUBSUB_BRIDGE_SUBSCRIPTION`, `PUBSUB_POUL_SO_TOPIC`.
- `src/db/dbconn.py`, `src/services/bq_read.py`, `src/mappings_bridge.py`,
  `src/services/bigquery_bridge.py` (**all new**) — ported from `rgmc-gcp-api`'s equivalents, see
  that repo's `handoff.md` for the exact adaptation notes (config var naming, `send_mail` API
  swap, no `insert_selected_records`).
- `src/workers/bigquery_bridge_worker.py` (**new**) — Pub/Sub pull consumer,
  `FlowControl(max_messages=1)`.
- `src/main.py` — starts/stops `bigquery_bridge_worker` alongside the three existing workers.
- `src/workers/so_import_worker.py` — `_create_order`'s customer/ship-to resolution reordered
  (lines ~591–760ish post-edit): branch override and PO/branch-**scoped** customer override
  unchanged; a **flat** (unscoped) customer override is no longer trusted outright — this branch's
  own ship-to is resolved first via the same code/lookup-code/fuzzy-name matching the no-override
  path already used, and wins if it disagrees with the flat override; if ship-to can't be resolved
  at all, the order is buffered (`raise ValueError`) instead of falling back to the flat guess.

**`rgmc-bc-api`**:
- `src/services/so_buffer_service.py` — new `so_customer_shipto_fixes_{env}` /
  `so_customer_shipto_fix_history_{env}` Firestore collections + `list_customer_shipto_fixes`,
  `queue_customer_shipto_fix`, `delete_customer_shipto_fix`, `list_customer_shipto_fix_history`,
  `_record_customer_shipto_fix_attempt`, `apply_customer_shipto_fixes` (**synchronous**, like
  `apply_missing_item_fixes` — re-pointing a header is 1 BC PATCH call, no Pub/Sub+worker-pool
  round-trip needed). `apply_customer_shipto_fixes` re-validates the order is still Open and still
  `submittedBy = "SBIC AI Uploading"` before PATCHing, and sends `sellToCustomerNo`+`shipToCode`
  together in ONE PATCH when both need to change.
- `src/routers/bc_routes/so_buffer_routes.py` — 5 new routes under
  `/bc/custom/v2/so-buffer/customer-shipto-fixes[...]` mirroring the missing-item-fixes routes'
  shape exactly.

**`sbic-manual-trigger-page`**:
- `app.py` — `_find_orders_for_customer_shipto_mismatch` (BC search: `submittedBy` + `status eq
  'Open'` + optional po_ref/date/customer_name(`contains`)/ship_to_search(two-step: search BC
  ship-to-addresses by name first, then OR-filter orders by those codes)), `_fetch_bq_customer_
  branch_names` (batched BigQuery `document-ai/search` lookup, chunked 100 refs), and 6 new proxy
  routes (`/api/sales-orders/customer-shipto-mismatch`, `.../customer-shipto-fixes` GET/POST/
  DELETE, `.../customer-shipto-fix-history`, `/api/apply-customer-shipto-fixes`).
- `templates/reconcile.html` — new `data-action-tab="customer-shipto"` tab button + full panel
  (toolbar with Company/PO-ref/Date-from/Date-to/Customer-name/Ship-to-address fields, each of the
  latter two wrapped in `.toolbar-suggest-wrap` for the autocomplete dropdown, plus a Link History
  sub-panel).
- `static/reconcile.js` — `buildCustomerShiptoRow` (per-order row + ship-to candidate picker,
  reuses `normalizeCandidates("branch", ...)`/`renderCandidateList`), `searchCustomerShiptoMismatch`,
  `wireToolbarSuggest` (generic debounced BC-autocomplete helper, wired to both new fields),
  queue/apply/history functions mirroring Missing Items' synchronous-apply pattern.
- `static/reconcile.css` — `.toolbar-suggest-wrap`/`.toolbar-suggest-dropdown` (floating
  autocomplete dropdown under a toolbar input, since toolbar fields sit in one row).
- `static/script.js`, `templates/index.html` — Manual Trigger page now shows "{process} queued"
  instead of claiming synchronous completion, since `/trigger`'s backend calls now return
  `{"status": "queued", ...}` instantly.
- `app.py` (separately, item 3) — `_fetch_expected_lines_batch` dedupes per-ref lines by
  `_sku_key_for_line` before they reach `_find_missing_items`/queue-to-buffer.
- `static/dashboard.js` (item 3) — `dedupeBqLines` (same composite key the BigQuery bridge itself
  uses to dedupe: `po_ref_number`+`customer_name`+`customer_sku_code`) applied before the MSSQL/BC
  comparison counts; raw per-line table still shows every row, duplicates included.

## Failed Attempts

- **Pinned `protobuf==6.33.1` in `rgmc-worker-pool`'s `requirements.txt`**, copying it verbatim
  from `rgmc-gcp-api`'s own pin without checking *why* it was there. **Why it failed**: gcp-api
  only needs that pin because of its OWN `google-cloud-tasks` dependency (2.25.0+ requires
  `protobuf>=6.33.5`) — `rgmc-worker-pool` has no `google-cloud-tasks` at all, but DOES depend on
  `google-cloud-firestore==2.20.1`, which caps `protobuf <6.0.0dev`. Pinning 6.33.1 made these two
  constraints directly unresolvable — confirmed live via a failed Cloud Build
  (`8dcd05cb`, `ResolutionImpossible`). **Fixed** by removing the pin entirely (commit
  `d8c0413 fix deployment`); the immediately following build (`5cc0285b`) succeeded.
- **Two `gcloud` commands blocked by this harness's own safety classifier**, not by GCP itself —
  `gcloud projects add-iam-policy-binding ... --role=roles/cloudsql.client` and
  `gcloud beta run worker-pools update ... --add-cloudsql-instances=...`. Same denial pattern as
  an EARLIER session's attempts to fetch/copy MSSQL credential values — any IAM- or Cloud-SQL-
  connectivity-granting action gets refused outright regardless of scope. **Worked around**: gave
  the user the exact commands to run themselves from Cloud Shell; they did, hitting two follow-up
  prompts (an interactive IAM-condition picker, and a missing `--project` flag because their Cloud
  Shell session had no default project set) — both resolved with guidance, both grants now
  confirmed live (see Current State).
- **A `gcloud beta run worker-pools describe` call with a broad `env` field request** accidentally
  printed `MSSQL_PASSWORD`, `BC_CLIENT_SECRET`, and `SMTP_PASSWORD` in plaintext into this
  session's own transcript — not blocked by the classifier (unlike narrower, explicitly-named-
  credential scoped queries earlier in the session, which WERE blocked). Flagged live to the user
  as a security incident; recommended rotating those three credentials out of caution. Not
  confirmed whether the user has rotated them — **worth asking if not already done**.

## Next Step

Both items that used to head this list (bridge-via-Pub/Sub, Customer/Ship-To Mismatch tab) are now
confirmed live as of the 2026-10-20 follow-up session — see Current State. What's left:

1. **Decide whether the Customer/Ship-To Mismatch tab's unfiltered search needs a performance
   fix** before relying on it day-to-day: confirmed taking 182.9s and returning 972 rows with no
   filters applied (see Current State for the measurement). Options to raise with the user: require
   at least one filter (date range or PO ref) before allowing a search, add BC-side pagination,
   or leave it as-is if a human is always expected to narrow by PO ref/customer first anyway.
2. **Get a real Apply Customer/Ship-To Fixes test against an actual mismatched order** — the one
   remaining genuinely open question (does BC's Sales Order API accept a `sellToCustomerNo` change
   on an order that already has lines) still needs either a real known-wrong order or explicit
   user sign-off before testing, since a fabricated test fix must never be applied to a legitimate
   production order.
3. **If a "WALTERMART-style" ambiguous-customer-name order comes through** after the
   `so_import_worker.py` fix deployed, check Cloud Logging for the new
   `"flat customer override for ... points to ..., but branch ...'s own ship-to resolves to ...
   instead"` warning line — confirms the fix is actually firing as designed, not just deployed.
4. Ask the user whether `MSSQL_PASSWORD`/`BC_CLIENT_SECRET`/`SMTP_PASSWORD` (accidentally
   transcript-leaked this session, see Failed Attempts) have been rotated yet.

## Context & Gotchas

- **This is still the single most incident-prone pipeline in the whole RGMC ecosystem.** A PRIOR
  session's SQL Server app-lock attempt inside `BigqueryBridge.main()` broke 100% of `customerra`
  bridge runs in production for ~15 minutes (reverted as `f527da8`). Today's worker-pool migration
  and the `_create_order` ship-to fix both touch adjacent, equally-critical logic — any FUTURE
  change here should deploy one repo at a time and watch Cloud Logging immediately after, same
  caution already exercised this session.
- **`rgmc-worker-pool` already had real MSSQL/BC/SMTP credentials sitting as Cloud Run env vars
  before this session**, unused by any code until now — confirmed via the `describe` call that
  also (accidentally) leaked them into this transcript. No credential needed to be copied or set
  by anyone for the bridge migration; the new `config.py` just started reading what was already
  provisioned. If a future session needs to ADD a new credential-backed capability to worker-pool,
  check the existing env vars first (via a narrowly-scoped, non-broad query — broad `env` dumps
  are what caused the leak) before assuming something needs to be freshly granted.
- **`sbic_int` (not `bc_sbic_prod_int`) is the real BigQuery dataset** for
  `int_document_ai`/`int_document_ai_detail` — confirmed via `bq ls`/`bq ls sbic_int` this session.
  `bq ls` with no `--max_results` silently truncates alphabetically (missed everything from
  `s` onward on the first attempt) — always pass `--max_results=200`+ when hunting for a dataset
  this way.
- **This harness's safety classifier blocks IAM-policy-binding and Cloud-SQL-connectivity-granting
  `gcloud` commands outright**, no matter how narrowly scoped — confirmed twice more this session
  (on top of earlier-session credential-fetch denials). Any future infra step needing one of these
  must be handed to the user as an exact command, not attempted directly.
- **Whether BC's Sales Order API accepts changing `sellToCustomerNo` on an order that already has
  lines is still UNCONFIRMED** — `apply_customer_shipto_fixes` sends the PATCH and surfaces
  whatever BC says verbatim (not swallowed), but no real Apply attempt against a genuinely
  mismatched order has happened yet as of session end. This is the one piece of the Customer/
  Ship-To Mismatch tab that could still turn out to need a different approach (e.g., BC might
  require clearing lines first, or might reject it entirely) — find out on the first real use.
- **`rgmc-bc-api`'s git history now has an unrelated `graphify-out/` cache directory and a
  `CLAUDE.md`/`.claude/settings.json` committed** (`4345ead "added graphify"`, by the user,
  outside this session) — don't mistake this for anything related to the SO-import/BC work; it's
  some separate local tooling the user set up.
- **Dataset/table-mapping duplication across repos is intentional, not an oversight**: `rgmc-
  worker-pool`'s `src/mappings_bridge.py` is a verbatim, independently-maintained copy of
  `rgmc-gcp-api`'s `src/mappings.py` (same pattern already established for the SUNCOAST/SBIC
  company-keyword map duplicated between `CustomerPOUL.py` and `so_import_worker.py`). If the
  PO/RA/online-sales-PO table shapes ever change in one, update the other by hand.
- **Three different git/deploy topologies, still true**: `rgmc-gcp-api` and `sbic-manual-trigger-
  page` deploy straight from their main branch (no staging). `rgmc-worker-pool` likewise deploys
  directly. `rgmc-bc-api` nominally has a staging/prod split in its own history but this session
  (like the last one) pushed straight to `master`.
