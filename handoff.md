# Handoff

## Goal

Two threads, both still open:

1. **Harden the RGMC/SBIC SO-import pipeline** (BigQuery → MSSQL → Business Central)
   across `sbic-manual-trigger-page` / `rgmc-worker-pool` / `rgmc-bc-api` /
   `rgmc-gcp-api`, and keep clearing the backlog of POs that got stuck in MSSQL
   without ever reaching BC. This is a continuation of a prior session (see the old
   handoff content folded into git history — commit `5e289b1`).
2. **UI simplification**: replace the "Retrigger (MSSQL)" / "Retrigger BC only" button
   pairs on every screen of `sbic-manual-trigger-page` with a single "Retrigger"
   button, letting the backend decide which path a PO needs (already in MSSQL →
   BC-only check/create; not yet in MSSQL → full BigQuery refetch + re-insert) instead
   of making the user pick. Explicitly requested: "have the worker pool d[e]cide on
   where to insert the handoff (based on the company mapping)".

Constraints carried over from the prior session (still in force):
- Never guess BC company from customer name — always resolve via `_resolve_bc_company`
  / `BC_HANDOFF_COMPANY_MAP`, never inferred from free text alone.
- Queue-then-apply only — never write to BC from a pick/suggest step, only an
  explicit Apply/retrigger action (user confirms before any BC write).
- PRINCE RETAIL is still paused — do not touch.
- MTC (Manila Taste Curators) has no BC company configured (`RECONCILE_COMPANIES =
  ["SBIC"]` in `app.py`) — any MTC retrigger attempt is expected to report
  "company not BC-routable", not an error to chase.

## Current State

**Root cause of the "stuck in MSSQL, never reaches BC" bug — found and fixed,
committed by the user:**

`rgmc-worker-pool/src/workers/so_import_worker.py`'s `_run_backfill_from_cloudsql`
(the function behind `POST /customerpoul/backfill-from-cloudsql`, called both by the
`customerpoul-backfill-held-back` Cloud Scheduler cron and synchronously from
`bc_direct_handoff.run_direct_bc_handoff` right after a fresh MSSQL insert) had one
failure path explicitly documented as "never buffered — the check itself failed": if
`bc_client.v2_find_sales_order_by_external_doc_no` (the pre-create existence check)
raised for any reason — a transient BC blip, a timeout, anything — that PO was logged
to an in-memory `errors` list and then **completely dropped**: no BC order, no
Firestore buffer entry, nothing visible on `/reconcile`. Since `bc_direct_handoff`'s own
dedup guard (`_existing_customer_po_refs`) treats "already in CustomerPOUL" as "already
handled" on every future run, a PO that hit this exact failure mode was stuck forever —
no automated path would ever retry it again.

**Fix applied and VERIFIED COMMITTED** (user committed it themselves, under an
unrelated-looking message — confirmed live):
- `C:\claude\rgmc-worker-pool\src\workers\so_import_worker.py` lines ~1774-1788 — moved
  the `lines = gcp_api_client.fetch_customerpouldetailbq(po_ref)` fetch to BEFORE the
  existence check (was after), and the existence-check `except` block now calls
  `so_buffer.save_failed_order(h, lines, company, f"cloudsql-backfill:{company}",
  str(e))` instead of just appending to `errors` and moving on.
- **Committed** as `1fcc309` ("DI-0246 fix for missing items on garments webapp" — the
  message doesn't match the actual change; this is a known recurring pattern with this
  user, see Context & Gotchas). Confirmed via `git show --stat 1fcc309 -- 
  src/workers/so_import_worker.py` (8 insertions, 2 deletions) and
  `grep -n "Fetched before the existence check" src/workers/so_import_worker.py`
  both match. **This still needs an actual Cloud Run deploy of `rgmc-worker-pool` to
  take effect live** — a git commit alone doesn't redeploy the service. Not confirmed
  deployed this session.
- `rgmc-worker-pool` also has an UNRELATED uncommitted change sitting in the working
  tree: `src/services/price_firestore_service.py` (modified, not staged) — **not
  touched by this session, not related to any of this work**, presumably the user's
  own separate in-progress edit (garments webapp, matching the commit message above).
  Leave it alone unless the user says otherwise.

**Two separate batches of stuck POs found and cleared this session (both via the
EXACT same remediation — `POST /customerpoul/backfill-from-cloudsql` scoped to the
specific `po_ref_numbers`, `create_by=bc_direct_handoff`, `companies=SBIC`):**

