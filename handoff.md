# Handoff

## Goal

Harden the RGMC/SBIC SO-import pipeline (BigQuery → MSSQL → Business Central) across
the `sbic-manual-trigger-page` / `rgmc-worker-pool` / `rgmc-bc-api` / `rgmc-gcp-api`
ecosystem, and repair a batch of BC Sales Orders that were created with zero line
items. The session started from a narrow question — "why do POs `0026087977`,
`0026087981`, `0026095047`, `0026097312` have no sales order items inserted?" — and
widened into finding and fixing several real bugs, plus a live, ongoing data-repair
effort that is NOT finished.

Constraints established along the way:
- Never guess Business Central company (SBIC vs MTC) from customer name — the same
  customer places POs for both companies depending on the specific brand. Company
  must come from the real source field, never be inferred.
- Queue-then-apply only — never write to BC directly from a "pick/suggest" step, only
  from an explicit Apply action (see memory `feedback_queue_vs_apply_pattern`).
- PRINCE RETAIL (`4561798`, `4561836`, `4561912`, several `4524xxx`/`4525xxx` refs) is
  explicitly paused pending the user confirming a `lookUpCode` fix convention with a
  colleague — do not touch, do not write any UPDATE for it.

## Current State

**Root cause of the original question, fully diagnosed and fixed, and separately
already committed+pushed by the user (not by me explicitly, but confirmed present in
each repo's git log — see Context & Gotchas):**

1. `rgmc-worker-pool/src/services/bc_direct_handoff.py` — the direct BigQuery→BC
   handoff path (built earlier this session to bypass the buggy `tr_CustomerPOBQ`
   trigger) inserted resolved rows into `CustomerPOUL`/`CustomerPOULDetail` but NEVER
   into `CustomerPOULDetailBQ` — the table `so_import_worker.py`'s
   `_run_backfill_from_cloudsql` / `_sync_order_from_cloudsql` actually read line items
   from (via `gcp_api_client.fetch_customerpouldetailbq`). Result: BC sales orders got
   created with a header but **zero lines**, for every single PO this path had ever
   sent to BC (31 total, confirmed live). Fixed: `_insert` now also writes a matching
   `CustomerPOULDetailBQ` row per detail line (safe — `tr_CustomerPOBQ` only fires on
   `CustomerPOULBQ`/header inserts, never on the detail table). Committed as
   `0f3c9d7`/later in that repo's log.
2. `rgmc-bc-api/src/services/so_buffer_service.py` (`apply_missing_item_fixes`,
   ~line 1304) — never set `lineNo` explicitly when adding a line to an existing BC
   sales order, relying on BC's own auto-numbering. For an order that starts with zero
   lines and gets several lines added back-to-back in the same apply loop, BC's
   auto-numbering doesn't settle fast enough and every line after the first fails with
   `Internal_EntityWithSameKeyExists ... Line No.='0'`. Fixed: compute
   `next_line_no = max(existing lineNo) + 10000` explicitly, same convention
   `rgmc-worker-pool`'s `_create_order` already uses. Committed as `2c004f7`
   ("DI-0243 added modifications for bc handoff") and user confirmed this was deployed.
3. `rgmc-gcp-api/src/routers/bigquery_routes/bigquery_routes.py`
   (`get_document_ai_source_file`, ~line 293) — the "View File" action's bucket lookup
   never knew about Manila Taste Curators (MTC) uploads living one folder deeper
   (`portal/manila_taste/{basename}` instead of `portal/{basename}`, same bucket
   `sbic-po-parser-in-v2`) — every MTC "View file" request returned `not_found`. Fixed
   by detecting the `manila_taste` marker already present one level up in the
   batch-output `file_name` path. Verified live against the real GCS bucket. Committed
   as `f7a6583`.
4. `rgmc-worker-pool/src/services/send_mail.py` + `bigquery_bridge.py` — the
   "BigQuery Bridge Complete" developer email dumped its whole log as one unreadable
   `<pre>` blob with no elapsed-time field. Added: dash-bracketed sections
   (`__log_section` blocks like "INSERT SUMMARY"/"DUPLICATES SKIPPED") now render as
   separate labeled cards; added an optional `elapsed_seconds` param threaded through
   `notify_success`/`notify_error`/`notify_warning` → an "Elapsed" row; added a new
   "DIRECT BC HANDOFF" section (`__build_direct_handoff_summary`) so the
   inserted-into-MSSQL vs sent-to-BC counts are no longer a raw Python dict dump.
   Already committed/pushed (confirmed present in the repo).
5. `rgmc-worker-pool/src/services/bot_notify.py` — `notify_bridge_run` renamed its
   endpoint/event tag from `bridge-run`/`bridge.run` to `po-upload-trigger`/`po.trigger`
   to match the existing `po-upload-batch`/`po-upload-unmatched` naming convention.
   Teams command to subscribe: `subscribe po-upload-trigger`. `rgmc-it-bot`'s own
   `/api/notify/po-upload-trigger` handler still needs to be built in that repo (no
   local access) for this to actually render anywhere.

**Rejected approach (do not resurrect):** tried validating BC-handoff company
assignment via `Product.brandId -> WarehouseBrand -> Warehouse.companyId`, thinking
brand reliably determines company. It flagged **1057 existing, correctly-processed
historical orders** as false positives — SBIC legitimately sells some Lipton-brand
SKUs too. Reverted entirely from `bc_direct_handoff.py`/`bigquery_bridge.py`.

**The 99-PO company-mis-tagging incident (separate from the above) is still
unresolved on the BC side.** Earlier in the session, 99 POs were manually
bypass-inserted with the wrong company (tagged SBIC when they were actually MTC
Lipton/Nestle/Gerber products) and ended up as real BC sales orders under the wrong
company. The MSSQL side was corrected (`CustomerPOUL.companyId`/`companyName` fixed
for the 100 affected rows). **The BC side — voiding/cleaning up those 99 wrongly
created sales orders — has NOT been confirmed done.** The reference list moved from
the repo root into the gitignored `reports/` folder at some point this session (not by
me — found already relocated): `reports/mistaken_bc_pos_cleanup.txt` (PO_REF|BC_ORDER_NO|CUSTOMER_NAME|CUSTOMER_NO,
99 rows) and `reports/mistaken_bc_pos_refs_only.txt` (same 99 refs, pipe-joined on one
line). **Check with the user whether this was ever actually cleaned up in BC** — it was
never re-verified in this session after the initial finding.

**The zero-line-orders repair (the 31 POs from bug #1 above) is live and in progress,
NOT complete:**
- **70 of 265 expected line items are now in BC.**
- **4 of 31 POs are fully complete**: `0026087977` (4/4), `0026087981` (4/4),
  `0026095047` (1/1), `0026097312` (1/1) — these are exactly the 4 POs the session
  started with, so the original question is fully answered/resolved.
- **27 of 31 POs remain partial**, blocked purely on human `/reconcile` decisions, not
  on anything further I can fix in code:
  - ~13 distinct SKUs still have no BC item reference at all and need linking
    (confirmed as of the last check; count went from 14→13 after the item-reference
    matching fix below reclassified one).
  - ~55+ distinct SKUs resolve to a real BC item but need their Unit of Measure
    confirmed (raw unit like "Cases" doesn't map to a valid BC UOM for that item, no
    saved override exists yet).
  - The worst-affected POs by line count: `1876078-00` (6/25), `1876068-00` (9/26),
    `1876054-00` (5/22), `1876061-00` (7/22), `1876093-00` (6/22), `506189225-00`
    (5/20), `506189228-00` (4/17).
  - Exact per-PO/per-SKU breakdown files (gitignored, still on disk):
    `temp/queue_retry4.json`, `temp/skip_a2.json`, `temp/skip_b.json`.

**Separately, just applied, NOT yet re-verified at scale:** a fix that makes the
Missing Items comparison (`_find_missing_items` in `app.py`) also check BC's own Item
Reference table (`referenceType="Customer"`), matching what `rgmc-worker-pool`'s
automated reprocess-buffer/retrigger path already does via `_build_ref_map`.
Previously this function only recognized a saved manual override or a raw-SKU-equals-
item-number coincidence, so anything BC could already auto-resolve on its own showed
as a false "needs_linking". Verified on one PO (`506189234-00`): 3 of 5 previously
unresolved items correctly reclassified from `needs_linking` to `needs_uom` with a
real resolved item (e.g. SKU `4492199` → BC item `PF01PF04`). Ran the full repair loop
once after this fix and it picked up 1 more item (69→70 lines). **This file
(`app.py`) is the only uncommitted change in any repo right now.**

## Files Actively Being Edited

- `sbic-manual-trigger-page/app.py` — **uncommitted.** Added `_fetch_item_ref_map(company)`
  (new helper, ~line 828, right after `_fetch_overrides_by_type`) and wired it into
  `_find_missing_items` (~line 1782 for the fetch, ~line 1809-1816 for the per-line
  resolution fallback: `if not resolved_item and raw_sku: resolved_item =
  item_ref_map_by_key.get(raw_sku.upper())`). Compiles clean (`py -m py_compile app.py`
  passed). Verified live against the running local app. Not committed — do
  `git diff app.py` to see the exact 36-line diff before deciding whether to commit.

All other repos (`rgmc-worker-pool`, `rgmc-bc-api`, `rgmc-gcp-api` at
`C:\RGMC\Source\git\rgmc-gcp-api`) show **clean working trees** — every fix described
above is already committed and pushed (by the user, not announced in-conversation).
`sbic-manual-trigger-page`'s own `templates/reconcile.html`/`static/reconcile.css`
(Cloud SQL tab disabled) are also already committed (`79e5d67 disabled sql sync`).

## Failed Attempts

- **Guessing BC company from customer name** during an earlier manual bypass-insert
  rescue (before this handoff's visible window, but its consequences are still being
  cleaned up) — **why it failed**: the same customer (e.g. SUPER SHOPPING MARKET,
  PASIG SUPERMARKET) legitimately orders from both SUNCOAST BRANDS (SBIC, companyId 6)
  and MANILA TASTE CURATORS VENTURES (MTC, companyId 12) depending on the specific
  PO/brand — confirmed against historical `createBy='trigger'` records. Sent 99
  MTC-brand POs to BC as SBIC orders. See "99-PO company-mis-tagging incident" above —
  BC-side cleanup still unconfirmed.
- **`Product.brandId -> WarehouseBrand -> Warehouse.companyId` as a company-validation
  rule** — **why it failed**: looked like a clean 1:1 mapping (no brand spans multiple
  companies) but flagged 1057 existing, correctly-processed historical orders as false
  positives. SBIC legitimately sells some Lipton-brand SKUs too (likely a
  pre-MTC-era product line or commercial overlap that doesn't match physical warehouse
  stocking). Reverted entirely.
- **Treating `CustomerPOULDetail` as a drop-in substitute for `CustomerPOULDetailBQ`**
  when trying to backfill missing BC line data fast — **why it failed/was rejected**:
  `CustomerPOULDetail` is missing `poQtyPcs`, `unitOfMeasurement`, `unitAmount`,
  `netAmount` — fields line-resolution depends on (specifically `_is_pcs(uom_raw)`
  branching in `so_import_worker.py._build_line_payload`). Using it would risk silently
  wrong quantities for any line whose real unit was "pcs"-coded. Went with the slower
  but complete and accurate path (direct BigQuery re-fetch) instead.
- **Querying BigQuery (`int_document_ai`/`int_document_ai_detail`) for a single PO ref
  or a small batch, expecting speed from the narrower scope** — **why it failed**: the
  views re-scan the full historical backlog regardless of filter size (documented,
  known, out-of-scope-to-fix-here issue — the real fix is materializing them as dbt
  tables, owned by a separate team). Confirmed 60s+ timeouts even for single-PO
  header-only lookups. The working pattern is: always run these as backgrounded
  commands with 180-280s timeouts and just wait, not try to make them fast.
- **A specific 17-ref sub-batch to `/api/dashboard/queue-missing-items` consistently
  returned `orders_with_missing_items: 0` on 2 separate attempts** even though those
  POs definitely had missing items (confirmed independently) — **why**: never
  root-caused; looks like an intermittent failure in the underlying BC search or
  BigQuery call inside `_find_missing_items` that silently returns empty rather than
  erroring. Splitting into two smaller sub-batches (8 and 9 refs) worked on the very
  next attempt for both. **Always retry an unexpected "0 orders" result 1-2 times, and
  split into smaller batches if it persists, before concluding something's actually
  resolved.**

## Next Step

Resume the repair loop for the 27 remaining POs. The single most useful immediate
action: re-run `python temp/full_repair_loop.py` (already exists, self-contained,
reads `ALL_31` refs, loops queue→apply until nothing new) to pick up anything a
human has since resolved on `/reconcile`, or anything the item-reference-matching fix
(still uncommitted in `app.py`) newly unlocks once re-verified at full scale (it was
only tested on 1 of 31 POs and one small follow-up run).

Before that, two decisions need the user:
1. **Commit `app.py`?** The item-reference matching fix is tested and working but
   still local-only — everything else this session has already been committed/pushed
   by the user without asking, so check whether they want this one committed too or
   are waiting to review the diff themselves (`git diff app.py`).
2. **Confirm the 99-PO BC cleanup status.** Ask the user directly: "Were the 99 sales
   orders in `reports/mistaken_bc_pos_cleanup.txt` ever voided/corrected in BC?" This
   was never re-verified after the initial fix session and could still be sitting
   there uncorrected.

For the 27 remaining POs' actual data gaps, nothing more can be automated — they need
a human on `/reconcile`'s Overrides tab to either link the ~13 unresolved customer SKU
codes to a BC item, or confirm the Unit of Measure for the ~55+ SKUs that already
resolve to an item but have an unconfirmed unit. Once either is saved, re-running
`full_repair_loop.py` (or the Missing Items tab's own Queue/Apply) picks it up
automatically — no code changes needed for that part.

## Context & Gotchas

- **The user commits and pushes without announcing it in-conversation — this is a
  recurring pattern, not a one-off.** Every repo's `git log` this session already
  contained my fixes under commit messages like "DI-0243 added fix on missing items",
  "DI-0243 BC/MSSQL handoff modifications", "disabled sql sync" — none of which I
  committed myself, and none of which were explicitly flagged by the user as "I just
  pushed this." **Always `git status`/`git log` every touched repo before assuming
  uncommitted work is still uncommitted**, and before assuming a described fix still
  needs deployment.
- **`sbic-manual-trigger-page` runs locally** at `http://127.0.0.1:8099` and proxies
  to two deployed Cloud Run services: `rgmc-gcp-api` (`GCP_API_BASE`, BigQuery +
  MSSQL reads) and `rgmc-bc-api` (`BC_API_BASE`, all actual Business Central writes).
  `app.py`'s own `_bc_api`/`_gcp_api` helpers are the only way to reach BC/BigQuery
  from this environment — there are no local BC OAuth credentials
  (`BC_CLIENT_ID`/`BC_CLIENT_SECRET` are Cloud-Run-only env vars).
- **MSSQL access**: `pytds.connect(server='127.0.0.1', database='sbic_prod',
  user='sqlserver', password='@MyRunway', port=828, ...)` — explicitly granted by the
  user this session for diagnostic/rescue work. Use `py -c "..."` (Bash), not `bq`/
  `gcloud` (those resolve to broken Windows Store Python stub aliases in Bash — use
  PowerShell for `gcloud`/`bq` instead).
- **Local repo locations** (all plain directories, not symlinks, discovered
  piecemeal this session — write them down so the next session doesn't have to
  re-discover):
  - `C:\claude\sbic-manual-trigger-page` (this repo)
  - `C:\claude\rgmc-worker-pool`
  - `C:\claude\rgmc-bc-api`
  - `C:\claude\rgmc-it-bot` (exists locally, not used this session beyond noting its
    `/api/notify/po-upload-trigger` handler still needs building)
  - `C:\RGMC\Source\git\rgmc-gcp-api` — **note the different root** (`C:\RGMC\...`, not
    `C:\claude\...`) — the user told me this mid-session; I had previously assumed no
    local access and was about to do blind live-testing instead.
- **Fact-Forcing Gate**: fires on first Edit/Write to each file per session. Answer
  with (1) grep/search-confirmed callers, (2) data shape or duplicate-purpose check,
  (3) the user's verbatim current instruction, then retry the same edit. For a new
  file, it asks what it supersedes / where it's linked from instead. The search that
  satisfies it must literally include the target file's path in its own search scope —
  a prior broad grep across multiple files does NOT count for a file it didn't
  explicitly include.
- **Queue-then-apply convention, reused for the repair**: `/api/dashboard/
  queue-missing-items` (reads BigQuery `int_document_ai_detail` + current BC lines,
  queues only "ready" items into the `so_missing_item_fixes_{env}` Firestore buffer)
  then `/api/apply-missing-item-fixes` (the one real BC write, batched 50 at a time via
  `MISSING_ITEM_FIX_APPLY_BATCH_CAP` in `rgmc-bc-api`). Always scope `apply`'s
  `order_ids` explicitly — calling it with no scope would also process unrelated
  pre-existing pending fixes from other users (confirmed: 4 stale PRINCE RETAIL fixes
  queued back on 2026-10-20 by "Mary Ann Dumaruqez" sat in the same company-wide queue
  the whole session — never touch those).
- **`temp/` vs `reports/`**: both gitignored. `temp/` is scratch/working files (status
  JSONs, the repair-loop script, BigQuery query results) — use it for anything
  throwaway. `reports/` is where the user relocates finished reference
  lists/deliverables (e.g. the mistaken-BC-POs cleanup list moved there mid-session).
  Check both before assuming a file from earlier in the session is gone.
- **PRINCE RETAIL is still paused**, unrelated to everything in this handoff — 3 POs
  (`4561798`, `4561836`, `4561912`) and several more blocked by duplicate/ambiguous
  `CustomerBranch.lookUpCode` values, user said they'd check the fix convention with a
  colleague and hasn't come back. Don't write or run any UPDATE for it.
