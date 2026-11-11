# Handoff

## Goal

Harden and speed up the RGMC/SBIC SO-import pipeline (BigQuery → MSSQL → Business
Central) across the `sbic-manual-trigger-page` / `rgmc-gcp-api` / `rgmc-worker-pool`
/ `rgmc-bc-api` / `rgmc-it-bot` ecosystem, and separately resolve a backlog of
PRINCE RETAIL (BC customer `PR006`) POs stuck out of BC. This session had two
threads:

1. **(Carried over, still paused)** PRINCE RETAIL investigation from the prior
   session: 3 POs (`4561798`, `4561836`, `4561912`) blocked by 10 groups of
   duplicate/ambiguous `CustomerBranch.lookUpCode` values (33 rows, all under
   `PRINCE RETAIL MDSG INC` / customerId 553), and 2 POs (`4563702`, `4563680`)
   blocked by a branch that doesn't exist in `CustomerBranch` at all. **Explicitly
   asked the user to confirm the `lookUpCode` fix convention this session — they
   answered "Not sure yet / need to check with someone."** Do not write or propose
   running any UPDATE script for this until they come back with an answer.

2. **(This session's actual work)** A user asked to check a specific Excel batch
   (`C:\Users\erarellano\Downloads\Suncoast AI PO.xlsx`, "Extract 3" sheet — 53 POs,
   all `SUNCOAST BRANDS`/SBIC) against BC and reprocess whichever were missing. That
   led into a deep, log-evidence-backed investigation of **why SBIC handoffs
   sometimes take 6+ minutes (one traced case: 32+ minutes) to reach BC**, and five
   concrete code fixes in `rgmc-worker-pool`, described below.

## Current State

**All 5 `rgmc-worker-pool` code changes are written, compiled, self-checked, AND —
discovered only when writing this handoff — already committed and pushed to
`origin/master` by the user**, outside this conversation's visible actions:

```
664a968 DI-0239 added optimization of the handoff process   (parallelized per-order BC loop)
405e8c0 DI-0239 add changes for the handoff                 (the other 4 fixes, bundled)
```

`git status` in `rgmc-worker-pool` is clean (`nothing to commit, working tree
clean`, `up to date with origin/master`). Per the established deployment model
(Cloud Build auto-deploys on push, ~3 min), **these fixes should already be live in
production** — this has NOT been verified post-deploy (no log check done after the
push landed). That verification is the main thing still open.

The 5 changes, all in `rgmc-worker-pool`:

1. **Retry-with-backoff on `not_promoted`** (`src/workers/so_import_worker.py`,
   `_run_retrigger_file`) — up to 2 extra attempts (5s/15s backoff) when
   `tr_CustomerPOBQ` fails to promote a retriggered PO, since the trigger scans
   every `poStatus='NEW'` row table-wide (not scoped to the new insert) and the
   automated bridge is NOT locked against a concurrent manual retrigger for the same
   po_ref (`bridge_run_lock.py`'s own docstring: "A manual trigger always bypasses
   this; only the automated path is gated").
2. **Elapsed-time rundown** — `bigquery_bridge.py` captures `bq_fetch_start` (before
   the BigQuery fetch) and `mssql_handoff_end` (right after trigger-promotion
   verification), attaches both (ISO strings) to the `poul-so-import-batch` Pub/Sub
   payload in `__trigger_so_import`. `so_import_worker.py` extracts them, threads
   through `_run_batch` → `_send_batch_notification`, and a new
   `_elapsed_time_rundown()` helper appends a "BigQuery Fetch Started / MSSQL
   Handoff Elapsed / BC Handoff Elapsed" block to the bottom of the existing "POUL
   SO Import" email (the only email sent after BC creation actually finishes).
   Aggregate-only (not per-PO), per explicit user choice via AskUserQuestion.
3. **Orphaned-detail-row cleanup** (`bigquery_bridge.py`, new
   `__header_key_columns`/`__orphaned_detail_keys`/`__cleanup_orphaned_detail_rows`,
   called from `__insert_dataframes` before the existing `__drop_already_inserted`
   pre-check) — detects `CustomerPOULDetailBQ` rows with no matching
   `CustomerPOULBQ` header and deletes them so this run's genuinely-new insert for
   that po_ref isn't silently excluded forever. **Had a real bug the user caught**:
   first version keyed on `poRefNumber` alone; fixed to key on the full composite
   `(poRefNumber, customerName)` resolved via `mappings_bridge.required_columns`/
   `table_mappings` (same two columns `tr_CustomerPOBQ`'s own join uses) — a bare
   `poRefNumber` match could have deleted a different customer's valid, already-
   matched detail rows sharing the same po_ref text (the documented "ghost-tag"
   duplicate-header scenario). Self-checked with a standalone query-builder test
   confirming two customers sharing one po_ref now produce two separately-scoped
   DELETE clauses, never a bare po_ref match.
4. **Self-healing `sbic_db` engine** (`src/services/sbic_db.py`, `_get_engine()`) —
   removed the module-level `_engine` singleton; now returns a fresh
   `DbConn(logger).main()` every call (mirrors `bigquery_bridge.py`'s own
   already-proven-resilient per-run pattern). Root cause this fixes: a Cloud SQL
   Connector background cert-refresh asyncio task crashed
   (`aiohttp.ServerDisconnectedError`, "Task exception was never retrieved",
   confirmed in logs at `07:51:56` and again at `10:54:37`), and the cached engine
   had no way to recover — every `so_import_worker` batch depending on `sbic_db`
   silently stalled for ~1 hour (the ephemeral cert's TTL) until the process
   otherwise restarted.
5. **Parallelized per-order BC creation** (`so_import_worker.py`) — extracted the
   per-order loop body into a new `_process_one_order()` function, now run via
   `ThreadPoolExecutor(max_workers=_ORDER_CONCURRENCY)` with `_ORDER_CONCURRENCY = 5`
   (matches `order_worker.py`'s own existing BC-concurrency bound). Preserves the
   original control flow exactly (including a BC-locked order still landing in
   `successes` before its buffer/status steps are skipped). Self-checked that
   `executor.map` preserves input order for the `successes`/`errors` split despite
   concurrent completion timing.

**Separately, the actual Excel-batch task that kicked this off is fully resolved**:
of 53 POs in Extract 3, 20 were missing from BC; reprocessed via
`/customerpoul/retrigger-file?po_ref_numbers=...`; 19/20 now confirmed in BC
(`SBSO269656`–`SBSO269674`). The 20th, `PO407026110401891`, was the one that led to
fix #1 above — last directly observed in the logs, it **did** eventually promote on
its own at `08:54:39` UTC the same day (`POUL SO created ? PO 'PO407026110401891' ?
BC 'SBSO269821'`), via a manual reprocess-buffer trigger from another user
(Mary Ann Dumarquez), not via my retry fix (which wasn't committed/deployed yet at
that point). Not re-verified since.

**Unrelated, pre-existing, not part of this session's work** (noticed only while
checking git status for this handoff): in `sbic-manual-trigger-page`, the user has
separately added `sql/` to `.gitignore` and moved loose diagnostic/fix SQL scripts
there, including a new `sql/fix_customerpouldetailbq_overflow.sql` (dated
2026-11-06, a `numeric(8,2)` → `numeric(14,2)` overflow fix for
`CustomerPOULDetailBQ`'s amount columns — unrelated to anything in this session).
`prince_retail_po_investigation.md` (repo root) is still untracked, unchanged from
the prior session.

## Files Actively Being Edited

All committed/pushed already (see Current State) — nothing is mid-edit:
- `rgmc-worker-pool/src/workers/so_import_worker.py` — retry-with-backoff
  (`_run_retrigger_file`), elapsed-time-rundown consumption (`_send_batch_notification`,
  `_elapsed_time_rundown`, `_format_elapsed`), `_process_one_order` extraction +
  `ThreadPoolExecutor` parallelization, `_ORDER_CONCURRENCY = 5` constant, new
  `from concurrent.futures import ThreadPoolExecutor` / `from datetime import datetime`
  imports.
- `rgmc-worker-pool/src/services/bigquery_bridge.py` — `bq_fetch_start`/
  `mssql_handoff_end` capture + Pub/Sub payload fields, `__header_key_columns`/
  `__orphaned_detail_keys`/`__cleanup_orphaned_detail_rows` (composite-key orphan
  cleanup), call site wired into `__insert_dataframes` before
  `__drop_already_inserted`.
- `rgmc-worker-pool/src/services/sbic_db.py` — removed `_engine` module singleton;
  `_get_engine()` now always builds fresh.

Left over from the *previous* session, still present, still not acted on:
- `sbic-manual-trigger-page/prince_retail_po_investigation.md` — untracked.
- `sbic-manual-trigger-page/sql/find_ambiguous_branch_lookup_codes.sql` — untracked
  (now inside the user's own new `sql/` gitignored folder).

## Failed Attempts

- **First version of the orphaned-detail cleanup keyed on `poRefNumber` alone** (both
  the `NOT EXISTS` orphan-detection query and the `DELETE`) — **why it was wrong**:
  the user caught it immediately ("make sure the deletion only targets the correct
  columns and not delete excess records"). `CustomerPOULBQ`'s real identity is the
  composite `(poRefNumber, customerName)` — confirmed via
  `mappings_bridge.required_columns['v1']['customerpoulbq']` — and this codebase
  already documents that the same po_ref can have two unrelated header rows under
  different `customerName` values ("ghost-tag" duplicates, see
  `retrigger_client.merge_duplicate_headers`'s own docstring). A po_ref-only DELETE
  could have wiped out a different customer's valid, already-matched detail rows.
  Fixed by resolving the header's full key via `mappings_bridge` (not hardcoded) and
  building one `(poRefNumber = x AND customerName = y)` OR-clause per orphan instead
  of a single `poRefNumber IN (...)` clause.
- **Assumed a single traced example (the 08:27:39→09:00:14 batch) was itself the
  "6+ minute" complaint** — turned out to be worse (32+ min), and tracing it
  further revealed the REAL mechanism was a ~1h53m total consumer silence
  (07:00:33→08:53:57), not just one slow batch — don't anchor on the first data
  point found; the gap was the thing worth explaining, not the single batch.
  (Not a dead end exactly, but worth flagging: the investigation's first hypothesis
  — CPU/thread contention from the single `cpu: '1'` limit on the worker pool
  container — was considered and NOT pursued/confirmed; the actual confirmed cause
  (dead Cloud SQL Connector background task) was found by comparing why
  `bigquery_bridge.py` kept working through the same window while `so_import_worker`
  didn't. The CPU-limit theory is still an open, unverified possibility worth
  revisiting if stalls recur after this fix.)
- **`gcloud logging read` with `resource.labels.service_name="rgmc-worker-pool"`
  returns nothing** — this resource type (Cloud Run *Worker Pool*, not a regular
  Cloud Run *service*) doesn't expose logs under that label the way an HTTP service
  does. Use `textPayload:"..."` substring filters instead (e.g. `"worker.so_import"`,
  `"worker.bigquery_bridge"`) — this is how every successful log query in this
  session worked.
- **Querying `/bigquery_routes/document-ai/search` for a single PO ref still timed
  out at 140s client-side** — the `int_document_ai_detail` view is slow even for
  narrow queries sometimes; fell back to direct `bq query` (BigQuery CLI) against
  `durable-woods-465907-n1.sbic_int.int_document_ai_detail`, which also took ~140-170s
  but at least completed (ran these as Bash background tasks given the predictable
  2-3 min latency).

## Next Step

**Verify the 5 committed/pushed fixes are actually live and working in production.**
Specifically:
1. Confirm the Cloud Build deploy for commits `405e8c0`/`664a968` actually succeeded
   (check Cloud Build history / the `rgmc-worker-pool` Cloud Run Worker Pool's
   `latestReadyRevisionName` — it was `rgmc-worker-pool-00128-w8t` as of this
   session, built from image `...rgmc-worker-pool:405e8c0`; a newer revision should
   exist now reflecting `664a968` too).
2. Watch the next several automated SBIC bridge runs' "POUL SO Import" emails for
   the new "Elapsed Time Rundown" section at the bottom, and confirm MSSQL/BC
   elapsed times look reasonable (BC handoff should now be seconds to low minutes,
   not 30+ minutes).
3. Specifically check whether the `07:51:56`-style Cloud SQL Connector crash
   recurs post-deploy — if `sbic_db`'s fresh-engine-per-call fix is working, a
   recurrence of that traceback should no longer cause an extended stall (next
   batch's fresh engine should just work).
4. Separately, circle back to the user on the **still-unanswered PRINCE RETAIL
   `lookUpCode` fix convention** (they said they'd check with someone) — don't
   write the UPDATE script until they confirm.

## Context & Gotchas

- **The user commits/pushes without always announcing it in-conversation** — this
  session's biggest process surprise: all 5 `rgmc-worker-pool` changes were found
  already committed+pushed only when checking `git status` to write this handoff,
  with no explicit "yes commit" message visible in this transcript. Always
  `git status`/`git log` both repos before assuming uncommitted work is still
  uncommitted.
- **Deployment model**: every repo auto-deploys via Cloud Build on push to GitHub
  (~3 min build+deploy), per established pattern from prior sessions.
- **`gcloud` only works reliably via the PowerShell tool**, not Bash (Bash's
  `python`/`gcloud` resolve to broken Windows Store stub aliases). `py` (real
  Python) and `bq` (BigQuery CLI) both work fine in Bash.
- **BigQuery's `int_document_ai_detail` view is confirmed slow** — directly measured
  at `168.08s` via `pandas_gbq.gbq_connector`'s own "Total time taken" log line. Not
  a worker-pool code issue; flagged as out of scope for these fixes, worth escalating
  to whoever owns the dbt view separately.
- **Cloud Run Worker Pool, not Service** — `rgmc-worker-pool` runs 4 independent
  Pub/Sub subscriber loops (order, sync, so_import, bigquery_bridge) in one process
  (`src/main.py`), each via its own `subscriber.subscribe(...)` call (own default
  executor/thread pool), sharing a single `cpu: '1'` container resource limit. Not
  concluded whether this per-container CPU limit contributes to stalls — flagged as
  an open question, not fixed this session.
- **`mappings_bridge.py`** (`rgmc-worker-pool/src/mappings_bridge.py`) is the
  canonical source for MSSQL table composite keys (`required_columns`) and
  snake_case→camelCase column name translation (`table_mappings`) — always resolve
  key columns through this, never hardcode (`poRefNumber`/`customerName` etc.),
  since that's exactly the bug the user caught this session.
- **The Fact-Forcing Gate hook** fires on the *first* Edit/Write to each file in a
  session (and apparently again if the grep search path used to satisfy it doesn't
  literally contain the target file's full path) — when it fires, answer with (1)
  grep-confirmed call sites, (2) data-file format or "not applicable", (3) the
  user's verbatim current instruction, then retry the same edit. For a *new* file,
  it instead asks what it supersedes / where it's linked from / the verbatim
  instruction.
- **PO407026110401891** (SUPERVALUE INC., branch `[0286] SVI MEGAMALL A`) was the
  specific PO that triggered fix #1's investigation — its branch/customer lookup
  were confirmed UNambiguous (unlike the PRINCE RETAIL case), so its repeated
  `not_promoted` failures were from the broader table-wide-trigger-scan +
  bridge-vs-manual-retrigger race, not a master-data issue.
