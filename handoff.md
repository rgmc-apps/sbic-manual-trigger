# Handoff

## Goal

Continue hardening the RGMC/SBIC SO-import ecosystem across five repos —
`rgmc-gcp-api` (`C:\RGMC\Source\git\rgmc-gcp-api` — BigQuery/MSSQL bridge + BC trigger
endpoints), `rgmc-worker-pool` (`C:\claude\rgmc-worker-pool` — Cloud Run Worker Pool,
Pub/Sub-driven BC writes, the BigQuery bridge itself), `rgmc-bc-api`
(`C:\claude\rgmc-bc-api` — BC/Firestore buffer API), `rgmc-it-bot`
(`C:\claude\rgmc-it-bot` — MS Teams bot), and `sbic-manual-trigger-page`
(`C:\claude\sbic-manual-trigger-page`, this repo — the `/`, `/trigger`, and
`/reconcile` Flask UI). This session's concrete asks, in order:

1. Resume/verify prior session's work (sbic_db.py MSSQL resolution path, redesigned
   emails) via live reprocess-buffer tests.
2. Investigate why a Waltermart PDF upload got its customer/ship-to tagged as the
   wrong customer (Puregold) in BC, and add buffering (instead of guessing) when
   ship-to lookup/legacy codes aren't set up.
3. Add a new "PO Uploads" Teams notification group on `rgmc-it-bot`
   (`po-all`/`po-bc`/`po-buffer` subscriptions), converting the existing email
   notifications into compact chat cards — **alongside** email, not replacing it.
4. Add a dedicated success email (with full line-item detail) for `order_task`
   (previously had zero success email), and close hard-error alerting gaps across
   the whole worker pool so nothing crashes silently without an email.
5. Investigate and fix why a batch of SUNCOAST POs was falsely reported
   "held back (0 line items extracted yet)" when they were already in BC.
6. Investigate why 51 specific Waltermart POs were stuck in true limbo (never
   reached BC, never buffered), trigger the fix, and "figure out a way to avoid
   these lapses on transferring data to BC" going forward.
7. "Stitch" the BigQuery-bridge insert step and the BC handoff step together: the
   data actually accepted-for-insert from BigQuery (company-filtered) must be the
   same data used for BC processing/sanitizing — not a post-dedup-mutated subset.
8. Add a lightweight Firestore "handoff history" (PO ref numbers only, per target
   MSSQL table/"collection", plus date/time/status) and display it on the manual
   trigger page, grouped **by source filename** for organization.

## Current State

**Everything is implemented and working** (verified live against production for
items 2, 5, 6; items 3/4/7/8 are implemented, compile/syntax-checked clean, but
**not yet deployed or live-verified**). Commit status, per repo:

- **`rgmc-worker-pool`** (branch `master`): **fully committed**, nothing
  outstanding. `dc67ee6` "added notification" (Waltermart/Puregold fuzzy-match
  removal + PO-upload chat-notification wiring, made by this session, committed by
  the user), `c536392` "bigquery bridge modifications" (hard-error
  `notify_error` additions across `so_import_worker.py`/`order_worker.py`/
  `sync_worker.py`/`bigquery_bridge_worker.py`, plus `order_task`'s new success
  email with line detail), `2e7b95f` "added worker pool modifications" (the
  bridge's "held back" false-positive fix + duplicate-retry efficiency rewrite +
  dedicated held-back alert email + new `bridge_handoff_history.py` + filename
  grouping). **None of this has been deployed** (no `gcloud builds submit` run this
  session).
