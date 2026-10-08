(function () {
  "use strict";

  const companySelect = document.getElementById("company-select");
  const cloudsqlCompanySelect = document.getElementById("cloudsql-company-select");
  const historyCompanySelect = document.getElementById("history-company-select");
  const historyOutcomeSelect = document.getElementById("history-outcome-select");
  const historyPoRefInput    = document.getElementById("history-po-ref-input");
  const historyLoadBtn       = document.getElementById("history-load-btn");
  const historyStatusLine    = document.getElementById("history-status-line");
  const historyListEl        = document.getElementById("history-list");
  const overridesTypeSelect    = document.getElementById("overrides-type-select");
  const overridesKeyInput      = document.getElementById("overrides-key-input");
  const overridesCompanySelect = document.getElementById("overrides-company-select");
  const overridesLoadBtn       = document.getElementById("overrides-load-btn");
  const overridesStatusLine    = document.getElementById("overrides-status-line");
  const overridesListEl        = document.getElementById("overrides-list");
  const overrideRowTemplate    = document.getElementById("override-row-template");
  const bqPoRefInput      = document.getElementById("bq-po-ref-input");
  const bqCustomerInput   = document.getElementById("bq-customer-input");
  const bqDateFrom        = document.getElementById("bq-date-from");
  const bqDateTo          = document.getElementById("bq-date-to");
  const bqSearchBtn       = document.getElementById("bq-search-btn");
  const bqStatusLine      = document.getElementById("bq-status-line");
  const bqResultsList     = document.getElementById("bq-results-list");
  const bqInsertAction    = document.getElementById("bq-insert-action");
  const bqInsertBtn       = document.getElementById("bq-insert-btn");
  const bqInsertResults   = document.getElementById("bq-insert-results");
  const bqReportBtn       = document.getElementById("bq-report-btn");
  const bqReportResult    = document.getElementById("bq-report-result");
  const documentAiRowTemplate = document.getElementById("document-ai-row-template");
  const soUpdateModalOverlay   = document.getElementById("so-update-modal-overlay");
  const soUpdateModalSubtitle  = document.getElementById("so-update-modal-subtitle");
  const soUpdateModalTbody     = document.getElementById("so-update-modal-tbody");
  const soUpdateSelectAll      = document.getElementById("so-update-select-all");
  const soUpdateModalClose     = document.getElementById("so-update-modal-close");
  const soUpdateModalCancel    = document.getElementById("so-update-modal-cancel");
  const soUpdateModalConfirm   = document.getElementById("so-update-modal-confirm");
  const loadBtn       = document.getElementById("load-btn");
  const reprocessBtn  = document.getElementById("reprocess-btn");
  const syncBtn       = document.getElementById("sync-btn");
  const backfillBtn   = document.getElementById("backfill-btn");
  const backfillDateFrom = document.getElementById("backfill-date-from");
  const backfillDateTo   = document.getElementById("backfill-date-to");
  const statusLine    = document.getElementById("status-line");
  const summaryRow    = document.getElementById("summary-row");
  const tabsCard      = document.getElementById("tabs-card");
  const rowTemplate   = document.getElementById("group-row-template");
  const skeletonBlock = document.getElementById("skeleton-block");
  const overallProgress = document.getElementById("overall-progress");
  const allResolvedHint = document.getElementById("all-resolved-hint");

  const reprocessStatusEl = document.getElementById("reprocess-status");
  const reprocessTitleEl  = document.getElementById("reprocess-status-title");
  const reprocessDetailEl = document.getElementById("reprocess-status-detail");
  const reprocessElapsedEl = document.getElementById("reprocess-elapsed");
  const stopWatchingBtn   = document.getElementById("stop-watching-btn");

  // ── Your Details gate ────────────────────────────────────────────────────
  // Required before the buffer data becomes usable, and sent along with a
  // reprocess trigger so rgmc-worker-pool can email this person the result.
  // Persisted in localStorage (this browser only) so it's pre-filled next time.
  const EMPLOYEE_STORAGE_KEY = "sbic_reconcile_employee_details";
  const gatedArea  = document.getElementById("gated-area");
  const gateHint   = document.getElementById("gate-hint");
  const employeeFields = {
    name:       document.getElementById("employee-name"),
    company:    document.getElementById("employee-company"),
    department: document.getElementById("employee-department"),
    email:      document.getElementById("employee-email"),
  };
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

  function loadEmployeeDetails() {
    try {
      const saved = JSON.parse(localStorage.getItem(EMPLOYEE_STORAGE_KEY) || "{}");
      Object.keys(employeeFields).forEach((key) => {
        if (saved[key]) employeeFields[key].value = saved[key];
      });
    } catch (e) { /* corrupt/blocked storage — start blank */ }
  }

  function saveEmployeeDetails() {
    try {
      const values = {};
      Object.keys(employeeFields).forEach((key) => { values[key] = employeeFields[key].value.trim(); });
      localStorage.setItem(EMPLOYEE_STORAGE_KEY, JSON.stringify(values));
    } catch (e) { /* storage unavailable — details just won't persist */ }
  }

  function employeeDetailsValid() {
    return (
      employeeFields.name.value.trim() &&
      employeeFields.company.value.trim() &&
      employeeFields.department.value.trim() &&
      EMAIL_RE.test(employeeFields.email.value.trim())
    );
  }

  function getEmployeeDetails() {
    return {
      employee_name: employeeFields.name.value.trim(),
      employee_company: employeeFields.company.value.trim(),
      employee_department: employeeFields.department.value.trim(),
      email: employeeFields.email.value.trim(),
    };
  }

  function refreshGate() {
    const valid = employeeDetailsValid();
    gatedArea.classList.toggle("is-locked", !valid);
    gateHint.classList.toggle("hidden", valid);
    return valid;
  }

  Object.values(employeeFields).forEach((el) => {
    el.addEventListener("input", () => {
      saveEmployeeDetails();
      refreshGate();
    });
  });

  loadEmployeeDetails();
  refreshGate();

  // ── Button loading spinner (caller still manages .disabled separately) ────
  function setBtnLoading(spinnerId, labelId, loading, loadingText) {
    const spinner = document.getElementById(spinnerId);
    const label = document.getElementById(labelId);
    spinner.classList.toggle("hidden", !loading);
    if (loading) {
      if (!label.dataset.origLabel) label.dataset.origLabel = label.textContent;
      label.textContent = loadingText;
    } else if (label.dataset.origLabel) {
      label.textContent = label.dataset.origLabel;
    }
  }

  const LOOKUP_PATH = { sku: "items", branch: "ship-to", customer: "customers" };

  // { company, order_count, orders, groups: { sku: [...], branch: [...], customer: [...] } }
  let state = null;

  // ── Shared motion helpers ────────────────────────────────────────────────
  // Reveal an element the first time it goes visible (state entry), never on
  // repeat re-renders — repeated reveal animation is decoration, not state.
  function revealOnce(el) {
    if (!el.classList.contains("hidden")) return;
    el.classList.remove("hidden");
    el.classList.add("reveal-in");
    el.addEventListener("animationend", () => el.classList.remove("reveal-in"), { once: true });
  }

  // Like revealOnce, but re-triggers the reveal animation every time the element
  // transitions from hidden to shown (not just the first time) — for state that can
  // legitimately toggle back and forth, like "everything's resolved" after an undo.
  function toggleReveal(el, show) {
    const isHidden = el.classList.contains("hidden");
    if (show) {
      if (isHidden) {
        el.classList.remove("hidden");
        el.classList.add("reveal-in");
        el.addEventListener("animationend", () => el.classList.remove("reveal-in"), { once: true });
      }
    } else if (!isHidden) {
      el.classList.add("hidden");
    }
  }

  // Set a stat's text and give it a brief pulse only when the value actually changed.
  function setStatText(id, newText) {
    const el = document.getElementById(id);
    newText = String(newText);
    if (el.textContent === newText) return;
    el.textContent = newText;
    el.classList.remove("stat-pulse");
    void el.offsetWidth; // restart the animation if it's mid-flight
    el.classList.add("stat-pulse");
  }

  // Brief opacity dip + recover on a region that just got new data in place
  // (background line-recovery pass, a live reprocess poll) — signals "this
  // just refreshed" without a jarring full-DOM flash.
  function refreshPulse(el) {
    el.classList.remove("data-refresh");
    void el.offsetWidth;
    el.classList.add("data-refresh");
  }

  // ── Status / summary ────────────────────────────────────────────────────
  function setStatus(msg, isError) {
    statusLine.textContent = msg || "";
    statusLine.classList.toggle("error", !!isError);
  }

  function resolvedCount(list) {
    return list.filter((g) => g.resolved).length;
  }

  function renderSummary() {
    setStatText("stat-orders", state.order_count);

    let totalGroups = 0, totalResolved = 0;
    ["sku", "branch", "customer"].forEach((type) => {
      const list = state.groups[type];
      const done = resolvedCount(list);
      setStatText(`stat-${type}`, `${done}/${list.length}`);
      document.getElementById(`tab-count-${type}`).textContent = list.length ? `(${done}/${list.length})` : "";
      totalGroups += list.length;
      totalResolved += done;
    });

    // Inactive items are excluded from the SKU resolved/total counts entirely — just
    // show how many are parked here, not a resolved/total ratio (there's nothing to
    // resolve for an inactive SKU).
    const inactiveList = state.groups.sku_inactive || [];
    document.getElementById("tab-count-sku-inactive").textContent = inactiveList.length ? `(${inactiveList.length})` : "";

    const readyOrders = state.orders.filter((o) => orderReadiness(o).ready).length;
    document.getElementById("tab-count-orders").textContent =
      state.orders.length ? `(${readyOrders}/${state.orders.length} ready)` : "";

    revealOnce(summaryRow);

    const pct = totalGroups ? Math.round((totalResolved / totalGroups) * 100) : 0;
    document.getElementById("overall-progress-fill").style.width = pct + "%";
    document.getElementById("overall-progress-text").textContent =
      totalGroups ? `${totalResolved}/${totalGroups} groups resolved (${pct}%)` : "Nothing to resolve.";
    revealOnce(overallProgress);

    const allResolved = totalGroups > 0 && totalResolved === totalGroups && state.order_count > 0;
    toggleReveal(allResolvedHint, allResolved);
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
      // customer — RGMC's custom customers page uses customerNo/name/address
      return { code: c.customerNo, name: c.name || "", address: c.address || "", score: c.score, extra: null, raw: c };
    });
  }

  function resolvedPayload(type, candidate) {
    if (type === "sku") return { itemNo: candidate.code, description: candidate.name };
    if (type === "branch") return { customerNo: candidate.extra, shipToCode: candidate.code, name: candidate.name };
    return { customerNo: candidate.code, displayName: candidate.name };
  }

  async function saveOverride(type, key, resolved, resolvedBy, bufferIds) {
    const res = await fetch("/api/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, key, resolved, resolved_by: resolvedBy || "", buffer_ids: bufferIds || [] }),
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

  // ── Suggestion fetch + link-apply (shared by the manual "Suggest" button and
  // the auto-resolve pass below) ───────────────────────────────────────────
  // BC fuzzy match via rgmc-gcp-api — sku/branch only, "customer" has no suggest
  // endpoint (a customer is always inferred via its branch's link instead).
  async function fetchSuggestionCandidates(type, group) {
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
    return normalizeCandidates(type, raw);
  }

  // Saves `candidate` as the resolved link for `group` and mutates `group` in place
  // (resolved/resolved_by/resolved_at/override_id) — no DOM access, so this is usable
  // both from a row's own "pick a candidate" handler and from a background pass with
  // no row rendered yet. A ship-to address belongs to a specific BC customer —
  // selecting one tells us that customer too, so the matching "customer" group is
  // auto-linked alongside it instead of making the user resolve the same information
  // twice. Returns { customerLinkError } rather than throwing on that secondary save,
  // since the branch link itself still succeeded.
  async function applyLinkToGroup(type, group, candidate, resolvedBy) {
    const resolved = resolvedPayload(type, candidate);
    const data = await saveOverride(type, group.key, resolved, resolvedBy, group.buffer_ids);
    applyResolvedFields(group, resolved, data);

    let customerLinkError = null;
    if (type === "branch" && group.customer_name && candidate.extra) {
      const custResolved = { customerNo: candidate.extra, displayName: candidate.customerName || candidate.extra };
      try {
        const custGroup = findGroup("customer", group.customer_name);
        const custData = await saveOverride(
          "customer", group.customer_name, custResolved, resolvedBy, custGroup ? custGroup.buffer_ids : []
        );
        if (custGroup) applyResolvedFields(custGroup, custResolved, custData);
      } catch (e) {
        customerLinkError = e.message;
      }
    }
    return { customerLinkError };
  }

  // ── Highlight high-confidence suggestions ─────────────────────────────────
  // fuzzy_match.py scores a containment match (one string fully inside the other) at
  // 0.9 — a fuzzy match at or above that is overwhelmingly a real match, not a
  // coincidence. This used to be auto-linked (and the link auto-triggered a
  // reprocess-buffer run) without any human in the loop; both of those were removed
  // because users found links appearing — and reprocess runs firing — with no action
  // of their own confusing. Now this only flags the group (group.highConfidenceMatch)
  // so buildGroupRow can badge it; a human still has to click "Link…" to apply it,
  // and still has to click "Reprocess Buffer" to send it to BC.
  const HIGH_CONFIDENCE_THRESHOLD = 0.9;

  async function highlightHighConfidenceMatches() {
    if (!state) return;
    const pending = [];
    ["sku", "branch"].forEach((type) => {
      state.groups[type].forEach((group) => {
        if (!group.resolved) pending.push({ type, group });
      });
    });
    if (!pending.length) return;

    let flaggedCount = 0;
    // Sequential, not parallel — each lookup re-fetches a full BC catalog (every
    // item, or every ship-to address) server-side, so firing all of them at once
    // would hit BC with one heavy request per unresolved group simultaneously.
    for (const { type, group } of pending) {
      if (group.resolved) continue;
      let ranked;
      try {
        ranked = await fetchSuggestionCandidates(type, group);
      } catch (e) {
        continue; // best-effort — leave it for the manual "Suggest" button instead
      }
      const top = ranked[0];
      if (!top || typeof top.score !== "number" || top.score < HIGH_CONFIDENCE_THRESHOLD) continue;
      group.highConfidenceMatch = top;
      flaggedCount++;
    }

    if (flaggedCount) {
      renderGroupsPanel("sku");
      renderGroupsPanel("branch");
      setStatus(
        `Loaded ${state.order_count} buffered order(s) for ${state.company}. ` +
        `${flaggedCount} group(s) have a high-confidence match (≥90%, highlighted below) — ` +
        `link them yourself, then trigger Reprocess Buffer when you're ready.`
      );
    }
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
      // Customer candidates show address inline ("name (address)") to help tell apart
      // same-named customers at different branches — everything else keeps the
      // existing "name · extraLabel" format (extraLabel/extra is branch/SKU-specific).
      const extraLabel = c.extraLabel || c.extra;
      const nameLine = c.address
        ? `${escapeHtml(c.name || "")} (${escapeHtml(c.address)})`
        : `${escapeHtml(c.name || "")}${extraLabel ? " · " + escapeHtml(extraLabel) : ""}`;
      main.innerHTML =
        `<div class="candidate-code">${escapeHtml(c.code || "")}</div>` +
        `<div class="candidate-name">${nameLine}</div>`;
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

  // A group can be "Linked" and still not reach BC — orderReadiness (below) is
  // per-order across all three link types, so linking this one group's key doesn't
  // mean every PO referencing it is actually ready. Returns null when every one of
  // this group's buffered orders is fully ready (nothing to warn about), otherwise
  // how many are still blocked and by which other link type(s).
  function groupBlockedSummary(group) {
    if (!state.orders || !state.orders.length || !group.buffer_ids || !group.buffer_ids.length) return null;
    const ids = new Set(group.buffer_ids);
    const orders = state.orders.filter((o) => ids.has(o.id));
    if (!orders.length) return null;

    const labelFor = { branch: "Branch", customer: "Customer", sku: "Items" };
    const needs = { branch: 0, customer: 0, sku: 0 };
    const blocked = []; // { poRef, labels } — which specific PO(s), and what each still needs
    orders.forEach((o) => {
      const r = orderReadiness(o);
      if (r.ready) return;
      const missing = [];
      if (!r.branchOk) { needs.branch++; missing.push(labelFor.branch); }
      if (!r.customerOk) { needs.customer++; missing.push(labelFor.customer); }
      if (!r.skuOk) { needs.sku++; missing.push(labelFor.sku); }
      if (missing.length) blocked.push({ poRef: (o.header || {}).poRefNumber || o.id, labels: missing });
    });
    if (!blocked.length) return null;

    const labels = Object.keys(needs).filter((k) => needs[k]).map((k) => labelFor[k]);
    if (!labels.length) return null;
    return { blockedCount: blocked.length, total: orders.length, labels, blocked };
  }

  // Renders groupBlockedSummary()'s per-PO detail onto its one-line message, e.g.
  // "1 of 1 PO still need Items linked before reaching BC: 21560265." — or, when
  // different POs in the same group are blocked for different reasons, each PO gets
  // its own reason: "PO123 (Items), PO456 (Branch, Customer)". Capped so one group
  // referencing dozens of POs doesn't turn into a wall of text.
  const BLOCKED_DETAIL_MAX = 5;
  function blockedSummaryText(blocked) {
    const sameEverywhere = blocked.blocked.every(
      (b) => b.labels.length === blocked.labels.length && b.labels.every((l) => blocked.labels.includes(l))
    );
    const shown = blocked.blocked.slice(0, BLOCKED_DETAIL_MAX);
    const parts = shown.map((b) => (sameEverywhere ? b.poRef : `${b.poRef} (${b.labels.join(", ")})`));
    if (blocked.blocked.length > BLOCKED_DETAIL_MAX) {
      parts.push(`+${blocked.blocked.length - BLOCKED_DETAIL_MAX} more`);
    }
    return (
      `${blocked.blockedCount} of ${blocked.total} PO${blocked.total === 1 ? "" : "s"} still need ` +
      `${blocked.labels.join(", ")} linked before reaching BC: ${parts.join(", ")}.`
    );
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
    const markInactiveBtn = row.querySelector(".btn-mark-inactive");

    if (type === "sku") {
      markInactiveBtn.classList.remove("hidden");
      markInactiveBtn.addEventListener("click", async () => {
        markInactiveBtn.disabled = true;
        try {
          const res = await fetch("/api/inactive-skus", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ key: group.key, marked_by: getEmployeeDetails().employee_name }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || "Could not mark inactive");

          const skuList = state.groups.sku;
          const idx = skuList.indexOf(group);
          if (idx !== -1) skuList.splice(idx, 1);
          group.inactive_id = data.id;
          group.inactive_marked_by = data.marked_by;
          group.inactive_marked_at = data.marked_at;
          state.groups.sku_inactive.push(group);

          renderGroupsPanel("sku");
          renderInactivePanel();
          renderGroupsPanel("branch"); // branch/customer blocked-notes may change — inactive SKUs no longer count against order readiness
          renderGroupsPanel("customer");
          renderOrdersPanel(); // readiness may have changed
          renderSummary();
          setStatus(`Marked "${group.key}" inactive.`);
        } catch (e) {
          setStatus("Could not mark inactive: " + e.message, true);
        } finally {
          markInactiveBtn.disabled = false;
        }
      });
    }

    const blockedNote = row.querySelector(".group-blocked-note");
    const confidenceBadge = row.querySelector(".group-confidence-badge");

    function applyResolvedState(g) {
      if (g.resolved) {
        row.classList.remove("has-high-confidence-match");
        confidenceBadge.classList.add("hidden");
        row.classList.add("is-resolved");
        resolvedBox.classList.remove("hidden");
        resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(g.resolved);
        resolvedBox.querySelector(".resolved-by").textContent =
          g.resolved_by ? `(by ${g.resolved_by}, ${g.resolved_at || ""})` : "";
        toggleBtn.textContent = "Change…";

        const blocked = groupBlockedSummary(g);
        if (blocked) {
          blockedNote.textContent = blockedSummaryText(blocked);
          blockedNote.classList.remove("hidden");
        } else {
          blockedNote.classList.add("hidden");
        }
      } else {
        row.classList.remove("is-resolved");
        resolvedBox.classList.add("hidden");
        blockedNote.classList.add("hidden");
        toggleBtn.textContent = "Link…";

        if (g.highConfidenceMatch) {
          row.classList.add("has-high-confidence-match");
          confidenceBadge.textContent = `✓ ${Math.round(g.highConfidenceMatch.score * 100)}% match found — link it, then Reprocess Buffer`;
          confidenceBadge.classList.remove("hidden");
        } else {
          row.classList.remove("has-high-confidence-match");
          confidenceBadge.classList.add("hidden");
        }
      }
    }
    applyResolvedState(group);

    toggleBtn.addEventListener("click", () => linkPanel.classList.toggle("is-open"));

    unlinkBtn.addEventListener("click", async () => {
      if (!group.override_id) return;
      unlinkBtn.disabled = true;
      try {
        await fetch(`/api/overrides/${encodeURIComponent(group.override_id)}`, { method: "DELETE" });
        group.resolved = null;
        group.resolved_by = null;
        group.override_id = null;
        applyResolvedState(group);
        refreshOtherGroupPanels(type); // other groups' blocked-notes may change now that this one is unresolved
        renderOrdersPanel();
        renderSummary();
      } catch (e) {
        setStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function saveLink(candidate) {
      linkPanel.classList.add("is-saving");
      try {
        const resolvedBy = getEmployeeDetails().employee_name;
        const { customerLinkError } = await applyLinkToGroup(type, group, candidate, resolvedBy);
        applyResolvedState(group);
        resolvedBox.classList.add("pop-in");
        resolvedBox.addEventListener("animationend", () => resolvedBox.classList.remove("pop-in"), { once: true });
        linkPanel.classList.remove("is-open");
        if (customerLinkError) {
          setStatus("Branch linked, but auto-linking the customer failed: " + customerLinkError, true);
        }

        refreshOtherGroupPanels(type); // other groups' blocked-notes may change now that this one resolved (also covers the auto-linked customer group, if any)
        renderOrdersPanel(); // readiness may have changed
        renderSummary();
      } catch (e) {
        setStatus("Could not save link: " + e.message, true);
      } finally {
        linkPanel.classList.remove("is-saving");
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
        suggestStatus.innerHTML = `<span class="btn-spinner"></span> Looking up BC…`;
        suggestResults.innerHTML = "";
        try {
          const candidates = await fetchSuggestionCandidates(type, group);
          suggestStatus.textContent = candidates.length ? `${candidates.length} suggestion(s)` : "No suggestions found.";
          renderCandidateList(suggestResults, type, candidates, saveLink);
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
        searchResults.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/${LOOKUP_PATH[type]}?search=${encodeURIComponent(term)}&company=${encodeURIComponent(state.company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          // Stale response guard — the debounce already waits 350ms, but a slow BC
          // round-trip can still resolve after the user has typed something newer.
          if (searchInput.value.trim() !== term) return;
          renderCandidateList(searchResults, type, normalizeCandidates(type, data.data), saveLink);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
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

  // A link saved/removed under one type can change whether OTHER types' groups are
  // still "blocked" (groupBlockedSummary) — e.g. linking a branch can resolve orders
  // that a SKU group's blocked-note was counting as unready. Refresh the two panels
  // the caller didn't just rebuild itself so their blocked notes stay in sync.
  function refreshOtherGroupPanels(exceptType) {
    ["sku", "branch", "customer"].forEach((t) => {
      if (t !== exceptType) renderGroupsPanel(t);
    });
  }

  // One-time "how do I resolve this" hint shown above the Items (SKU) list — the
  // tab most likely to need explaining, since it offers three different ways to
  // resolve a group (auto-link, pick a suggestion, or search) plus a fourth escape
  // hatch (Mark Inactive) that doesn't resolve it at all.
  function buildSkuResolveHint() {
    const hint = document.createElement("div");
    hint.className = "tab-resolve-hint";
    hint.innerHTML =
      `How to resolve a SKU ` +
      `<span class="info-tip" tabindex="0" data-tooltip="` +
      escapeHtml(
        `Each row is one raw SKU code (or description, if the code is blank) shared by one or more buffered POs. ` +
        `To resolve it: click "Show suggested matches" for BC's best fuzzy-matched items, ranked by confidence — ` +
        `picking one links it instantly. Rows badged "match found" have a 90%+ confidence match already — click ` +
        `"Link…" to apply it, since nothing is linked automatically. ` +
        `If none of the suggestions are right, type in the search box below them to find the exact BC item instead. ` +
        `If this SKU is discontinued or should never be ordered, use "Mark Inactive" to exclude it from ` +
        `reconciliation entirely — that's different from linking it, so it won't count toward any PO's readiness.`
      ) +
      `">ⓘ</span>`;
    return hint;
  }

  function renderGroupsPanel(type) {
    const panel = document.getElementById(`panel-${type}`);
    panel.innerHTML = "";
    if (type === "sku") panel.appendChild(buildSkuResolveHint());
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

  // ── Inactive Items tab — SKUs marked inactive, excluded from the SKU counts ──
  function renderInactivePanel() {
    const panel = document.getElementById("panel-sku-inactive");
    panel.innerHTML = "";
    const groups = state.groups.sku_inactive || [];
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No SKUs marked inactive.";
      panel.appendChild(empty);
      return;
    }
    groups.forEach((g) => panel.appendChild(buildInactiveRow(g)));
  }

  function buildInactiveRow(group) {
    const row = document.createElement("div");
    row.className = "group-row is-inactive";
    row.innerHTML = `
      <div class="group-head">
        <div class="group-key-wrap">
          <span class="group-key">${escapeHtml(group.key)}</span>
          <span class="group-desc">${escapeHtml(groupDescText("sku", group))}</span>
        </div>
        <div class="group-meta">
          <span class="group-po-count">${group.po_count} PO${group.po_count === 1 ? "" : "s"}</span>
          <button class="btn-reactivate" type="button">Reactivate</button>
        </div>
      </div>
      <div class="group-resolved inactive-marked-by">
        Marked inactive${group.inactive_marked_by ? ` by ${escapeHtml(group.inactive_marked_by)}` : ""}${group.inactive_marked_at ? ` (${escapeHtml(group.inactive_marked_at)})` : ""}
      </div>
      <div class="group-po-refs">POs: ${escapeHtml(group.po_refs.join(", "))}</div>
    `;
    row.querySelector(".btn-reactivate").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const res = await fetch(`/api/inactive-skus/${encodeURIComponent(group.inactive_id)}`, { method: "DELETE" });
        if (!res.ok && res.status !== 204) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.error || "Could not reactivate");
        }
        const inactiveList = state.groups.sku_inactive;
        const idx = inactiveList.indexOf(group);
        if (idx !== -1) inactiveList.splice(idx, 1);
        delete group.inactive_id;
        delete group.inactive_marked_by;
        delete group.inactive_marked_at;
        state.groups.sku.push(group);

        renderGroupsPanel("sku");
        renderInactivePanel();
        renderGroupsPanel("branch"); // branch/customer blocked-notes may change — this SKU counts against order readiness again
        renderGroupsPanel("customer");
        renderOrdersPanel(); // readiness may have changed
        renderSummary();
        setStatus(`Reactivated "${group.key}".`);
      } catch (err) {
        setStatus("Could not reactivate: " + err.message, true);
        btn.disabled = false;
      }
    });
    return row;
  }

  // ── Buffered Orders tab (readiness is informational only) ───────────────
  function groupResolvedFor(type, key) {
    const list = state.groups[type];
    const upper = (key || "").trim().toUpperCase();
    const g = list.find((x) => x.key.trim().toUpperCase() === upper);
    return !!(g && g.resolved);
  }

  // Inactive SKUs are excluded from readiness entirely (not "resolved", just not
  // counted) — an order whose only unresolved lines are inactive SKUs is ready.
  function isSkuInactive(key) {
    const upper = (key || "").trim().toUpperCase();
    return (state.groups.sku_inactive || []).some((g) => g.key.trim().toUpperCase() === upper);
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
    }))].filter((s) => !isSkuInactive(s));
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
  // Crossfade instead of an instant hide/show — the panels hold genuinely
  // different content (SKU vs branch vs customer vs orders), so this is a
  // state transition, not decoration.
  const TAB_EXIT_MS = 150;
  function activateTab(tabKey) {
    const targetBtn = document.querySelector(`.tab-btn[data-tab="${tabKey}"]`);
    const next = document.getElementById(`panel-${tabKey}`);
    if (!targetBtn || !next || targetBtn.classList.contains("active")) return;

    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    targetBtn.classList.add("active");

    const current = document.querySelector(".tab-panel:not(.hidden)");
    if (current && current !== next) {
      current.classList.add("tab-panel-exit");
      setTimeout(() => {
        current.classList.add("hidden");
        current.classList.remove("tab-panel-exit");
      }, TAB_EXIT_MS);
    }

    next.classList.remove("hidden");
    next.classList.add("tab-panel-enter");
    void next.offsetWidth; // force reflow so the enter state is registered before transitioning out of it
    requestAnimationFrame(() => next.classList.remove("tab-panel-enter"));
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  // ── Action tabs (Buffer vs Cloud SQL process controls) ───────────────────
  // Separate class names/selectors from the .tab-btn/.tab-panel system above —
  // these switch which *controls* are shown (Load Buffer/Reprocess vs Sync/Backfill),
  // not buffer content, and must not be managed by activateTab's generic
  // ".tab-panel:not(.hidden)" lookup, which would otherwise treat both tab groups as
  // one set. A plain instant swap is enough here — no crossfade needed for controls.
  function activateActionTab(tabKey) {
    const targetBtn = document.querySelector(`.action-tab-btn[data-action-tab="${tabKey}"]`);
    const next = document.getElementById(`action-panel-${tabKey}`);
    if (!targetBtn || !next || targetBtn.classList.contains("active")) return;

    document.querySelectorAll(".action-tab-btn").forEach((b) => b.classList.remove("active"));
    targetBtn.classList.add("active");
    document.querySelectorAll(".action-tab-panel").forEach((p) => p.classList.toggle("hidden", p !== next));
  }

  document.querySelectorAll(".action-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateActionTab(btn.dataset.actionTab));
  });

  // ── Load buffer ───────────────────────────────────────────────────────────
  function renderAll() {
    renderSummary();
    renderGroupsPanel("sku");
    renderInactivePanel();
    renderGroupsPanel("branch");
    renderGroupsPanel("customer");
    renderOrdersPanel();
    revealOnce(tabsCard);
    reprocessBtn.disabled = state.order_count === 0;
  }

  async function loadBuffer() {
    if (!refreshGate()) {
      setStatus("Fill in your details above first.", true);
      return;
    }
    const company = companySelect.value;
    if (!company) {
      setStatus("Select a company first.", true);
      return;
    }
    stopWatching(); // a fresh load already gives the current truth — no need to keep polling
    loadBtn.disabled = true;
    setBtnLoading("load-spinner", "load-btn-label", true, "Loading…");
    setStatus("Loading buffer…");
    const isFirstLoad = !state;
    if (isFirstLoad) skeletonBlock.classList.remove("hidden");
    try {
      // Fast pass first — buffered orders as Firestore actually has them, so the
      // page renders immediately instead of waiting on line recovery.
      const res = await fetch(`/api/buffer?company=${encodeURIComponent(company)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load buffer");
      state = data;
      skeletonBlock.classList.add("hidden");
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
            refreshPulse(tabsCard);
            const n = state.orders.filter((o) => o._lines_recovered_from).length;
            setStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.` +
              (n ? ` Recovered lines for ${n} order(s).` : ""));
          })
          .catch(() => { /* best-effort — page already works with what it has */ });
      }

      // Background pass — flags any SKU/branch group whose top BC fuzzy match is ≥90%
      // confident, so a human can spot near-identical spelling variants at a glance
      // instead of clicking "Suggest" on every row. Not awaited, same as the
      // lines-recovery pass above.
      highlightHighConfidenceMatches();
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      skeletonBlock.classList.add("hidden");
      loadBtn.disabled = false;
      setBtnLoading("load-spinner", "load-btn-label", false);
    }
  }

  loadBtn.addEventListener("click", loadBuffer);

  // ── Reprocess: trigger + watch ───────────────────────────────────────────
  // The trigger only kicks off an async worker-pool job. Primary signal: poll
  // rgmc-worker-pool's actual run status (ongoing/done/error), written to
  // Firestore as it processes the job and read back via rgmc-bc-api — real
  // state, not a guess. Falls back to inferring progress from the buffer count
  // only if the backend didn't hand back a run_id (e.g. not yet deployed).
  const RUN_POLL_MS = 4000;              // check run status every 4s — real work, not a long wait
  const BUFFER_POLL_MS = 20000;          // fallback mode: check the live buffer every 20s
  const MAX_WATCH_MS = 10 * 60 * 1000;   // auto-stop after 10 minutes either way
  let watcher = null; // { mode: "run"|"buffer-count", startedAt, runId?, baselineCount?, pollTimer, tickTimer }

  function formatElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function stopWatching(finalMessage, variant, detailMessage) {
    if (!watcher) return;
    clearInterval(watcher.tickTimer);
    clearTimeout(watcher.pollTimer);
    watcher = null;
    if (finalMessage) {
      reprocessStatusEl.classList.remove("is-success", "is-error", "is-stopped");
      if (variant) reprocessStatusEl.classList.add(variant);
      reprocessTitleEl.textContent = finalMessage;
      // detailMessage is the caller's final-state text (e.g. the run summary or error
      // string) — pass it in here rather than setting reprocessDetailEl separately
      // beforehand, since this used to unconditionally blank it back to "" right after.
      reprocessDetailEl.textContent = detailMessage || "";
      stopWatchingBtn.classList.add("hidden");
    } else {
      reprocessStatusEl.classList.add("hidden");
    }
  }

  function summarizeRun(summary) {
    if (!summary) return "";
    // Reprocess-buffer/Backfill report "orders_created"/"orders_failed"; Sync reports
    // "orders_synced"/"orders_not_found"/"orders_errored" instead — reading the wrong
    // shape silently showed "0 order(s) created" for every Sync run. Render whichever
    // shape this summary actually has.
    const parts = [];
    if ("orders_synced" in summary || "orders_not_found" in summary || "orders_errored" in summary) {
      parts.push(`${summary.orders_synced || 0} order(s) checked`);
      if (summary.orders_not_found) parts.push(`${summary.orders_not_found} not found in BC`);
      if (summary.orders_errored) parts.push(`${summary.orders_errored} errored (not necessarily missing — retry)`);
    } else {
      parts.push(`${summary.orders_created || 0} order(s) created`);
      if (summary.orders_failed) parts.push(`${summary.orders_failed} still failed`);
      if (summary.orders_skipped_existing) parts.push(`${summary.orders_skipped_existing} skipped (already in BC)`);
    }
    parts.push(`${summary.lines_created || 0} line(s) created`);
    if (summary.lines_skipped) parts.push(`${summary.lines_skipped} line(s) skipped`);
    if (summary.unmatched_items) parts.push(`${summary.unmatched_items} item(s) with no BC match`);
    return parts.join(", ") + ".";
  }

  // Silently re-fetch the buffer (no skeleton, no status-line spam) so the
  // table/groups/stats reflect what the run just changed.
  async function refreshBufferSilently() {
    if (!state) return;
    try {
      const res = await fetch(`/api/buffer?company=${encodeURIComponent(state.company)}`);
      const data = await res.json();
      if (!res.ok) return;
      state = data;
      renderAll();
      refreshPulse(tabsCard);
    } catch (e) { /* best-effort */ }
  }

  async function pollRunStatusOnce() {
    if (!watcher) return;
    try {
      const res = await fetch(`/api/reprocess-status/${encodeURIComponent(watcher.runId)}`);
      const data = await res.json();
      if (!watcher || !res.ok) return;

      if (data.status === "done") {
        stopWatching("Reprocessing done.", "is-success", summarizeRun(data.summary));
        refreshBufferSilently(); // numbers on screen (buffered POs, SKU/branch/customer counts) catch up to what this run just changed
        return;
      }
      if (data.status === "error") {
        stopWatching("Reprocessing failed.", "is-error", data.error || "Unknown error.");
        return;
      }
      // "queued" or "processing" — still ongoing.
      reprocessTitleEl.textContent = data.status === "queued"
        ? "Reprocessing triggered — waiting for the worker to pick it up…"
        : "Reprocessing in progress…";

      if (Date.now() - watcher.startedAt >= MAX_WATCH_MS) {
        stopWatching("Stopped auto-checking after 10 minutes — reload to see the latest.", "is-stopped");
        return;
      }
      watcher.pollTimer = setTimeout(pollRunStatusOnce, RUN_POLL_MS);
    } catch (e) {
      if (watcher) watcher.pollTimer = setTimeout(pollRunStatusOnce, RUN_POLL_MS); // transient hiccup — try again next tick
    }
  }

  async function pollBufferCountOnce() {
    if (!watcher) return;
    try {
      const res = await fetch(`/api/buffer?company=${encodeURIComponent(state.company)}`);
      const data = await res.json();
      if (!watcher || !res.ok) return;

      const changed = data.order_count !== state.order_count;
      state = data;
      renderAll();
      if (changed) refreshPulse(tabsCard);

      if (data.order_count === 0) {
        stopWatching("All buffered orders cleared.", "is-success");
        return;
      }
      if (changed) {
        const delta = watcher.baselineCount - data.order_count;
        reprocessDetailEl.textContent = delta > 0
          ? `${delta} order(s) cleared since you triggered this — ${data.order_count} still buffered.`
          : `Buffer count changed — ${data.order_count} order(s) currently buffered.`;
      }
      if (Date.now() - watcher.startedAt >= MAX_WATCH_MS) {
        stopWatching("Stopped auto-checking after 10 minutes — reload to see the latest.", "is-stopped");
        return;
      }
      watcher.pollTimer = setTimeout(pollBufferCountOnce, BUFFER_POLL_MS);
    } catch (e) {
      if (watcher) watcher.pollTimer = setTimeout(pollBufferCountOnce, BUFFER_POLL_MS); // transient hiccup — try again next tick
    }
  }

  function startWatching(runId, baselineCount) {
    stopWatching();
    const mode = runId ? "run" : "buffer-count";
    watcher = { mode, startedAt: Date.now(), runId, baselineCount, pollTimer: null, tickTimer: null };
    reprocessStatusEl.classList.remove("hidden", "is-success", "is-error", "is-stopped");
    reprocessTitleEl.textContent = "Reprocessing triggered — watching for results…";
    reprocessDetailEl.textContent = `You'll also get an email at ${employeeFields.email.value.trim()}.`;
    stopWatchingBtn.classList.remove("hidden");
    reprocessElapsedEl.textContent = "0:00";
    watcher.tickTimer = setInterval(() => {
      if (watcher) reprocessElapsedEl.textContent = formatElapsed(Date.now() - watcher.startedAt);
    }, 1000);
    if (mode === "run") {
      watcher.pollTimer = setTimeout(pollRunStatusOnce, RUN_POLL_MS);
    } else {
      watcher.pollTimer = setTimeout(pollBufferCountOnce, BUFFER_POLL_MS);
    }
  }

  stopWatchingBtn.addEventListener("click", () => stopWatching());

  reprocessBtn.addEventListener("click", async () => {
    if (!state) return;
    if (!refreshGate()) {
      setStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Trigger the reprocess-buffer retry for ${state.company}? Any links saved above are applied first, ahead of automatic matching. You'll get an email at ${employeeFields.email.value.trim()} with the result.`)) {
      return;
    }
    reprocessBtn.disabled = true;
    setBtnLoading("reprocess-spinner", "reprocess-btn-label", true, "Reprocessing…");
    setStatus("Triggering reprocess…");
    try {
      const res = await fetch("/api/reprocess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: state.company, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Reprocess trigger failed");
      setStatus(`Reprocess triggered for ${state.company}.`);
      startWatching(data.run_id || null, state.order_count);
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("reprocess-spinner", "reprocess-btn-label", false);
      reprocessBtn.disabled = false;
    }
  });

  // Sync from Cloud SQL / Backfill from Cloud SQL don't need a loaded buffer — just
  // a company, picked from their own dropdown on the Cloud SQL tab, independent of
  // the Buffer tab's selection — so they're enabled independently of `state`.
  cloudsqlCompanySelect.addEventListener("change", () => {
    syncBtn.disabled = !cloudsqlCompanySelect.value;
    backfillBtn.disabled = !cloudsqlCompanySelect.value;
  });

  syncBtn.addEventListener("click", async () => {
    const company = cloudsqlCompanySelect.value;
    if (!company) {
      setStatus("Select a company first.", true);
      return;
    }
    if (!refreshGate()) {
      setStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Sync inserted orders for ${company} from Cloud SQL? This backfills missing lines onto BC orders already created for CustomerPOUL rows (createBy='trigger') — any line that still can't be matched gets buffered for reconciliation. You'll get an email at ${employeeFields.email.value.trim()} with the result.`)) {
      return;
    }
    syncBtn.disabled = true;
    setBtnLoading("sync-spinner", "sync-btn-label", true, "Syncing…");
    setStatus("Triggering Cloud SQL sync…");
    try {
      const res = await fetch("/api/sync-inserted-orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Sync trigger failed");
      setStatus(`Cloud SQL sync triggered for ${company}.`);
      startWatching(data.run_id || null, state && state.company === company ? state.order_count : 0);
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("sync-spinner", "sync-btn-label", false);
      syncBtn.disabled = false;
    }
  });

  backfillBtn.addEventListener("click", async () => {
    const company = cloudsqlCompanySelect.value;
    if (!company) {
      setStatus("Select a company first.", true);
      return;
    }
    if (!refreshGate()) {
      setStatus("Fill in your details above first.", true);
      return;
    }
    const dateFrom = backfillDateFrom.value;
    const dateTo = backfillDateTo.value;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      setStatus("The From date must be on or before the To date.", true);
      return;
    }
    const rangeText = dateFrom || dateTo
      ? ` (${dateFrom || "earliest"} through ${dateTo || "latest"})`
      : " (no date range — every createBy='trigger' row)";
    if (!confirm(`Backfill BC sales orders for ${company} from Cloud SQL${rangeText}? This creates a BC order for any CustomerPOUL row not already in BC — anything unresolved gets buffered for reconciliation. You'll get an email at ${employeeFields.email.value.trim()} with the result.`)) {
      return;
    }
    backfillBtn.disabled = true;
    setBtnLoading("backfill-spinner", "backfill-btn-label", true, "Backfilling…");
    setStatus("Triggering Cloud SQL backfill…");
    try {
      const res = await fetch("/api/backfill-from-cloudsql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, date_from: dateFrom, date_to: dateTo, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Backfill trigger failed");
      setStatus(`Cloud SQL backfill triggered for ${company}.`);
      startWatching(data.run_id || null, state && state.company === company ? state.order_count : 0);
    } catch (e) {
      setStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("backfill-spinner", "backfill-btn-label", false);
      backfillBtn.disabled = false;
    }
  });

  // ── History tab (buffer-reconciliation log) ──────────────────────────────
  // Read-only log of every PO a Reprocess Buffer run has touched (so_buffer_history_{env},
  // written by rgmc-worker-pool) — distinct from the sku/branch/customer "Past
  // resolutions" history above, which is about override links, not PO attempts.
  const OUTCOME_LABEL = { resolved: "Resolved", still_buffered: "Still buffered", failed: "Failed" };
  const OUTCOME_BADGE_CLASS = { resolved: "ready", still_buffered: "pending", failed: "failed" };

  function setHistoryStatus(msg, isError) {
    historyStatusLine.textContent = msg || "";
    historyStatusLine.classList.toggle("error", !!isError);
  }

  function formatHistoryDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  function renderHistoryLines(lines) {
    if (!lines || !lines.length) return '<p class="tab-empty">No line details recorded.</p>';
    const rows = lines.map((l) => `
      <tr>
        <td>${escapeHtml(l.customerSKUCode || "—")}</td>
        <td>${escapeHtml(l.customerSKUDesc || "—")}</td>
        <td>${escapeHtml(l.poQty || l.poQtyPcs || "—")}</td>
      </tr>
    `).join("");
    return `
      <table class="history-lines-table">
        <thead><tr><th>SKU</th><th>Description</th><th>Qty</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function renderHistory(records) {
    historyListEl.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No reconciliation history found for these filters.";
      historyListEl.appendChild(empty);
      return;
    }
    records.forEach((rec) => {
      const header = rec.header || {};
      const lines = rec.lines || [];
      const triggeredBy = rec.triggered_by || {};
      const badgeClass = OUTCOME_BADGE_CLASS[rec.outcome] || "pending";
      const badgeLabel = OUTCOME_LABEL[rec.outcome] || rec.outcome || "Unknown";
      const whoBits = [triggeredBy.department, triggeredBy.company].filter(Boolean).join(", ");

      const row = document.createElement("div");
      row.className = "order-row history-row";
      row.innerHTML = `
        <div class="order-row-head">
          <span class="order-ref">${escapeHtml(rec.po_ref || "—")}</span>
          <span class="order-badge ${badgeClass}">${escapeHtml(badgeLabel)}</span>
        </div>
        <div class="order-detail">
          Company: ${escapeHtml(rec.company || "—")} &middot;
          Customer: ${escapeHtml(header.customerName || "—")} &middot;
          Branch: ${escapeHtml(header.customerBranchName || "—")} &middot;
          ${lines.length} line(s)${rec.so_number ? ` &middot; BC ${escapeHtml(rec.so_number)}` : ""}
        </div>
        <div class="history-meta">
          Triggered by ${escapeHtml(triggeredBy.name || "—")}${triggeredBy.email ? ` &lt;${escapeHtml(triggeredBy.email)}&gt;` : ""}${whoBits ? ` (${escapeHtml(whoBits)})` : ""}
          on ${formatHistoryDate(rec.triggered_at)}
        </div>
        ${rec.detail ? `<div class="order-error">${escapeHtml(rec.detail)}</div>` : ""}
        <button class="btn-history-toggle" type="button">Show PO details</button>
        <div class="history-details hidden"></div>
      `;
      const toggleBtn = row.querySelector(".btn-history-toggle");
      const detailsEl = row.querySelector(".history-details");
      toggleBtn.addEventListener("click", () => {
        const show = detailsEl.classList.contains("hidden");
        if (show && !detailsEl.dataset.rendered) {
          detailsEl.innerHTML = renderHistoryLines(lines);
          detailsEl.dataset.rendered = "1";
        }
        detailsEl.classList.toggle("hidden", !show);
        toggleBtn.textContent = show ? "Hide PO details" : "Show PO details";
      });
      historyListEl.appendChild(row);
    });
  }

  async function loadHistory() {
    if (!refreshGate()) {
      setHistoryStatus("Fill in your details above first.", true);
      return;
    }
    const params = new URLSearchParams();
    if (historyCompanySelect.value) params.set("company", historyCompanySelect.value);
    if (historyOutcomeSelect.value) params.set("outcome", historyOutcomeSelect.value);
    if (historyPoRefInput.value.trim()) params.set("po_ref", historyPoRefInput.value.trim());

    historyLoadBtn.disabled = true;
    setBtnLoading("history-load-spinner", "history-load-btn-label", true, "Loading…");
    setHistoryStatus("Loading history…");
    try {
      const res = await fetch(`/api/history?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load history");
      renderHistory(data.data || []);
      setHistoryStatus(`Loaded ${data.total != null ? data.total : (data.data || []).length} history record(s).`);
    } catch (e) {
      setHistoryStatus("Error: " + e.message, true);
    } finally {
      historyLoadBtn.disabled = false;
      setBtnLoading("history-load-spinner", "history-load-btn-label", false);
    }
  }

  historyLoadBtn.addEventListener("click", loadHistory);
  historyPoRefInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadHistory();
  });

  // ── Overrides tab (full link registry, independent of any one loaded buffer) ────
  // Lists every so_buffer_overrides_{env} doc regardless of whether its key still has
  // anything buffered — the Buffer tab above only shows a link next to a group that's
  // currently unresolved, so a key that's since been fully reprocessed (no buffer docs
  // left referencing it) would otherwise be invisible. Editing here is the same upsert
  // POST /api/overrides every group row already uses (same (type,key) → overwrite).
  //
  // "Find previous orders" is read-only: it searches BC directly (GET /bc/sales-orders
  // ?customer_no=) for Sales Orders already created under a given customer — it never
  // changes an existing order. There's no code path anywhere in this system that edits
  // Sell-to Customer No. on an already-created order; that's deliberately left to a
  // human doing it in BC itself, since BC's own behavior once a header has lines isn't
  // something this page has ever exercised.
  const OVERRIDE_TYPE_LABEL = { sku: "SKU", branch: "Branch", customer: "Customer" };

  function setOverridesStatus(msg, isError) {
    overridesStatusLine.textContent = msg || "";
    overridesStatusLine.classList.toggle("error", !!isError);
  }

  // Restricted server-side to submittedBy=="SBIC AI Uploading" (rgmc-worker-pool's own
  // automated SO-import orders) — see _find_orders_by_customer in app.py. A human-created
  // BC order sharing the same (wrong) customer number is never matched or touched.
  async function findPreviousOrders(customerNo, statusEl, resultsEl, ov) {
    const company = overridesCompanySelect.value;
    resultsEl.classList.add("hidden");
    resultsEl.innerHTML = "";
    if (!company) {
      statusEl.textContent = "Select a company above first, then try again.";
      statusEl.classList.add("error");
      statusEl.classList.remove("hidden");
      return;
    }
    statusEl.textContent = `Searching ${company} for Sales Orders under customer ${customerNo} (submittedBy = "SBIC AI Uploading" only)…`;
    statusEl.classList.remove("hidden", "error");
    try {
      const res = await fetch(`/api/sales-orders?company=${encodeURIComponent(company)}&customer_no=${encodeURIComponent(customerNo)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Lookup failed");
      const orders = data.data || [];
      if (!orders.length) {
        statusEl.textContent = `No existing Sales Orders found under customer ${customerNo} in ${company} (submittedBy = "SBIC AI Uploading").`;
        return;
      }
      statusEl.textContent =
        `${orders.length} existing Sales Order(s) in ${company} are still under customer ${customerNo} — ` +
        `not yet changed.`;
      const newCustomerNo = ov && ov.type !== "sku" ? (ov.resolved || {}).customerNo : null;
      resultsEl.innerHTML = orders.map((o) => `
        <div class="order-row">
          <div class="order-row-head">
            <span class="order-ref">${escapeHtml(o.number || "—")}</span>
            <span class="order-badge pending">${escapeHtml(o.sellToCustomerNo || customerNo)}</span>
          </div>
          <div class="order-detail">
            PO ref: ${escapeHtml(o.externalDocumentNo || "—")} &middot;
            Order date: ${escapeHtml(o.orderDate || "—")}
          </div>
        </div>
      `).join("");
      if (newCustomerNo && newCustomerNo !== customerNo) {
        const actionBox = document.createElement("div");
        actionBox.className = "override-update-action";
        const newLabel = ov.resolved.shipToCode ? `${newCustomerNo} (ship-to ${ov.resolved.shipToCode})` : newCustomerNo;
        actionBox.innerHTML = `
          <button class="btn-reprocess btn-update-orders" type="button">Review &amp; update to ${escapeHtml(newLabel)}…</button>
          <div class="override-update-hint">Opens a confirmation listing every order below — pick which ones to update.</div>
        `;
        resultsEl.appendChild(actionBox);
        actionBox.querySelector(".btn-update-orders").addEventListener("click", () => {
          openSalesOrderUpdateModal({ orders, company, oldCustomerNo: customerNo, ov, resultsEl });
        });
      }
      resultsEl.classList.remove("hidden");
    } catch (e) {
      statusEl.textContent = "Could not search Business Central: " + e.message;
      statusEl.classList.add("error");
    }
  }

  // ── Sales Order update confirmation modal ────────────────────────────────────
  // Lets the user see exactly which orders a correction would touch — and uncheck any
  // they don't want — before anything is PATCHed. One shared modal instance, populated
  // fresh each time "Review & update…" is clicked on any override row.
  let soUpdateContext = null; // { company, oldCustomerNo, ov, resultsEl }

  function soUpdateCheckboxes() {
    return [...soUpdateModalTbody.querySelectorAll(".so-update-row-check")];
  }

  function refreshSoUpdateConfirmState() {
    const checks = soUpdateCheckboxes();
    const checkedCount = checks.filter((c) => c.checked).length;
    soUpdateModalConfirm.disabled = checkedCount === 0;
    soUpdateModalConfirm.textContent = `Update Selected (${checkedCount})`;
    soUpdateSelectAll.checked = checks.length > 0 && checkedCount === checks.length;
    soUpdateSelectAll.indeterminate = checkedCount > 0 && checkedCount < checks.length;
  }

  function openSalesOrderUpdateModal({ orders, company, oldCustomerNo, ov, resultsEl }) {
    soUpdateContext = { company, oldCustomerNo, ov, resultsEl };
    const newCustomerNo = ov.resolved.customerNo;
    const newLabel = ov.resolved.shipToCode ? `${newCustomerNo} (ship-to ${ov.resolved.shipToCode})` : newCustomerNo;
    soUpdateModalSubtitle.textContent =
      `${company} — ${orders.length} order(s) currently under customer ${oldCustomerNo}. ` +
      `Orders left checked below will be changed to ${newLabel}.`;
    soUpdateModalTbody.innerHTML = orders.map((o) => `
      <tr>
        <td class="modal-check-col"><input type="checkbox" class="so-update-row-check" data-order-id="${escapeHtml(o.id || "")}" checked /></td>
        <td>${escapeHtml(o.number || "—")}</td>
        <td>${escapeHtml(o.sellToCustomerNo || oldCustomerNo)}</td>
        <td>${escapeHtml(o.externalDocumentNo || "—")}</td>
        <td>${escapeHtml(o.orderDate || "—")}</td>
      </tr>
    `).join("");
    refreshSoUpdateConfirmState();
    soUpdateModalOverlay.classList.remove("hidden");
  }

  function closeSalesOrderUpdateModal() {
    soUpdateModalOverlay.classList.add("hidden");
    soUpdateContext = null;
  }

  soUpdateModalClose.addEventListener("click", closeSalesOrderUpdateModal);
  soUpdateModalCancel.addEventListener("click", closeSalesOrderUpdateModal);
  soUpdateModalOverlay.addEventListener("click", (e) => {
    if (e.target === soUpdateModalOverlay) closeSalesOrderUpdateModal();
  });
  soUpdateModalTbody.addEventListener("change", (e) => {
    if (e.target.classList.contains("so-update-row-check")) refreshSoUpdateConfirmState();
  });
  soUpdateSelectAll.addEventListener("change", () => {
    soUpdateCheckboxes().forEach((c) => { c.checked = soUpdateSelectAll.checked; });
    refreshSoUpdateConfirmState();
  });

  soUpdateModalConfirm.addEventListener("click", async () => {
    if (!soUpdateContext) return;
    const { company, oldCustomerNo, ov, resultsEl } = soUpdateContext;
    if (!refreshGate() || !employeeDetailsValid()) {
      setOverridesStatus("Fill in valid details (including email) above first — the result gets emailed there.", true);
      return;
    }
    const selectedIds = soUpdateCheckboxes().filter((c) => c.checked).map((c) => c.dataset.orderId).filter(Boolean);
    if (!selectedIds.length) return;

    soUpdateModalConfirm.disabled = true;
    const origLabel = soUpdateModalConfirm.textContent;
    soUpdateModalConfirm.textContent = "Updating…";
    try {
      const employee = getEmployeeDetails();
      const res = await fetch("/api/sales-orders/update-customer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          company,
          old_customer_no: oldCustomerNo,
          new_resolved: ov.resolved,
          selected_ids: selectedIds,
          override_key: ov.key,
          override_type: ov.type,
          requested_by_email: employee.email,
          requested_by_name: employee.employee_name,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Update failed");
      const results = data.data || [];
      let updateResultsEl = resultsEl.querySelector(".override-update-results");
      if (!updateResultsEl) {
        updateResultsEl = document.createElement("div");
        updateResultsEl.className = "override-update-results";
        resultsEl.appendChild(updateResultsEl);
      }
      updateResultsEl.innerHTML = results.map((r) => `
        <div class="update-result-row ${r.ok ? "ok" : "fail"}">
          <span class="order-ref">${escapeHtml(r.number || "—")}</span>
          <span>${r.ok ? "✅ Updated" : "❌ " + escapeHtml(r.detail || "Failed")}</span>
        </div>
      `).join("");
      updateResultsEl.classList.remove("hidden");
      // Reload BEFORE setting the final status — loadOverrides() ends with its own
      // "Loaded N saved link(s)" status, which would otherwise immediately overwrite
      // the more specific message below.
      if (data.followup_created) await loadOverrides();
      setOverridesStatus(
        `Updated ${data.updated}/${data.total} Sales Order(s) in ${company}. ` +
        `An email with the full result was sent to ${employee.email}.` +
        (data.followup_created
          ? ` A follow-up override was added to this list tracking the order(s) still stuck under ${oldCustomerNo}.`
          : "")
      );
      closeSalesOrderUpdateModal();
    } catch (e) {
      setOverridesStatus("Could not update Sales Orders: " + e.message, true);
    } finally {
      soUpdateModalConfirm.disabled = false;
      soUpdateModalConfirm.textContent = origLabel;
    }
  });

  function buildOverrideRow(ov) {
    const node = overrideRowTemplate.content.cloneNode(true);
    const row = node.querySelector(".override-row");
    const findBtn = row.querySelector(".btn-find-orders");
    const unlinkBtn = row.querySelector(".btn-unlink");
    const toggleBtn = row.querySelector(".btn-link-toggle");
    const linkPanel = row.querySelector(".group-link-panel");
    const resolvedBox = row.querySelector(".group-resolved");
    const findStatusEl = row.querySelector(".override-find-orders-status");
    const findResultsEl = row.querySelector(".override-find-orders-results");

    row.querySelector(".override-type-badge").textContent = OVERRIDE_TYPE_LABEL[ov.type] || ov.type;
    row.querySelector(".group-key").textContent = ov.key;
    resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(ov.resolved);
    resolvedBox.querySelector(".resolved-by").textContent =
      ov.resolved_by ? `(by ${ov.resolved_by}, ${ov.resolved_at || ""})` : "";

    // Mutable, not a const snapshot — an edit below updates this so the button and the
    // auto-triggered post-edit search always act on the CURRENT vs. the just-replaced
    // customer number, not whatever was true when the row was first built.
    let currentCustomerNo = ov.type === "sku" ? null : (ov.resolved || {}).customerNo;
    if (currentCustomerNo) findBtn.classList.remove("hidden");
    findBtn.addEventListener("click", () => findPreviousOrders(currentCustomerNo, findStatusEl, findResultsEl, ov));

    unlinkBtn.addEventListener("click", async () => {
      if (!ov.id) return;
      unlinkBtn.disabled = true;
      try {
        await fetch(`/api/overrides/${encodeURIComponent(ov.id)}`, { method: "DELETE" });
        row.remove();
        setOverridesStatus(`Removed the link for "${ov.key}".`);
      } catch (e) {
        setOverridesStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    toggleBtn.addEventListener("click", () => linkPanel.classList.toggle("is-open"));

    const searchInput = row.querySelector(".search-input");
    const searchResults = row.querySelector(".search-results");
    if (ov.type === "branch") searchInput.placeholder = "Search by ship-to name, code, or lookup code…";
    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = searchInput.value.trim();
      if (term.length < 2) {
        searchResults.innerHTML = "";
        return;
      }
      searchTimer = setTimeout(async () => {
        const company = overridesCompanySelect.value;
        if (!company) {
          searchResults.innerHTML = `<div class="search-hint">Select a company above first.</div>`;
          return;
        }
        searchResults.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/${LOOKUP_PATH[ov.type]}?search=${encodeURIComponent(term)}&company=${encodeURIComponent(company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          if (searchInput.value.trim() !== term) return; // stale response guard
          renderCandidateList(searchResults, ov.type, normalizeCandidates(ov.type, data.data), async (candidate) => {
            const oldCustomerNo = currentCustomerNo;
            linkPanel.classList.add("is-saving");
            try {
              const resolvedBy = getEmployeeDetails().employee_name;
              const resolved = resolvedPayload(ov.type, candidate);
              const data = await saveOverride(ov.type, ov.key, resolved, resolvedBy, []);
              ov.resolved = resolved;
              ov.resolved_by = data.resolved_by || "";
              ov.resolved_at = data.resolved_at || "";
              ov.id = data.id;
              resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(ov.resolved);
              resolvedBox.querySelector(".resolved-by").textContent =
                ov.resolved_by ? `(by ${ov.resolved_by}, ${ov.resolved_at || ""})` : "";
              linkPanel.classList.remove("is-open");
              setOverridesStatus(`Updated the link for "${ov.key}".`);

              currentCustomerNo = ov.type === "sku" ? null : resolved.customerNo;
              if (currentCustomerNo) findBtn.classList.remove("hidden");
              if (oldCustomerNo && currentCustomerNo && oldCustomerNo !== currentCustomerNo) {
                // The link just changed — show what's now stale under the OLD value
                // first, since that's the actionable list (the current value's own
                // orders, if any, are presumably already correct).
                await findPreviousOrders(oldCustomerNo, findStatusEl, findResultsEl, ov);
              }
            } catch (e) {
              setOverridesStatus("Could not save link: " + e.message, true);
            } finally {
              linkPanel.classList.remove("is-saving");
            }
          });
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    return row;
  }

  function renderOverrides(overrides) {
    overridesListEl.innerHTML = "";
    if (!overrides.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No saved links found for these filters.";
      overridesListEl.appendChild(empty);
      return;
    }
    const sorted = [...overrides].sort((a, b) =>
      a.type === b.type ? a.key.localeCompare(b.key) : a.type.localeCompare(b.type)
    );
    sorted.forEach((ov) => overridesListEl.appendChild(buildOverrideRow(ov)));
  }

  async function loadOverrides() {
    if (!refreshGate()) {
      setOverridesStatus("Fill in your details above first.", true);
      return;
    }
    const params = new URLSearchParams();
    if (overridesTypeSelect.value) params.set("type", overridesTypeSelect.value);

    overridesLoadBtn.disabled = true;
    setBtnLoading("overrides-load-spinner", "overrides-load-btn-label", true, "Loading…");
    setOverridesStatus("Loading overrides…");
    try {
      const res = await fetch(`/api/overrides?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load overrides");
      let overrides = data.data || [];
      const keyFilter = overridesKeyInput.value.trim().toUpperCase();
      if (keyFilter) overrides = overrides.filter((ov) => (ov.key || "").toUpperCase().includes(keyFilter));
      renderOverrides(overrides);
      setOverridesStatus(`Loaded ${overrides.length} saved link(s).`);
    } catch (e) {
      setOverridesStatus("Error: " + e.message, true);
    } finally {
      overridesLoadBtn.disabled = false;
      setBtnLoading("overrides-load-spinner", "overrides-load-btn-label", false);
    }
  }

  overridesLoadBtn.addEventListener("click", loadOverrides);
  overridesKeyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadOverrides();
  });

  // ── BigQuery (int_document_ai) lookup tab ────────────────────────────────────
  // Finds POs Document AI parsed into BigQuery that may never have reached MSSQL at
  // all — independent of the normal automated bridge. Only a header not already in
  // MSSQL can be selected for insertion; one already there is shown (so the user can
  // see its BC/MSSQL status) but its checkbox stays disabled — the normal sync/
  // backfill/reprocess flows already cover it.
  let bqDetails = []; // the current search's full detail_data, filtered per-row by po_ref_number when expanded
  let bqHeaders = []; // the current search's full header list, for re-matching a selection back to its exact BigQuery-shaped dict

  function setBqStatus(msg, isError) {
    bqStatusLine.textContent = msg || "";
    bqStatusLine.classList.toggle("error", !!isError);
  }

  function bqBadgeClass(state) {
    if (state === true) return "ok";
    if (state === false) return "missing";
    return "unknown"; // null — the check itself failed, not a confirmed absence
  }

  function bqBadgeLabel(label, state) {
    if (state === true) return `✓ ${label}`;
    if (state === false) return `✗ ${label}`;
    return `? ${label}`;
  }

  function renderBqDetailLines(poRef) {
    const lines = bqDetails.filter((d) => d.po_ref_number === poRef);
    if (!lines.length) return '<p class="tab-empty">No detail lines found for this PO in BigQuery.</p>';
    const rows = lines.map((l) => `
      <tr>
        <td>${escapeHtml(l.customer_sku_code || "—")}</td>
        <td>${escapeHtml(l.customer_sku_desc || "—")}</td>
        <td>${escapeHtml(l.po_qty ?? l.po_qty_pcs ?? "—")}</td>
        <td>${escapeHtml(l.unit_price ?? "—")}</td>
      </tr>
    `).join("");
    return `
      <table>
        <thead><tr><th>SKU</th><th>Description</th><th>Qty</th><th>Unit Price</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function updateBqInsertActionState() {
    const checked = bqResultsList.querySelectorAll(".bq-row-check:checked");
    bqInsertAction.classList.toggle("hidden", bqResultsList.querySelectorAll(".bq-row-check").length === 0);
    bqInsertBtn.disabled = checked.length === 0;
    bqInsertBtn.textContent = `Add Selected (${checked.length}) to Buffer`;
  }

  function buildDocumentAiRow(header) {
    const node = documentAiRowTemplate.content.cloneNode(true);
    const row = node.querySelector(".bq-row");
    const checkbox = row.querySelector(".bq-row-check");
    const poRef = header.po_ref_number || "";
    checkbox.dataset.poRef = poRef;

    const isMssqlSourced = header._source === "mssql";

    row.querySelector(".group-key").textContent = poRef || "(no PO ref)";
    row.querySelector(".group-desc").textContent =
      `${header.customer_name || "—"} · PO date ${header.po_date || "—"} · created ${header.created_at || "—"}`;

    // Only ever "mssql" when the exact-PO-ref search fell back to CustomerPOUL because
    // BigQuery had nothing at all for this ref (e.g. legacy-app-encoded) — a plain
    // BigQuery search never produces this tag, so this badge is never "missing" for a
    // row that genuinely came from the search results above it.
    row.querySelector(".bq-status-bigquery").className =
      `bq-status-badge bq-status-bigquery ${isMssqlSourced ? "missing" : "ok"}`;
    row.querySelector(".bq-status-bigquery").textContent = isMssqlSourced ? "✗ Not in BigQuery" : "✓ BigQuery";

    row.querySelector(".bq-status-mssql").className =
      `bq-status-badge bq-status-mssql ${bqBadgeClass(header.in_mssql)}`;
    row.querySelector(".bq-status-mssql").textContent = bqBadgeLabel("MSSQL", header.in_mssql);
    row.querySelector(".bq-status-bc").className =
      `bq-status-badge bq-status-bc ${bqBadgeClass(header.in_bc)}`;
    row.querySelector(".bq-status-bc").textContent = bqBadgeLabel("BC", header.in_bc);

    // Eligible to select: either genuinely not yet in MSSQL (the normal BigQuery-
    // sourced case — needs an MSSQL insert), OR found only via the CustomerPOUL
    // fallback (already in MSSQL by definition, but that's the whole point of this
    // row existing — it still needs buffering, just with no MSSQL insert step).
    // Anything else already in MSSQL through the normal BigQuery path is shown for
    // status visibility only — the existing Sync/Backfill/Reprocess flows already
    // cover it.
    if (!isMssqlSourced && header.in_mssql !== false) {
      checkbox.disabled = true;
      checkbox.title = header.in_mssql === null
        ? "Could not confirm MSSQL status — re-run the search before relying on this."
        : "Already in MSSQL — use Sync/Backfill/Reprocess instead.";
      row.classList.add("bq-ineligible");
    } else {
      checkbox.addEventListener("change", updateBqInsertActionState);
    }

    const toggleBtn = row.querySelector(".bq-detail-toggle");
    const detailEl = row.querySelector(".bq-detail-lines");
    toggleBtn.addEventListener("click", () => {
      const show = detailEl.classList.contains("hidden");
      if (show && !detailEl.dataset.rendered) {
        detailEl.innerHTML = renderBqDetailLines(poRef);
        detailEl.dataset.rendered = "1";
      }
      detailEl.classList.toggle("hidden", !show);
      toggleBtn.textContent = show ? "Hide detail lines" : "Show detail lines";
    });

    const alignBtn = row.querySelector(".btn-quick-align");
    const alignResultEl = row.querySelector(".bq-align-result");
    alignBtn.addEventListener("click", () => quickAlignOneRow(header, alignBtn, alignResultEl));

    return row;
  }

  const QUICK_ALIGN_ICON = {
    inserted: "✅", merged: "✅", triggered: "✅", aligned: "✅",
    blocked: "⛔", unknown: "❓", not_found: "❓", failed: "❌",
  };

  // Checks this one PO against all three data sources server-side (BigQuery is
  // always the source of truth) and runs whichever existing tool brings MSSQL/BC up
  // to match it — see /api/bigquery/quick-align. Scoped to this single row's own
  // detail lines, not the whole search result set.
  async function quickAlignOneRow(header, btn, resultEl) {
    if (!refreshGate()) {
      setBqStatus("Fill in your details above first.", true);
      return;
    }
    const poRef = header.po_ref_number || "";
    const lines = bqDetails.filter((d) => d.po_ref_number === poRef);
    const spinner = btn.querySelector(".btn-spinner");
    const label = btn.querySelector(".btn-quick-align-label");

    btn.disabled = true;
    if (spinner) spinner.classList.remove("hidden");
    if (label) label.textContent = "Aligning…";
    try {
      const res = await fetch("/api/bigquery/quick-align", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ headers: [header], details: lines, employee: getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Quick Align failed");
      const results = data.data || [];
      resultEl.innerHTML = results.map((r) => `
        <div>${QUICK_ALIGN_ICON[r.status] || "❓"} ${escapeHtml(r.detail)}</div>
      `).join("") || "<div>Nothing to align.</div>";
      resultEl.classList.remove("hidden");
    } catch (e) {
      resultEl.innerHTML = `<div>❌ ${escapeHtml(e.message)}</div>`;
      resultEl.classList.remove("hidden");
    } finally {
      btn.disabled = false;
      if (spinner) spinner.classList.add("hidden");
      if (label) label.textContent = "⚡ Quick Align";
    }
  }

  function renderBqResults(headers) {
    bqResultsList.innerHTML = "";
    bqInsertResults.innerHTML = "";
    bqInsertResults.classList.add("hidden");
    if (!headers.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No matching records found in BigQuery.";
      bqResultsList.appendChild(empty);
      bqInsertAction.classList.add("hidden");
      return;
    }
    headers.forEach((h) => bqResultsList.appendChild(buildDocumentAiRow(h)));
    updateBqInsertActionState();
  }

  async function searchDocumentAi() {
    const params = new URLSearchParams();
    const poRef = bqPoRefInput.value.trim();
    const customer = bqCustomerInput.value.trim();
    if (poRef) params.set("po_ref_number", poRef);
    if (customer) params.set("customer_name", customer);
    if (bqDateFrom.value) params.set("date_from", bqDateFrom.value);
    if (bqDateTo.value) params.set("date_to", bqDateTo.value);

    bqSearchBtn.disabled = true;
    setBtnLoading("bq-search-spinner", "bq-search-btn-label", true, "Searching…");
    setBqStatus("Searching BigQuery…");
    try {
      const res = await fetch(`/api/bigquery/document-ai/search?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
      bqHeaders = data.headers || [];
      bqDetails = data.details || [];
      renderBqResults(bqHeaders);
      setBqStatus(`Found ${bqHeaders.length} header(s) in BigQuery.`);
    } catch (e) {
      setBqStatus("Error: " + e.message, true);
    } finally {
      bqSearchBtn.disabled = false;
      setBtnLoading("bq-search-spinner", "bq-search-btn-label", false);
    }
  }

  bqSearchBtn.addEventListener("click", searchDocumentAi);
  bqCustomerInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchDocumentAi(); });
  // bq-po-ref-input is a textarea now (multiple PO refs, one-per-line or comma-separated)
  // — plain Enter has to stay a newline; Ctrl/Cmd+Enter triggers the search instead.
  bqPoRefInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) searchDocumentAi();
  });

  bqInsertBtn.addEventListener("click", async () => {
    if (!refreshGate()) {
      setBqStatus("Fill in your details above first.", true);
      return;
    }
    const checkedBoxes = [...bqResultsList.querySelectorAll(".bq-row-check:checked")];
    if (!checkedBoxes.length) return;
    const selectedRefs = new Set(checkedBoxes.map((c) => c.dataset.poRef));
    // Re-match against the search's own header list (not the DOM) for the exact
    // snake_case BigQuery shape the backend expects — the DOM only has display text.
    const headers = bqHeaders.filter((h) => selectedRefs.has(h.po_ref_number || ""));
    const details = bqDetails.filter((d) => selectedRefs.has(d.po_ref_number));

    const toInsert = headers.filter((h) => h._source !== "mssql").length;
    const toBufferOnly = headers.length - toInsert;
    const confirmParts = [];
    if (toInsert) confirmParts.push(`insert ${toInsert} PO(s) into MSSQL (CustomerPOULBQ/CustomerPOULDetailBQ)`);
    if (toBufferOnly) confirmParts.push(`add ${toBufferOnly} PO(s) already in MSSQL straight to the buffer (no MSSQL insert needed)`);
    if (!confirm(
      `This will ${confirmParts.join(" and ")}. None of them will be auto-imported into BC. Continue?`
    )) return;

    bqInsertBtn.disabled = true;
    const origLabel = bqInsertBtn.textContent;
    bqInsertBtn.textContent = "Adding…";
    try {
      const res = await fetch("/api/bigquery/insert-and-buffer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ headers, details, employee: getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Insert failed");
      const results = data.data || [];
      const statusIcon = { inserted: "✅", merged: "➕", blocked: "⛔", failed: "❌" };
      const rowClass = { inserted: "ok", merged: "ok", blocked: "blocked" };
      bqInsertResults.innerHTML = results.map((r) => `
        <div class="update-result-row ${rowClass[r.status] || "fail"}">
          <span class="order-ref">${escapeHtml(r.po_ref || "—")}</span>
          <span>${statusIcon[r.status] || "?"} ${escapeHtml(r.detail)}</span>
        </div>
      `).join("");
      bqInsertResults.classList.remove("hidden");
      setBqStatus(
        `${data.ok_count} added to the buffer (new or merged), ${data.blocked_count} already in BC ` +
        `(blocked, recorded to history), ${data.total - data.ok_count - data.blocked_count} failed. ` +
        `Re-run the search to refresh MSSQL/BC status.`
      );
    } catch (e) {
      setBqStatus("Could not insert: " + e.message, true);
    } finally {
      bqInsertBtn.disabled = false;
      bqInsertBtn.textContent = origLabel;
    }
  });

  // Generate Report covers every result the current search found (not just what's
  // checked) — it's a status snapshot for a person-in-charge, not an action on BC/MSSQL.
  bqReportBtn.addEventListener("click", async () => {
    if (!refreshGate()) {
      setBqStatus("Fill in your details above first.", true);
      return;
    }
    if (!bqHeaders.length) {
      setBqStatus("Nothing to report — run a search first.", true);
      return;
    }
    const criteria = {
      po_ref_number: bqPoRefInput.value.trim(),
      customer_name: bqCustomerInput.value.trim(),
      date_from: bqDateFrom.value,
      date_to: bqDateTo.value,
    };

    bqReportBtn.disabled = true;
    setBtnLoading("bq-report-spinner", "bq-report-btn-label", true, "Generating…");
    try {
      const res = await fetch("/api/bigquery/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ headers: bqHeaders, details: bqDetails, criteria, employee: getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Could not generate the report");

      bqReportResult.innerHTML = `
        <span>✅ Report ready — share this link with the person-in-charge:</span>
        <a href="${data.url}" target="_blank" rel="noopener" class="report-link">${escapeHtml(data.url)}</a>
        <button class="btn-copy-link" id="bq-report-copy-btn" type="button">Copy link</button>
      `;
      bqReportResult.classList.remove("hidden");
      document.getElementById("bq-report-copy-btn").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        try {
          await navigator.clipboard.writeText(data.url);
          btn.textContent = "Copied!";
          setTimeout(() => { btn.textContent = "Copy link"; }, 1500);
        } catch {
          setBqStatus("Could not copy automatically — select and copy the link manually.", true);
        }
      });
    } catch (e) {
      setBqStatus("Could not generate report: " + e.message, true);
    } finally {
      bqReportBtn.disabled = false;
      setBtnLoading("bq-report-spinner", "bq-report-btn-label", false);
    }
  });
})();
