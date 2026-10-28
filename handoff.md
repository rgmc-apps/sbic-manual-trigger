# Handoff

## Goal

Continue hardening the RGMC/SBIC SO-import ecosystem across five repos —
`rgmc-gcp-api` (`C:\RGMC\Source\git\rgmc-gcp-api` — BigQuery/MSSQL bridge + BC trigger
endpoints), `rgmc-worker-pool` (`C:\claude\rgmc-worker-pool` — Cloud Run Worker Pool,
Pub/Sub-driven BC writes, the BigQuery bridge itself), `rgmc-bc-api`
(`C:\claude\rgmc-bc-api` — BC/Firestore buffer API), `rgmc-it-bot`
(`C:\claude\rgmc-it-bot` — MS Teams bot, untouched this session), and
`sbic-manual-trigger-page` (`C:\claude\sbic-manual-trigger-page`, this repo — the `/`,
`/trigger`, `/dashboard`, and `/reconcile` Flask UI). This session picked up from a
prior handoff and tackled, in order:

1. Investigated a live production bug: PO ref "CSS #320647" got **32 duplicate Sales
   Orders** in BC (31 empty, one real). Root-caused to a `#` character in the PO ref
   silently truncating a URL in `bc_client.py`'s existing-order dedup check (`requests`/
   `urllib` treats a bare `#` as a URL fragment and drops everything after it).
2. Added a "Retrigger Handoff (MSSQL)" button (single-PO, then batch) to let a human
   manually re-send a stuck PO's BigQuery data into MSSQL's staging tables.
3. Built a full "BigQuery Bridge Handoff History" log — Firestore-backed, one record
   per PO, showing three independent statuses: did it land in MSSQL, did the AFTER
   INSERT trigger actually promote it to the real table, did Business Central
   creation succeed — displayed on both `/trigger` and `/dashboard`.
4. Fixed a second live crash (`'dict' object has no attribute 'drop'`) in the
   BigQuery bridge's `main()`, and added a 5-minute cooldown + in-progress lock to
   stop automated bridge triggers from re-firing back-to-back and spamming
   notifications.
5. Disabled email/Teams notifications for the noisy 2-hourly `customerpoul-backfill-
   held-back` Cloud Scheduler job, while keeping real upload/handoff notifications.
6. Investigated (and found NOT a bug, but a UX clarity gap) why linked SKUs on the
   Buffer Reconciliation page "don't seem to apply" without clicking Reprocess
   Buffer — added an explicit "Saved — click Reprocess Buffer to send to BC" note.
7. Fixed a real duplicate-SKU-group display bug (a numeric SKU's pandas/BigQuery
   float-coercion artifact, e.g. `"218720608619709"` vs `"218720608619709.0"`,
   showing as two separate groups).
8. Added a "filter to one file" search box on the Buffer Reconciliation page
   (by filename or a sample PO ref number).
9. Added `referenceNo` (the raw linked SKU code) to saved SKU overrides in Firestore.

## Current State

**Everything from this session is committed AND deployed, confirmed live.** This is
a major change from where the session started (nothing deployed) — the user
committed and deployed repeatedly throughout, out-of-band, as has been their pattern
all along. Verified directly against production:

- **`sbic-manual-trigger-page`** (this repo, branch `master`): clean working tree,
  HEAD = `f393405` "DI-0229 added manual trigger for handoff and other fixes". Cloud
  Run revision `sbic-manual-trigger-00097-m26` is live and serving — confirmed by
  curling `/api/buffer/resolve-file-scope` (added in the LAST commit) and getting a
  proper JSON 404, not an HTML error page.
- **`rgmc-gcp-api`**: clean, HEAD = `db1a67d` "DI-0229 added handoff retrigger with
  cleanup". Confirmed live: `POST /customerpoul/cleanup-staging` responds correctly.