- **`rgmc-it-bot`** (branch `master`): **fully committed** as `ae16b41` "added po
  upload notifications" (new `po_upload_subscriptions` Supabase table, `subscribe
  po-all/po-bc/po-buffer` commands, compact Adaptive Cards, new webhook routes).
  **Not deployed.** Supabase migration (`supabase_setup.sql`'s new table block) has
  **not been run** against the live Supabase project either — needs doing before
  this works at all.
- **`rgmc-bc-api`** (branch `staging`... actually check — see Context & Gotchas):
  **fully committed** as `615b128` "added manual trigger modifications" (new
  `/bc/custom/v2/so-buffer/handoff-history` read endpoint). **Not deployed.** Also
  still has the user's own unrelated uncommitted "session-history verification"
  work untouched (not this session's — see Context & Gotchas).
- **`sbic-manual-trigger-page`** (this repo, branch `master`): **uncommitted** —
  `app.py`, `static/style.css`, `templates/index.html` modified, `static/
  handoff-history.js` new/untracked. See Files Actively Being Edited below.
- **`rgmc-gcp-api`**: untouched this session except read-only investigation
  (confirmed its own duplicate `BigqueryBridge`/`__insert_dataframes` copy is dead
  for the SO-trigger path — only reachable via `insert_selected_records`, which
  always passes `trigger_so_import=False` — so none of this session's bridge fixes
  needed to be mirrored there).

**Live-verified working** (via direct production calls this session):
- The Waltermart/Puregold fuzzy-match bug — root-caused and fixed (though the
  FIX itself, being in `so_import_worker.py`, is committed but **not yet
  deployed** — so the live service is still running the old, buggy fuzzy-match
  code as of session end).
- The "held back" false-positive bug and the slow duplicate-retry loop — both
  root-caused via live log analysis; the FIX is committed but **not deployed**, so
  production is still running the old code.
- Manually triggered `POST /customerpoul/backfill-from-cloudsql?companies=SBIC`
  successfully created all 50 of the 51-PO-ref list's unique refs in BC (`SBSO265223`–
  `SBSO265272`) — confirmed via direct BC API query. This was a **workaround**
  using existing (old) code, not proof the new code works — the new code has not
  been deployed or exercised live yet.

**Not yet live-verified at all**: the PO-upload chat notifications (Teams bot),
the `order_task` success email, the new hard-error alert emails, the bridge's
held-back-fix/efficiency-fix/alert-email, and the entire handoff-history
feature (Firestore collection has never been written to or read from live,
since nothing is deployed).

## Files Actively Being Edited

**`sbic-manual-trigger-page`** (this repo) — all uncommitted:
- `app.py` — two new proxy routes: `/api/handoff-history` (near existing
  `/api/history`, ~line 1034) forwarding `group_code`/`status`/`date` filters to
  rgmc-bc-api's new endpoint. (The earlier session's `_find_missing_items`
  `netPrice` change from a prior session is already committed, untouched.)
- `templates/index.html` — new "BigQuery Bridge Handoff History" card appended
  after the existing result card, with process/status filter dropdowns and a
  `<script src="/static/handoff-history.js">` tag.
- `static/style.css` — new `.handoff-history-*` rules plus ported `.order-row`/
  `.order-badge`/`.btn-history-toggle`/`.tab-empty` rules (copied from
  `reconcile.css`, since `index.html` doesn't load that stylesheet) appended
  before the `.hidden`/`@media` utilities block at the end of the file.
- `static/handoff-history.js` (**new**) — fetches `/api/handoff-history`, renders
  each record as a row with per-table, per-filename grouped PO-ref lists
  (expandable "Show PO refs" toggle), sorted largest-file-first.

**`rgmc-worker-pool`** (committed, `2e7b95f`/`c536392`/`dc67ee6` — for reference if
revisited):
- `src/services/bigquery_bridge.py` — `__insert_dataframes` now captures
  `full_header_table`/`full_detail_table` (unpruned) before the duplicate-retry
  loop and passes THOSE (not the pruned ones) to `__trigger_so_import`, fixing
  the false "held back" report. New `__existing_key_rows`/`__drop_already_inserted`
  do a bulk pre-check (one SELECT per table, scoped to this batch's PO refs)
  BEFORE the insert attempt, replacing the old one-row-at-a-time
  IntegrityError-retry loop that could take 15+ minutes on a heavily-duplicated
  batch. New `__group_by_file` groups a table's PO refs by BigQuery's `fileName`
  column (header tables have it; detail tables don't — a `po_to_file` lookup
  built once from the header table covers detail's grouping too). New dedicated
  `notify_warning` fires the moment any order is held back (visibility fix, not
  waiting for the 2-hourly Scheduler job to silently self-heal). Three
  `record_handoff(...)` call sites (success + 2 error paths) write to the new
  Firestore history.
- `src/services/bridge_handoff_history.py` (**new**) — `record_handoff(...)`
  writes to Firestore `bridge_handoff_history_{env}`, lazy-singleton client
  pattern matching `so_buffer_history.py`.
- `src/workers/so_import_worker.py` — (a) removed `_fuzzy_ship_to_lookup`/
  `_normalize_name`/`_SHIP_TO_FUZZY_THRESHOLD`/`ship_to_by_name` entirely (the
  Waltermart→Puregold root cause); ship-to cascade is now structured-tier →
  saved override → exact code/lookupCode match → buffer, no guessing. (b) Primary
  inbound path (`_run_batch` call in `_process`) now wrapped in try/except with
  `notify_error` + nack (previously **zero** exception handling there at all —
  the single biggest gap). (c) The other 4 manual-trigger branches' existing
  except blocks now also call `notify_error`, not just log.
- `src/workers/order_worker.py` — new `_line_summary` helper; `notify_success`
  now fires on every completed order task with full line-item detail (item,
  qty, price, description) — this pipeline previously had **zero** success
  email. Decode-failure path now also alerts.
- `src/workers/sync_worker.py` — decode-failure path now alerts.
- `src/workers/bigquery_bridge_worker.py` — decode-failure path AND the main
  `except Exception` (previously log+nack only, no email) both now alert
  (safety net alongside `bigquery_bridge.py`'s own internal `__notify`, which
  doesn't cover everything that can go wrong before its own try starts).
- `src/config.py` — `RGMC_IT_BOT_URL`/`RGMC_IT_BOT_API_KEY` env vars added.
- `.env.example` — documents the two new vars.

**`rgmc-it-bot`** (committed, `ae16b41`):
- `supabase_setup.sql` — new `po_upload_subscriptions` table (`notify_po_bc`,
  `notify_po_buffer` booleans) — **migration not yet run against live Supabase.**
- `src/types/index.ts`, `src/services/supabase.ts`, `src/services/
  channelService.ts`, `src/services/notificationService.ts`,
  `src/cards/poUploadCard.ts` (new), `src/routes/webhook.ts`, `src/bot.ts`,
  `src/cards/helpCard.ts`, `src/config.ts`, `.env.example` — full PO-upload
  notification group (subscribe po-all/po-bc/po-buffer, unregister po, status po,
  compact batch/unmatched cards, new `/api/notify/po-upload-batch` and
  `/po-upload-unmatched` webhook routes).

**`rgmc-bc-api`** (committed, `615b128`):
- `src/services/so_buffer_service.py` — `_handoff_history_collection()` +
  `list_handoff_history(...)`.
- `src/routers/bc_routes/so_buffer_routes.py` — `GET /bc/custom/v2/so-buffer/
  handoff-history` (note: its `status` query param is aliased to
  `handoff_status` internally to avoid shadowing FastAPI's `status` module
  import used for `HTTPException` — this was a real bug caught and fixed before
  committing, see Failed Attempts).

## Failed Attempts

- **OData `in (...)` filter against BC's sales-orders search** — tried batch-
  checking all 51 PO refs in one call via `externalDocumentNo in ('a','b',...)`.
  **Failed**: BC returned `501 BadRequest_MethodNotImplemented` — BC's OData
  implementation doesn't support `in`. This exact failed call surfaced as a real
  "RGMC BC API — Error 502" alert the user saw mid-session — confirmed it was
  this debugging call, not a new system problem. **Fixed** by switching to an
  `externalDocumentNo eq 'x' or externalDocumentNo eq 'y' or ...` OR-chain, which
  BC accepts fine.
- **Assumed a reprocess-buffer/backfill trigger's result email would send
  automatically** — it doesn't; `notify_email`/`notify_name` must be passed
  explicitly as query params on the trigger call, or no email goes out at all
  (confirmed via `notify=None` in logs on the first trigger attempt this
  session). Re-triggered with `notify_email=erwin.arellano@rgmcgroup.com` to get
  a real email for visual template verification.
- **`status: Optional[str] = Query(...)` as a FastAPI route parameter name in
  `get_handoff_history`** — shadowed the module-level `from fastapi import
  status` import within that function's scope, which would have made
  `status.HTTP_500_INTERNAL_SERVER_ERROR` in the except block raise
  `AttributeError` at runtime (string has no such attribute) instead of ever
  actually returning the intended error response. **Caught before committing**
  by re-reading the function; fixed by renaming the Python parameter to
  `handoff_status` with `alias="status"` so the public query string (`?status=
  success`) is unchanged.
- **Assumed `createDate`/`updateDate` on `CustomerPOUL` reflect true MSSQL
  insertion time** — they don't; a live bridge run was seen flagging a PO ref as
  an MSSQL duplicate *before* that PO's own `createDate` timestamp (per the API).
  Concluded these fields are copied through verbatim from BigQuery's own
  source timestamp (closer to Document-AI extraction time), not stamped at
  actual MSSQL write time — don't trust this field for "how long has this been
  stuck" reasoning in a future session; cross-check against actual Cloud
  Logging timestamps instead.
- **Considered widening the `customerpoul-backfill-held-back` Cloud Scheduler
  job to run hourly/24-7** as additional defense-in-depth — **user explicitly
  declined**, choosing to rely on the code-level fixes alone. Don't re-propose
  this without new evidence it's needed.

## Next Step

**Nothing is deployed from this session yet.** In order:

1. **Decide whether to commit `sbic-manual-trigger-page`'s uncommitted changes**
   (`app.py`, `static/style.css`, `templates/index.html`, `static/
   handoff-history.js`) — this is the one repo where this session's work isn't
   already committed. The user was about to be asked this when the session
   ended.
2. **Run the Supabase migration** (`rgmc-it-bot/supabase_setup.sql`'s new
   `po_upload_subscriptions` table block) against the live Supabase project —
   required before the PO-upload Teams notifications can work at all.
3. **Deploy, in dependency order**: `rgmc-worker-pool` first (self-contained,
   inert until Teams bot/handoff-history consumers exist, but also where the
   actual bug fixes live — highest value), then `rgmc-it-bot` (needs
   `RGMC_IT_BOT_URL`/`RGMC_IT_BOT_API_KEY` env vars set on `rgmc-worker-pool`'s
   Cloud Run Worker Pool config to match, and vice versa — they're a matched
   pair, see `rgmc-worker-pool/.env.example`), then `rgmc-bc-api`, then
   `sbic-manual-trigger-page` last (depends on rgmc-bc-api's new endpoint
   existing).
4. **Live-verify after deploy**, in order of risk: (a) trigger a real
   reprocess-buffer/bridge run and confirm the "held back" false-positive is
   gone and the duplicate-retry no longer stalls for minutes; (b) subscribe a
   Teams channel to `po-all` and confirm a real card arrives looking right;
   (c) check `/trigger`'s new History card renders real Firestore data grouped
   by filename correctly; (d) confirm `order_task`'s new success email actually
   arrives with correct line detail (field names there — `number`/
   `lineObjectNumber`/`itemNo`, `quantity`, `unitPrice` — were written
   defensively/best-effort since the real BC API payload schema for this
   specific generic order-task path was never directly inspected this session;
   worth confirming the rendered email's item/qty/price actually show real
   values and not "—" placeholders).

## Context & Gotchas

- **The user commits this session's work themselves, out-of-band, repeatedly**
  — confirmed again this session (`dc67ee6`, `c536392`, `2e7b95f` in
  rgmc-worker-pool; `615b128` in rgmc-bc-api; `ae16b41` in rgmc-it-bot all
  appeared without this session running `git commit`). **Always run `git log`/
  `git status` fresh before assuming something needs committing** — this
  session nearly reported a much larger uncommitted backlog than actually
  existed by end of session.
- **`rgmc-bc-api`'s branch**: earlier reminders in this conversation showed it
  on `staging`; verify current branch before any deploy — don't assume `master`.
- **`rgmc-bc-api` has unrelated, uncommitted, in-progress work**
  (`src/main.py`, `src/routers/bc_routes/session_history_routes.py`) from the
  user's own parallel "session-history verification" feature, confirmed still
  present/untouched. Don't commit, revert, or even `git add -A` near it without
  being asked.
- **This remains the single most incident-prone pipeline in the ecosystem** —
  the bridge fixes this session (`bigquery_bridge.py`) are the most
  structurally significant changes of the session (new bulk pre-check queries
  against MSSQL, a rewritten insert-retry flow, three new Firestore writes per
  run) and have **never been exercised against production** — watch Cloud
  Logging closely after the first post-deploy bridge run.
- **Known, accepted tradeoff**: as long as BigQuery's upstream
  `int_document_ai`/`int_document_ai_detail` tables keep re-serving
  already-ingested rows past the bridge's own watermark (root cause never
  identified — it's outside these 4 repos, likely a dbt/export-pipeline issue
  re-stamping `created_at` on every materialization), every future bridge run
  will keep "resolving" (safely, idempotently, via `_create_order`'s existing
  `externalDocumentNo` lookup) the same already-synced POs and sending a
  "success" notification for them each time. This is now accurate instead of
  falsely alarming, but still repetitive. Not fixed this session; flagged as a
  known limitation.
- **Email vs. chat decision, explicit**: PO-upload notifications run on BOTH
  channels now — email was NOT removed. If a future session is asked to
  "clean up duplicate notifications," confirm this was intentional (confirmed
  via `AskUserQuestion` this session) before removing either.
- **`order_task`'s new success-email line detail uses defensive/best-effort
  field lookups** (`line.get("number") or line.get("lineObjectNumber") or
  line.get("itemNo")`, etc.) because the real BC v1/v2 Sales Order Line API
  payload shape for this specific generic task-queue path was never directly
  inspected — if the email shows "—" placeholders instead of real values after
  deploy, check the actual payload `order_worker.py` receives and adjust
  `_line_summary`'s field names accordingly.
- **Firestore collection naming convention, now three deep**: `so_buffer_{env}`,
  `so_buffer_history_{env}`, and now `bridge_handoff_history_{env}` — all keyed
  off `GCP_ENV` (e.g. `_production`), lowercased. Keep using this convention for
  anything new.
- **`gcloud` CLI quirks hit this session**: `gcloud run worker-pools` needs
  `gcloud beta`; some `gcloud ... describe`-style commands asking for full
  env-var/secret blocks get refused by the sandbox classifier
  ("Credential Materialization") even when read-only and for non-secret
  purposes — don't retry those, just ask the user to run them directly if truly
  needed. `curl -X POST` with no body needs an explicit `-H "Content-Length: 0"`
  against these Cloud Run services or you get a `411`.
