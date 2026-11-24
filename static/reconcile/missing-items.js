// Missing Items tab — BC Sales Orders missing lines Document AI says should be
// there, their fix queue/apply/history flows — split out of the original monolithic
// reconcile.js.
import { getEmployeeDetails, refreshGate, setBtnLoading, escapeHtml, setStatus } from "./core.js";
import { resolvedPayload, renderCandidateList, normalizeCandidates, fetchPendingFixes } from "./override-utils.js";

  const missingItemsCompanySelect  = document.getElementById("missing-items-company-select");
  const missingItemsPoRefInput     = document.getElementById("missing-items-po-ref-input");
  const missingItemsDateFrom       = document.getElementById("missing-items-date-from");
  const missingItemsDateTo         = document.getElementById("missing-items-date-to");
  const missingItemsSearchBtn      = document.getElementById("missing-items-search-btn");
  const missingItemsStatusLine     = document.getElementById("missing-items-status-line");
  const missingItemsResultsList    = document.getElementById("missing-items-results-list");
  const missingItemFixesAction       = document.getElementById("missing-item-fixes-action");
  const missingItemFixesQueuedCount  = document.getElementById("missing-item-fixes-queued-count");
  const missingItemFixesApplyBtn     = document.getElementById("missing-item-fixes-apply-btn");
  const missingItemsHistoryToggleBtn   = document.getElementById("missing-items-history-toggle-btn");
  const missingItemsHistoryPanel       = document.getElementById("missing-items-history-panel");
  const missingItemsHistorySearchInput = document.getElementById("missing-items-history-search-input");
  const missingItemsHistorySearchBtn   = document.getElementById("missing-items-history-search-btn");
  const missingItemsHistoryStatusLine  = document.getElementById("missing-items-history-status-line");
  const missingItemsHistoryResultsList = document.getElementById("missing-items-history-results-list");

  // ── Missing Items tab ────────────────────────────────────────────────────
  // Finds already-created BC Sales Orders whose current lines are missing items the
  // original Document AI extraction says should be there, lets a human resolve/verify
  // each one (auto via a saved SKU override, or by linking fresh), then transfers to BC
  // only on an explicit button click. See missing_items_plan.md for the full design and
  // its one accepted limitation (doesn't replicate BC's own item-reference matching).
  export function setMissingItemsStatus(msg, isError) {
    setStatus(missingItemsStatusLine, msg, isError);
  }

  function fetchPendingMissingItemFixes(company) {
    return fetchPendingFixes("/api/sales-orders/missing-item-fixes", company, (f) => `${f.order_id}::${f.sku_key}`);
  }

  async function refreshMissingItemFixesQueueCount(company) {
    try {
      const res = await fetch(`/api/sales-orders/missing-item-fixes?company=${encodeURIComponent(company)}&applied=false`);
      const data = await res.json();
      const n = res.ok ? (data.data || []).length : 0;
      missingItemFixesQueuedCount.textContent = n ? `${n} item${n === 1 ? "" : "s"} queued, not yet transferred.` : "";
      missingItemFixesAction.classList.toggle("hidden", n === 0);
    } catch (e) { /* best-effort */ }
  }

  // One missing-item row within an order's group — SKU/desc, expected qty/UOM/price,
  // and either a "Queue" button (already resolved via a saved override) or a
  // "Link item…" toggle that opens a small BC item search, reusing the same
  // normalizeCandidates/renderCandidateList/resolvedPayload the Overrides tab's SKU
  // links already use for type "sku".
  function buildMissingItemRow(order, item, company, pendingFix) {
    const row = document.createElement("div");
    row.className = "order-row missing-item-row";
    row.innerHTML = `
      <div class="order-row-head">
        <span class="order-ref">${escapeHtml(item.customer_sku_code || item.customer_sku_desc || "(no SKU)")}</span>
        <span class="order-badge"></span>
        <button class="btn-link-toggle btn-missing-item-action" type="button"></button>
      </div>
      <div class="order-detail">
        ${escapeHtml(item.customer_sku_desc || "—")} &middot;
        Qty: ${escapeHtml(String(item.po_qty ?? "—"))} ${escapeHtml(item.unit_of_measurement || "")} &middot;
        Unit price: ${escapeHtml(String(item.unit_price ?? "—"))}
        ${item.resolved_item ? ` &middot; Links to item <strong>${escapeHtml(item.resolved_item.itemNo || "")}</strong>${item.resolved_item.description ? " — " + escapeHtml(item.resolved_item.description) : ""}` : ""}
      </div>
      <div class="search-hint search-loading shipto-queuing-status hidden">
        <span class="btn-spinner"></span> <span class="queuing-text"></span>
      </div>
      <div class="group-resolved hidden">
        Queued item <strong class="resolved-text"></strong>
        <button class="btn-unlink" type="button">Cancel</button>
        <div class="shipto-fix-error hidden"></div>
      </div>
      <div class="shipto-link-panel hidden">
        <div class="search-row item-search-row"><input type="text" class="search-input" placeholder="Search Business Central items…" /></div>
        <div class="search-results item-manual-results"></div>
        <div class="search-hint uom-hint hidden">
          This item's Document AI unit (<strong>${escapeHtml(item.unit_of_measurement || "")}</strong>) isn't one of its
          valid units in Business Central — pick the correct one:
        </div>
        <div class="search-results uom-results"></div>
      </div>
    `;

    const actionBtn = row.querySelector(".btn-missing-item-action");
    const badge = row.querySelector(".order-badge");
    const panel = row.querySelector(".shipto-link-panel");
    const queuingStatus = row.querySelector(".shipto-queuing-status");
    const queuingText = row.querySelector(".queuing-text");
    const resolvedBox = row.querySelector(".group-resolved");
    const unlinkBtn = row.querySelector(".btn-unlink");
    const fixErrorEl = row.querySelector(".shipto-fix-error");
    const searchRow = row.querySelector(".item-search-row");
    const searchInput = row.querySelector(".search-input");
    const manualResultsEl = row.querySelector(".item-manual-results");
    const uomHint = row.querySelector(".uom-hint");
    const uomResultsEl = row.querySelector(".uom-results");
    let currentFixId = pendingFix ? pendingFix.id : null;
    let resolvedItem = item.resolved_item || null;
    let needsUom = item.status === "needs_uom";

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
      queuingStatus.classList.add("hidden");
      resolvedBox.querySelector(".resolved-text").textContent =
        `${fix.item_no}${fix.item_description ? " — " + fix.item_description : ""}`;
      // A fix left queued (applied still false) after a failed Transfer attempt carries
      // the reason on .error — shown here so it doesn't look identical to a freshly
      // queued, never-attempted fix.
      if (fix.error) {
        fixErrorEl.textContent = `⚠ Last Transfer attempt: ${fix.error}`;
        fixErrorEl.classList.remove("hidden");
      } else {
        fixErrorEl.classList.add("hidden");
      }
      resolvedBox.classList.remove("hidden");
      panel.classList.add("hidden");
      actionBtn.textContent = "Change…";
      badge.textContent = fix.error ? "Queued (retry)" : "Queued";
      badge.className = "order-badge pending";
      row.classList.remove("is-loading");
      row.classList.add("is-queued");
    }

    function showUnqueued() {
      queuingStatus.classList.add("hidden");
      resolvedBox.classList.add("hidden");
      fixErrorEl.classList.add("hidden");
      // Item search picker only applies while the SKU itself is unresolved; once an item
      // is resolved, any remaining problem is the UOM picker's job instead.
      searchRow.classList.toggle("hidden", !!resolvedItem);
      manualResultsEl.classList.toggle("hidden", !!resolvedItem);
      uomHint.classList.toggle("hidden", !needsUom);
      uomResultsEl.classList.toggle("hidden", !needsUom);
      if (!resolvedItem) {
        actionBtn.textContent = "Link item…";
        badge.textContent = "⚠ Needs linking";
        badge.className = "order-badge failed";
      } else if (needsUom) {
        actionBtn.textContent = "Resolve UOM…";
        badge.textContent = "⚠ Needs UOM";
        badge.className = "order-badge failed";
      } else {
        actionBtn.textContent = "Queue";
        badge.textContent = "✓ Ready to insert";
        badge.className = "order-badge ready";
      }
      row.classList.remove("is-queued", "is-loading");
    }

    // Only QUEUES the fix — never writes to BC directly. Transfer to BC below is the
    // one real write (see api_apply_missing_item_fixes / apply_missing_item_fixes),
    // re-validated fresh against BC immediately before each line is added.
    async function queueFix(resolved, resolvedByNote) {
      actionBtn.disabled = true;
      showQueuing(`Queuing item ${resolved.itemNo}…`);
      try {
        const res = await fetch("/api/sales-orders/missing-item-fixes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            company, order_id: order.id, order_no: order.number || "",
            po_ref: order.externalDocumentNo || "", sku_key: item.sku_key,
            customer_sku_code: item.customer_sku_code, customer_sku_desc: item.customer_sku_desc,
            item_no: resolved.itemNo, item_description: resolved.description || "",
            po_qty: item.po_qty, unit_of_measurement: item.unit_of_measurement,
            unit_price: item.unit_price, resolved_by: resolvedByNote,
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Queue failed");
        currentFixId = data.id;
        resolvedItem = resolved;
        showQueued({ item_no: resolved.itemNo, item_description: resolved.description || "" });
        setMissingItemsStatus(`Queued ${resolved.itemNo} on ${order.number || order.externalDocumentNo} — click Transfer to BC below to write it.`);
        refreshMissingItemFixesQueueCount(company);
      } catch (e) {
        queuingStatus.classList.add("hidden");
        showUnqueued();
        panel.classList.toggle("hidden", !!resolvedItem); // reopen the picker only if this was a fresh (not yet resolved) link attempt
        setMissingItemsStatus(`Could not queue ${resolved.itemNo}: ${e.message}`, true);
      } finally {
        actionBtn.disabled = false;
      }
    }

    unlinkBtn.addEventListener("click", async () => {
      if (!currentFixId) return;
      unlinkBtn.disabled = true;
      showQueuing("Cancelling queued fix…");
      try {
        const res = await fetch(`/api/sales-orders/missing-item-fixes/${encodeURIComponent(currentFixId)}`, { method: "DELETE" });
        if (!res.ok && res.status !== 404) throw new Error(`Cancel failed (${res.status})`);
        currentFixId = null;
        showUnqueued();
        setMissingItemsStatus(`Cancelled the queued fix for ${order.number || order.externalDocumentNo}.`);
        refreshMissingItemFixesQueueCount(company);
      } catch (e) {
        showQueued({ item_no: resolvedItem.itemNo, item_description: resolvedItem.description || "" });
        setMissingItemsStatus(`Could not cancel: ${e.message}`, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function pickCandidate(candidate) {
      const resolved = resolvedPayload("sku", candidate, item.sku_key);
      // Linking also saves the override (so future searches see this SKU as "ready"
      // automatically) — same /api/overrides endpoint the Overrides tab's SKU links use.
      try {
        const res = await fetch("/api/overrides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "sku", key: item.sku_key, resolved,
            resolved_by: getEmployeeDetails().employee_name, buffer_ids: [],
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Saving the item link failed");
      } catch (e) {
        setMissingItemsStatus(`Could not save item link: ${e.message}`, true);
        return;
      }
      await queueFix(resolved, getEmployeeDetails().employee_name);
    }

    // Resolves the UOM problem for an item whose SKU is already fine — saves a "uom"
    // override keyed to this exact item+raw-unit combo (same shape the Buffer tab's
    // Unit of Measure overrides already use), then queues with the item unchanged.
    async function pickUomCandidate(candidate) {
      const resolved = resolvedPayload("uom", candidate);
      try {
        const res = await fetch("/api/overrides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            type: "uom", key: `${resolvedItem.itemNo}::${item.unit_of_measurement}`, resolved,
            resolved_by: getEmployeeDetails().employee_name, buffer_ids: [],
          }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Saving the UOM link failed");
      } catch (e) {
        setMissingItemsStatus(`Could not save UOM link: ${e.message}`, true);
        return;
      }
      needsUom = false;
      await queueFix(resolvedItem, getEmployeeDetails().employee_name);
    }

    actionBtn.addEventListener("click", () => {
      if (!currentFixId && resolvedItem && !needsUom) {
        queueFix(resolvedItem, getEmployeeDetails().employee_name); // "Queue" — fully resolved, no panel needed
        return;
      }
      if (!panel.dataset.uomRendered && needsUom) {
        panel.dataset.uomRendered = "1";
        renderCandidateList(uomResultsEl, "uom", normalizeCandidates("uom", item.uom_options || []), pickUomCandidate);
      }
      panel.classList.toggle("hidden"); // "Link item…", "Resolve UOM…", or "Change…" — open/close the picker
    });

    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = searchInput.value.trim();
      if (term.length < 2) { manualResultsEl.innerHTML = ""; return; }
      searchTimer = setTimeout(async () => {
        manualResultsEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/items?search=${encodeURIComponent(term)}&company=${encodeURIComponent(company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          if (searchInput.value.trim() !== term) return;
          renderCandidateList(manualResultsEl, "sku", normalizeCandidates("sku", data.data), pickCandidate);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          manualResultsEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    if (pendingFix) {
      showQueued(pendingFix);
    } else {
      showUnqueued();
    }
    return row;
  }

  function buildMissingItemsOrderGroup(entry, company, pendingByKey) {
    const { order, missing_items } = entry;
    const group = document.createElement("div");
    group.className = "group-row bq-row missing-items-order-group";
    const poRef = order.externalDocumentNo || "";
    const readyCount = missing_items.filter(
      (it) => it.status === "ready" && !pendingByKey[`${order.id}::${it.sku_key}`]
    ).length;
    group.innerHTML = `
      <div class="group-head">
        <div class="group-key-wrap bq-key-wrap">
          <div>
            <span class="group-key"></span>
            <span class="group-desc"></span>
          </div>
        </div>
        <div class="group-meta bq-status-badges">
          <span class="bq-status-badge missing">${missing_items.length} missing item${missing_items.length === 1 ? "" : "s"}</span>
        </div>
      </div>
      <div class="bq-row-actions">
        ${readyCount > 0 ? `<button class="btn-history-toggle btn-queue-all-ready" type="button">Queue all ${readyCount} ready item${readyCount === 1 ? "" : "s"}</button>` : ""}
      </div>
      <div class="missing-items-sub-list"></div>
    `;
    group.querySelector(".group-key").textContent = order.number || poRef || "(no order no.)";
    group.querySelector(".group-desc").textContent =
      `PO ref ${poRef || "—"} · ${order.sellToCustomerName || order.sellToCustomerNo || "—"} · order date ${order.orderDate || "—"}`;

    const subList = group.querySelector(".missing-items-sub-list");
    missing_items.forEach((item) => {
      const pendingFix = pendingByKey[`${order.id}::${item.sku_key}`] || null;
      subList.appendChild(buildMissingItemRow(order, item, company, pendingFix));
    });

    const queueAllBtn = group.querySelector(".btn-queue-all-ready");
    if (queueAllBtn) {
      queueAllBtn.addEventListener("click", async () => {
        queueAllBtn.disabled = true;
        queueAllBtn.textContent = "Queuing…";
        const readyBtns = Array.from(subList.querySelectorAll(".btn-missing-item-action"))
          .filter((b) => b.textContent === "Queue" && !b.disabled);
        for (const b of readyBtns) {
          b.click();
          await new Promise((resolve) => setTimeout(resolve, 150)); // stagger, avoid hammering the endpoint
        }
        queueAllBtn.remove();
      });
    }

    return group;
  }

  function renderMissingItemsGroups(groups, company, pendingByKey) {
    missingItemsResultsList.innerHTML = "";
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No missing items found for the given filters.";
      missingItemsResultsList.appendChild(empty);
      return;
    }
    groups.forEach((entry) => {
      missingItemsResultsList.appendChild(buildMissingItemsOrderGroup(entry, company, pendingByKey));
    });
  }

  export async function searchMissingItems() {
    const company = missingItemsCompanySelect.value;
    if (!company) {
      setMissingItemsStatus("Select a company above first.", true);
      return;
    }
    missingItemsSearchBtn.disabled = true;
    setBtnLoading("missing-items-search-spinner", "missing-items-search-btn-label", true, "Searching…");
    setMissingItemsStatus(`Searching ${company} for Sales Orders and comparing lines against Document AI…`);
    try {
      const params = new URLSearchParams({ company });
      const poRef = missingItemsPoRefInput.value.trim();
      if (poRef) params.set("po_ref_number", poRef);
      if (missingItemsDateFrom.value) params.set("date_from", missingItemsDateFrom.value);
      if (missingItemsDateTo.value) params.set("date_to", missingItemsDateTo.value);
      const res = await fetch(`/api/sales-orders/missing-items?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
      const groups = data.data || [];
      const pendingByKey = await fetchPendingMissingItemFixes(company);
      renderMissingItemsGroups(groups, company, pendingByKey);
      const totalMissing = groups.reduce((sum, g) => sum + g.missing_items.length, 0);
      setMissingItemsStatus(`Found ${groups.length} order(s) with ${totalMissing} missing item(s) total in ${company}.`);
      refreshMissingItemFixesQueueCount(company);
    } catch (e) {
      setMissingItemsStatus("Error: " + e.message, true);
    } finally {
      missingItemsSearchBtn.disabled = false;
      setBtnLoading("missing-items-search-spinner", "missing-items-search-btn-label", false);
    }
  }

  missingItemsSearchBtn.addEventListener("click", searchMissingItems);
  missingItemsPoRefInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchMissingItems(); });

  // ── Missing Items Link History ───────────────────────────────────────────
  missingItemsHistoryToggleBtn.addEventListener("click", () => {
    missingItemsHistoryPanel.classList.toggle("hidden");
  });

  function setMissingItemsHistoryStatus(msg, isError) {
    setStatus(missingItemsHistoryStatusLine, msg, isError);
  }

  function renderMissingItemsHistory(records) {
    missingItemsHistoryResultsList.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No link-history records found.";
      missingItemsHistoryResultsList.appendChild(empty);
      return;
    }
    missingItemsHistoryResultsList.innerHTML = records.map((r) => `
      <div class="shipto-history-row">
        <div class="shipto-history-row-head">
          <span class="order-ref">${escapeHtml(r.order_no || "—")}</span>
          <span class="shipto-history-outcome ${escapeHtml(r.outcome || "")}">${escapeHtml((r.outcome || "—").replace(/_/g, " ").toUpperCase())}</span>
        </div>
        <div class="order-detail">
          PO ref: ${escapeHtml(r.po_ref || "—")} &middot;
          Company: ${escapeHtml(r.company || "—")} &middot;
          Item: ${escapeHtml(r.item_no || "—")} (SKU ${escapeHtml(r.customer_sku_code || r.sku_key || "—")}) &middot;
          By: ${escapeHtml(r.attempted_by || "—")} &middot;
          ${r.attempted_at ? escapeHtml(new Date(r.attempted_at).toLocaleString()) : "—"}
        </div>
        ${r.error ? `<div class="shipto-history-detail">${escapeHtml(r.error)}</div>` : ""}
      </div>
    `).join("");
  }

  export async function searchMissingItemsHistory() {
    const term = missingItemsHistorySearchInput.value.trim();
    if (!term) {
      setMissingItemsHistoryStatus("Enter a PO ref or order number first.", true);
      return;
    }
    missingItemsHistorySearchBtn.disabled = true;
    setBtnLoading("missing-items-history-search-spinner", "missing-items-history-search-btn-label", true, "Searching…");
    setMissingItemsHistoryStatus("Searching link history…");
    try {
      const [poRes, orderRes] = await Promise.all([
        fetch(`/api/sales-orders/missing-item-fix-history?po_ref=${encodeURIComponent(term)}`),
        fetch(`/api/sales-orders/missing-item-fix-history?order_no=${encodeURIComponent(term)}`),
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
      renderMissingItemsHistory(records);
      setMissingItemsHistoryStatus(`Found ${records.length} record(s) for "${term}".`);
    } catch (e) {
      setMissingItemsHistoryStatus("Error: " + e.message, true);
    } finally {
      missingItemsHistorySearchBtn.disabled = false;
      setBtnLoading("missing-items-history-search-spinner", "missing-items-history-search-btn-label", false);
    }
  }

  missingItemsHistorySearchBtn.addEventListener("click", searchMissingItemsHistory);
  missingItemsHistorySearchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchMissingItemsHistory(); });

  // ── Transfer to BC — unlike Apply Ship-To Fixes, this is synchronous (no Pub/Sub +
  // rgmc-worker-pool round-trip): adding a Sales Order Line is already a single existing
  // BC call, re-validated fresh against BC immediately before each write. See
  // apply_missing_item_fixes in rgmc-bc-api's so_buffer_service.py.
  missingItemFixesApplyBtn.addEventListener("click", async () => {
    const company = missingItemsCompanySelect.value;
    if (!company) {
      setMissingItemsStatus("Select a company above first.", true);
      return;
    }
    if (!refreshGate()) {
      setMissingItemsStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Transfer every queued missing item for ${company} to Business Central now? This writes directly to BC.`)) {
      return;
    }
    missingItemFixesApplyBtn.disabled = true;
    setBtnLoading("missing-item-fixes-apply-spinner", "missing-item-fixes-apply-btn-label", true, "Transferring…");
    setMissingItemsStatus(`Transferring queued items for ${company} to BC…`);
    try {
      const res = await fetch("/api/apply-missing-item-fixes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, applied_by: getEmployeeDetails().employee_name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Transfer failed");
      let msg = `${data.applied || 0} item(s) added to BC, ${data.already_present || 0} were already present`;
      if (data.failed) msg += `, ${data.failed} failed — still queued for retry`;
      if (data.remaining) msg += `. ${data.remaining} more queued item(s) weren't processed this run (cap of 50 per Transfer) — click Transfer to BC again`;
      msg += ".";
      await searchMissingItems(); // re-render — applied/already-present items drop off
      setMissingItemsStatus(msg, data.failed > 0 && !data.applied);
    } catch (e) {
      setMissingItemsStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("missing-item-fixes-apply-spinner", "missing-item-fixes-apply-btn-label", false);
      missingItemFixesApplyBtn.disabled = false;
    }
  });