- **`rgmc-bc-api`** (branch `staging`): clean, HEAD = `19ca7fc` "DI-0229 added mssql
  handoff retrigger with cleanup". Confirmed live: `GET .../handoff-history
  ?mssql_status=inserted` returns the new per-PO schema (`mssql_status`,
  `trigger_status`, `bc_status` fields) — **and already has a real record** from the
  user testing the retrigger button live (`PO407026102301579`, `trigger_status:
  "promoted"`, `method: "manual-dashboard-retrigger"`), so the single-PO retrigger
  flow is confirmed working end-to-end in production.
- **`rgmc-worker-pool`**: clean, HEAD = `1edcc52` "DI-0229 added handoff retrigger
  with cleanup". Worker-pool revision `rgmc-worker-pool-00112-8kh`, created
  2026-10-28T10:30:46Z (today, recent — consistent with this session's commits being
  live).
- **`rgmc-it-bot`**: untouched this session.

**Not independently confirmed working** (built and deployed, but not yet observed
live by either the user or this session):
- The Batch Retrigger panel (PO Ref Number(s) / Date Range modes) and the
  per-file-group "Retrigger whole file" button on the Handoff History tab.
- The Buffer page's new "filter to one file" search box.
- The 5-minute bridge-run cooldown lock (`bridge_run_lock.py`) actually preventing a
  real back-to-back trigger storm — no bridge run has been observed since deploy.
- Whether the `customerpoul-backfill-held-back` Scheduler job's next run (every 2h,
  7am-8pm Mon-Fri Asia/Manila) is actually silent now (no email/Teams notification).
- Whether the "CSS #320647"-style duplicate-order bug is actually fixed in a live
  re-trigger scenario (the fix itself — `params=` instead of raw URL interpolation —
  was verified via a local `requests` test proving the `#` now survives intact, but
  not re-tested against a live duplicate-prone PO).

**Still unresolved from a prior handoff, carried forward, never addressed this
session**: whether the Supabase migration for `rgmc-it-bot`'s new
`po_upload_subscriptions` table was ever actually run. The user said "I'll run it
now" early in this session; no confirmation was given afterward, and it was never
circled back to. The PO-upload Teams chat notifications feature (built in an earlier
session) still depends on this table existing.

## Files Actively Being Edited

All committed — listed for reference on what changed and why, newest work first.

**`sbic-manual-trigger-page`** (commits `428563d`..`f393405`):
- `app.py` — major additions: `_canonical_sku_code()`/`_NUMERIC_SKU_FLOAT_ARTIFACT_RE`
  (strips a numeric SKU's trailing ".0" artifact before grouping, used in
  `_group_buffer`); `/api/buffer/resolve-file-scope` (resolves a filename-or-PO-ref
  query to the full po_ref_number set for that BigQuery upload); `/api/handoff-history/
  retrigger-mssql` + `/api/handoff-history/retrigger-mssql-batch` (the latter accepts
  exactly one of `po_ref_numbers`/`file_name`/`date_from`+`date_to`) plus shared
  helpers `_insert_mssql_and_log`/`_cleanup_and_reinsert_po`; `/api/dashboard/
  mssql-status` extended with `promoted_in_mssql`/`retrigger_supported`; `/api/handoff-
  history` proxy updated for the new `mssql_status`/`bc_status` filter shape. The OLD
  `/api/dashboard/retrigger-mssql-handoff` route was removed (superseded by the
  shared retrigger-mssql endpoint, nothing else called it).
- `static/reconcile.js` — `visibleGroups()`/`visibleOrders()` (client-side file-scope
  filtering, never mutates `state.groups` itself — lookups like `findGroup`/
  `groupResolvedFor`/`highlightHighConfidenceMatches` deliberately still read
  `state.groups[type]` directly, unfiltered); `applyFileScope()`/`clearFileScope()`;
  `resolvedPayload()` now takes an optional `key` param, stored as `referenceNo` for
  "sku"-type resolutions.
- `templates/reconcile.html` — new file-scope filter toolbar in `#action-panel-
  buffer`; the `group-resolved` badge in `#group-row-template` now includes a
  `.resolved-queued-note` explaining Reprocess Buffer is still required.
- `static/reconcile.css` — `.file-scope-toolbar` styling.
- `static/dashboard.js` — `renderRetriggerAction()` (renamed/rewritten from
  `renderRetriggerMssqlAction`, now calls the shared `/api/handoff-history/
  retrigger-mssql` by `po_ref_number` alone instead of header+details); Check MSSQL
  now also checks `promoted_in_mssql` and offers retrigger for the "staged but never
  promoted" case, not just "not found at all".
- `static/handoff-history.js` — shared by `/trigger` and `/dashboard` (both load this
  file + matching card markup): per-PO MSSQL/Trigger/BC status badges; per-record
  retrigger button (gated on `group_code === "customerpoul"` and `mssql_status ===
  "error" || trigger_status === "not_promoted"`); per-file-group "Retrigger whole
  file (N)" button; new Batch Retrigger panel (PO Ref Number(s) / Date Range modes)
  wired to the batch endpoint.
- `templates/index.html` / `templates/dashboard.html` — Handoff History card markup
  updated: `mssql_status`/`bc_status` filter dropdowns (replacing one generic
  `status` dropdown), new Batch Retrigger panel markup.
