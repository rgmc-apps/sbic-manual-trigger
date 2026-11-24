# Handoff

## Goal

This session had one explicit ask: **refactor the codebase for readability — split the
monolithic backend and frontend into smaller files organized by feature/category** —
plus "do testing for the imports afterwards." No behavior change was requested or
should have happened; this is a pure structural refactor.

Specifically:
1. Split `app.py` (4,494 lines, ~130 functions/routes, all in one file) into logical
   modules by category/function type.
2. (Added mid-session by the user) Also split `static/reconcile.js` (4,454 lines, one
   giant IIFE, ~126 functions) into smaller files organized by feature, in a folder.

Carried-over constraints from prior sessions (still in force, unrelated to this
session's work but still true about this codebase):
- Never guess BC company from customer name — always resolve via `_resolve_bc_company`
  (now in `core/company.py`) / `BC_HANDOFF_COMPANY_MAP`.
- Queue-then-apply only — never write to BC from a pick/suggest step.
- PRINCE RETAIL is still paused — do not touch.
- MTC has no BC company configured.

## Current State

**Both refactors are complete, independently verified, and uncommitted.** `git status
--short` shows: `M app.py`, `M handoff.md` (this file), `D static/reconcile.js`,
`M templates/reconcile.html`, plus untracked `core/`, `routes/`, `static/reconcile/`.
Nothing has been committed — this is all sitting in the working tree for the user to
review/commit themselves.

### 1. Backend: `app.py` → `core/` + `routes/` — DONE, verified

`app.py` is now 36 lines: imports, `app = Flask(__name__)`, 12 blueprint
imports/registrations, and the original `if __name__ == "__main__":` block. All
business logic moved out, zero logic rewritten.

**`core/` (7 shared modules, used by 2+ route modules):**
- `core/config.py` (142 lines) — every module-level constant (`BC_API_BASE`,
  `GCP_API_BASE`, `API_TIMEOUT`, `BQ_PROXY_TIMEOUT`, `BQ_UI_ONLY_HEADER_FIELDS`,
  `RECONCILE_COMPANIES`, company maps, `BQ_COMPANY_NAME_TERMS`, `EMAIL_CONFIG`,
  `PROCESSES`, `TIMEOUT_SECONDS`, `DASHBOARD_UPLOAD_TYPES`, `_SBIC_AI_SUBMITTED_BY`,
  `_SHIPTO_MISSING_FILTER`).
- `core/http_clients.py` (28) — `_bc_api`, `_gcp_api`, `_proxy_json`.
- `core/email_utils.py` (94) — `_html_table`, `send_email`, `_trigger_email_body`,
  `_error_email_body`, `_employee_notify_params`.
- `core/company.py` (87) — `_resolve_bc_company`, `_company_name_matches`,
  `_merge_duplicate_headers`.
- `core/mssql.py` (76) — `_split_po_refs`, `_customerpoul_row_to_search_header`
  (carries this session's EARLIER bug-fix `"create_by": row.get("createBy")` key, see
  Context below), `_customerpouldetail_rows_to_search_lines`, `_fetch_from_customerpoul`.
- `core/overrides_data.py` (40) — `_fetch_overrides_by_type`, `_fetch_item_ref_map`.
- `core/reprocess_polling.py` (59) — `_poll_reprocess_run`, `_trigger_bc_followup`
  (both used by `routes/dashboard.py` AND `routes/handoff_retrigger.py`).

**`routes/` (12 Flask Blueprints, one per feature, line counts):** `pages.py` 20,
`buffer.py` 405, `lookups.py` 297, `overrides.py` 136, `handoff_history.py` 68,
`shipto_fixes.py` 358, `customer_shipto.py` 483, `missing_items.py` 420,
`document_ai_search.py` 735, `reprocess.py` 235, `dashboard.py` 375,
`handoff_retrigger.py` 507 (contains BOTH of last session's bug fixes —
`effective_create_by` logic — confirmed intact with original comments).

Cross-route-module imports discovered during extraction (all one-directional, no
cycles): `shipto_fixes.py` imports `_match_branch_override`/`_match_customer_override`
from `lookups.py`; `customer_shipto.py` imports `_SBIC_AI_SUBMITTED_BY`/
`_find_orders_by_customer` from `shipto_fixes.py`; `dashboard.py` imports
`_find_missing_items` from `missing_items.py` and `_insert_and_buffer` from
`document_ai_search.py`; `handoff_retrigger.py` imports `_insert_mssql_and_log` from
`dashboard.py`. The one `url_for('report_page', ...)` call in the whole codebase was
updated to `url_for('document_ai_search.report_page', ...)` (Blueprint registration
prefixes endpoint names).

**Verification performed (by the executing agent, then independently re-verified by
me):**
- `py -m py_compile` on all 20 files — clean.
- `py -c "import app"` — clean, no circular imports.
- Route count: original 69 `@app.route` decorators; new blueprints register exactly
  69 routes too (confirmed twice — once via the agent's own count, once by me via
  `len(list(app.url_map.iter_rules()))` = 70, which is 69 + Flask's own always-present
  implicit `/static/<path:filename>` rule — not a discrepancy, just a different
  counting method; both agree once you account for that).
- I independently curl'd 9 GET routes spanning 9 of the 12 blueprints against the live
  server — 8 clean 200s, one expected 400 (`/api/dashboard/files` with no
  `upload_type` param — correct validation behavior, not a bug).

### 2. Frontend: `static/reconcile.js` → `static/reconcile/*.js` — DONE, verified

No ES modules existed anywhere in this codebase before this session (everything was
classic `<script src>` tags). Confirmed before starting: zero inline `onclick="..."`
HTML handlers anywhere in `templates/reconcile.html`, zero `window.X =` global
exposure in the original file — fully self-contained, safe to convert to ES modules
without breaking anything external.

**New files in `static/reconcile/` (final line counts):**
`core.js` 99 (`refreshGate`, `loadEmployeeDetails`, `saveEmployeeDetails`,
`employeeDetailsValid`, `getEmployeeDetails`, `escapeHtml`, `setBtnLoading`,
`formatElapsed`, `RUN_POLL_MS`, `MAX_WATCH_MS` — the only truly cross-tab utilities,
confirmed via grep call-site line-spread across the whole original file),
`tabs.js` 65 (page-level tab-switcher, `activateTab`/`activateActionTab`),
`buffer.js` 1587 (the Buffer/Groups reconcile tab — by far the largest single
feature; also exports mutable `state`/`fileScope` as live ES-module bindings that
`tabs.js` and `bigquery-lookup.js` import), `buffer-history.js` 130, `overrides.js`
440, `override-utils.js` 172 (candidate-suggestion-dropdown helpers —
`renderCandidateList`, `normalizeCandidates`, `saveOverride`, `resolvedDisplay`, etc. —
**this file was NOT in the original plan**; the executing agent correctly discovered
these are shared across `customer-shipto.js`, `missing-items.js`, and
`missing-shipto.js`, not Buffer-tab-local as I'd assumed when planning — a better call
than my original module map), `bigquery-lookup.js` 376, `missing-shipto.js` 627,
`missing-items.js` 531, `customer-shipto.js` 493, `main.js` 13 (entry point — just
`import`s every other module by side effect, in original top-to-bottom execution
order). Total 4,533 lines across 11 files vs. 4,454 in the original single file
(growth is import/export boilerplate, not duplicated logic).

`templates/reconcile.html` line ~843 updated from
`<script src="/static/reconcile.js"></script>` to
`<script type="module" src="/static/reconcile/main.js"></script>`.
Old `static/reconcile.js` **deleted** (confirmed via repo-wide grep that nothing else
references it as code — a few comments elsewhere in `app.py`/`dashboard.js` mention
"reconcile.js" by name descriptively, left as-is, harmless).

**Verification performed (this part hit the session's usage-limit reset mid-run — the
executing agent got through writing all 11 files + updating the template + deleting
the old file, but was cut off during its own final verification pass with the message
"Comprehensive verification complete. Cleaning up and finalizing." I resumed and ran
every verification step myself from scratch, trusting nothing from its cut-off
report):**
- `node --check` on all 11 files individually — all pass (node is at
  `/c/Program Files/nodejs/node`, confirmed available).
- Function-inventory reconciliation: extracted all 126 original top-level function
  names via grep, confirmed every single one appears somewhere in the new files
  (`comm -23` diff came back empty — zero dropped).
- Cross-module import/export wiring check: wrote a small Python script
  (`/tmp/check_exports.py`, since deleted) that parses every `import { X } from
  "./Y.js"` statement and every `export function/const/let` + `export { ... }`
  statement across all 11 files, and confirmed every imported name resolves to an
  actual export in its target file. Zero problems found.
- **Live browser smoke test** via Claude-in-Chrome against the running dev server
  (`http://127.0.0.1:8099/reconcile`): loaded the page, zero JS console errors (only
  unrelated Chrome-extension noise, a crypto-wallet content script —
  `nkbihfbeogaeaoehlefnkodbefgpgknn`, not ours). Clicked through all 7 real tabs
  (Buffer, History, Overrides, BigQuery Lookup, Missing Ship-To, Missing Items,
  Customer/Ship-To Mismatch — "Cloud SQL" is a disabled/greyed tab, not wired to any
  JS, correctly ignored) — every tab switched and rendered its correct content with
  zero new console errors. Went one step further than a pure tab-switch test: selected
  the Customer/Ship-To Mismatch tab and clicked its Search button with no company
  selected — got back the correct client-side validation message ("Select a company
  above first."), proving the full fetch-validation-render pipeline actually executes
  end-to-end, not just that the tab shell paints.

## Files Actively Being Edited

Nothing is mid-edit — both refactors are complete and internally verified, just
uncommitted.

- `C:\claude\sbic-manual-trigger-page\app.py` — rewritten from 4,494 lines to 36;
  thin Flask entrypoint only.
- `C:\claude\sbic-manual-trigger-page\core\` — **new directory**, 7 files + `__init__.py`
  (empty), see Current State above.
- `C:\claude\sbic-manual-trigger-page\routes\` — **new directory**, 12 files +
  `__init__.py` (empty), see Current State above.
- `C:\claude\sbic-manual-trigger-page\static\reconcile\` — **new directory**, 11 `.js`
  files, see Current State above.
- `C:\claude\sbic-manual-trigger-page\static\reconcile.js` — **deleted**.
- `C:\claude\sbic-manual-trigger-page\templates\reconcile.html` — one line changed
  (the `<script>` tag, ~line 843) to load the new ES-module entry point.
- `C:\claude\sbic-manual-trigger-page\handoff.md` — this file, rewritten this turn.
- `C:\claude\sbic-manual-trigger-page\static\dashboard.js`,
  `static\handoff-history.js` — **untouched this session** (still the state from the
  PRIOR session's UI-consolidation work, already committed as `35d1d1c`).
- Dev server: still running at `http://127.0.0.1:8099`, PID 17040 as of this
  handoff — confirm with `netstat -ano | grep 8099` before trusting this PID.

## Failed Attempts

- **Trusting the `reconcile.js`-split agent's own final report** — it was cut off by
  a usage-limit reset mid-sentence ("Comprehensive verification complete. Cleaning up
  and finalizing.") with status `failed` (error: `rate_limit`, HTTP 429). **Why this
  matters**: all the files it had written up to that point (all 11 new `.js` files,
  the `reconcile.html` template edit, the deletion of the old `reconcile.js`) were
  already on disk and turned out to be complete and correct — but its own claimed
  verification steps could NOT be trusted since the message cutting it off was
  mid-verification, not post-verification. I re-ran every verification step myself
  (`node --check`, function-inventory diff, import/export cross-check, live browser
  test) from scratch rather than assuming anything was actually checked. **Lesson for
  next session**: a `status: "failed"` task notification with a `result` field that
  sounds like a success message is NOT evidence of success — check the actual files
  on disk and redo verification independently.
- **First browser_batch attempt to click through all 7 reconcile.js tabs in one
  batched call** — clicked "History" (worked, page scrolled down afterward since the
  "Your Details" card collapsed/shifted), then immediately tried to click "Overrides"/
  "BigQuery Lookup"/"Missing Ship-To"/"Missing Items"/"Customer/Ship-To Mismatch" using
  the SAME pre-scroll coordinates from the first screenshot, all in the same batch —
  **why it failed**: `browser_batch`'s own documentation note — "coordinates you write
  in THIS batch refer to the screenshot taken BEFORE this call" — only accounts for
  coordinates staying valid relative to ONE pre-batch screenshot; it does NOT mean the
  page won't move between individual actions WITHIN the batch. Every click after the
  first one landed on stale, scrolled-away coordinates and silently did nothing (no
  error — the page just didn't change, which could easily be mistaken for "the tab
  button isn't wired up" if not caught). **Fix**: took a fresh screenshot after every
  single click when testing this page (one tool call per click+screenshot pair, not
  batched), which correctly showed each tab switching.
- **`python3 - <<'EOF' ... EOF` and `py3 - <<'EOF' ... EOF` heredoc invocations** to
  run a quick cross-export-check Python script — **why it failed**: neither `python3`
  nor `py3` exist in this Windows/Git-Bash environment (only `py`, the Python
  launcher, is on PATH — same gotcha already documented from prior sessions, just
  re-triggered with a slightly different wrong command name this time). Also, the
  first combined attempt's heredoc delimiter handling broke across the `||` fallback
  boundary. **Fix**: wrote the script to a real temp file first
  (`cat > /tmp/check_exports.py << 'PYEOF' ... PYEOF`), then ran it with
  `py /tmp/check_exports.py` — always use `py`, never `python`/`python3`/`py3`, in
  this environment.

## Next Step

Nothing is blocking. Both refactors are done, verified, and uncommitted. The single
next action is: **ask the user whether to commit this** (I already asked at the end
of this session and have not yet received an answer in this conversation — resume by
checking if the user responded, and if not, re-surface the question). If they say
yes:
1. `git add app.py core/ routes/ static/reconcile/ static/reconcile.js templates/reconcile.html`
   (note: `static/reconcile.js` needs `git add` too, to stage its deletion — a bare
   `git add -A` would also work but stages everything including this handoff.md, which
   is fine/expected, just confirm `git status` first per the standing safety protocol).
2. Do NOT amend — this is a brand new commit on top of `35d1d1c`.
3. Write a commit message describing the refactor (not a vague "cleanup" — name the
   two things: app.py → core/+routes/, reconcile.js → static/reconcile/*.js by
   feature).
If they say no / want changes first: ask what they'd like adjusted before touching
anything further — don't re-run either fork again without a clear reason, both are
already internally verified.

Separately, still carried over, completely untouched this session (see the PRIOR
handoff's own "Next Step" — unchanged, not revisited):
- Deciding whether to fix the underlying `rgmc-worker-pool` bugs found last session
  (the empty-`fetch_customerpouldetailbq`-silently-accepted-as-"zero lines" bug, and
  the now-permanently-unreachable `create_by == "trigger-bc-only"` Teams-notification
  gate).
- Scope-checking whether other historical POs (before the batch checked last session)
  also have BC-orders-with-zero-lines.
- The ~170-ish still-stuck SBIC POs from `temp/week_scan2.json`.
- The 97 orphaned SBIC buffer entries from the Nov 13 mis-tagging cleanup.
- The ~55+ SKU UOM-confirmation / ~13 SKU-linking backlog on `/reconcile`.

## Context & Gotchas

- **Two background forks ran for this session, both using `subagent_type: "fork"` so
  they inherited full conversation context** — the `app.py` one completed cleanly
  (930s, 176 tool calls, 466,908 tokens); the `reconcile.js` one got cut off by a
  session usage-limit reset partway through its own final verification pass (see
  Failed Attempts above) but had already finished all the actual file-writing work
  before being cut off — only its self-reported verification was unreliable, not its
  output. When resuming ANY interrupted fork/agent in future sessions: check the
  actual files on disk first, don't assume a `status: "failed"` means no useful work
  happened, and don't assume a `status: "completed"` summary's claimed verification
  steps were actually performed correctly either — spot-check independently either
  way, which is exactly what caught nothing wrong here but is the discipline that
  matters.
- **The `app.py` fork used an isolated git worktree it created itself**
  (`.claude/worktrees/agent-a8be71fcfb35ae97c/`) even though I did not pass
  `isolation: "worktree"` in the Agent tool call — noted as a drive-by observation
  during a repo-wide grep this session, never investigated further since it didn't
  cause any actual conflict (the final result ended up correctly in the main working
  tree, confirmed via `git status`/`wc -l`/direct file reads). If a future session
  sees a stray `.claude/worktrees/` directory, it's very likely leftover from a fork
  like this — safe to ignore/clean up unless it's still actively referenced by a
  running task.
- **`node --check <multiple files>` does NOT reliably check every file passed** —
  pass one file per invocation (looped over `static/reconcile/*.js` in a shell `for`
  loop) rather than listing all files in one `node --check` call, to guarantee every
  file actually gets validated.
- **ES modules were a new pattern introduced to this codebase this session** — every
  other `.js` file in `static/` (`dashboard.js`, `handoff-history.js`, `script.js`)
  is still a classic script with no `import`/`export`. Only `static/reconcile/*.js`
  uses `type="module"` now. This is an intentional, justified exception (confirmed
  safe: no inline `onclick` handlers, no `window.X` global dependencies in the
  original `reconcile.js`), not an inconsistency to "fix" by converting the others —
  but worth knowing if a future session wonders why one page's JS looks structurally
  different from the rest.
- **`core/` and `routes/` as directory names were chosen deliberately to avoid any
  name collision with `app.py`** (the Flask entrypoint) or Python's own `app` module
  naming conventions — no special significance beyond that, free to rename if the
  user prefers different naming, nothing else references these paths by name except
  the import statements inside `app.py` itself and the cross-module imports listed in
  Current State above.
- **Dev server quirks from prior sessions still apply and were re-relied-on this
  session**: single-threaded (one slow BigQuery-backed request blocks everything
  else), and `pkill -f "py app.py"` is unreliable on this Windows+Git-Bash setup for
  killing a previously-started `py`-launched process — always verify with
  `netstat -ano | grep ":8099" | grep LISTENING` and use
  `taskkill //F //PID <pid> //T` (double-slash) if a restart is ever needed again.
