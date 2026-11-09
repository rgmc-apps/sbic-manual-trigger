# Handoff

## Goal

Harden and improve observability of the RGMC/SBIC SO-import pipeline (5 repos:
`sbic-manual-trigger-page` the Flask monitoring dashboard, `rgmc-gcp-api` the FastAPI
BigQuery/MSSQL/BC bridge API, `rgmc-worker-pool` the Cloud Run Worker Pool doing the
real BigQuery→MSSQL→BC work, `rgmc-bc-api` the BC/Firestore buffer API, `rgmc-it-bot`
the Teams notification bot). This session's concrete asks, in order:

1. Investigate why "handoff to MSSQL and BC takes some time to be triggered after
   uploading" and whether the Cloud Tasks debounce is the cause.
2. Implement the recommended fix from that investigation.
3. Add a file-scoped "Retrigger BC only" button + presence check on the Search tab, a
   new IT-bot Teams notification for BC-only retriggers, and distinguishable buffer
   reasons ("no item reference" / "customer has no code" / "branch has no lookup
   code") instead of one generic buffered-order message.
4. Add a pop-up on the Monitoring page showing live status of all automated BigQuery-
   bridge triggers (per group_code: running/idle/stale).
5. Diagnose and fix a live, recurring failure where retriggering a specific uploaded
   file ("Retrigger Handoff (cleanup & re-insert all)") inserted nothing into MSSQL or
   BC, across three different root causes discovered one after another.
6. Diagnose why one specific PO in that file still ends up buffered even though its
   branch has a matching BC legacy code — **in progress, not yet fixed**, awaiting the
   user's choice of fix approach (see Next Step).

## Current State

**Fully done and deployed/verified:**
- MSSQL `numeric(8,2)` overflow bug on `CustomerPOULDetailBQ` (unitAmount/netAmount/
  netPrice/netPricePcs too small for large bulk-order totals) — root-caused via a live
  `INFORMATION_SCHEMA.COLUMNS` query through the existing `GET /sbic/by_table/value`
  route (table_name accepts ANY string, including system views — see Gotchas). Fix
  script written to `fix_customerpouldetailbq_overflow.sql` (repo root, committed) —
  **UNKNOWN whether the user has actually run this ALTER TABLE script against
  sbic_prod yet.** Until it's run, any PO batch containing a line total over
  999,999.99 will still fail the same way.
- `bridge_run_lock.py`'s stuck-forever "running" lock (confirmed live: customerpoul's
  lock was stuck ~23 hours on 2026-11-05/06, silently dropping every automated
  trigger) — fixed with a 20-minute `STALE_AFTER` staleness fallback. Committed,
  deployed, live.
