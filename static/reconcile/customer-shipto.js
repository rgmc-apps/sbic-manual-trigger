// Customer/Ship-To Mismatch tab — already-created BC Sales Orders whose Sell-to
// Customer and/or Ship-to Code are set but wrong — split out of the original
// monolithic reconcile.js.
import { getEmployeeDetails, refreshGate, setBtnLoading, escapeHtml, setStatus } from "./core.js";
import { renderCandidateList, normalizeCandidates, fetchPendingFixes } from "./override-utils.js";

  const customerShiptoCompanySelect  = document.getElementById("customer-shipto-company-select");
  const customerShiptoPoRefInput     = document.getElementById("customer-shipto-po-ref-input");
  const customerShiptoDateFrom       = document.getElementById("customer-shipto-date-from");
  const customerShiptoDateTo         = document.getElementById("customer-shipto-date-to");
  const customerShiptoCustomerInput  = document.getElementById("customer-shipto-customer-input");
  const customerShiptoCustomerSuggest = document.getElementById("customer-shipto-customer-suggest");
  const customerShiptoShiptoInput    = document.getElementById("customer-shipto-shipto-input");
  const customerShiptoShiptoSuggest  = document.getElementById("customer-shipto-shipto-suggest");
  const customerShiptoSearchBtn      = document.getElementById("customer-shipto-search-btn");
  const customerShiptoStatusLine     = document.getElementById("customer-shipto-status-line");
  const customerShiptoResultsList    = document.getElementById("customer-shipto-results-list");
  const customerShiptoFixesAction      = document.getElementById("customer-shipto-fixes-action");
  const customerShiptoFixesQueuedCount = document.getElementById("customer-shipto-fixes-queued-count");
  const customerShiptoFixesApplyBtn    = document.getElementById("customer-shipto-fixes-apply-btn");
  const customerShiptoHistoryToggleBtn   = document.getElementById("customer-shipto-history-toggle-btn");
  const customerShiptoHistoryPanel       = document.getElementById("customer-shipto-history-panel");
  const customerShiptoHistorySearchInput = document.getElementById("customer-shipto-history-search-input");
  const customerShiptoHistorySearchBtn   = document.getElementById("customer-shipto-history-search-btn");
  const customerShiptoHistoryStatusLine  = document.getElementById("customer-shipto-history-status-line");
  const customerShiptoHistoryResultsList = document.getElementById("customer-shipto-history-results-list");

  // ── Customer/Ship-To Mismatch ─────────────────────────────────────────────
  // Finds already-created BC Sales Orders (submittedBy = "SBIC AI Uploading") whose
  // Sell-to Customer and/or Ship-to Code are already set but wrong -- there's nothing
  // to auto-compare against (unlike Missing Items/Missing Ship-To), so a human has to
  // spot it visually from the search results, then pick the correct ship-to address.
  // Unlike Missing Ship-To's buildMissingShiptoRow, picking a ship-to here is explicitly
  // allowed to belong to a DIFFERENT customer than the order's current one -- queuing
  // re-points BOTH sellToCustomerNo and shipToCode together, applied synchronously
  // (like Missing Items), not via the Pub/Sub + rgmc-worker-pool batch-patch pipeline
  // Missing Ship-To uses.
  export function setCustomerShiptoStatus(msg, isError) {
    setStatus(customerShiptoStatusLine, msg, isError);
  }

  function fetchPendingCustomerShiptoFixes(company) {
    return fetchPendingFixes("/api/sales-orders/customer-shipto-fixes", company);
  }

  async function refreshCustomerShiptoFixesQueueCount(company) {
    try {
      const res = await fetch(`/api/sales-orders/customer-shipto-fixes?company=${encodeURIComponent(company)}&applied=false`);
      const data = await res.json();
      const n = res.ok ? (data.data || []).length : 0;
      customerShiptoFixesQueuedCount.textContent = n ? `${n} fix${n === 1 ? "" : "es"} queued, not yet applied.` : "";
      customerShiptoFixesAction.classList.toggle("hidden", n === 0);
    } catch (e) { /* best-effort */ }
  }

  function buildCustomerShiptoRow(order, company, pendingFix) {
    const row = document.createElement("div");
    row.className = "order-row";
    const poRef = order.externalDocumentNo || "";
    row.innerHTML = `
      <div class="order-row-head">
        <span class="order-ref">${escapeHtml(order.number || "—")}</span>
        <span class="order-badge"></span>
        <button class="btn-link-toggle btn-customer-shipto-toggle" type="button">Update Ship-To…</button>
      </div>
      <div class="order-detail">
        Customer: ${escapeHtml(order.sellToCustomerName || order.sellToCustomerNo || "—")}
        (${escapeHtml(order.sellToCustomerNo || "—")}) &middot;
        Ship-to: ${escapeHtml(order.shipToCode || "(none)")} &middot;
        PO ref: ${escapeHtml(poRef || "—")} &middot;
        Order date: ${escapeHtml(order.orderDate || "—")}
      </div>
      <div class="order-detail bq-branch-detail">
        BigQuery branch on file: <strong>${escapeHtml(order.bqCustomerBranchName || "—")}</strong>
      </div>
      <div class="search-hint search-loading shipto-queuing-status hidden">
        <span class="btn-spinner"></span> <span class="queuing-text"></span>
      </div>
      <div class="group-resolved hidden">
        Queued — Customer <strong class="resolved-customer-text"></strong>, Ship-to <strong class="resolved-shipto-text"></strong>
        <button class="btn-unlink" type="button">Cancel</button>
        <div class="shipto-fix-error hidden"></div>
      </div>
      <div class="shipto-link-panel hidden">
        <div class="search-row"><input type="text" class="search-input" placeholder="Search BC ship-to addresses by name…" /></div>
        <div class="search-results shipto-manual-results"></div>
      </div>
    `;

    const toggleBtn = row.querySelector(".btn-customer-shipto-toggle");
    const badge = row.querySelector(".order-badge");
    const panel = row.querySelector(".shipto-link-panel");
    const queuingStatus = row.querySelector(".shipto-queuing-status");
    const queuingText = row.querySelector(".queuing-text");
    const resolvedBox = row.querySelector(".group-resolved");
    const unlinkBtn = row.querySelector(".btn-unlink");
    const fixErrorEl = row.querySelector(".shipto-fix-error");
    const searchInput = row.querySelector(".search-input");
    const manualResultsEl = row.querySelector(".shipto-manual-results");
    let currentFixId = pendingFix ? pendingFix.id : null;
    let lastQueued = pendingFix || null;

    function showQueuing(text) {
      panel.classList.add("hidden");
      resolvedBox.classList.add("hidden");
      queuingText.textContent = text;
      queuingStatus.classList.remove("hidden");
      badge.textContent = "Queuing…";
      badge.className = "order-badge loading";
      row.classList.remove("is-queued");
      row.classList.add("is-loading");
    }

    function showQueued(fix) {
      lastQueued = fix;
      queuingStatus.classList.add("hidden");
      const customerChanged = (fix.new_customer_no || "").toUpperCase() !== (order.sellToCustomerNo || "").toUpperCase();
      resolvedBox.querySelector(".resolved-customer-text").textContent = customerChanged
        ? `${order.sellToCustomerNo || "—"} → ${fix.new_customer_no}${fix.new_customer_name ? " (" + fix.new_customer_name + ")" : ""}`
        : `${fix.new_customer_no} (unchanged)`;
      resolvedBox.querySelector(".resolved-shipto-text").textContent =
        `${order.shipToCode || "(none)"} → ${fix.new_ship_to_code}${fix.new_ship_to_name ? " — " + fix.new_ship_to_name : ""}`;
      // A fix left queued (applied still false) after a failed Apply attempt carries the
      // reason on .error — shown here so it doesn't look identical to a freshly queued,
      // never-attempted fix.
      if (fix.error) {
        fixErrorEl.textContent = `⚠ Last Apply attempt: ${fix.error}`;
        fixErrorEl.classList.remove("hidden");
      } else {
        fixErrorEl.classList.add("hidden");
      }
      resolvedBox.classList.remove("hidden");
      panel.classList.add("hidden");
      toggleBtn.textContent = "Change…";
      badge.textContent = fix.error ? "Queued (retry)" : "Queued";
      badge.className = "order-badge pending";
      row.classList.remove("is-loading");
      row.classList.add("is-queued");
    }

    function showUnqueued() {
      lastQueued = null;
      queuingStatus.classList.add("hidden");
      resolvedBox.classList.add("hidden");
      fixErrorEl.classList.add("hidden");
      toggleBtn.textContent = "Update Ship-To…";
      badge.textContent = "Not queued";
      badge.className = "order-badge";
      row.classList.remove("is-queued", "is-loading");
    }

    if (pendingFix) { showQueued(pendingFix); } else { showUnqueued(); }

    // Only QUEUES the fix (so_customer_shipto_fixes_{env}) — never writes to BC
    // directly. Apply Customer/Ship-To Fixes below is the one real write, synchronous
    // (see api_apply_customer_shipto_fixes / apply_customer_shipto_fixes), re-validated
    // fresh against BC immediately before each PATCH. Unlike Missing Ship-To's
    // applyShipTo, a ship-to belonging to a DIFFERENT customer than the order's current
    // one is explicitly allowed — candidate.extra (the ship-to's own customerNumber,
    // same shape normalizeCandidates("branch", ...) already produces) becomes the new
    // sellToCustomerNo.
    async function applyCustomerShipTo(candidate) {
      if (!refreshGate()) {
        setCustomerShiptoStatus("Fill in your details above first.", true);
        return;
      }
      toggleBtn.disabled = true;
      showQueuing(`Queuing ship-to ${candidate.code}…`);
      try {
        const res = await fetch("/api/sales-orders/customer-shipto-fixes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            company, order_id: order.id,
            new_ship_to_code: candidate.code, new_ship_to_name: candidate.name || "",
            new_customer_no: candidate.extra || "", new_customer_name: candidate.customerName || "",
            source: candidate.source || "", resolved_by: getEmployeeDetails().employee_name,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Queue failed");
        currentFixId = data.id;
        showQueued({
          new_customer_no: candidate.extra || "", new_customer_name: candidate.customerName || "",
          new_ship_to_code: candidate.code, new_ship_to_name: candidate.name || "",
        });
        setCustomerShiptoStatus(`Queued ${order.number} for ship-to ${candidate.code} — click Apply Customer/Ship-To Fixes below to write it to BC.`);
        refreshCustomerShiptoFixesQueueCount(company);
      } catch (e) {
        queuingStatus.classList.add("hidden");
        if (lastQueued) { showQueued(lastQueued); } else { showUnqueued(); panel.classList.remove("hidden"); }
        setCustomerShiptoStatus(`Could not queue a fix for ${order.number}: ${e.message}`, true);
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
        const res = await fetch(`/api/sales-orders/customer-shipto-fixes/${encodeURIComponent(currentFixId)}`, { method: "DELETE" });
        if (!res.ok && res.status !== 404) throw new Error(`Cancel failed (${res.status})`);
        currentFixId = null;
        showUnqueued();
        setCustomerShiptoStatus(`Cancelled the queued fix for ${order.number}.`);
        refreshCustomerShiptoFixesQueueCount(company);
      } catch (e) {
        showQueued(wasQueued);
        setCustomerShiptoStatus(`Could not cancel: ${e.message}`, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    toggleBtn.addEventListener("click", () => {
      panel.classList.toggle("hidden");
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
          if (searchInput.value.trim() !== term) return;
          renderCandidateList(manualResultsEl, "branch", normalizeCandidates("branch", data.data), applyCustomerShipTo);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          manualResultsEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    return row;
  }

  function renderCustomerShiptoResults(orders, company, pendingByOrderId) {
    customerShiptoResultsList.innerHTML = "";
    if (!orders.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No Open Sales Orders found for the given filters.";
      customerShiptoResultsList.appendChild(empty);
      customerShiptoFixesAction.classList.add("hidden");
      return;
    }
    orders.forEach((o) => {
      const pendingFix = pendingByOrderId[o.id] || null;
      customerShiptoResultsList.appendChild(buildCustomerShiptoRow(o, company, pendingFix));
    });
  }

  export async function searchCustomerShiptoMismatch() {
    const company = customerShiptoCompanySelect.value;
    if (!company) {
      setCustomerShiptoStatus("Select a company above first.", true);
      return;
    }
    customerShiptoSearchBtn.disabled = true;
    setBtnLoading("customer-shipto-search-spinner", "customer-shipto-search-btn-label", true, "Searching…");
    setCustomerShiptoStatus(`Searching ${company} for Open Sales Orders (submittedBy = "SBIC AI Uploading")…`);
    try {
      const params = new URLSearchParams({ company });
      const poRef = customerShiptoPoRefInput.value.trim();
      if (poRef) params.set("po_ref_number", poRef);
      if (customerShiptoDateFrom.value) params.set("date_from", customerShiptoDateFrom.value);
      if (customerShiptoDateTo.value) params.set("date_to", customerShiptoDateTo.value);
      const customerName = customerShiptoCustomerInput.value.trim();
      if (customerName) params.set("customer_name", customerName);
      const shipToSearch = customerShiptoShiptoInput.value.trim();
      if (shipToSearch) params.set("ship_to_search", shipToSearch);
      const res = await fetch(`/api/sales-orders/customer-shipto-mismatch?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
      const orders = data.data || [];
      const pendingByOrderId = await fetchPendingCustomerShiptoFixes(company);
      renderCustomerShiptoResults(orders, company, pendingByOrderId);
      setCustomerShiptoStatus(`Found ${orders.length} order(s) in ${company}.`);
      refreshCustomerShiptoFixesQueueCount(company);
    } catch (e) {
      setCustomerShiptoStatus("Error: " + e.message, true);
    } finally {
      customerShiptoSearchBtn.disabled = false;
      setBtnLoading("customer-shipto-search-spinner", "customer-shipto-search-btn-label", false);
    }
  }

  customerShiptoSearchBtn.addEventListener("click", searchCustomerShiptoMismatch);
  customerShiptoPoRefInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchCustomerShiptoMismatch(); });

  // BC-backed autocomplete for a toolbar filter input — debounced search-as-you-type,
  // rendered via the same renderCandidateList/normalizeCandidates the resolution
  // pickers already use, just floated in a dropdown (.toolbar-suggest-dropdown) instead
  // of an inline block, since this sits in a single-row toolbar. Picking a suggestion
  // fills the field with BC's own exact name and immediately re-runs the search — the
  // field itself still just free-texts into the existing contains(...) filter
  // server-side, so typing without ever picking a suggestion keeps working exactly as
  // before.
  function wireToolbarSuggest(inputEl, dropdownEl, fetchCandidates, candidateType) {
    let timer = null;
    function hide() {
      dropdownEl.classList.add("hidden");
      dropdownEl.innerHTML = "";
    }
    inputEl.addEventListener("input", () => {
      clearTimeout(timer);
      const term = inputEl.value.trim();
      if (term.length < 2) { hide(); return; }
      timer = setTimeout(async () => {
        dropdownEl.classList.remove("hidden");
        dropdownEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const rows = await fetchCandidates(term);
          if (inputEl.value.trim() !== term) return; // stale response guard
          if (!rows.length) {
            dropdownEl.innerHTML = `<div class="search-hint">No matches.</div>`;
            return;
          }
          renderCandidateList(dropdownEl, candidateType, normalizeCandidates(candidateType, rows), (candidate) => {
            inputEl.value = candidate.name || "";
            hide();
            searchCustomerShiptoMismatch();
          });
        } catch (e) {
          if (inputEl.value.trim() !== term) return;
          dropdownEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 300);
    });
    // Delayed so a click on a dropdown item registers (mousedown→click) before blur
    // hides it out from under the cursor.
    inputEl.addEventListener("blur", () => setTimeout(hide, 150));
    inputEl.addEventListener("focus", () => {
      if (inputEl.value.trim().length >= 2 && dropdownEl.innerHTML.trim()) dropdownEl.classList.remove("hidden");
    });
  }

  async function fetchBcCustomerSuggestions(term) {
    const company = customerShiptoCompanySelect.value;
    if (!company) throw new Error("Select a company above first.");
    const res = await fetch(`/api/lookup/customers?search=${encodeURIComponent(term)}&company=${encodeURIComponent(company)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
    return data.data || [];
  }

  async function fetchBcShipToSuggestions(term) {
    const company = customerShiptoCompanySelect.value;
    if (!company) throw new Error("Select a company above first.");
    const res = await fetch(`/api/lookup/ship-to?search=${encodeURIComponent(term)}&company=${encodeURIComponent(company)}`);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
    return data.data || [];
  }

  wireToolbarSuggest(customerShiptoCustomerInput, customerShiptoCustomerSuggest, fetchBcCustomerSuggestions, "customer");
  wireToolbarSuggest(customerShiptoShiptoInput, customerShiptoShiptoSuggest, fetchBcShipToSuggestions, "branch");
  // Enter still runs a plain free-text search even if the suggestion dropdown never
  // opened/was dismissed — wireToolbarSuggest only handles picking a suggestion.
  customerShiptoCustomerInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchCustomerShiptoMismatch(); });
  customerShiptoShiptoInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchCustomerShiptoMismatch(); });

  // ── Customer/Ship-To Link History ────────────────────────────────────────
  customerShiptoHistoryToggleBtn.addEventListener("click", () => {
    customerShiptoHistoryPanel.classList.toggle("hidden");
  });

  function setCustomerShiptoHistoryStatus(msg, isError) {
    setStatus(customerShiptoHistoryStatusLine, msg, isError);
  }

  function renderCustomerShiptoHistory(records) {
    customerShiptoHistoryResultsList.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No link-history records found.";
      customerShiptoHistoryResultsList.appendChild(empty);
      return;
    }
    customerShiptoHistoryResultsList.innerHTML = records.map((r) => `
      <div class="shipto-history-row">
        <div class="shipto-history-row-head">
          <span class="order-ref">${escapeHtml(r.order_no || "—")}</span>
          <span class="shipto-history-outcome ${escapeHtml(r.outcome || "")}">${escapeHtml((r.outcome || "—").replace(/_/g, " ").toUpperCase())}</span>
        </div>
        <div class="order-detail">
          PO ref: ${escapeHtml(r.po_ref || "—")} &middot;
          Company: ${escapeHtml(r.company || "—")} &middot;
          Customer: ${escapeHtml(r.current_customer_no || "—")} → ${escapeHtml(r.new_customer_no || "—")} &middot;
          Ship-to: ${escapeHtml(r.new_ship_to_code || "—")} &middot;
          By: ${escapeHtml(r.attempted_by || "—")} &middot;
          ${r.attempted_at ? escapeHtml(new Date(r.attempted_at).toLocaleString()) : "—"}
        </div>
        ${r.error ? `<div class="shipto-history-detail">${escapeHtml(r.error)}</div>` : ""}
      </div>
    `).join("");
  }

  async function searchCustomerShiptoHistory() {
    const term = customerShiptoHistorySearchInput.value.trim();
    if (!term) {
      setCustomerShiptoHistoryStatus("Enter a PO ref or order number first.", true);
      return;
    }
    customerShiptoHistorySearchBtn.disabled = true;
    setBtnLoading("customer-shipto-history-search-spinner", "customer-shipto-history-search-btn-label", true, "Searching…");
    setCustomerShiptoHistoryStatus("Searching link history…");
    try {
      const [poRes, orderRes] = await Promise.all([
        fetch(`/api/sales-orders/customer-shipto-fix-history?po_ref=${encodeURIComponent(term)}`),
        fetch(`/api/sales-orders/customer-shipto-fix-history?order_no=${encodeURIComponent(term)}`),
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
      renderCustomerShiptoHistory(records);
      setCustomerShiptoHistoryStatus(`Found ${records.length} record(s).`);
    } catch (e) {
      setCustomerShiptoHistoryStatus("Error: " + e.message, true);
    } finally {
      customerShiptoHistorySearchBtn.disabled = false;
      setBtnLoading("customer-shipto-history-search-spinner", "customer-shipto-history-search-btn-label", false);
    }
  }

  customerShiptoHistorySearchBtn.addEventListener("click", searchCustomerShiptoHistory);
  customerShiptoHistorySearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchCustomerShiptoHistory(); });

  customerShiptoFixesApplyBtn.addEventListener("click", async () => {
    const company = customerShiptoCompanySelect.value;
    if (!company) {
      setCustomerShiptoStatus("Select a company above first.", true);
      return;
    }
    if (!refreshGate()) {
      setCustomerShiptoStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Apply every queued customer/ship-to fix for ${company} to Business Central now? This writes directly to BC.`)) {
      return;
    }
    customerShiptoFixesApplyBtn.disabled = true;
    setBtnLoading("customer-shipto-fixes-apply-spinner", "customer-shipto-fixes-apply-btn-label", true, "Applying…");
    setCustomerShiptoStatus(`Applying queued customer/ship-to fixes for ${company} to BC…`);
    try {
      const res = await fetch("/api/apply-customer-shipto-fixes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, applied_by: getEmployeeDetails().employee_name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Apply failed");
      let msg = `${data.applied || 0} order(s) updated in BC, ${data.already_present || 0} already matched`;
      if (data.failed) msg += `, ${data.failed} failed — still queued for retry`;
      if (data.remaining) msg += `. ${data.remaining} more queued fix(es) weren't processed this run (cap of 50 per Apply) — click Apply Customer/Ship-To Fixes again`;
      msg += ".";
      await searchCustomerShiptoMismatch(); // re-render — applied/already-matched rows drop off
      setCustomerShiptoStatus(msg, data.failed > 0 && !data.applied);
    } catch (e) {
      setCustomerShiptoStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("customer-shipto-fixes-apply-spinner", "customer-shipto-fixes-apply-btn-label", false);
      customerShiptoFixesApplyBtn.disabled = false;
    }
  });