1. First batch — the 5 refs the user named directly: `10092880`, `10092882`,
   `10092883`, `10092885`, `10092886` (all WATSONS PERSONALCARE / SUNCOAST BRANDS).
   Run `e29152ca418a454eaba6411d74bc826f` — **all 5 succeeded**: `SBSO26A001292`,
   `SBSO26A001290`, `SBSO26A001293`, `SBSO26A001291`, `SBSO26A001289` respectively (32
   lines total, 0 failed). Verified live against BC.
2. Second batch — 6 refs the user supplied from their own query: `26114282` (real
   MSSQL value is zero-padded: `0026114282` — the user's copy/paste had dropped the
   leading zeros, a BigQuery/Excel artifact, not a data bug), `MDSG-11355`,
   `MDSG-11374`, `PO407026111303099`, `PO407026111300487`, `PO407026111301092`. Run
   `9d2c0486e88446419a536caaaee96753` — **4 of 6 succeeded**:
   - `0026114282` → `SBSO26A001294`
   - `PO407026111300487` → `SBSO26A001295`
   - `PO407026111301092` → `SBSO26A001296`
   - `PO407026111303099` → `SBSO26A001297`
   - `MDSG-11355` and `MDSG-11374` (both PYC FOODS CORPORATION) **failed and landed in
     the SO buffer** (proof the fix works — this is the NEW correct behavior, not a
     regression): `last_error` is "No ship-to address in BC for branch code='OWD-TMOG
     RETAIL BOH\n74 TIMOG AVENUE\nSACRED HEART DILIMAN\nQUEZON CITY MNLA 1103'" (and
     the Alabang equivalent for the other) — `customerBranchName` for these two is a
     full multi-line address block instead of a short branch code, and BC has no
     matching ship-to. **This is a genuine, separate `/reconcile` branch-linking job,
     not a code bug** — needs a human to link that branch name to a BC ship-to address
     on the Overrides tab. Not fixed, not expected to be fixed by this code.

**Broader context still true from before this session (NOT re-verified this
session, may have changed):** the earlier full-week BigQuery scan found ~183 distinct
SBIC PO refs (beyond the ones named above) in the exact same stuck state (in MSSQL via
`bc_direct_handoff`, not in BC, not in buffer) — see `temp/week_scan2.json`. Only the
11 refs named explicitly by the user across the two batches above have actually been
retriggered. **The other ~170-some are still sitting there un-retriggered** — the
worker-pool fix prevents NEW occurrences but does nothing for ones already stuck from
before the fix was deployed (and again, deploy status is unconfirmed).

**The 97-of-104 orphaned SBIC buffer entries (from the Nov 13 "99-PO company
mis-tagging" cleanup) are STILL sitting there, unaddressed this session** — confirmed
their BC sales orders were voided (direct BC lookup on 3 sample order numbers all
returned empty), but nobody has cleared/re-created the matching buffer entries yet.
Still needs a decision: re-create fresh orders for these, or just purge the stale
buffer docs.

**UI consolidation — implemented, uncommitted, smoke-tested live, NOT committed:**

`git status --short` in `sbic-manual-trigger-page` shows exactly these 3 files
modified (uncommitted as of this handoff):
```
 M app.py
 M static/dashboard.js
 M static/handoff-history.js
```

Backend (`app.py`):
- `_retrigger_bc_only_core(po_ref)` (~line 3978) and `_retrigger_mssql_core(po_ref)`
  (~line 4193) — extracted from the old route bodies, same logic, unchanged behavior.
  The old routes (`POST /api/handoff-history/retrigger-bc-only`,
  `POST /api/handoff-history/retrigger-mssql`) are now thin wrappers around these and
  still work exactly as before (kept for backward compat / nothing else was found to
  call them besides the JS that now calls the new consolidated route instead).
- **New** `POST /api/handoff-history/retrigger` (~line 4217) — single entry point:
  calls `_fetch_from_customerpoul(po_ref)`; if a header already exists, delegates to
  `_retrigger_bc_only_core` (path="bc_only"); otherwise delegates to
  `_retrigger_mssql_core` (path="mssql_full", which already fires the BC follow-up on
  success). Response always carries `"path"` so the frontend can word its message
  correctly.
- `_resolve_file_refs_by_mssql_presence(file_name)` and
  `_run_bc_only_for_refs_by_company(present_refs_by_company)` (~line 4076-4133) —
  extracted from the old `retrigger-bc-only-file` route body, same logic.
- **New** `POST /api/handoff-history/retrigger-file` (~line 4190) — file-scoped
  equivalent: splits the file's refs into present (→ synchronous BC-only poll, via
  `_run_bc_only_for_refs_by_company`) vs missing (→ async
  `POST /customerpoul/retrigger-file` scoped to ONLY the missing refs via
  `po_ref_numbers=`, so already-good POs in the same file aren't redundantly
  re-fetched from BigQuery). Returns `{bc_only_result, missing, company_unresolved,
  full_retrigger_run_id}` — the missing-side result is a run_id the frontend polls
  separately.