- 4-part feature (file-scoped BC-only retrigger + presence check, new IT-bot "bc.
  retrigger" Teams notification, buffer-reason categorization) — implemented across
  `app.py`, `dashboard.js`, `rgmc-worker-pool/src/workers/so_import_worker.py`,
  `rgmc-worker-pool/src/services/bot_notify.py`, `rgmc-it-bot`'s types/cards/routes/
  notificationService. Compiles/type-checks clean. **Not yet manually end-to-end
  tested in a live browser** (unlike item below, which was).
- New "Trigger Status" pop-up modal on the Monitoring page header (visible on both
  Search and Handoff History tabs) — backed by a new `rgmc-bc-api` route reading the
  `bridge_run_lock_{env}` Firestore collection. **Live-tested via Claude-in-Chrome
  browser automation against the deployed site** — button renders, modal opens,
  shows all 3 group_codes (customerpoul/customerra/onlinesalespo) with correct
  status/timestamps.
- The 3-layer JSON-serialization bug in `rgmc-worker-pool/src/services/
  retrigger_client.py` (introduced by this session's earlier BigQuery-direct-read
  migration) — fully fixed across `pandas.Timestamp` → `datetime.date` →
  `decimal.Decimal`, plus a generic `json.dumps(default=str)` fallback as a safety
  net for any future type BigQuery hands back. **Verified end-to-end**: retriggered
  file `portal/batch/20261106-081715-9e7b/6021583648998283090/0/20261106-081714-
  e648a3-PO_DETAIL_0 (66)-0.json` — 19/20 POs now insert into MSSQL, promote, AND
  create as real BC Sales Orders (spot-checked several directly against BC's API,
  e.g. `SBSO268450`, `SBSO268439`).

**In progress, NOT fixed yet:**
- The 1 remaining PO from that same file, `PO407026110400667`, still lands in
  `so_buffer_production` instead of reaching BC. Root-caused (not yet fixed) to the
  MSSQL trigger `tr_CustomerPOBQ` (on `CustomerPOULBQ`, promotes into `CustomerPOUL`):
  its branch-matching heuristic only resolves `customerBranchId` when the raw OCR'd
  branch name text contains the numeric store code as a literal substring (e.g.
  `"0229 SM SAN LAZARO"` → extracts `"0229"`). For this PO, Document AI extracted the
  branch name as plain `"SVI SAN LAZARO"` with no embedded numeric code, so
  `customerBranchId` stays at its `-1` sentinel and `customerBranchLookUpCode` ends up
  holding the raw branch-name text instead of a real code — even though `CustomerBranch`
  genuinely has this exact branch registered (`customerBranchId=109`,
  `lookUpCode="0229"`, `customerBranchCode`/legacy code `"SU001-S003-SU"`). The
  worker-pool's own resolution cascade in `so_import_worker.py`'s `_create_order`
  behaved correctly given this bad input — it has no reliable signal to find the
  right ship-to automatically, so it buffers rather than guessing. User was offered two
  fix options and has not yet chosen (see Next Step).

## Files Actively Being Edited

All of the below are **already committed and deployed** (confirmed via `git status`
clean across all 5 repo working trees, and via Cloud Build build history / live
`curl` checks against the deployed services). Nothing is mid-edit or uncommitted.

- `C:\claude\sbic-manual-trigger-page\fix_customerpouldetailbq_overflow.sql` — NEW.
  4 `ALTER TABLE` statements widening `CustomerPOULDetailBQ`'s money columns to
  `numeric(14,2)`. Committed; **not confirmed run against the live DB**.
- `C:\claude\sbic-manual-trigger-page\app.py` — several additions this session:
  `_resolve_po_refs_for_file`, `POST /api/handoff-history/retrigger-bc-only-file`
  (file-scoped BC-only retrigger with presence check), `create_by="trigger-bc-only"`
  threaded through the existing single-PO BC-only route, new
  `GET /api/bridge-run-locks` (proxies rgmc-bc-api for the trigger-status modal).
- `C:\claude\sbic-manual-trigger-page\static\dashboard.js` — new
  `.dash-retrigger-bc-file-btn` button + handler in `buildFileRow` (file-scoped BC-only
  retrigger UI); new Trigger Status modal logic (`renderTriggerStatus`,
  `loadTriggerStatus`, `openTriggerStatusModal`, `humanizeSeconds`), reusing the
  existing shared `#dash-details-modal-overlay` with a new `activeModalRefreshTimer`
  (15s auto-refresh, cleared in `closeDetailsModal`).
- `C:\claude\sbic-manual-trigger-page\templates\dashboard.html` — new "Trigger Status"
  button in the header `site-nav`.
- `C:\claude\sbic-manual-trigger-page\static\style.css` — `.nav-link` got
  `background: transparent; cursor: pointer; font-family: inherit;` so it also looks
  right as a `<button>`, not just an `<a>`.
- `C:\claude\rgmc-worker-pool\src\services\bridge_run_lock.py` — added `STALE_AFTER =
  timedelta(minutes=20)`; `try_start` now reclaims a "running" lock older than that
  instead of blocking forever.
- `C:\claude\rgmc-worker-pool\src\services\retrigger_client.py` — the 3-layer JSON fix:
  `_json_safe_scalar`/`_json_safe` (element-wise `pandas.Timestamp`/`datetime.date`/
  `decimal.Decimal` → JSON-safe conversion, applied to both header/detail DataFrames in
  `fetch_headers_and_details`) + `_json_default` (generic `json.dumps(default=...)`
  fallback used in `insert_from_bigquery`'s POST body instead of `requests`' own
  `json=` kwarg).
- `C:\claude\rgmc-worker-pool\src\workers\so_import_worker.py` — several additions:
  dispatcher now fires `bot_notify.notify_bc_retrigger(...)` when `create_by ==
  "trigger-bc-only"` for both `poul-so-sync-from-cloudsql`/`poul-so-backfill-from-
  cloudsql`; `_resolve_valid_lines` now splits `"no_bc_match"` into
  `"no_item_reference"` (blank SKU) vs `"item_not_matched"` (SKU given, unmatched);
  `unmatched_items` entries now carry a `reason` field; the order-level ValueError
  cascade (ship-to/branch resolution failure, ~line 777-801) now prefixes "Customer
  has no code on file..." or "Branch has no lookup code on file..." when those
  specific conditions hold, else keeps the original "No ship-to address..." wording.
- `C:\claude\rgmc-worker-pool\src\services\bot_notify.py` — new
  `notify_bc_retrigger(companies, po_ref_numbers, summary, notify, label)`.
- `C:\claude\rgmc-bc-api\src\services\so_buffer_service.py` — new
  `GROUP_CODE_LABELS`, `_bridge_run_lock_collection()`, `list_bridge_run_locks()`
  (mirrors `bridge_run_lock.py`'s own `STALE_AFTER` by hand to compute `is_stale`).
- `C:\claude\rgmc-bc-api\src\routers\bc_routes\so_buffer_routes.py` — new
  `GET /bc/custom/v2/so-buffer/bridge-run-locks` route.
- `C:\claude\rgmc-it-bot\src\types\index.ts` — new `BcRetriggerPayload` interface;
  `PoUploadOrderResult.unmatched_items` entries now carry optional `reason?: string`.
- `C:\claude\rgmc-it-bot\src\cards\poUploadCard.ts` — new `buildBcRetriggerCard`;
  unmatched-items rendering now shows "(no item reference)" when `reason ===
  "no_item_reference"`.
- `C:\claude\rgmc-it-bot\src\services\notificationService.ts` — new
  `notifyBcRetrigger`.
- `C:\claude\rgmc-it-bot\src\routes\webhook.ts` — new
  `POST /api/notify/bc-retrigger` route.

## Failed Attempts

- **Assumed `gcloud` works the same in Bash as PowerShell** — **Why it failed**:
  `gcloud`/`python` resolve to broken Windows Store stub aliases under the Bash tool
  in this environment (exit code 49, "Python was not found"). `gcloud` only works
  reliably via the PowerShell tool here. One Bash-based revision-check loop silently
  gave a false "new revision detected" because the error text happened to
  lexicographically sort after the comparison timestamp.
- **`gcloud logging read` with `--freshness` + `--order=asc`** — **Why it failed**:
  known gotcha (already documented from a prior session) — returns stale/wrong-ordered
  entries. Always use explicit `timestamp>=`/`timestamp<=` bounds instead; `--freshness`
  alone with `--order=desc` is fine.
- **Assumed code changes auto-deploy on file save** — **Why it failed**: this
  environment deploys via Cloud Build triggers (e.g. `rgmc-worker-pool-deploy`) that
  fire on `git push` to each repo's GitHub remote, not on local file edits. Retriggered
  the same file twice in a row expecting a fix to be live, got the exact same error
  both times, before realizing the fix was sitting uncommitted locally. Build+deploy
  takes ~3 minutes after a push (confirmed via Cloud Build history timestamps).
- **First JSON-serialization fix (dtype-based `is_datetime64_any_dtype` check in
  `_json_safe`)** — **Why it failed**: only caught `TIMESTAMP`/`DATETIME` BigQuery
  columns (proper `datetime64` dtype). BigQuery `DATE` columns (`po_date`,
  `delivery_date`, `cancellation_date`) come back as **object-dtype columns holding
  plain `datetime.date` scalars**, which a dtype-based check misses entirely — next
  retrigger attempt failed with `Object of type date is not JSON serializable`.
- **Second fix (broadened `_json_safe` to element-wise date+Timestamp handling)** —
  **Why it failed (partially)**: still didn't cover `decimal.Decimal` (from BigQuery
  `NUMERIC`/`BIGNUMERIC` columns, e.g. `unitAmount`/`netAmount`) — next retrigger
  failed with `Object of type Decimal is not JSON serializable`. Fixed by adding
  Decimal handling to `_json_safe_scalar` AND a generic `_json_default` fallback in
  `json.dumps` (belt-and-suspenders, so a 4th unknown type doesn't repeat this cycle).

## Next Step

Resolve the `PO407026110400667` / "SVI SAN LAZARO" branch-resolution gap. The user
was given two options and has not yet responded:
1. **Quick, PO-specific**: add a branch-scoped override on `/reconcile`'s Overrides tab
   linking raw branch text `"SVI SAN LAZARO"` → customer `SU001` / ship-to
   `SU001-S003-SU`. No code/SQL change; unblocks this PO and any future PO whose
   extracted branch text matches exactly.
2. **Systemic SQL fix**: add an exact-name fallback match to the `tr_CustomerPOBQ`
   trigger on `CustomerPOULBQ` (MSSQL, `sbic_prod`) — when the lookup-code-substring
   match fails, also try `CustomerBranch.name = #tmpPO.customerBranchLookUpCode` (or
   match against the raw branch name column before it gets mangled into
   `customerBranchLookUpCode`). This is a shared production trigger — get explicit
   user sign-off before touching it, and consider also checking whether OTHER
   currently-buffered POs share this same "non-numeric branch name, no embedded code"
   pattern (would make the systemic fix clearly worth it vs. one-off overrides).

Also confirm with the user whether `fix_customerpouldetailbq_overflow.sql` has
actually been run against `sbic_prod` yet — if not, that's still an active landmine
for any batch with a PO line total over 999,999.99.

## Context & Gotchas

- **Deployment model**: every repo here auto-deploys via a Cloud Build trigger on
  push to its GitHub remote (`gcloud builds list` shows trigger names like
  `rgmc-worker-pool-deploy`, `rmgpgab-rgmc-bc-api-prod-...`, etc. — some are
  "cloudbuild.yaml"-driven, some are GitHub-connected auto-triggers). The **user**
  (git identity `erar404 <it.arellanoerwin@gmail.com>`) has been the one actually
  committing and pushing my edits each time, following the pattern established
  earlier this session — I should keep asking before committing unless told
  otherwise, but the user has consistently done it themselves within the same turn
  when asked.
- **rgmc-worker-pool's GitHub remote moved**: `git remote -v` still shows
  `github.com/rgmc-apps/rgmc-worker-pool.git`, but a push now prints "This repository
  moved. Please use the new location: github.com/erar404/rgmc-worker-pool.git" — this
  is expected/harmless (an org→personal-account transfer), not an error to chase.
- **Useful live-diagnostic trick discovered this session**: rgmc-gcp-api's
  `GET /sbic/by_table/value?table_name=...&where_column=...&where_value=...` route
  (mssql_routes.py) builds its query as a raw string-formatted `SELECT * FROM
  {table_name} WHERE {where_column} = :where_value` — `table_name` accepts literally
  anything, including system catalogs: `INFORMATION_SCHEMA.COLUMNS` (column
  precision/scale — this is how the overflow bug's exact columns were found),
  `sys.triggers` (list triggers), `sys.sql_modules` (get a trigger/proc's full
  definition by `object_id`). Extremely useful for live MSSQL diagnosis without a
  direct DB connection tool — also worth flagging to the user as a latent
  SQL-injection-shaped surface on `table_name`/`where_column` (not fixed this
  session, pre-existing).
- **Browser automation (Claude-in-Chrome)** was used to directly verify the Trigger
  Status modal live against the deployed site (navigate → screenshot → click →
  screenshot) — confirmed a feature truly renders/works, not just that the HTML
  text is present. Good pattern to reuse for future UI verification in this project.
- **The `_json_safe`/`_json_default` fix only applies to `retrigger_client.py`'s own
  direct-BigQuery-read path** (used by the `poul-so-retrigger-file` message handler).
  The OLDER HTTP-proxy path through rgmc-gcp-api's FastAPI layer (used by
  `cleanup_staging`/`insert_from_bigquery`/`check_promoted` — still HTTP calls per
  that module's own docstring — and by sbic-manual-trigger-page's own single-PO/batch
  retrigger routes in `app.py`) was never affected by this bug class, since FastAPI's
  `jsonable_encoder` already serializes datetime/Decimal natively. No parallel fix
  needed there.
- **Known, still-unfixed issues from earlier sessions** (not touched this session,
  just carried forward): the dbt-view performance issue (`int_document_ai`/
  `int_document_ai_detail` are non-materialized views re-scanning full history on
  every read — owned by a separate dbt project, out of scope for any of these 5
  repos); the dead-letter Pub/Sub topic (`rgmc-poul-so-import-dead`) has no
  subscription, so dead-lettered messages are silently discarded with zero
  visibility; MTC (Manila Taste Curators) still has no BC company configured by
  design (the "30 POs" SBIC-attribution merge-priority logic from earlier this
  session exists specifically to route around this).
- **`bridge_run_lock_{env}` / `reprocess_runs_{env}` Firestore collections**: `{env}`
  resolves to `"staging"` or `"production"` depending on `config.GCP_ENV` — confirmed
  via `rgmc-bc-api`'s own `_env_slug()` helper (`(config.GCP_ENV or
  "staging").lower().replace(" ", "_")`) and `bridge_run_lock.py`'s simpler
  `config.GCP_ENV.lower()` — both agree in practice since there are no spaces in
  either value.
