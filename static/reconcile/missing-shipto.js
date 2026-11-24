// Missing Ship-To tab — Open BC Sales Orders with no Ship-to Code, their fix
// queue/apply/history flows — split out of the original monolithic reconcile.js.
import { getEmployeeDetails, refreshGate, setBtnLoading, escapeHtml, RUN_POLL_MS, MAX_WATCH_MS, startElapsedTicker, setStatus } from "./core.js";
import { renderCandidateList, normalizeCandidates, fetchPendingFixes } from "./override-utils.js";

  const missingShiptoCompanySelect = document.getElementById("missing-shipto-company-select");
  const missingShiptoSearchBtn     = document.getElementById("missing-shipto-search-btn");
  const missingShiptoStatusLine    = document.getElementById("missing-shipto-status-line");
  const missingShiptoResultsList   = document.getElementById("missing-shipto-results-list");
  const applyKnownOverridesBtn     = document.getElementById("apply-known-overrides-btn");
  const shiptoHistoryToggleBtn     = document.getElementById("shipto-history-toggle-btn");
  const shiptoHistoryPanel         = document.getElementById("shipto-history-panel");
  const shiptoHistorySearchInput   = document.getElementById("shipto-history-search-input");
  const shiptoHistorySearchBtn     = document.getElementById("shipto-history-search-btn");
  const shiptoHistoryStatusLine    = document.getElementById("shipto-history-status-line");
  const shiptoHistoryResultsList   = document.getElementById("shipto-history-results-list");
  const shiptoFixesAction          = document.getElementById("shipto-fixes-action");
  const shiptoFixesQueuedCount     = document.getElementById("shipto-fixes-queued-count");
  const shiptoFixesApplyBtn        = document.getElementById("shipto-fixes-apply-btn");
  const shiptoFixesStatusEl        = document.getElementById("shipto-fixes-status");
  const shiptoFixesStatusTitleEl   = document.getElementById("shipto-fixes-status-title");
  const shiptoFixesStatusDetailEl  = document.getElementById("shipto-fixes-status-detail");
  const shiptoFixesElapsedEl       = document.getElementById("shipto-fixes-elapsed");
  const shiptoFixesStopWatchingBtn = document.getElementById("shipto-fixes-stop-watching-btn");

  // ── Missing Ship-To tab ───────────────────────────────────────────────────
  // Queries BC directly (not the buffer) for Open Sales Orders this app's own
  // SO-import pipeline created that have no Ship-to Code saved. Read-only — see
  // _find_orders_missing_shipto in app.py for the exact server-side filter.
  export function setMissingShiptoStatus(msg, isError) {
    setStatus(missingShiptoStatusLine, msg, isError);
  }

  // One Missing-Ship-To row, with an inline "Link Ship-To…" panel — mirrors
  // buildSplitSubRow's toggle/suggest/search-fallback shape. Suggestions come from
  // rgmc-gcp-api's /customerpoul/{po_ref}/shipto-match, which fuzzy-matches this PO's
  // BigQuery/CustomerPOUL-sourced customerBranchName (and an exact customerBranchLookUpCode
  // match, if any) against BC's live Ship-to Addresses for the order's company — the
  // same mechanism the Buffer tab's branch-suggestion already uses (fetchSuggestionCandidates),
  // just driven by an already-created order's externalDocumentNo instead of a buffered PO.
  function buildMissingShiptoRow(order, company, pendingFix, placeRow) {
    const row = document.createElement("div");
    row.className = "order-row";
    const poRef = order.externalDocumentNo || "";
    row.innerHTML = `
      <div class="order-row-head">
        <span class="order-ref">${escapeHtml(order.number || "—")}</span>
        <span class="order-badge failed">No Ship-To</span>
        <button class="btn-link-toggle btn-missing-shipto-toggle" type="button">Link Ship-To…</button>
      </div>
      <div class="order-detail">
        Customer: ${escapeHtml(order.sellToCustomerName || order.sellToCustomerNo || "—")}
        (${escapeHtml(order.sellToCustomerNo || "—")}) &middot;
        PO ref: ${escapeHtml(poRef || "—")} &middot;
        Order date: ${escapeHtml(order.orderDate || "—")}
      </div>
      <div class="search-hint search-loading shipto-queuing-status hidden">
        <span class="btn-spinner"></span> <span class="queuing-text"></span>
      </div>
      <div class="group-resolved hidden">
        Queued ship-to <strong class="resolved-text"></strong>
        <button class="btn-unlink" type="button">Cancel</button>
        <div class="shipto-fix-error hidden"></div>
      </div>
      <div class="shipto-link-panel hidden">
        <div class="search-hint shipto-branch-context"></div>
        <div class="shipto-suggestions search-results"></div>
        <div class="search-row"><input type="text" class="search-input" placeholder="Or search existing BC ship-to addresses…" /></div>
        <div class="search-results shipto-manual-results"></div>
      </div>
    `;

    const toggleBtn = row.querySelector(".btn-missing-shipto-toggle");
    const badge = row.querySelector(".order-badge");
    const panel = row.querySelector(".shipto-link-panel");
    const queuingStatus = row.querySelector(".shipto-queuing-status");
    const queuingText = row.querySelector(".queuing-text");
    const resolvedBox = row.querySelector(".group-resolved");
    const unlinkBtn = row.querySelector(".btn-unlink");
    const fixErrorEl = row.querySelector(".shipto-fix-error");
    const branchContext = row.querySelector(".shipto-branch-context");
    const suggestionsEl = row.querySelector(".shipto-suggestions");
    const searchInput = row.querySelector(".search-input");
    const manualResultsEl = row.querySelector(".shipto-manual-results");
    let currentFixId = pendingFix ? pendingFix.id : null;
    let lastQueued = pendingFix || null; // last confirmed {ship_to_code, ship_to_name} — used to revert the UI if a cancel fails mid-flight

    // In-flight state between "pick a candidate"/"Cancel" and the server actually
    // confirming it — without this, the row jumped straight from the candidate list to
    // "Queued" with nothing shown during the network round trip, making the click feel
    // unresponsive. badge text doubles as the row's own status label throughout.
    // row.classList "is-queued"/"is-loading" give the row itself a visible accent (see
    // reconcile.css) so it's obvious at a glance — without reading any text — which
    // rows are actually included in the next Apply Ship-To Fixes run, independent of
    // the Queued/Not-queued section headers renderMissingShiptoResults groups them into.
    function showQueuing(text) {
      panel.classList.add("hidden");
      resolvedBox.classList.add("hidden");
      queuingText.textContent = text;
      queuingStatus.classList.remove("hidden");
      badge.textContent = "Queuing…";
      badge.classList.remove("failed", "pending");
      badge.classList.add("loading");
      row.classList.remove("is-queued");
      row.classList.add("is-loading");
    }

    function showQueued(codeAndName) {
      lastQueued = codeAndName;
      queuingStatus.classList.add("hidden");
      resolvedBox.querySelector(".resolved-text").textContent =
        `${codeAndName.ship_to_code}${codeAndName.ship_to_name ? " — " + codeAndName.ship_to_name : ""}`;
      // A fix left queued (applied still false) after a failed/skipped Apply attempt
      // carries the reason on .error — shown here so it doesn't look identical to a
      // freshly-queued, never-attempted fix (both would otherwise just say "Queued").
      if (codeAndName.error) {
        fixErrorEl.textContent = `⚠ Last Apply attempt: ${codeAndName.error}`;
        fixErrorEl.classList.remove("hidden");
      } else {
        fixErrorEl.classList.add("hidden");
      }
      resolvedBox.classList.remove("hidden");
      panel.classList.add("hidden");
      toggleBtn.textContent = "Change…";
      badge.textContent = codeAndName.error ? "Queued (retry)" : "Queued";
      badge.classList.remove("failed", "loading");
      badge.classList.add("pending");
      row.classList.remove("is-loading");
      row.classList.add("is-queued");
      if (placeRow) placeRow(row, true); // moves the row into the "Queued" section, if not already there
    }

    function showUnqueued() {
      lastQueued = null;
      queuingStatus.classList.add("hidden");
      resolvedBox.classList.add("hidden");
      fixErrorEl.classList.add("hidden");
      toggleBtn.textContent = "Link Ship-To…";
      badge.textContent = "No Ship-To";
      badge.classList.remove("pending", "loading");
      badge.classList.add("failed");
      row.classList.remove("is-queued", "is-loading");
      if (placeRow) placeRow(row, false); // moves the row into the "Not yet linked" section
    }

    if (pendingFix) showQueued(pendingFix);

    // Only QUEUES the fix (rgmc-bc-api's so_shipto_fixes_{env} — see _find_orders_missing_shipto
    // in app.py for the re-validation this goes through server-side first). Never
    // writes to BC directly — the "Apply Ship-To Fixes" button below is the one real
    // write, batched via the same trigger -> Pub/Sub -> rgmc-worker-pool pipeline
    // Reprocess Buffer uses (see so_import_worker.py's _run_shipto_fixes_batch).
    async function applyShipTo(candidate) {
      if (!refreshGate()) {
        setMissingShiptoStatus("Fill in your details above first.", true);
        return;
      }
      toggleBtn.disabled = true;
      showQueuing(`Queuing ship-to ${candidate.code}…`);
      try {
        const res = await fetch("/api/sales-orders/shipto-fixes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            company, order_id: order.id, ship_to_code: candidate.code,
            ship_to_name: candidate.name || "", ship_to_customer_no: candidate.extra || "",
            source: candidate.source || "", resolved_by: getEmployeeDetails().employee_name,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Queue failed");
        currentFixId = data.id;
        showQueued({ ship_to_code: candidate.code, ship_to_name: candidate.name || "" });
        setMissingShiptoStatus(`Queued ${order.number} for ship-to ${candidate.code} — click Apply Ship-To Fixes below to write it to BC.`);
        refreshShiptoFixesQueueCount(company);
      } catch (e) {
        queuingStatus.classList.add("hidden");
        // Revert to whatever was true before this attempt — already-queued (Change…
        // failed, the old fix is still the current one) or never-queued.
        if (lastQueued) { showQueued(lastQueued); } else { showUnqueued(); panel.classList.remove("hidden"); }
        setMissingShiptoStatus(`Could not queue a fix for ${order.number}: ${e.message}`, true);
      } finally {
        toggleBtn.disabled = false;
      }
    }

    unlinkBtn.addEventListener("click", async () => {
      if (!currentFixId) return;
      unlinkBtn.disabled = true;
      const wasQueued = lastQueued;
      showQueuing("Cancelling queued fix…");
      try {
        const res = await fetch(`/api/sales-orders/shipto-fixes/${encodeURIComponent(currentFixId)}`, { method: "DELETE" });
        if (!res.ok && res.status !== 404) throw new Error(`Cancel failed (${res.status})`);
        currentFixId = null;
        showUnqueued();
        setMissingShiptoStatus(`Cancelled the queued fix for ${order.number}.`);
        refreshShiptoFixesQueueCount(company);
      } catch (e) {
        showQueued(wasQueued);
        setMissingShiptoStatus(`Could not cancel: ${e.message}`, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function loadSuggestions() {
      if (!poRef) {
        suggestionsEl.innerHTML = `<div class="search-hint">No PO ref on this order — use the search box below.</div>`;
        return;
      }
      suggestionsEl.innerHTML =
        `<div class="search-hint search-loading"><span class="btn-spinner"></span> ` +
        `Looking up this PO's branch in BigQuery/CustomerPOUL and fuzzy-matching it against BC ship-to addresses…</div>`;
      try {
        const res = await fetch(`/api/suggest/shipto/${encodeURIComponent(poRef)}?company=${encodeURIComponent(company)}`);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Lookup failed");
        branchContext.textContent = data.customerBranchName
          ? `PO's branch on file: "${data.customerBranchName}"${data.customerBranchLookUpCode ? ` (lookup code: ${data.customerBranchLookUpCode})` : ""}`
          : "No customer branch name found for this PO in BigQuery/CustomerPOUL — use the search box below.";
        // overrideMatch (an already-saved branch/customer override resolving this PO's
        // ship-to — see _find_shipto_override in app.py) is shown first and flagged
        // distinctly (renderCandidateList's "✓ saved ... override" tag) since it's a
        // human's already-vetted decision, not a fresh guess — still just a candidate
        // to click, never auto-applied.
        const seen = new Set();
        const raw = [];
        [data.overrideMatch, data.exactCodeMatch, ...(data.fuzzyMatches || [])].forEach((m) => {
          if (!m || !m.code || seen.has(m.code)) return;
          seen.add(m.code);
          raw.push(m);
        });
        renderCandidateList(suggestionsEl, "branch", normalizeCandidates("branch", raw), applyShipTo);
      } catch (e) {
        suggestionsEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
      }
    }

    toggleBtn.addEventListener("click", () => {
      const opening = panel.classList.toggle("hidden") === false;
      if (opening && !panel.dataset.loaded) {
        panel.dataset.loaded = "1";
        loadSuggestions();
      }
    });

    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = searchInput.value.trim();
      if (term.length < 2) { manualResultsEl.innerHTML = ""; return; }
      searchTimer = setTimeout(async () => {
        manualResultsEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/ship-to?search=${encodeURIComponent(term)}&company=${encodeURIComponent(company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          if (searchInput.value.trim() !== term) return; // stale response guard
          renderCandidateList(manualResultsEl, "branch", normalizeCandidates("branch", data.data), applyShipTo);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          manualResultsEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    return row;
  }

  function fetchPendingShiptoFixes(company) {
    return fetchPendingFixes("/api/sales-orders/shipto-fixes", company);
  }

  async function refreshShiptoFixesQueueCount(company) {
    try {
      const res = await fetch(`/api/sales-orders/shipto-fixes?company=${encodeURIComponent(company)}&applied=false`);
      const data = await res.json();
      const n = res.ok ? (data.data || []).length : 0;
      shiptoFixesQueuedCount.textContent = n ? `${n} fix${n === 1 ? "" : "es"} queued, not yet applied.` : "";
      shiptoFixesAction.classList.toggle("hidden", n === 0);
    } catch (e) { /* best-effort */ }
  }

  // Queued rows are split into their own headed section, ahead of the not-yet-linked
  // ones — reuses the BigQuery Lookup tab's .bq-group-heading/.bq-group-count styling.
  // This is the "what's actually included if I click Apply Ship-To Fixes right now"
  // view; the per-row .is-queued/.is-loading accent (see buildMissingShiptoRow) then
  // reinforces the same distinction at a glance even if the sections scroll apart.
  //
  // Both group headings/bodies are always present in the DOM (hidden via class when
  // empty) rather than rebuilt per search, so a single row settling into Queued/
  // Not-queued (via the placeRow callback passed into buildMissingShiptoRow) can just
  // move its own existing DOM node into the other body — every OTHER row's open
  // panel/in-flight state survives untouched, instead of the whole list re-rendering
  // from scratch on every pick/cancel.
  function renderMissingShiptoGroups(orders, pendingByOrderId) {
    missingShiptoResultsList.innerHTML = `
      <div class="bq-group-heading hidden" data-group="queued">
        <span>✅ Queued — included in the next Apply Ship-To Fixes</span><span class="bq-group-count"></span>
      </div>
      <div class="shipto-group-body" data-group-body="queued"></div>
      <div class="bq-group-heading hidden" data-group="not-queued">
        <span>Not yet linked — won't be touched by Apply</span><span class="bq-group-count"></span>
      </div>
      <div class="shipto-group-body" data-group-body="not-queued"></div>
    `;
    const queuedHeading = missingShiptoResultsList.querySelector('[data-group="queued"]');
    const notQueuedHeading = missingShiptoResultsList.querySelector('[data-group="not-queued"]');
    const queuedBody = missingShiptoResultsList.querySelector('[data-group-body="queued"]');
    const notQueuedBody = missingShiptoResultsList.querySelector('[data-group-body="not-queued"]');

    function updateGroupHeadings() {
      const qn = queuedBody.children.length;
      const nn = notQueuedBody.children.length;
      queuedHeading.classList.toggle("hidden", qn === 0);
      queuedHeading.querySelector(".bq-group-count").textContent = qn;
      notQueuedHeading.classList.toggle("hidden", nn === 0);
      notQueuedHeading.querySelector(".bq-group-count").textContent = nn;
    }

    function placeRow(rowEl, isQueued) {
      (isQueued ? queuedBody : notQueuedBody).appendChild(rowEl);
      updateGroupHeadings();
    }

    orders.forEach((o) => {
      const pendingFix = pendingByOrderId[o.id] || null;
      const row = buildMissingShiptoRow(o, missingShiptoCompanySelect.value, pendingFix, placeRow);
      placeRow(row, !!pendingFix);
    });
  }

  async function renderMissingShiptoResults(orders, company) {
    missingShiptoResultsList.innerHTML = "";
    if (!orders.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No Open Sales Orders with a missing Ship-to Code found.";
      missingShiptoResultsList.appendChild(empty);
      shiptoFixesAction.classList.add("hidden");
      return;
    }
    const pendingByOrderId = await fetchPendingShiptoFixes(company);
    renderMissingShiptoGroups(orders, pendingByOrderId);
    refreshShiptoFixesQueueCount(company);
  }

  export async function searchMissingShipto() {
    const company = missingShiptoCompanySelect.value;
    if (!company) {
      setMissingShiptoStatus("Select a company above first.", true);
      return;
    }
    missingShiptoSearchBtn.disabled = true;
    setBtnLoading("missing-shipto-search-spinner", "missing-shipto-search-btn-label", true, "Searching…");
    setMissingShiptoStatus(`Searching ${company} for Open Sales Orders with no Ship-to Code (submittedBy = "SBIC AI Uploading")…`);
    try {
      const res = await fetch(`/api/sales-orders/missing-shipto?company=${encodeURIComponent(company)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
      const orders = data.data || [];
      await renderMissingShiptoResults(orders, company);
      setMissingShiptoStatus(`Found ${orders.length} Open Sales Order(s) in ${company} with no Ship-to Code.`);
    } catch (e) {
      setMissingShiptoStatus("Error: " + e.message, true);
    } finally {
      missingShiptoSearchBtn.disabled = false;
      setBtnLoading("missing-shipto-search-spinner", "missing-shipto-search-btn-label", false);
    }
  }

  missingShiptoSearchBtn.addEventListener("click", searchMissingShipto);

  // ── Apply Known Overrides — bulk-queues (never writes to BC) every Missing-Ship-To
  // order whose branch/customer already has a saved override, via the server-side
  // /api/sales-orders/apply-known-overrides (see app.py for the matching precedence).
  // A search must have run first so there's something to report against.
  applyKnownOverridesBtn.addEventListener("click", async () => {
    const company = missingShiptoCompanySelect.value;
    if (!company) {
      setMissingShiptoStatus("Select a company above first.", true);
      return;
    }
    if (!refreshGate()) {
      setMissingShiptoStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Auto-queue a Ship-to Code fix for every Open Sales Order in ${company} whose branch/customer already has a saved override? This only queues — nothing is written to BC until you click Apply Ship-To Fixes.`)) {
      return;
    }
    applyKnownOverridesBtn.disabled = true;
    setBtnLoading("apply-known-overrides-spinner", "apply-known-overrides-btn-label", true, "Checking overrides…");
    setMissingShiptoStatus(`Checking ${company}'s Missing Ship-To orders against saved overrides…`);
    try {
      const res = await fetch("/api/sales-orders/apply-known-overrides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, resolved_by: getEmployeeDetails().employee_name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Apply Known Overrides failed");
      const failed = (data.data || []).filter((r) => !r.ok);
      let msg = `Queued ${data.queued} of ${data.total} order(s) from saved overrides in ${company}`;
      if (data.already_queued) msg += ` (${data.already_queued} were already queued)`;
      if (data.no_override_match) msg += `, ${data.no_override_match} had no matching override — link those manually`;
      if (data.customer_mismatch) msg += `, ${data.customer_mismatch} matched an override for a DIFFERENT customer and were skipped (BC would reject them) — link those manually`;
      msg += ".";
      if (failed.length) msg += ` ${failed.length} queue attempt(s) failed — see ${failed.map((f) => f.number).join(", ")}.`;
      await searchMissingShipto(); // re-render first so newly-queued orders move into the ✅ Queued section…
      setMissingShiptoStatus(msg, failed.length > 0 && data.queued === 0); // …then overwrite its "Found N" status with this summary
    } catch (e) {
      setMissingShiptoStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("apply-known-overrides-spinner", "apply-known-overrides-btn-label", false);
      applyKnownOverridesBtn.disabled = false;
    }
  });

  // ── Ship-To Link History — searchable, append-only record of every Apply Ship-To
  // Fixes attempt (applied/skipped/failed), by PO ref or order number, so a human can
  // find what ship-to was actually applied to a PO and correct it if it was wrong. See
  // api_shipto_fix_history in app.py / shipto_fix_history.py (rgmc-worker-pool) for
  // where each record comes from.
  shiptoHistoryToggleBtn.addEventListener("click", () => {
    shiptoHistoryPanel.classList.toggle("hidden");
  });

  function setShiptoHistoryStatus(msg, isError) {
    setStatus(shiptoHistoryStatusLine, msg, isError);
  }

  function renderShiptoHistory(records) {
    shiptoHistoryResultsList.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No link-history records found.";
      shiptoHistoryResultsList.appendChild(empty);
      return;
    }
    shiptoHistoryResultsList.innerHTML = records.map((r) => `
      <div class="shipto-history-row">
        <div class="shipto-history-row-head">
          <span class="order-ref">${escapeHtml(r.order_no || "—")}</span>
          <span class="shipto-history-outcome ${escapeHtml(r.outcome || "")}">${escapeHtml((r.outcome || "—").replace(/_/g, " ").toUpperCase())}</span>
        </div>
        <div class="order-detail">
          PO ref: ${escapeHtml(r.po_ref || "—")} &middot;
          Company: ${escapeHtml(r.company || "—")} &middot;
          Ship-to: ${escapeHtml(r.ship_to_code || "—")}${r.ship_to_name ? " — " + escapeHtml(r.ship_to_name) : ""} &middot;
          By: ${escapeHtml(r.resolved_by || "—")} &middot;
          ${r.attempted_at ? escapeHtml(new Date(r.attempted_at).toLocaleString()) : "—"}
        </div>
        ${r.detail ? `<div class="shipto-history-detail">${escapeHtml(r.detail)}</div>` : ""}
      </div>
    `).join("");
  }

  async function searchShiptoHistory() {
    const term = shiptoHistorySearchInput.value.trim();
    if (!term) {
      setShiptoHistoryStatus("Enter a PO ref or order number first.", true);
      return;
    }
    shiptoHistorySearchBtn.disabled = true;
    setBtnLoading("shipto-history-search-spinner", "shipto-history-search-btn-label", true, "Searching…");
    setShiptoHistoryStatus("Searching link history…");
    try {
      // Try as a PO ref and as an order number in parallel — a user may paste either
      // one, and the two filters are mutually exclusive server-side (api_shipto_fix_history).
      const [poRes, orderRes] = await Promise.all([
        fetch(`/api/sales-orders/shipto-fix-history?po_ref=${encodeURIComponent(term)}`),
        fetch(`/api/sales-orders/shipto-fix-history?order_no=${encodeURIComponent(term)}`),
      ]);
      const [poData, orderData] = await Promise.all([poRes.json(), orderRes.json()]);
      if (!poRes.ok && !orderRes.ok) throw new Error(poData.error || orderData.error || "Search failed");
      const seen = new Set();
      const records = [];
      [...(poData.data || []), ...(orderData.data || [])].forEach((r) => {
        if (seen.has(r.id)) return;
        seen.add(r.id);
        records.push(r);
      });
      records.sort((a, b) => (b.attempted_at || "").localeCompare(a.attempted_at || ""));
      renderShiptoHistory(records);
      setShiptoHistoryStatus(`Found ${records.length} record(s) for "${term}".`);
    } catch (e) {
      setShiptoHistoryStatus("Error: " + e.message, true);
    } finally {
      shiptoHistorySearchBtn.disabled = false;
      setBtnLoading("shipto-history-search-spinner", "shipto-history-search-btn-label", false);
    }
  }

  shiptoHistorySearchBtn.addEventListener("click", searchShiptoHistory);
  shiptoHistorySearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchShiptoHistory(); });

  // ── Apply Ship-To Fixes — same trigger -> Pub/Sub -> rgmc-worker-pool -> batch
  // BC-write pipeline as Reprocess Buffer (see /api/apply-shipto-fixes in app.py and
  // so_import_worker.py's _run_shipto_fixes_batch), just for queued Ship-to Code fixes
  // instead of buffered orders. A self-contained watcher, not the Buffer tab's
  // startWatching/stopWatching (those are entangled with the Buffer tab's own
  // state/buffer-count fallback) — reuses only the already job-agnostic
  // GET /api/reprocess-status/<run_id> polling endpoint and the shared RUN_POLL_MS/
  // MAX_WATCH_MS constants and startElapsedTicker helper.
  let shiptoFixesWatcher = null;

  function stopWatchingShiptoFixes(finalMessage, variant, detailMessage) {
    if (!shiptoFixesWatcher) return;
    clearInterval(shiptoFixesWatcher.tickTimer);
    clearTimeout(shiptoFixesWatcher.pollTimer);
    shiptoFixesWatcher = null;
    if (finalMessage) {
      shiptoFixesStatusEl.classList.remove("is-success", "is-error", "is-stopped");
      if (variant) shiptoFixesStatusEl.classList.add(variant);
      shiptoFixesStatusTitleEl.textContent = finalMessage;
      shiptoFixesStatusDetailEl.textContent = detailMessage || "";
      shiptoFixesStopWatchingBtn.classList.add("hidden");
    } else {
      shiptoFixesStatusEl.classList.add("hidden");
    }
  }

  function summarizeShiptoFixesRun(summary) {
    if (!summary) return "";
    const parts = [`${summary.fixes_applied || 0} fix(es) applied and removed from the list below`];
    if (summary.fixes_already_resolved) parts.push(`${summary.fixes_already_resolved} already had a ship-to set another way — also removed, nothing more to do`);
    if (summary.fixes_skipped) parts.push(`${summary.fixes_skipped} skipped (no longer eligible) — still shown as Queued, with the reason, below`);
    if (summary.fixes_failed) parts.push(`${summary.fixes_failed} failed — still shown as Queued for the next Apply attempt`);
    return parts.join(", ") + ".";
  }

  async function pollShiptoFixesRunStatusOnce(company) {
    if (!shiptoFixesWatcher) return;
    try {
      const res = await fetch(`/api/reprocess-status/${encodeURIComponent(shiptoFixesWatcher.runId)}`);
      const data = await res.json();
      if (!shiptoFixesWatcher || !res.ok) return;

      if (data.status === "done") {
        stopWatchingShiptoFixes("Apply done.", "is-success", summarizeShiptoFixesRun(data.summary));
        searchMissingShipto(); // re-run the search — applied rows drop off (shipToCode no longer blank)
        return;
      }
      if (data.status === "error") {
        stopWatchingShiptoFixes("Apply failed.", "is-error", data.error || "Unknown error.");
        return;
      }
      shiptoFixesStatusTitleEl.textContent = data.status === "queued"
        ? "Apply triggered — waiting for the worker to pick it up…"
        : "Applying…";
      if (Date.now() - shiptoFixesWatcher.startedAt >= MAX_WATCH_MS) {
        stopWatchingShiptoFixes("Stopped auto-checking after 10 minutes — reload to see the latest.", "is-stopped");
        return;
      }
      shiptoFixesWatcher.pollTimer = setTimeout(() => pollShiptoFixesRunStatusOnce(company), RUN_POLL_MS);
    } catch (e) {
      if (shiptoFixesWatcher) shiptoFixesWatcher.pollTimer = setTimeout(() => pollShiptoFixesRunStatusOnce(company), RUN_POLL_MS);
    }
  }

  function startWatchingShiptoFixes(runId, company) {
    stopWatchingShiptoFixes();
    shiptoFixesWatcher = { startedAt: Date.now(), runId, pollTimer: null, tickTimer: null };
    shiptoFixesStatusEl.classList.remove("hidden", "is-success", "is-error", "is-stopped");
    shiptoFixesStatusTitleEl.textContent = "Apply triggered — watching for results…";
    shiptoFixesStatusDetailEl.textContent = `You'll also get an email at ${getEmployeeDetails().email}.`;
    shiptoFixesStopWatchingBtn.classList.remove("hidden");
    shiptoFixesWatcher.tickTimer = startElapsedTicker(shiptoFixesElapsedEl, shiptoFixesWatcher.startedAt);
    shiptoFixesWatcher.pollTimer = setTimeout(() => pollShiptoFixesRunStatusOnce(company), RUN_POLL_MS);
  }

  shiptoFixesStopWatchingBtn.addEventListener("click", () => stopWatchingShiptoFixes());

  shiptoFixesApplyBtn.addEventListener("click", async () => {
    const company = missingShiptoCompanySelect.value;
    if (!company) {
      setMissingShiptoStatus("Select a company above first.", true);
      return;
    }
    if (!refreshGate()) {
      setMissingShiptoStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Apply every queued Ship-to Code fix for ${company}? This writes directly to Business Central. You'll get an email at ${getEmployeeDetails().email} with the result.`)) {
      return;
    }
    shiptoFixesApplyBtn.disabled = true;
    setBtnLoading("shipto-fixes-apply-spinner", "shipto-fixes-apply-btn-label", true, "Applying…");
    setMissingShiptoStatus("Triggering Apply Ship-To Fixes…");
    try {
      const res = await fetch("/api/apply-shipto-fixes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Apply trigger failed");
      setMissingShiptoStatus(`Apply triggered for ${company}.`);
      if (data.run_id) {
        startWatchingShiptoFixes(data.run_id, company);
      } else {
        setMissingShiptoStatus(`Apply triggered for ${company} — check back shortly.`);
      }
    } catch (e) {
      setMissingShiptoStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("shipto-fixes-apply-spinner", "shipto-fixes-apply-btn-label", false);
      shiptoFixesApplyBtn.disabled = false;
    }
  });