- Old `POST /api/handoff-history/retrigger-bc-only-file` route kept, now calls the
  same two extracted helpers.
- `pylance` flags a couple of pre-existing-style unused-var warnings (`_lines`,
  `status_code`) — intentional underscore-prefixed/already-existed-before-this-session
  convention, not real issues, left as-is.

Frontend:
- `static/dashboard.js`: per-PO `renderRetriggerAction` (~line 711) now renders ONE
  `.dash-retrigger-btn` ("Retrigger") instead of two, posts to
  `/api/handoff-history/retrigger`, branches display text on `data.path`. File-row
  buttons (`.dash-retrigger-file-btn` + `.dash-retrigger-bc-file-btn`, ~line 195-400)
  merged into one `.dash-retrigger-file-btn` ("Retrigger") posting to
  `/api/handoff-history/retrigger-file`; handles the synchronous `bc_only_result` via
  new helper `renderBcOnlyFileResult(data)` (~line 61, added right after
  `pollRunStatus`), then if `full_retrigger_run_id` is present, polls it with the
  existing `pollRunStatus` helper the same way the old whole-file button did.
- `static/handoff-history.js`: `renderRetriggerAction` (~line 82) now renders ONE
  button posting to `/api/handoff-history/retrigger`; `renderBcOnlyRetriggerAction`
  deleted entirely; `renderRecord` (~line 127) now has a single
  `.handoff-history-retrigger-action` container (the second
  `.handoff-history-retrigger-bc-action` div removed) and ONE condition:
  `needsRetrigger = mssql_status === "error" || trigger_status === "not_promoted" ||
  bc_status !== "created"`.
- **Deliberately NOT touched**: `templates/dashboard.html` / `templates/index.html`'s
  `#handoff-batch-retrigger-btn` ("Retrigger Batch (MSSQL)") — this is a standalone
  manual batch tool (paste refs or a date range) with no "BC only" sibling button to
  consolidate with, so it was left alone as out of scope.

**Verification done this session:**
- `py -m py_compile app.py` — clean.
- `node --check static/dashboard.js` / `static/handoff-history.js` — both clean.
- Live smoke test against the local dev server (`http://127.0.0.1:8099`, started via
  `PORT=8099 nohup py app.py > temp/app_server2.log 2>&1 &`, PID 33168 — **still
  running** as of this handoff):
  - `curl POST /api/handoff-history/retrigger {"po_ref_number":"10092880"}` → correctly
    took `path: "bc_only"` (already in MSSQL), returned `status: "done"`.
  - Live in Chrome (Handoff History tab): clicked the single new "Retrigger" button on
    an MTC record (`PO407026111302621`) → correctly took the bc_only path and rendered
    "Error: Could not resolve a BC company from companyName='MANILA TASTE CURATORS
    VENTURES' companyId=12 — not sent to BC." inline, button re-enabled, no JS
    exceptions.
- **NOT tested live**: the new file-scoped `/api/handoff-history/retrigger-file`
  route's actual button click — deliberately skipped because the only real file
  available to test against had 81 real POs and clicking it would have triggered 81
  real BC-write attempts just to smoke-test the UI. Confidence here rests on: (a) it
  reuses the exact same `_run_bc_only_for_refs_by_company` helper already proven live
  on the per-PO path, and (b) the async-missing-refs branch reuses the pre-existing,
  already-working `/customerpoul/retrigger-file` endpoint and the pre-existing
  `pollRunStatus` polling loop, unchanged.

