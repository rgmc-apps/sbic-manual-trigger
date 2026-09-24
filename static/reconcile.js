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

  function renderSummary() {
    document.getElementById("stat-orders").textContent   = state.order_count;
    document.getElementById("stat-sku").textContent      = state.groups.sku.length;
    document.getElementById("stat-branch").textContent   = state.groups.branch.length;
    document.getElementById("stat-customer").textContent = state.groups.customer.length;
    summaryRow.classList.remove("hidden");
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
        // extra stays the raw customerNumber (used when saving the link);
        // extraLabel is what's actually shown, preferring the resolved name
        // (attached server-side for suggestions) so candidates are easy to tell apart.
        return {
          code: c.code, name: c.name || "",
          score: c.score, extra: c.customerNumber,
          extraLabel: c.customerName ? `${c.customerName} (${c.customerNumber})` : c.customerNumber,
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
      } catch (e) {
        setStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function saveLink(candidate) {
      const resolved = resolvedPayload(type, candidate);
      try {
        const res = await fetch("/api/overrides", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type, key: group.key, resolved, resolved_by: "" }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "Save failed");
        group.resolved = resolved;
        group.resolved_by = data.resolved_by || "";
        group.resolved_at = data.resolved_at || "";
        group.override_id = data.id;
        applyResolvedState(group);
        linkPanel.classList.add("hidden");
        renderOrdersPanel(); // readiness may have changed
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
          const res = await fetch(path);
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
      const branchOk = groupResolvedFor("branch", header.customerBranchName);
      const customerOk = groupResolvedFor("customer", header.customerName);
      // Mirror _group_buffer's key derivation server-side: a blank SKU code still
      // needs reconciling, keyed by description (or a fixed bucket) instead.
      const skuKeys = [...new Set(lines.map((l) => {
        const sku = (l.customerSKUCode || "").trim();
        if (sku) return sku;
        return (l.customerSKUDesc || "").trim() || "(no SKU code, no description)";
      }))];
      const skuOk = skuKeys.length === 0 || skuKeys.every((s) => groupResolvedFor("sku", s));
      const ready = branchOk && customerOk && skuOk;

      const row = document.createElement("div");
      row.className = "order-row";
      row.innerHTML = `
        <div class="order-row-head">
          <span class="order-ref">${escapeHtml(header.poRefNumber || order.id)}</span>
          <span class="order-badge ${ready ? "ready" : "pending"}">
            ${ready ? "All links resolved" : "Unresolved links remain"}
          </span>
        </div>
        <div class="order-detail">
          Customer: ${escapeHtml(header.customerName || "—")} &middot;
          Branch: ${escapeHtml(header.customerBranchName || "—")} &middot;
          ${lines.length} line(s) &middot; attempt ${order.attempt_count || 0}
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
  async function loadBuffer() {
    const company = companySelect.value;
    if (!company) {
      setStatus("Select a company first.", true);
      return;
    }
    loadBtn.disabled = true;
    setStatus("Loading buffer…");
    try {
      const res = await fetch(`/api/buffer?company=${encodeURIComponent(company)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load buffer");
      state = data;
      renderSummary();
      renderGroupsPanel("sku");
      renderGroupsPanel("branch");
      renderGroupsPanel("customer");
      renderOrdersPanel();
      tabsCard.classList.remove("hidden");
      reprocessBtn.disabled = state.order_count === 0;
      setStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.`);
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
