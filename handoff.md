# Handoff

## Goal

Keep hardening the RGMC/SBIC SO-import ecosystem across the same multi-repo set as
prior sessions:
- `sbic-manual-trigger-page` (`C:\claude\sbic-manual-trigger-page`, this repo) — the
  `/`, `/trigger`, `/dashboard` (Upload Monitoring Dashboard, Search + Handoff History
  tabs), `/reconcile` Flask UI.
- `rgmc-gcp-api` (`C:\RGMC\Source\git\rgmc-gcp-api`) — BigQuery/MSSQL bridge + BC
  trigger HTTP endpoints, Eventarc/Cloud-Scheduler-facing routes.
- `rgmc-worker-pool` (`C:\claude\rgmc-worker-pool`) — the real Cloud Run Worker Pool
  (`gcloud beta run worker-pools`, not a regular Cloud Run service) that does the
  actual BigQuery-read + MSSQL-write + BC Sales Order work, pulled via Pub/Sub.
- `rgmc-bc-api` (`C:\claude\rgmc-bc-api`) — BC/Firestore buffer API. **Not touched this
  session** — every new feature deliberately reused its already-generic
  `reprocess_runs_{env}` / `GET /bc/custom/v2/so-buffer/reprocess-status/{run_id}`
  infrastructure as-is.