- `static/style.css` — `.order-badge.pending`, `.handoff-history-retrigger-action`/
  `-result`, `.handoff-history-batch-retrigger` rules.
- `cloudbuild.yaml` — **real bug fix, not just a workaround**: image tag changed from
  `$COMMIT_SHA` to `$BUILD_ID`. `$COMMIT_SHA` is only populated for trigger-based Cloud
  Build runs; a manual `gcloud builds submit` left it empty, producing an invalid
  image reference (`...sbic-manual-trigger:` with no tag) and failing outright.
  `$BUILD_ID` is always populated regardless of how the build was started.

**`rgmc-gcp-api`** (commit `db1a67d`):
- `src/services/bc_client.py` — `v2_find_sales_order_by_external_doc_no` and
  `v2_find_sales_order_by_number` now pass their OData `$filter` via `params=`
  instead of hand-interpolating it into the URL string. Root cause of the CSS
  #320647 duplicate-orders bug: a raw `#` in the URL is treated as a fragment
  delimiter and silently dropped by `requests`/`urllib`, truncating the filter to an
  unterminated string literal that matched nothing — so the existing-order dedup
  check always reported "not found," and `_create_order` kept creating a fresh
  duplicate header every automated run.
- `src/routers/sbic_routes/_db.py` — new `run_execute()` (commits writes; the
  pre-existing `run_query()` never calls `.commit()`, which is fine for SELECTs but
  would silently roll back a DELETE/UPDATE).
- `src/routers/sbic_routes/CustomerPOUL.py` — new `POST /customerpoul/cleanup-staging
  ?po_ref_number=X` — deletes a PO's rows from `CustomerPOULDetailBQ`/`CustomerPOULBQ`
  (staging only, never the promoted tables) so a follow-up insert gets a genuinely
  fresh `INSERT` for the AFTER INSERT trigger to react to.

**`rgmc-bc-api`** (commits `c00fafe`, `19ca7fc`, branch `staging`):
- `src/services/so_buffer_service.py` — `list_handoff_history` now filters on
  `mssql_status`/`bc_status` (was a single generic `status`); new
  `record_manual_handoff(po_ref_number, file_name, mssql_status, error, trigger_status)`.
- `src/routers/bc_routes/so_buffer_routes.py` — `GET .../handoff-history` updated;
  new `POST .../handoff-history/manual-entry`.

**`rgmc-worker-pool`** (commits `2b2b5fc`..`1edcc52`):
- `src/services/bridge_handoff_history.py` — **redesigned from run-level/table-
  grouped docs to one Firestore document per PO ref**. `record_handoff(group_code,
  method, mssql_status, po_refs_by_file, error=None, trigger_results=None)` —
  `mssql_status` is `"inserted"`/`"error"`; a duplicate-key skip (PO already in MSSQL
  from an earlier run) is deliberately never re-recorded, to avoid spamming the
  history every time BigQuery re-serves an already-ingested row (a known, accepted
  upstream limitation — see Context & Gotchas). New `update_bc_status(po_ref_number,
  bc_status, so_number=None, error=None)` — patches whichever doc is still
  `bc_status: "pending"` for that po_ref, called from `so_import_worker.py`.
- `src/services/bigquery_bridge.py` — **two fixes**: (1) `main()` now bails out
  immediately with the original fetch's own error if either `header_table` or
  `detail_table` isn't a real DataFrame (i.e. `__get_bigquery_data` returned its
  `{"status":"error",...}` sentinel dict) — previously only bailed when BOTH were
  invalid, so a header-fetch-succeeds/detail-fetch-fails split sailed into
  `__rename_columns`, which crashed calling `.drop()` on a dict. (2) new
  `__verify_trigger_promotion(header_table, mssql_header_table_name, main_key)` — one
  bulk SELECT against the PROMOTED table (`CustomerPOUL`/`CustomerRA`, mapped via
  `_PROMOTED_TABLE_BY_STAGING_TABLE`) confirming which po_refs the AFTER INSERT
  trigger actually promoted; wired into the success-path `record_handoff` call as
  `trigger_results`. Also removed now-dead `po_to_file` local (only needed for a
  detail-table grouping call site that no longer exists after the redesign).
- `src/workers/bigquery_bridge_worker.py` — new `bridge_run_lock` gate: for
  `method == "triggered"` messages only (never manual), skips (acks, drops) a run if
  one is already in progress for that `group_code` or the last one started <5 minutes
  ago; calls `bridge_run_lock.finish()` in a `finally` block once the real run
  completes either way.
