# Handoff

## Goal

Continue hardening the RGMC/SBIC SO-import ecosystem across five repos —
`rgmc-gcp-api` (`C:\RGMC\Source\git\rgmc-gcp-api` — BigQuery/MSSQL bridge + BC trigger
endpoints), `rgmc-worker-pool` (`C:\claude\rgmc-worker-pool` — Cloud Run Worker Pool,
Pub/Sub-driven BC writes, the BigQuery bridge itself), `rgmc-bc-api`
(`C:\claude\rgmc-bc-api` — BC/Firestore buffer API), `rgmc-it-bot`
(`C:\claude\rgmc-it-bot` — MS Teams bot, untouched this session), and
`sbic-manual-trigger-page` (`C:\claude\sbic-manual-trigger-page`, this repo — the `/`,
`/trigger`, `/dashboard`, and `/reconcile` Flask UI).

This session picked up from a prior handoff (which built the Retrigger/Handoff
History feature set) and, after everything was confirmed deployed, moved into a live
production investigation: **why do some POs in a given upload batch never make it
from MSSQL's staging tables (`CustomerPOULBQ`/`CustomerPOULDetailBQ`) into the real
`CustomerPOUL`/`CustomerPOULDetail` tables via the AFTER INSERT trigger**, using a
specific Manila Taste Curators (MTC) file as the concrete case:
`portal/batch/manila_taste/20261028-112455-9583/7198588257917888432/0/20261028-112455-74bd09-PO_DETAIL_0 (28) (3)-0.json`
(50 POs). This investigation found and fixed a real bug in this session's own batch
retrigger code, and surfaced a structural root-cause issue in the MSSQL trigger
itself that is **out of scope for application-code fixes** — needs a DBA.

## Current State

