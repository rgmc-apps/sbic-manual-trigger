(function () {
  "use strict";

  const companySelect = document.getElementById("company-select");
  const loadBtn       = document.getElementById("load-btn");
  const reprocessBtn  = document.getElementById("reprocess-btn");
  const statusLine    = document.getElementById("status-line");
  const summaryRow    = document.getElementById("summary-row");
  const tabsCard      = document.getElementById("tabs-card");
  const rowTemplate   = document.getElementById("group-row-template");

  const LOOKUP_PATH = { sku: "items", branch: "ship-to", customer: "customers" };

  // { company, order_count, orders, groups: { sku: [...], branch: [...], customer: [...] } }
  let state = null;

  // ── Status / summary ────────────────────────────────────────────────────
  function setStatus(msg, isError) {
    statusLine.textContent = msg || "";
    statusLine.classList.toggle("error", !!isError);
  }

  function resolvedCount(list) {
    return list.filter((g) => g.resolved).length;
  }

  function renderSummary() {
    document.getElementById("stat-orders").textContent = state.order_count;

    let totalGroups = 0, totalResolved = 0;
    ["sku", "branch", "customer"].forEach((type) => {
      const list = state.groups[type];
      const done = resolvedCount(list);
      document.getElementById(`stat-${type}`).textContent = `${done}/${list.length}`;
      document.getElementById(`tab-count-${type}`).textContent = list.length ? `(${done}/${list.length})` : "";
      totalGroups += list.length;
      totalResolved += done;
    });

    const readyOrders = state.orders.filter((o) => orderReadiness(o).ready).length;
    document.getElementById("tab-count-orders").textContent =
      state.orders.length ? `(${readyOrders}/${state.orders.length} ready)` : "";

    summaryRow.classList.remove("hidden");

    const progressEl = document.getElementById("overall-progress");
    const pct = totalGroups ? Math.round((totalResolved / totalGroups) * 100) : 0;
    document.getElementById("overall-progress-fill").style.width = pct + "%";
    document.getElementById("overall-progress-text").textContent =
      totalGroups ? `${totalResolved}/${totalGroups} groups resolved (${pct}%)` : "Nothing to resolve.";
    progressEl.classList.remove("hidden");
  }

  // ── Candidate normalization ──────────────────────────────────────────────
  // Search results (rgmc-bc-api's RGMC custom pages) and suggestion results
  // (rgmc-gcp-api, which reads BC's *standard* items API) name fields
  // differently for the same entity, so both are checked here.
  function normalizeCandidates(type, rawList) {
    return (rawList || []).map((c) => {
      if (type === "sku") {
        return {
          code: c.number,
          name: c.description || c.displayName || c.displayName2 || "",
          score: c.score, extra: null, raw: c,
        };
      }
      if (type === "branch") {
        // extra stays the raw customerNumber (used when saving the link, and to
        // auto-apply the matching customer link); extraLabel is what's shown.
        const custLabel = c.customerName ? `${c.customerName} (${c.customerNumber})` : c.customerNumber;
        return {
          code: c.code, name: c.name || "",
          score: c.score, extra: c.customerNumber, customerName: c.customerName || null,
          lookupCode: c.lookupCode || null,
          extraLabel: c.lookupCode ? `${custLabel} · lookup: ${c.lookupCode}` : custLabel,
          raw: c,
        };
      }
      // customer — RGMC's custom customers page uses customerNo/name
      return { code: c.customerNo, name: c.name || "", score: c.score, extra: null, raw: c };
    });
  }

  function resolvedPayload(type, candidate) {
    if (type === "sku") return { itemNo: candidate.code, description: candidate.name };
    if (type === "branch") return { customerNo: candidate.extra, shipToCode: candidate.code, name: candidate.name };
    return { customerNo: candidate.code, displayName: candidate.name };
  }

  async function saveOverride(type, key, resolved, resolvedBy) {
    const res = await fetch("/api/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, key, resolved, resolved_by: resolvedBy || "" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed");
    return data;
  }

  function findGroup(type, key) {
    const upper = (key || "").trim().toUpperCase();
    return state.groups[type].find((g) => g.key.trim().toUpperCase() === upper);
  }

  function applyResolvedFields(g, resolved, data) {
    g.resolved = resolved;
    g.resolved_by = data.resolved_by || "";
    g.resolved_at = data.resolved_at || "";
    g.override_id = data.id;
  }

  function resolvedDisplay(resolved) {
    if (!resolved) return "";
    if (resolved.itemNo) return `${resolved.itemNo} — ${resolved.description || ""}`;
    if (resolved.shipToCode) return `${resolved.shipToCode} (${resolved.customerNo}) — ${resolved.name || ""}`;
    if (resolved.customerNo) return `${resolved.customerNo} — ${resolved.displayName || ""}`;
    return JSON.stringify(resolved);
  }

  // ── Candidate list rendering ─────────────────────────────────────────────
  function renderCandidateList(container, type, candidates, onPick) {
    container.innerHTML = "";
    if (!candidates.length) {
      const empty = document.createElement("div");
      empty.className = "search-hint";
      empty.textContent = "No matches.";
      container.appendChild(empty);
      return;
    }
    const list = document.createElement("div");
    list.className = "candidate-list";
    candidates.forEach((c) => {
      const item = document.createElement("div");
      item.className = "candidate-item";
      const main = document.createElement("div");
      main.className = "candidate-main";
      const extraLabel = c.extraLabel || c.extra;
      main.innerHTML =
        `<div class="candidate-code">${escapeHtml(c.code || "")}</div>` +
        `<div class="candidate-name">${escapeHtml(c.name || "")}${extraLabel ? " · " + escapeHtml(extraLabel) : ""}</div>`;
      item.appendChild(main);
      if (typeof c.score === "number") {
        const score = document.createElement("span");
        score.className = "candidate-score";
        score.textContent = Math.round(c.score * 100) + "%";
        item.appendChild(score);
      }
      item.addEventListener("click", () => onPick(c));
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  // ── Group label / flags ───────────────────────────────────────────────────
  function groupDisplayLabel(type, group) {
    if (type === "branch") {
      const customer = group.customer_name || "—";
      const company = group.company_name || "—";
      return `${group.key} (${customer} - ${company})`;
    }
    return group.key;
  }

  function groupDescText(type, group) {
    const flags = [];
    if (type === "sku" && group.missing_sku) flags.push("no SKU code");
    if (type === "sku" && group.has_nonpositive_qty) flags.push("non-positive quantity");
    const flagText = flags.length ? `[${flags.join(", ")}] ` : "";
    return flagText + (group.description || "");
  }

  // ── One group row ────────────────────────────────────────────────────────
  function buildGroupRow(type, group) {
    const node = rowTemplate.content.cloneNode(true);
    const row = node.querySelector(".group-row");

    row.querySelector(".group-key").textContent = groupDisplayLabel(type, group);
    row.querySelector(".group-desc").textContent = groupDescText(type, group);
    if (type === "sku" && (group.missing_sku || group.has_nonpositive_qty)) {
      row.classList.add("group-flagged");
    }
    row.querySelector(".group-po-count").textContent =
      `${group.po_count} PO${group.po_count === 1 ? "" : "s"}`;
    row.querySelector(".group-po-refs").textContent = "POs: " + group.po_refs.join(", ");

    const resolvedBox = row.querySelector(".group-resolved");
    const linkPanel = row.querySelector(".group-link-panel");
    const toggleBtn = row.querySelector(".btn-link-toggle");
    const unlinkBtn = row.querySelector(".btn-unlink");

    function applyResolvedState(g) {
      if (g.resolved) {
        row.classList.add("is-resolved");
        resolvedBox.classList.remove("hidden");
        resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(g.resolved);
        resolvedBox.querySelector(".resolved-by").textContent =
          g.resolved_by ? `(by ${g.resolved_by}, ${g.resolved_at || ""})` : "";
        toggleBtn.textContent = "Change…";
      } else {
        row.classList.remove("is-resolved");
        resolvedBox.classList.add("hidden");
        toggleBtn.textContent = "Link…";
      }
    }
    applyResolvedState(group);

    toggleBtn.addEventListener("click", () => linkPanel.classList.toggle("hidden"));

    unlinkBtn.addEventListener("click", async () => {
      if (!group.override_id) return;
      unlinkBtn.disabled = true;
      try {
        await fetch(`/api/overrides/${encodeURIComponent(group.override_id)}`, { method: "DELETE" });
        group.resolved = null;
        group.resolved_by = null;
        group.override_id = null;
        applyResolvedState(group);
        renderOrdersPanel();
        renderSummary();
      } catch (e) {
        setStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function saveLink(candidate) {
      const resolved = resolvedPayload(type, candidate);
      try {
        const data = await saveOverride(type, group.key, resolved, "");
        applyResolvedFields(group, resolved, data);
        applyResolvedState(group);
        linkPanel.classList.add("hidden");

        // A ship-to address belongs to a specific BC customer — selecting one tells
        // us that customer too, so auto-apply it instead of making the user search
        // the Customers tab separately for the same information.
        if (type === "branch" && group.customer_name && candidate.extra) {
          const custResolved = { customerNo: candidate.extra, displayName: candidate.customerName || candidate.extra };
          try {
            const custData = await saveOverride("customer", group.customer_name, custResolved, "");
            const custGroup = findGroup("customer", group.customer_name);
            if (custGroup) {
              applyResolvedFields(custGroup, custResolved, custData);
              renderGroupsPanel("customer");
            }
          } catch (e) {
            setStatus("Branch linked, but auto-linking the customer failed: " + e.message, true);
          }
        }

        renderOrdersPanel(); // readiness may have changed
        renderSummary();
      } catch (e) {
        setStatus("Could not save link: " + e.message, true);
      }
    }

    // Suggestions (BC fuzzy match via rgmc-gcp-api) — sku/branch only.
    const suggestBtn = row.querySelector(".btn-suggest");
    const suggestStatus = row.querySelector(".suggest-status");
    const suggestResults = row.querySelector(".suggest-results");
    if (type === "customer") {
      suggestBtn.style.display = "none";
    } else {
      suggestBtn.addEventListener("click", async () => {
        suggestBtn.disabled = true;
        suggestStatus.textContent = "Loading…";
        suggestResults.innerHTML = "";
        try {
          const poRef = group.po_refs[0];
          const path = type === "sku" ? `/api/suggest/item/${encodeURIComponent(poRef)}`
                                       : `/api/suggest/shipto/${encodeURIComponent(poRef)}`;
          const res = await fetch(`${path}?company=${encodeURIComponent(state.company)}`);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Lookup failed");

          let raw = [];
          if (type === "sku") {
            const line = (data.lines || []).find((l) => (l.customerSKUCode || "").toUpperCase() === group.key.toUpperCase());
            raw = line ? line.fuzzyMatches : (data.lines[0] || {}).fuzzyMatches || [];
          } else {
            raw = data.fuzzyMatches || [];
          }
          suggestStatus.textContent = raw.length ? `${raw.length} suggestion(s)` : "No suggestions found.";
          renderCandidateList(suggestResults, type, normalizeCandidates(type, raw), saveLink);
        } catch (e) {
          suggestStatus.textContent = "";
          setStatus("Suggestion lookup failed: " + e.message, true);
        } finally {
          suggestBtn.disabled = false;
        }
      });
    }

    // Manual search (BC list/search via rgmc-bc-api).
    const searchInput = row.querySelector(".search-input");
    if (type === "branch") {
      searchInput.placeholder = "Search by ship-to name, code, or lookup code…";
    }
    const searchResults = row.querySelector(".search-results");
    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = searchInput.value.trim();
      if (term.length < 2) {
        searchResults.innerHTML = "";
        return;
      }
      searchTimer = setTimeout(async () => {
        try {
          const url = `/api/lookup/${LOOKUP_PATH[type]}?search=${encodeURIComponent(term)}&company=${encodeURIComponent(state.company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          renderCandidateList(searchResults, type, normalizeCandidates(type, data.data), saveLink);
        } catch (e) {
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    // History — past resolutions for this exact key, for reference (e.g. it was
    // resolved before under a different link, or by someone else).
    const historyToggle = row.querySelector(".btn-history-toggle");
    const historyResults = row.querySelector(".history-results");
    let historyLoaded = false;
    historyToggle.addEventListener("click", async () => {
      const nowHidden = historyResults.classList.toggle("hidden");
      if (nowHidden || historyLoaded) return;
      historyLoaded = true;
      historyResults.innerHTML = `<div class="search-hint">Loading…</div>`;
      try {
        const url = `/api/reference?type=${encodeURIComponent(type)}&key=${encodeURIComponent(group.key)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || "Lookup failed");
        const rows = data.data || [];
        if (!rows.length) {
          historyResults.innerHTML = `<div class="search-hint">No past resolutions for this key.</div>`;
          return;
        }
        historyResults.innerHTML = rows.map((h) => `
          <div class="history-item">
            <strong>${escapeHtml(resolvedDisplay(h.resolved))}</strong>
            — ${escapeHtml(h.resolved_at || "")}${h.resolved_by ? " by " + escapeHtml(h.resolved_by) : ""}
          </div>
        `).join("");
      } catch (e) {
        historyResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        historyLoaded = false;
      }
    });

    return row;
  }

  function renderGroupsPanel(type) {
    const panel = document.getElementById(`panel-${type}`);
    panel.innerHTML = "";
    const groups = state.groups[type];
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "Nothing to reconcile — no buffered orders reference this.";
      panel.appendChild(empty);
      return;
    }
    groups.forEach((g) => panel.appendChild(buildGroupRow(type, g)));
  }

  // ── Buffered Orders tab (readiness is informational only) ───────────────
  function groupResolvedFor(type, key) {
    const list = state.groups[type];
    const upper = (key || "").trim().toUpperCase();
    const g = list.find((x) => x.key.trim().toUpperCase() === upper);
    return !!(g && g.resolved);
  }

  // Per-order resolution progress — mirrors _group_buffer's key derivation
  // server-side so a blank-SKU line (keyed by description instead) still counts.
  function orderReadiness(order) {
    const header = order.header || {};
    const lines = order.lines || [];
    const branchOk = groupResolvedFor("branch", header.customerBranchName);
    const customerOk = groupResolvedFor("customer", header.customerName);
    const skuKeys = [...new Set(lines.map((l) => {
      const sku = (l.customerSKUCode || "").trim();
      if (sku) return sku;
      return (l.customerSKUDesc || "").trim() || "(no SKU code, no description)";
    }))];
    const skuResolved = skuKeys.filter((s) => groupResolvedFor("sku", s)).length;
    const skuOk = skuKeys.length === 0 || skuResolved === skuKeys.length;
    return {
      branchOk, customerOk, skuOk,
      skuResolved, skuTotal: skuKeys.length,
      ready: branchOk && customerOk && skuOk,
    };
  }

  function renderOrdersPanel() {
    const panel = document.getElementById("panel-orders");
    panel.innerHTML = "";
    if (!state.orders.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No buffered orders for this company.";
      panel.appendChild(empty);
      return;
    }
    state.orders.forEach((order) => {
      const header = order.header || {};
      const lines = order.lines || [];
      const r = orderReadiness(order);

      const row = document.createElement("div");
      row.className = "order-row";
      row.innerHTML = `
        <div class="order-row-head">
          <span class="order-ref">${escapeHtml(header.poRefNumber || order.id)}</span>
          <span class="order-badge ${r.ready ? "ready" : "pending"}">
            ${r.ready ? "All links resolved" : "Unresolved links remain"}
          </span>
        </div>
        <div class="order-detail">
          Customer: ${escapeHtml(header.customerName || "—")} &middot;
          Branch: ${escapeHtml(header.customerBranchName || "—")} &middot;
          ${lines.length} line(s) &middot; attempt ${order.attempt_count || 0}
          ${order._lines_recovered_from ? `<span class="recovered-badge">lines recovered from ${escapeHtml(order._lines_recovered_from === "cloudsql" ? "Cloud SQL" : "BigQuery")}</span>` : ""}
        </div>
        <div class="order-links">
          <span class="link-chip ${r.branchOk ? "ok" : "pending"}">Branch ${r.branchOk ? "✓" : "✗"}</span>
          <span class="link-chip ${r.customerOk ? "ok" : "pending"}">Customer ${r.customerOk ? "✓" : "✗"}</span>
          <span class="link-chip ${r.skuOk ? "ok" : "pending"}">Items ${r.skuResolved}/${r.skuTotal}</span>
        </div>
        <div class="order-error">${escapeHtml(order.last_error || "")}</div>
      `;
      panel.appendChild(row);
    });
  }

  // ── Tabs ──────────────────────────────────────────────────────────────────
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
      btn.classList.add("active");
      document.getElementById(`panel-${btn.dataset.tab}`).classList.remove("hidden");
    });
  });

  // ── Load buffer ───────────────────────────────────────────────────────────
  function renderAll() {
    renderSummary();
    renderGroupsPanel("sku");
    renderGroupsPanel("branch");
    renderGroupsPanel("customer");
    renderOrdersPanel();
    tabsCard.classList.remove("hidden");
    reprocessBtn.disabled = state.order_count === 0;
  }

  async function loadBuffer() {
    const company = companySelect.value;
    if (!company) {
      setStatus("Select a company first.", true);
      return;
    }
    loadBtn.disabled = true;
    setStatus("Loading buffer…");
    try {
      // Fast pass first — buffered orders as Firestore actually has them, so the
      // page renders immediately instead of waiting on line recovery.
      const res = await fetch(`/api/buffer?company=${encodeURIComponent(company)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load buffer");
      state = data;
      renderAll();
      setStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.`);

      // Background pass — recovers any missing lines from Cloud SQL/BigQuery (can
      // take a while per order under load) and re-renders once it's back, so the
      // SKU tab doesn't stay empty for orders buffered before the lines bug was fixed.
      const anyMissingLines = state.orders.some((o) => !o.lines || !o.lines.length);
      if (anyMissingLines) {
        setStatus(`Loaded ${state.order_count} order(s) for ${company} — recovering missing lines…`);
        fetch(`/api/buffer/lines?company=${encodeURIComponent(company)}`)
          .then((r) => r.json().then((d) => [r, d]))
          .then(([r, d]) => {
            if (!r.ok || company !== companySelect.value) return; // stale response, ignore
            state = d;
            renderAll();
            const n = state.orders.filter((o) => o._lines_recovered_from).length;
            setStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.` +
              (n ? ` Recovered lines for ${n} order(s).` : ""));
          })
          .catch(() => { /* best-effort — page already works with what it has */ });
      }
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      loadBtn.disabled = false;
    }
  }

  loadBtn.addEventListener("click", loadBuffer);

  reprocessBtn.addEventListener("click", async () => {
    if (!state) return;
    if (!confirm(`Trigger the reprocess-buffer retry for ${state.company}? This re-runs the normal buffer retry — it does not automatically apply the links saved above yet.`)) {
      return;
    }
    reprocessBtn.disabled = true;
    setStatus("Triggering reprocess…");
    try {
      const res = await fetch("/api/reprocess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: state.company }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Reprocess trigger failed");
      setStatus(`Reprocess triggered for ${state.company}. Reload the buffer in a minute to see results.`);
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      reprocessBtn.disabled = false;
    }
  });
})();