- `src/services/bridge_run_lock.py` (**new**) — the lock itself. Firestore collection
  `bridge_run_lock_{env}`, one doc per `group_code`, fields `status`/`started_at`/
  `finished_at`. Fails OPEN on any Firestore error (never permanently blocks a real
  run).
- `src/workers/so_import_worker.py` — `_run_backfill_from_cloudsql` (the hourly
  Scheduler job's handler) had ALL its notification calls removed (`_send_batch_
  notification`, `_send_unmatched_items_notification`, `bot_notify.notify_po_batch`,
  `bot_notify.notify_po_unmatched`, and the two "skipped (already in BC)"/"nothing to
  backfill" success emails) — plus the now-dead `extra_recipients`/`requested_by`/
  `total_headers_considered` locals that only existed to build those. `_run_batch`'s
  per-order loop (the function that actually talks to BC, used by both live inbound
  batches AND Reprocess Buffer) now calls `update_bc_status` at all 4 outcome points
  (created/buffered/BC-locked/exception) to patch the handoff-history record's
  `bc_status`.

## Failed Attempts

- **`gcloud builds submit --config cloudbuild.yaml .` with the original cloudbuild.yaml** —
  **Failed**: `INVALID_ARGUMENT: invalid build: invalid image name
  "...sbic-manual-trigger:": could not parse reference` — `$COMMIT_SHA` is empty on a
  manual (non-trigger) submit. Fixed by switching to `$BUILD_ID` in cloudbuild.yaml
  (see Files section) — this is a permanent fix to the file, not a one-off flag.
- **`curl -X POST .../customerpoul/cleanup-staging` with no body/headers** —
  **Failed**: `411 Length Required` from Google's front-end proxy, before even
  reaching Cloud Run. Known, already-documented gotcha from a prior session: add
  `-H "Content-Length: 0"` to any bodyless POST against these Cloud Run services.
- **Treating "linked SKUs don't apply without Reprocess Buffer" as a bug** — spent
  two rounds of sub-agent investigation; concluded it is NOT a bug, it's the
  intentional queue-then-apply design (`[[feedback_queue_vs_apply_pattern]]`) —
  `_run_batch` (Reprocess Buffer or a live inbound batch) is the ONLY code path that
  ever talks to BC, by design, confirmed in `so_import_worker.py`'s own comments
  re-deriving overrides fresh rather than trusting the earlier Firestore patch. Fix
  was a UX clarity note, not a logic change. **Do not re-investigate this as a code
  bug in a future session** without new evidence.
- **Assuming the LIPTON duplicate-SKU report could be reproduced live** — queried the
  live `/api/buffer?company=SBIC` and found no LIPTON line at all (buffer state had
  moved on since the user's report). The fix (`_canonical_sku_code`) was applied on
  code-path reasoning alone, not a live-confirmed repro — flagged as unconfirmed in
  the commit/PR sense, but low-risk and clearly correct given how `_recover_lines_
  for_order`'s BigQuery fallback works.

## Next Step

Nothing is blocking — everything built this session is committed and deployed. The
most valuable next action is **live verification of the pieces that were deployed
but never actually exercised**, roughly in this order:

1. **Confirm the Supabase migration** for `rgmc-it-bot`'s `po_upload_subscriptions`
   table was actually run (carried over from before this session, never confirmed
   — ask the user directly, or check via the Supabase dashboard/API if credentials
   are available).
2. **Watch the next automated `customerpoul-backfill-held-back` Scheduler run**
   (fires every 2h, 7am-8pm Mon-Fri Asia/Manila — `gcloud scheduler jobs describe
   customerpoul-backfill-held-back --location=asia-southeast1` to check `lastAttempt
   Time`/next schedule) to confirm: (a) no email/Teams notification fires for a
   routine run, (b) no regression in the actual backfill behavior itself.
3. **Watch Cloud Logging for the next Eventarc-triggered bridge run** (`method=
   "triggered"`, `group_code=customerpoul`) to confirm the `bridge_run_lock` 5-minute
   cooldown is actually preventing back-to-back runs, and that the earlier `'dict'
   object has no attribute 'drop'` crash class is gone.
4. **Exercise the Batch Retrigger panel and file-scope filter in a real browser**
   (`/trigger` or `/dashboard`'s Handoff History tab; `/reconcile`'s Buffer tab) —
   these were built and deployed but never clicked through live this session.
5. If a new real duplicate-order PO ref surfaces, use it to confirm the `#`-
   character URL-encoding fix actually prevents a repeat of the CSS #320647 incident.

## Context & Gotchas

- **The user commits AND deploys this session's work themselves, out-of-band,
  repeatedly** — reconfirmed strongly this session: every repo's git log and Cloud
  Run/worker-pool revision history showed fresh commits/deploys the assistant never
  ran (except the one explicit, user-approved `sbic-manual-trigger-page` deploy via
  `gcloud builds submit`, which the user then deployed AGAIN after on top of, taking
  it from revision `-00095-5bt` to `-00097-m26`). **Always re-check `git log`/`git
  status`/live endpoints fresh at the start of a session** before assuming anything
  is undeployed — this session nearly over-reported "nothing is deployed" again
  before directly verifying against production.
- **`#` in a PO ref number is a real, recurring hazard** — "CSS #320647" is a genuine
  customer PO number; any future code that builds a URL by string interpolation
  (rather than `requests`' `params=`) risks the exact same silent-truncation bug.
  Grep for `?$filter=` or similar raw-interpolated OData URLs before trusting a new
  BC lookup function — `rgmc-worker-pool/src/services/bc_client.py`'s
  `v2_list_records_by_filter` (used by the user's own separate, unrelated
  "session-history verification" feature) still has this exact same raw-URL pattern
  and was deliberately NOT touched this session (out of scope, and risked stepping
  on the user's own in-progress work) — worth fixing if it's ever reported.
- **Known, accepted tradeoff, reconfirmed**: BigQuery's upstream `int_document_ai`/
  `int_document_ai_detail` tables keep re-serving already-ingested rows past the
  bridge's own watermark (root cause outside these repos). The handoff-history
  redesign this session deliberately does NOT re-record a duplicate-key skip as a
  new "inserted" event (to avoid spamming the log), but the bridge still re-attempts
  `__trigger_so_import` for these rows every run — now safely idempotent thanks to
  the `#`-encoding fix, just still repetitive. Not fixed, not expected to be fixed
  here.
- **Firestore collection naming, now FOUR deep**: `so_buffer_{env}`,
  `so_buffer_history_{env}`, `bridge_handoff_history_{env}` (redesigned this session
  to per-PO docs), and `bridge_run_lock_{env}` (new this session) — all keyed off
  `GCP_ENV` lowercased (e.g. `_production`). Keep using this convention.
- **The dashboard's existing "Check MSSQL" feature has a narrower scope than its
  name suggests**: it only ever checked the `_BQ` STAGING tables
  (`CustomerPOULBQ`/`CustomerRABQ`), never the PROMOTED tables
  (`CustomerPOUL`/`CustomerRA`) — this was a deliberate earlier design choice
  ("per the dashboard's own spec," per its original docstring) that this session
  extended (not reverted) by adding a SEPARATE `promoted_in_mssql` field alongside
  the existing staging check, rather than changing what "Check MSSQL" has always
  meant.
- **Retrigger is PO-uploads-only (`group_code == "customerpoul"`), never RA or
  Online Sales PO** — `rgmc-gcp-api` has no RA equivalent of `insert-from-bigquery`/
  `cleanup-staging`, and Online Sales PO's BigQuery source table
  (`int_online_sales_data`) is different from `int_document_ai`, which the retrigger
  endpoints hard-code. Both the Search tab's gating (`retrigger_supported`) and the
  History tab's per-record/per-file-group buttons (`group_code === "customerpoul"`)
  enforce this — don't relax it without also building the RA/online-sales
  equivalents server-side first.
- **The file-scope filter on the Buffer page is purely a client-side DISPLAY
  filter** (`visibleGroups()`/`visibleOrders()` in `reconcile.js`) — it does not
  change what a "Link" action affects. A SKU group spanning POs both inside and
  outside the current file filter still shows (since it has ≥1 in-scope PO) with
  its FULL po_refs list, and resolving it still applies to every PO sharing that
  key, not just the visible ones. This is intentional, not a bug — flagged
  explicitly to the user as a known behavior.
- **`gcloud` CLI quirks, reconfirmed**: `gcloud run worker-pools` needs `gcloud
  beta` (actually `gcloud beta run worker-pools ...`); a bodyless POST against these
  Cloud Run services needs `-H "Content-Length: 0"` or you get a `411`; a manual
  `gcloud builds submit` has `$COMMIT_SHA` empty (use `$BUILD_ID` in
  `cloudbuild.yaml` instead, already fixed here).
- **Project/region for all `gcloud` operations this session**: project
  `durable-woods-465907-n1`, region `asia-southeast1`.