**A Flask dev-server quirk surfaced during testing, not a bug**: the local
`py app.py` dev server runs single-threaded by default (no `threaded=True` in
`app.run(...)`), so one slow in-flight request (e.g. the Search tab's known-slow
BigQuery `document-ai/search` full-table-scan query, which can take up to
`BQ_PROXY_TIMEOUT`=150s) blocks every other request including unrelated tabs/routes
until it resolves. Caused a stuck "Searching…" on the Handoff History tab for ~160s
during testing — resolved on its own once the slow request finished. Not something to
fix unless the user asks; mentioned here so the next session doesn't mistake it for a
new regression.

## Files Actively Being Edited

- `C:\claude\sbic-manual-trigger-page\app.py` — **uncommitted.** See "UI
  consolidation" above for the exact diff shape. Compiles clean, smoke-tested live for
  the per-PO path.
- `C:\claude\sbic-manual-trigger-page\static\dashboard.js` — **uncommitted.** Per-PO
  and file-row retrigger buttons consolidated. Syntax-checked clean.
- `C:\claude\sbic-manual-trigger-page\static\handoff-history.js` — **uncommitted.**
  Per-record retrigger buttons consolidated. Syntax-checked clean, smoke-tested live.
- `C:\claude\rgmc-worker-pool\src\workers\so_import_worker.py` — **committed** (by the
  user, as `1fcc309`). Confirmed present on disk and in git log. No further action
  needed on this file unless a deploy is still pending (unconfirmed — see Next Step).
- `C:\claude\rgmc-worker-pool\src\services\price_firestore_service.py` — modified,
  uncommitted, **NOT part of this session's work** — belongs to the user's own
  separate in-progress change. Do not touch, do not assume it's related.

## Failed Attempts

- **Querying `rgmc-gcp-api`'s `/bigquery_routes/by_table/value` with
  `table_name=CustomerPOULBQ`** — **why it failed**: that route is BigQuery-only;
  `CustomerPOULBQ` is an MSSQL table and doesn't exist in BigQuery, so the backend's
  BigQuery client 404'd and the route surfaced it as a 500 (`"Not found: Table
  durable-woods-465907-n1:sbic_int.CustomerPOULBQ was not found in location
  asia-southeast1"`). **This exact error is what triggered the "RGMC API — API
  Watchdog" alert the user saw and asked about** — confirmed to be this session's own
  diagnostic mistake, not a real system fault. The correct route for MSSQL tables is
  `/sbic/by_table/value` (used successfully right after, same params, same table).
  No fix needed/applied; explained to the user as a one-off, not a regression. (An
  optional future hardening — validate `table_name` against an allowlist and return a
  clean 400 instead of a raw 500 — was floated but **not implemented**, pending user
  interest.)
- **Trying to pull `rgmc-worker-pool` Cloud Run logs via `gcloud logging read` to
  pin down the EXACT moment/reason the existence-check failure first occurred** —
  **why it failed**: `gcloud logging read 'resource.type="cloud_run_revision" AND
  resource.labels.service_name="rgmc-worker-pool"'` returned zero rows even
  unfiltered by time, while the same query for `rgmc-gcp-api` returned recent rows
  fine — the worker-pool service's logs aren't reachable this way (unknown why;
  possibly a different resource type since it's Pub/Sub-triggered, or a different
  logging sink). Abandoned in favor of reasoning from the code's own documented
  behavior instead (the "never buffered — the check itself failed" comment was
  sufficient to pin the root cause without logs).
- **Testing the SO buffer via `company=MTC`** — not a failure exactly, but a dead
  end worth noting: MTC buffer entries (76 of them) are expected-permanently-parked,
  not bugs — `RECONCILE_COMPANIES = ["SBIC"]` means MTC has no BC company to route to
  at all (removed 2026-10-08, confirmed in `app.py` comments). Don't chase these.
- **Running the full past-week BigQuery scan unscoped (no company filter)** —
  timed out at the backend's own 120s cap first try (`{"detail":"BigQuery read timed
  out after 120s"}`); retried scoped to `company=SBIC` and it succeeded in ~15s. Same
  known-flaky-BigQuery-view pattern documented in the prior session's handoff — retry
  once, narrow the filter, don't assume "timed out" means something is broken.

## Next Step

1. **Confirm whether `rgmc-worker-pool` has actually been deployed** since the
   `1fcc309` commit. A git commit alone does not update the live Cloud Run service —
   check deploy status (e.g. `gcloud run services describe rgmc-worker-pool --region
   asia-southeast1 --format="value(status.latestReadyRevisionName,
   status.traffic)"` or ask the user directly) before assuming the fix is actually
   live and will prevent new POs from getting stuck the same way.