**All application code this session is committed and deployed, confirmed live**
(`sbic-manual-trigger-page` HEAD `a2eecb1`, Cloud Run revision
`sbic-manual-trigger-00099-59l`). The MTC file batch is now **52 of 54 POs
successfully promoted** into `CustomerPOUL` (up from 21 before this session's work) —
2 PO refs remain stubbornly stuck with no identified root cause yet (see below). The
underlying MSSQL trigger design flaw that caused the original mass-failure is fully
diagnosed but **not fixed** — fixing it requires DBA access to `tr_CustomerPOBQ`
(the trigger itself), which nobody in this session's toolchain has write access to.

### What's fixed and confirmed working
- **Rate-limit collision bug in the batch retrigger** (`app.py`,
  `_insert_mssql_and_log`): `/customerpoul/insert-from-bigquery` in rgmc-gcp-api is
  rate-limited 30s per `path+groupcode`, defaulting every call to the same
  `"customerpoul"` bucket. The batch retrigger calls this once per PO, back-to-back —
  without a distinct `groupcode` per PO, every call after the first in any 30s window
  got a 429 **after** `cleanup-staging` had already deleted that PO's staging rows,
  net effect: data loss with no insert to replace it. **Fixed** by passing
  `params={"groupcode": f"retrigger-{po_ref}"}` on that call, giving each PO its own
  rate-limit bucket (the actual protection the limiter exists for — stopping repeated
  hammering of the *same* PO — is untouched). Confirmed live: re-ran the batch after
  deploying this fix and got 54/54 successful inserts, 52/54 promoted.
- **Read/investigated `tr_CustomerPOBQ`'s actual SQL** directly from MSSQL via
  `sys.tables` → `sys.triggers` → `sys.sql_modules` (read-only, through the existing
  generic `GET /sbic/by_table/value` endpoint, which interpolates `table_name`/
  `where_column` unescaped — usable for diagnostic system-catalog queries by setting
  `table_name=sys.triggers` etc.; see Context & Gotchas).

### Root cause of the original "most of the batch not promoted" symptom (diagnosed, not fixed)
`tr_CustomerPOBQ` (AFTER INSERT trigger on `CustomerPOULBQ`) has two compounding
design issues:
1. It does **not** scope to the newly-inserted rows at all — its very first
   statement is `SELECT ... INTO #tmpPO FROM CustomerPOULBQ Header INNER JOIN
   CustomerPOULDetailBQ Detail ON ... WHERE poStatus = 'NEW'` — it reprocesses
   **every row in the whole table still marked `'NEW'`**, every single time it
   fires, regardless of company or file.
2. The final section (`BEGIN TRANSACTION` / two `INSERT`s into `CustomerPOUL`/
   `CustomerPOULDetail` / `UPDATE CustomerPOULBQ SET poStatus = 'DONE'` /
   `COMMIT TRANSACTION`) runs under `SET XACT_ABORT ON` with **no TRY/CATCH
   anywhere in the trigger**. If any single row in that combined `#tmpPO` set
   throws a DB-level error during the INSERT, the **entire transaction rolls
   back** — nothing gets promoted, and `poStatus` never gets flipped to `'DONE'`
   for anyone in that run, including perfectly good rows.

Since a failed row's `poStatus` stays `'NEW'` forever, every future insert
re-triggers reprocessing of the same accumulated backlog, which only grows. **Live-
confirmed 127 rows stuck at `poStatus = 'NEW'` across the whole table** (not just
MTC — old `SUNCOAST BRANDS` rows too), explaining why every multi-row automated/
manual bridge run for this MTC file promoted exactly 0 of ~30 rows, every time,
while single-row inserts (via the Retrigger button, which does `cleanup-staging`
then one isolated `insert-from-bigquery` per PO) worked — a single-row batch is far
less likely to collide with whatever's poisoning the shared backlog, and once the
rate-limit bug above was fixed, doing the whole file one-row-at-a-time via the batch
retrigger got 52/54 through.

### Unresolved: 2 PO refs that still won't promote
`PO407026102111893` and `PO407026102304020` — retried individually, repeatedly,
after the rate-limit fix was live. Every attempt shows the identical, reproducible
pattern:
- `insert-from-bigquery` reports HTTP 200 / `status: "success"`.
- **Detail** rows (5 and 3 respectively) land in `CustomerPOULDetailBQ` every time —
  confirmed by direct read immediately after.
- The **header** row **never persists** in `CustomerPOULBQ` afterward at all — not
  an error, just silently absent on the next read. `cleanup-staging`'s own response
  confirms this: `header_rows_deleted: 0` every time (there was never one there to
  delete before the "successful" insert that's about to not-persist again).
- `trigger_status: not_promoted` (expected — there's no header for the trigger to
  act on).

**This was NOT a cross-file duplicate-upload bug** (an earlier, disproven
hypothesis — see Failed Attempts) — it's specific to these two PO refs' header
insert itself failing to commit, for an unknown reason, every single time, via the
exact same code path that worked for the other 52. No tool available in this
session's toolchain can show the actual SQL Server error for a write statement
(only read-only equality lookups) — a DBA with direct query/profiler access to
MSSQL is needed to find out why.

## Files Actively Being Edited

- **`sbic-manual-trigger-page/app.py`** — committed (`a2eecb1`, `f5ddd34`), deployed
  (revision `-00099-59l`). Net change from this session's investigation:
  - `_insert_mssql_and_log`: added `params={"groupcode": f"retrigger-{po_ref}"}` on
    the `/customerpoul/insert-from-bigquery` call (the rate-limit collision fix —
    permanent, keep this).
  - `api_handoff_history_retrigger_mssql_batch`'s docstring updated to document
    *why* headers are re-fetched by `po_ref_number` (not read directly from the
    file-scoped lookup) — this was almost "fixed" to do the opposite during this
    session (see Failed Attempts) and reverted; the docstring now explains why that
    direction is wrong, to stop a future session from re-attempting it.
  - No other code changes this session — everything else (routes, UI) is exactly
    what was already deployed per the prior handoff.

No other repo's files were modified this session — this was investigation +
fixing a self-inflicted bug in already-shipped code, not new feature work.

## Failed Attempts

- **Diagnosed a `SUBSTRING`/`PATINDEX` "Invalid length parameter" hypothesis** for
  the trigger's numeric-branch-code extraction (lines ~155-163 of
  `tr_CustomerPOBQ`: `SUBSTRING(Header.customerBranchName, PATINDEX('%[0-9]%', ...),
  PATINDEX('%[0-9][^0-9]%', ... + 't') - PATINDEX('%[0-9]%', ...) + 1)` for a
  digit-prefixed branch name). Wrote a Python simulation of this exact logic against
  all 127 live stuck `poStatus='NEW'` rows' `customerBranchName` values — **zero**
  would produce a non-positive SUBSTRING length. Hypothesis not confirmed by current
  data (doesn't rule it out for a row that's since been fixed/removed, but not
  evidenced) — **don't re-pursue this specific computation as the culprit without
  new evidence.**
- **Hypothesized the 2 final stuck POs were a cross-file duplicate-upload problem**
  — found via `document-ai/search?po_ref_number=X` that these two POs' BigQuery row
  showed a *different* `file_name`
  (`.../20261028-122502-b5da/.../20261028-122502-a59ff8-PO_DETAIL_0 (28) (3)-0.json`)
  than the file originally asked about
  (`.../20261028-112455-9583/.../20261028-112455-74bd09-PO_DETAIL_0 (28) (3)-0.json`).
  Started fixing the batch retrigger to read headers directly from the file-scoped
  lookup instead of re-searching by `po_ref_number`. **Then disproved it**: re-queried
  the ORIGINAL file_name directly — it now returns **0 rows** (all 50 POs' data had
  moved to the new file_name). This is the *already-known, already-documented*
  upstream dbt/export-pipeline quirk that re-stamps metadata (now confirmed: not just
  `created_at`, but `file_name` itself) on every BigQuery materialization — **not** a
  genuine duplicate upload. **Reverted the fix** — scoping batch retrigger headers to
  a specific file_name would make it *more* fragile against this known, accepted
  quirk (a retrigger started right as a rematerialization happens would 404 instead
  of still finding the data via po_ref search). The code now explicitly documents
  this reasoning so a future session doesn't re-attempt the same "fix."
- **Assumed a `curl` empty-response / silent timeout on retrigger-mssql** meant
  something new was broken — it was rgmc-gcp-api being transiently slow under the
  repeated load of this session's own testing (one call read-timed out at 60s,
  immediate retry succeeded). Not a code bug — just retry once before assuming
  regression.