This session had four major threads, in order:
1. **UI polish** on the Upload Monitoring Dashboard's Search and Handoff History tabs
   (date filters, a Search button, button alignment/sizing, BigQuery `created_at`
   display, correctly surfacing retrigger outcomes instead of a naive "HTTP 200 =
   success").
2. **A real, user-reported production incident #1**: "no records were inserted since
   9am today" — diagnosed and (partially — see below) fixed.
3. **A related production incident #2**: rgmc-gcp-api's logs "full of debounce
   processes" — diagnosed and fixed (an Eventarc trigger scoping bug).
4. **The same incident #1 recurring after the first fix was deployed** — re-diagnosed
   down to its true structural root cause (a dbt VIEW design issue, not an
   application-code bug), applied a stopgap, and identified that the real fix is
   outside all four repos in scope.

Constraint established mid-session via explicit user choice (AskUserQuestion): when
asked whether slow retrigger/search operations should move to async Pub/Sub +
worker-pool processing or just get a longer synchronous timeout, the user picked
**synchronous with a corrected UI** for the general retrigger-status-display problem,
but later (a separate, specific complaint — "retrigger with cleanup per filename"
still timing out even after the synchronous timeout bump) explicitly asked to
**move that one specific operation to the worker pool**. Don't re-litigate either
choice without new evidence — both were deliberate, scoped decisions.

## Current State

### Deployment / commit status (IMPORTANT — check this before doing anything else)

- `sbic-manual-trigger-page`: clean except `handoff.md` itself (this file, being
  rewritten now). All app code changes from this session are committed, latest
  commit `676269c` "transferred process to worker pool".
- `rgmc-worker-pool`: **`src/services/bigquery_bridge.py` has UNCOMMITTED changes**
  (the 120→300s timeout bump + the dbt-view root-cause documentation from the very
  end of this session — see Thread 4 below). Latest commit is `aa5d997` "added
  timeout fix" (the earlier 60→120 change), which does NOT include the 120→300 bump.
  **Unlike every earlier round this session, the user had not yet committed this
  change as of session end.**
- `rgmc-gcp-api`: **`src/routers/bigquery_bridge.py` has the same UNCOMMITTED
  120→300s change**, mirroring the worker-pool copy. Latest commit is `9259432`
  "added handoff log optimization" (the Eventarc debounce fix), which also does NOT
  include the 120→300 bump.

**Neither of these two uncommitted files has been committed OR deployed.** Every
earlier fix this session (the 60→120 timeout bump, the watermark fallback, the
Eventarc debounce filtering, all the UI work) WAS committed and deployed (confirmed
via `gcloud run services list` / `gcloud beta run worker-pools list`: all three
services were last deployed together at 2026-10-29T10:56–10:57 UTC, which covers
everything through the 60→120 fix and the Eventarc fix, but predates the 120→300
change entirely).

### Thread 1 — UI polish (done, committed, deployed)

- Handoff History tab: Search button added, the existing batch-retrigger panel's
  date-range inputs now also drive the main list filter, per-record/batch retrigger
  results show three real outcomes (`success` / `not_promoted` / `error`,
  color-coded) instead of a naive HTTP-200-means-success read, each record shows a
  new "BigQuery inserted: ..." line (the row's real BigQuery `created_at`, fetched
  fresh, separate from the handoff event's own `timestamp`).
- Search tab: file-row action buttons (`View file`, `Queue Missing Items`,
  `Retrigger Handoff (cleanup & re-insert all)`, `Expand/Collapse`) now share
  consistent height/padding/font. An unfiltered search defaults server-side to the
  last 30 days (status line says so) instead of attempting a full unbounded table
  scan.

### Thread 2 — "No records since 9am" bridge outage, round 1 (fixed, committed, deployed)

Diagnosed via `gcloud logging read` against `resource.type="cloud_run_worker_pool"`:
every automated `customerpoul` bridge run had failed since 2026-10-29 04:22 UTC with
`BigQuery read timed out after 60s`. Root cause (round 1): `bigquery_bridge.py`'s
`__get_bigquery_data()` called `read_gbq_with_timeout()` with no explicit `timeout=`,
silently using the 60s module default. **Fixed by adding `timeout=120` explicitly**
to all 5 calls, in both copies of `bigquery_bridge.py`. This WAS deployed (10:56–10:57
UTC) and DID fix the total-outage symptom (confirmed: a subsequent manual test run
got past the header fetch and further into the detail fetch than before) — but see
Thread 4, it wasn't sufficient.

### Thread 3 — rgmc-gcp-api logs "full of debounce processes" (fixed, committed, deployed)

Diagnosed via `gcloud eventarc triggers describe` + raw BigQuery audit log
inspection: Eventarc trigger `bq-int-document-ai-ra-trigger-v2` matches
`serviceName=bigquery.googleapis.com` + `methodName=JobService.InsertJob` with no
table-level scoping (Eventarc audit-log filters can't reach that deep into the
payload) — confirmed by finding an actual burst-window audit log entry from a
scheduled `dbt-github@...` job building a completely unrelated inventory view.
**Fixed** in `handoff.py`'s `/runbridge/debounce/` handler: for `groupcode ==
"customerra"`, parses the CloudEvent body and skips the debounce/Cloud-Tasks call
when the real destination table is positively identified as something else — fails
open (still debounces) on any unrecognized payload shape. Also downgraded the
"already_scheduled" duplicate-suppression log line from INFO to DEBUG. This was
deployed in the same 10:56–10:57 UTC push.

### Thread 4 — Same outage recurs after the round-1 fix; true root cause found (stopgap applied, NOT committed/deployed)

User reported the identical-looking error again after the round-1 deploy, now
reading `BigQuery read timed out after 120s` (not 60s) — confirming the deploy went
live and the fix helped, but wasn't enough. Investigated further and found the REAL,
structural root cause:

- `bq show --format=prettyjson durable-woods-465907-n1:sbic_int.int_document_ai` and
  `...int_document_ai_detail` both report `"type": "VIEW"` — **these are dbt VIEWS,
  not materialized tables.**
- Pulled `int_document_ai_detail`'s actual view definition (`bq show` includes the
  `view.query` field): it has multiple CTEs, a `UNION ALL` of two staging tables,
  `FIRST_VALUE(...) OVER (PARTITION BY ... ROWS BETWEEN UNBOUNDED PRECEDING AND
  UNBOUNDED FOLLOWING)` window functions (to gap-fill SKU code/description), and a
  final `QUALIFY ROW_NUMBER() OVER (...) = 1` dedup.
- Because of those window functions, **BigQuery cannot push our `WHERE created_at >
  X` filter into the view** — every single read re-executes the ENTIRE
  transformation over ALL historical staging data first, then filters the output.
  This cost scales with total accumulated document volume, not with what's actually
  new, and will keep getting slower permanently as more data accumulates. No timeout
  value fixes this durably.
- Confirmed via `gcloud logging read` on `protoPayload.methodName=
  "google.cloud.bigquery.v2.JobService.InsertJob"` filtered to
  `destinationTable:"int_document_ai_detail"` that this view gets rebuilt
  (`CREATE_VIEW` statement) periodically by the same `dbt-github` scheduled job
  implicated in Thread 3 — but the rebuild itself isn't the bottleneck; querying the
  view AT ALL is expensive regardless of rebuild timing, because every SELECT
  re-runs the full window-function transformation.

**Stopgap applied (uncommitted)**: raised the timeout further, from 120 to **300
seconds**, in the same 5 `read_gbq_with_timeout()` calls in both copies of
`__get_bigquery_data`. Verified this is safe for a background worker: the Pub/Sub
subscription `rgmc-bridge-run-sub`'s ack deadline is 600 seconds (checked via
`gcloud pubsub subscriptions describe rgmc-bridge-run-sub`), comfortably above a
worst-case ~600s (header + detail sequentially at 300s each). This is explicitly
documented in the code (and must be communicated to the user/next session) as a
**stopgap, not a fix** — it buys time against BigQuery slot-contention variance
(observed 12s–150s+ for the identical query at different times this session) but
does not address the structural cost, which only grows over time.

**The real fix is outside all four repos in scope.** `int_document_ai` and
`int_document_ai_detail` need to be materialized as actual (ideally incremental) dbt
tables instead of views. This requires access to the separate dbt project run by the
`dbt-github@...` CI service account — not available in this session. This needs to
be escalated to whoever owns that dbt project, the same way the earlier
`tr_CustomerPOBQ` SQL Server trigger issue (from a prior session, see git history)
was escalated to a DBA as out-of-scope.

## Files Actively Being Edited

### `sbic-manual-trigger-page` (this repo) — all committed
- `app.py` — `BQ_PROXY_TIMEOUT = 150` constant (replaces ~15 scattered
  timeout values on `/bigquery_routes/*` calls); `_insert_mssql_and_log` now
  normalizes `mssql_status`/`status_code` so a "200 with internal error" response
  from insert-from-bigquery is no longer read as success; `api_handoff_history_
  retrigger_mssql_batch` dropped its `file_name` selector (moved to the async route)
  and does 3-state result classification; new
  `_enrich_handoff_history_with_bq_created_at(records)` wired into
  `api_handoff_history`; `api_dashboard_files` defaults to a 30-day window when
  totally unfiltered, returns `defaulted_date_window: true`; new route
  `POST /api/handoff-history/retrigger-file-async` (starts the worker-pool job via
  rgmc-gcp-api's `/customerpoul/retrigger-file`, returns `run_id` for polling).
- `static/dashboard.js` — `pollRunStatus` helper; `retriggerFileBtn` handler
  rewritten to start+poll instead of one long synchronous call; search status line
  notes the 30-day default when applied.
- `static/handoff-history.js` — same `pollRunStatus` helper (duplicated, no shared
  module system between these IIFE files); `renderBatchRetriggerResults` extracted;
  `runFileRetriggerAsync` added; `runBatchRetrigger` branches on
  `payload.file_name`; single-PO retrigger result now checks real
  `mssql_status`/`trigger_status`; Search button + date-range inputs wired into the
  main list's `load()`; each record shows "BigQuery inserted: ..." line.
- `static/dashboard.css` — `.dash-file-actions` button-size/alignment fix, scoped to
  not affect the shared button classes elsewhere.
- `templates/dashboard.html` — Handoff History Search button added; favicon link
  tag added (also added to `index.html`, `reconcile.html`, `report.html`).

### `rgmc-worker-pool`
- `src/services/bigquery_bridge.py` — **currently has uncommitted changes** (the
  120→300s bump + updated docstring explaining the dbt-view root cause, from Thread
  4). Also contains the earlier (committed) watermark-null fallback fix and the
  60→120 timeout fix from Thread 2.
- `src/services/bridge_handoff_history.py` — committed: new
  `record_single_handoff(...)` function (per-PO independent mssql_status/
  trigger_status, used by the new retrigger-file worker handler).
- `src/services/retrigger_client.py` — committed, **new file**: HTTP client for
  rgmc-gcp-api's cleanup-staging/insert-from-bigquery/by_table/value/
  document-ai/search endpoints, used only by the worker (no request deadline to
  protect, so generous timeouts).
- `src/workers/so_import_worker.py` — committed: new `_run_retrigger_file(...)`
  function and `if msg_type == "poul-so-retrigger-file":` dispatch block (7th
  message type), docstring updated.

### `rgmc-gcp-api`
- `src/routers/bigquery_bridge.py` — **currently has uncommitted changes**, mirrors
  the `rgmc-worker-pool` copy exactly (120→300s bump + docstring). Also contains the
  earlier committed watermark-fallback and 60→120 fixes.
- `src/routers/bigquery_routes/bigquery_routes.py` — committed:
  `_run_parameterized(..., timeout: float = 120)` parameter added; `get_by_value`
  given explicit `timeout=120`.
- `src/routers/sbic_routes/CustomerPOUL.py` — committed: new endpoint
  `POST /customerpoul/retrigger-file` (publishes `poul-so-retrigger-file` via the
  existing `_publish_poul_so_message` helper).
- `src/routers/sbic_routes/handoff.py` — committed: new
  `_extract_bq_destination_table(body)` (fail-safe CloudEvent payload parser);
  `run_handoff_bridge_debounced` now takes `request: Request` and filters
  `customerra` events by real destination table (fails open on ambiguity);
  `AlreadyExists` log line downgraded to DEBUG.

## Failed Attempts

- **Believed the 60→120s timeout bump (Thread 2) fully fixed the bridge outage** —
  it was deployed, and DID fix the complete-outage symptom (confirmed the error
  message changed from "...after 60s" to "...after 120s", meaning progress was
  made), but the user reported the same-shaped error again shortly after. **Why it
  wasn't enough**: the actual bottleneck wasn't a missing timeout override at all by
  that point — it was `int_document_ai_detail` being a dbt VIEW with window
  functions that can't benefit from predicate pushdown, so the query's cost is
  roughly constant (and large) regardless of the WHERE filter. Don't assume a
  timeout-value fix is complete just because the error's own number changed — a
  recurrence with a bigger ceiling number is itself a strong signal to look for a
  structural cause, not to just raise the number again (though in this specific case
  raising it again to 300 was still the right *stopgap*, since the task is a
  background worker with no real deadline — just don't mistake it for the fix).
- **Watermark-fallback fix alone (Thread 2, first deploy) was insufficient**: the
  very first bridge-outage fix (falling back to the header table's watermark when
  the detail table's own `MAX(createdAt)` is `NULL`) was necessary for a *different*,
  narrower edge case (an unbounded full-table scan when the watermark happens to be
  null) but did not address the general "every read of this view is slow" problem —
  both fixes were needed, for different reasons, and neither alone was sufficient.
- **Assumed raising the client-side HTTP proxy timeout (`BQ_PROXY_TIMEOUT`, bumped
  across several rounds to 150) would eventually be "enough" for the dashboard's
  unfiltered search** — it wasn't; a genuinely unbounded full-table `GROUP BY` scan
  exceeded even 150s in testing. Fixed behaviorally (default to a 30-day window)
  instead of raising the number further.
- **Added Date from/Date to filter *input fields* to the Handoff History tab** before
  realizing the batch-retrigger panel already had an equivalent "Date Range" mode —
  fully reverted (including a `rgmc-bc-api` service-layer change that came back to a
  clean `git diff` after reverting) and re-implemented by reusing the existing
  inputs instead.
- **First interpretation of "wait for the status of the process from the worker
  pool"** was assumed to mean routing retrigger-mssql actions through Pub/Sub with
  run_id polling — clarified via `AskUserQuestion` that the user actually meant "the
  backend already blocks until done synchronously, just fix what the UI displays."
  The real worker-pool-routing request came later, as a separate, narrower,
  specifically-scoped complaint about the file-name retrigger path only.
- **Verified the async retrigger-file UI flow by mocking `window.fetch`** in the
  browser rather than firing real destructive calls against production MSSQL —
  deliberate and correct, not a failure, but worth repeating as the standard
  technique for this app's retrigger/cleanup buttons going forward.

## Next Step

**Decide whether to commit and deploy the 120→300s stopgap** in
`rgmc-worker-pool/src/services/bigquery_bridge.py` and
`rgmc-gcp-api/src/routers/bigquery_bridge.py` — these are the only uncommitted
changes left from this session. Unlike every earlier round, the user had not yet
committed these as of session end, so don't assume they're already live. Concretely:
1. Confirm with the user whether to commit+deploy the 300s stopgap now, given it's
   explicitly a stopgap (buys time, doesn't fix the structural cost growth).
2. If deployed, verify via:
   `gcloud logging read 'resource.type="cloud_run_worker_pool" AND
   resource.labels.worker_pool_name="rgmc-worker-pool" AND textPayload:"Bridge run
   complete" AND textPayload:"customerpoul"' --project=durable-woods-465907-n1
   --freshness=30m --format="value(timestamp,textPayload)"` — look for a
   `'status': 'success'` with nonzero `header_records`/`detail_records` (confirming
   the backlog since 2026-10-29 01:26:33 UTC finally gets inserted), not another
   `'BigQuery read timed out after Ns'`.
3. **Separately and more importantly**: surface the dbt-view structural finding to
   whoever owns the `dbt-github` CI project (not accessible from this session). The
   concrete ask: materialize `int_document_ai` and `int_document_ai_detail` (dataset
   `sbic_int`, project `durable-woods-465907-n1`) as dbt tables — ideally
   `materialized: incremental` keyed on a stable ID with `created_at` as the
   incremental filter — instead of views, so the window-function/dedup logic runs
   once per new row instead of being recomputed from scratch on every single
   external read. Without this, expect the 300s stopgap to eventually become
   insufficient too as document volume grows, recreating this exact incident again.
4. If a `300s` timeout failure recurs before the dbt fix lands, the only remaining
   application-side lever is raising the timeout yet again (there is headroom up to
   just under the Pub/Sub subscription's 600s ack deadline) — but flag clearly to
   the user each time that this is purely delaying, not solving, the problem.

## Context & Gotchas

- **`gcloud`/`bq` CLI are available and authenticated** in this environment
  (`it.arellanoerwin@gmail.com`, project `durable-woods-465907-n1`, region
  `asia-southeast1`). Commands discovered/used this session:
  - `gcloud run services list --region=asia-southeast1` — regular Cloud Run
    services (includes a stale, long-undeployed `rgmc-worker-pool` *service* entry
    from Sept 16 — **legacy artifact, not the real worker**).
  - `gcloud beta run worker-pools list --region=asia-southeast1` — the *actual*
    Worker Pool resource type (`gcloud run worker-pools` without `beta` doesn't
    exist in this SDK version).
  - `gcloud logging read '<filter>' --project=durable-woods-465907-n1
    --freshness=<Nh> --limit=N --format="value(...)" --order=asc|desc` — prefer
    explicit `timestamp>="..." AND timestamp<="..."` bounds over `--freshness` +
    `--order=asc` together, which behaved unexpectedly (pulled entries far older
    than the freshness window) in one attempt this session.
  - Cloud Run Worker Pool log entries use `resource.type="cloud_run_worker_pool"`
    with label `resource.labels.worker_pool_name` — **not**
    `resource.labels.service_name` — querying with the wrong resource type silently
    returns `[]`.
  - `gcloud eventarc triggers list/describe --location=asia-southeast1` — trigger
    event filters and destinations.
  - `bq show --format=prettyjson <project>:<dataset>.<table>` — table/view metadata;
    for a VIEW, includes the full `view.query` definition text. This is how the
    Thread 4 root cause was confirmed — don't assume `numRows: 0, numBytes: 0` means
    "empty/just-recreated"; it's completely normal and expected for any BigQuery
    VIEW (no physical storage), and was a red herring investigated and ruled out
    this session before finding the real `"type": "VIEW"` + window-function cause.
  - `gcloud pubsub subscriptions describe <name> --format="value(ackDeadlineSeconds)"`
    — used to confirm the 300s worker timeout stopgap is safe against the
    subscription's 600s ack deadline.
- **BigQuery read performance for this project is genuinely variable AND
  structurally expensive** — two separate, compounding causes identified this
  session: (1) general slot-contention variance (same query 12s one time, 150s+
  another), and (2) `int_document_ai`/`int_document_ai_detail` being dbt views with
  non-pushdown-able window functions, meaning there's a real, large, unavoidable
  minimum cost per read regardless of contention. Don't treat future BigQuery
  slowness reports in this project as automatically "the same issue, just raise the
  timeout again" — check `bq show` for view vs. table status on the specific
  table/view involved before assuming more timeout is the answer.
- **`BQ_READ_TIMEOUT_SECONDS` (env var, default `"60"`) is the fallback for every
  un-overridden `read_gbq_with_timeout()` call**, in both `bq_read.py` copies. Any
  *new* call site added to either `bigquery_bridge.py` or `bigquery_routes.py` will
  silently inherit this 60s default unless `timeout=` is passed explicitly.
- **The `reprocess_runs_{env}` / `GET /api/reprocess-status/<run_id>` /
  `GET /bc/custom/v2/so-buffer/reprocess-status/{run_id}` infrastructure is fully
  generic** — reused as-is for the new `poul-so-retrigger-file` message type with
  zero changes to `rgmc-bc-api`. Follow the same pattern for any future "move a slow
  operation to the worker pool" request: publish via `_publish_poul_so_message` (or
  a sibling) in `rgmc-gcp-api`, add one `if msg_type == "...":` block in
  `so_import_worker.py`'s `_process()`, reuse the existing polling endpoints as-is.
- **Eventarc audit-log triggers cannot filter on nested payload fields** (like a
  BigQuery job's destination table) — only top-level attributes. Any future
  "this trigger fires too often/on the wrong thing" investigation for an
  audit-log-based Eventarc trigger needs the same fix pattern: parse the CloudEvent
  body in the Cloud Run handler, fail open on parse ambiguity.
- **The Flask dev server (`py -m flask run --port 5055`) does not auto-reload**
  (`debug=off`) — check `Get-NetTCPConnection -LocalPort 5055` for a stale bound
  process and kill it before restarting when testing locally.
- **Never fire real cleanup-staging/insert-from-bigquery calls against production
  MSSQL just to test frontend rendering logic** — mock `window.fetch` in the browser
  (via `javascript_tool`) instead, as done this session for the async retrigger-file
  UI flow.
- **The user usually commits promptly after Claude's edits, but not always
  immediately** — this session's very last change (the 120→300s stopgap) was left
  uncommitted at session end, breaking the pattern established earlier in the same
  session. Always check `git status`/`git log` fresh rather than assuming the
  established pattern holds for the most recent edit.