2. **Decide what to do with the other ~170-ish still-stuck SBIC POs** from
   `temp/week_scan2.json` (everything beyond the 11 refs already retriggered this
   session) — the same `POST /customerpoul/backfill-from-cloudsql` call, scoped to
   the remaining refs, `create_by=bc_direct_handoff`, `companies=SBIC`, is the proven
   remediation; just needs the user's go-ahead on scope (all at once vs. in batches)
   given the queue-then-apply / explicit-permission-for-BC-writes convention.
3. **Commit the UI consolidation** (`app.py`, `dashboard.js`, `handoff-history.js`) if
   the user is satisfied with it — not yet asked/confirmed this session. Mention the
   untested file-scoped retrigger button specifically and offer to test it live on a
   small real file (not the 81-PO one) before they rely on it in production, or let
   them test manually first.
4. Separately, still pending from the PRIOR session and never revisited this one:
   the 97 orphaned SBIC buffer entries (voided-but-not-cleared) and the ~55+ SKU
   UOM-confirmation / ~13 SKU-linking backlog on `/reconcile` for the original 27
   partial POs — both untouched this session, still open.

## Context & Gotchas

- **The user commits and deploys without announcing it in-conversation — confirmed
  again this session.** The `rgmc-worker-pool` fix was committed under the message
  "DI-0246 fix for missing items on garments webapp", which doesn't describe the
  actual change at all. **Always `git status`/`git log`/`git show --stat` before
  assuming an edit is still pending** — this is the second session in a row this
  exact pattern has shown up (see prior handoff's own note on this).
- **Local app runs at `http://127.0.0.1:8099`**, started this session via
  `PORT=8099 nohup py app.py > temp/app_server2.log 2>&1 &` (PID 33168, listening,
  confirmed at end of session). No special env vars needed — `BC_API_BASE`/
  `GCP_API_BASE` default straight to the real deployed Cloud Run URLs
  (`rgmc-bc-api-prod-935246372408.asia-southeast1.run.app` /
  `rgmc-gcp-api-935246372408.asia-southeast1.run.app`), both publicly reachable
  without auth headers — confirmed live this session, no need to hunt for local
  credentials to test this app.
- **The dev server is single-threaded** (`debug=False`, no `threaded=True`) — a slow
  BigQuery request from one browser tab/action will block every other request to the
  same local server until it resolves. If something looks "stuck" during testing,
  check `tail temp/app_server2.log` for what's actually in flight before assuming a
  real bug.
- **`po_ref_number` zero-padding**: BigQuery/MSSQL stores some PO refs zero-padded
  (e.g. `0026114282`) that look like plain integers when copied out of a spreadsheet
  or BigQuery console (losing the leading zeros). A ref search returning "not found"
  when the user is certain it exists is worth re-checking with/without padding before
  concluding it's genuinely missing.
- **`gcloud` only works reliably from PowerShell in this environment**, not Bash (Git
  Bash resolves `gcloud`/`bq` to broken Windows Store Python stub aliases) — carried
  over from the prior session's notes, reconfirmed when listing Cloud Scheduler jobs
  this session.
- **Direct MSSQL access via `pytds`/the Bash tool got blocked by the auto-mode
  classifier this session** ("Unauthorized Access") even though the prior session's
  handoff said the user had granted it — don't assume that grant still holds; use the
  `rgmc-gcp-api` HTTP endpoints (`/customerpoul`, `/sbic/by_table/value`, etc.)
  instead, which worked fine throughout this session with no permission friction.
- **BC-write actions always got an explicit human go-ahead this session** before
  running (the two `backfill-from-cloudsql` triggers) — the auto-mode classifier
  itself blocked the first attempt with "Destructive or Unwanted Actions" until the
  user typed an explicit "go ahead." Keep doing this for any further retrigger batches
  — don't assume a prior "go ahead" covers a new, differently-scoped batch.
- **`curl -X POST -G` together silently drops the request body/Content-Length**,
  causing a `411 Length Required` from the Cloud Run front end — observed directly
  this session. Use `-d ''` (explicit empty body) with a fully-built query-string URL
  for a POST-with-query-params-only call instead of combining `-X POST` with `-G`.