- **(Prior turn) Ran the batch retrigger once before the rate-limit `groupcode` fix
  was deployed** — 1 of 54 succeeded, the other 53 had their staging rows deleted by
  `cleanup-staging` and then failed to re-insert (429), leaving them in a *worse*
  state (nothing in staging at all) than before. Self-healing (a later retry just
  re-cleans-up as a no-op and retries the insert) but worth knowing this failure mode
  existed — **always confirm the rate-limit fix is live before re-running a large
  batch retrigger.**

## Next Step

1. **Get a DBA (or whoever owns the `sbic_prod` MSSQL database) to look at
   `tr_CustomerPOBQ`** (on `CustomerPOULBQ`) with the findings above: it reprocesses
   the entire `poStatus='NEW'` backlog on every firing, under `XACT_ABORT ON` with no
   per-row error isolation, so one bad row anywhere in the system can zero out an
   entire batch's promotions, and failures accumulate forever (127 currently stuck).
   The real fix needs to either (a) scope the trigger to only the `inserted`
   pseudo-table rows instead of the whole `'NEW'` backlog, and/or (b) wrap the
   per-row promotion logic in a way that isolates one row's failure from the rest
   (e.g. a cursor with TRY/CATCH per row, or pre-filtering out rows that are known to
   be bad before the INSERT). This is **not fixable from any of these 4 repos** —
   it's a database object, not application code.
2. **Investigate the 2 remaining stuck POs** (`PO407026102111893`,
   `PO407026102304020`) with direct SQL Server access (SSMS, or whatever the DBA
   uses) — try the exact same single-row `INSERT INTO CustomerPOULBQ` manually and
   see what error (if any) SQL Server actually throws. This needs write/execute SQL
   access this session's tools don't have (only read-only equality lookups via
   `GET /sbic/by_table/value`).
3. **Consider cleaning up the 127-row `poStatus='NEW'` backlog** once the trigger
   itself is fixed — these rows will keep getting reprocessed (and keep being a risk
   to every future batch) until either promoted or manually triaged/marked `'DONE'`/
   deleted. Don't do this before the trigger fix ships, or the backlog will just
   regrow.
4. No code changes are pending in any of the 4 repos — this session's only app-code
   change (the rate-limit `groupcode` fix) is already committed and deployed.

## Context & Gotchas

- **A previously-undocumented diagnostic capability**: `GET /sbic/by_table/value`
  (and the generic `GET /{db_name}/by_table/value` in `rgmc-gcp-api/src/routers/
  mssql_routes/mssql_routes.py`) builds its SQL as
  `"SELECT * FROM {} WHERE {} = :where_value".format(table_name, where_column)` —
  **`table_name` and `where_column` are interpolated completely unescaped, with no
  bracketing**. This means it can be pointed at MSSQL system catalog views for
  read-only diagnostics: `table_name=sys.tables` / `sys.triggers` / `sys.sql_modules`
  work fine (used this session to pull a trigger's full source — see the pattern:
  `sys.tables WHERE name=X` → get `object_id` → `sys.triggers WHERE parent_id=<id>`
  → get the trigger's own `object_id` → `sys.sql_modules WHERE object_id=<id>` →
  read the `definition` column for the full CREATE TRIGGER text). Extremely useful
  for this kind of investigation, but also a real SQL-injection-shaped surface in
  production code — worth flagging to whoever owns `rgmc-gcp-api` as something to
  harden eventually (out of scope to fix here without breaking its legitimate
  generic-lookup purpose).
- **BigQuery's `file_name` field is NOT stable** — confirmed this session that an
  upstream dbt/export-pipeline rematerialization can re-stamp a PO's `file_name` to
  a brand new value (not just `created_at`, which was the only previously-documented
  instance of this quirk). A `file_name` you queried five minutes ago can return 0
  rows now, with the exact same data sitting under a new, different-looking
  `file_name` path. **Never build a feature that assumes a given `file_name` stays
  valid/stable for the duration of a multi-step operation** — always re-resolve by
  `po_ref_number` for anything that needs the current, live data.
- **Rate limiting in `rgmc-gcp-api`** (`src/routers/sbic_routes/rate_limiter.py`) is
  global-in-memory, keyed on `request.url.path + "::" + (groupcode query param or
  "customerpoul")`, 30s cooldown. It reads `request.query_params` directly — a route
  doesn't need to declare `groupcode` as one of its own parameters for the limiter to
  see and key on it. Any future code that calls a rate-limited endpoint **more than
  once in a loop** (batch operations) must pass a distinct `groupcode` per call, or
  every call after the first in the same 30s window silently 429s — and if that
  rate-limited call comes *after* a destructive step (like this session's
  `cleanup-staging` → `insert-from-bigquery` sequence), the net effect is **data
  loss**, not just a slow retry. Check every batch-loop caller of a
  `Depends(rate_limit)`-protected endpoint for this.
- **MTC (Manila Taste Curators) has no BC company** — confirmed again this session.
  `__trigger_so_import` in `bigquery_bridge.py` only ever considers a PO for BC
  Sales Order creation if `companyName` contains `SUNCOAST` or `SBIC`; MTC orders
  never enter `so_buffer_{env}` at all. **"Reprocess Buffer" does nothing for MTC
  POs and will never log anything for them** — this is by design, not a bug, and
  was a source of confusion this session ("why no logs after reprocess" — answer:
  reprocess only ever applied to SBIC/SUNCOAST in the first place). The MSSQL
  staging→promoted trigger (`tr_CustomerPOBQ`) is a completely separate, BC-
  independent mechanism, and the only one that actually matters for MTC.
- **Project/region for all `gcloud`/MSSQL operations**: project
  `durable-woods-465907-n1`, region `asia-southeast1`. Live service URLs used this
  session: `https://sbic-manual-trigger-935246372408.asia-southeast1.run.app`,
  `https://rgmc-gcp-api-935246372408.asia-southeast1.run.app`,
  `https://rgmc-bc-api-prod-935246372408.asia-southeast1.run.app`.
- **The user deploys promptly after committing, consistently** — re-confirmed again
  this session (committed `a2eecb1`/`f5ddd34` themselves, then deployed to revision
  `-00099-59l`, all without this session running a deploy command). Always verify
  live state directly (curl the actual endpoint / check `gcloud run services
  describe`) rather than trusting `git log` alone to infer what's deployed.
