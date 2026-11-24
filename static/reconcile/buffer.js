// Buffer/Groups reconcile tab — the largest feature of the Reconcile page (SKU/
// branch/customer/UOM override resolution, the inactive-SKU panel, the buffered-
// orders panel, Reprocess/Sync/Backfill triggers and their run watchers) — split out
// of the original monolithic reconcile.js.
import {
  getEmployeeDetails, refreshGate, setBtnLoading, escapeHtml, formatElapsed,
  RUN_POLL_MS, MAX_WATCH_MS, startElapsedTicker, setStatus,
} from "./core.js";
import {
  LOOKUP_PATH, normalizeCandidates, resolvedPayload, saveOverride, resolvedDisplay,
  customerKeyParts, renderCandidateList, warnIfAmbiguousCandidates,
} from "./override-utils.js";
import { activateActionTab } from "./tabs.js";

  const companySelect = document.getElementById("company-select");
  const cloudsqlCompanySelect = document.getElementById("cloudsql-company-select");
  const overridesCompanySelect = document.getElementById("overrides-company-select");
  const loadBtn       = document.getElementById("load-btn");
  const reprocessBtn  = document.getElementById("reprocess-btn");
  const fileScopeInput    = document.getElementById("file-scope-input");
  const fileScopeApplyBtn = document.getElementById("file-scope-apply-btn");
  const fileScopeClearBtn = document.getElementById("file-scope-clear-btn");
  const fileScopeStatus   = document.getElementById("file-scope-status");
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

  // { company, order_count, orders, groups: { sku: [...], branch: [...], customer: [...] } }
  export let state = null;

  // Buffer tab's "filter to one file" scope — { fileName, poRefs: Set<upper string> } |
  // null. Narrows which groups visibleGroups() shows (a group stays visible if ANY of
  // its po_refs falls in scope — resolving it still applies to every PO it covers,
  // in or out of scope; this only narrows the DISPLAY list, never what a Link action
  // affects). Lookups/mutations that need the real, complete data (findGroup,
  // groupResolvedFor, highlightHighConfidenceMatches) deliberately keep reading
  // state.groups[type] directly, never this filtered view.
  export let fileScope = null;

  function visibleGroups(type) {
    const list = (state && state.groups[type]) || [];
    if (!fileScope) return list;
    return list.filter((g) => (g.po_refs || []).some((ref) => fileScope.poRefs.has(String(ref).trim().toUpperCase())));
  }

  function visibleOrders() {
    const list = (state && state.orders) || [];
    if (!fileScope) return list;
    return list.filter((o) => fileScope.poRefs.has(String((o.header || {}).poRefNumber || "").trim().toUpperCase()));
  }

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
  function setBufferStatus(msg, isError) {
    setStatus(statusLine, msg, isError);
  }

  function resolvedCount(list) {
    return list.filter((g) => g.resolved).length;
  }

  function renderSummary() {
    setStatText("stat-orders", state.order_count);

    let totalGroups = 0, totalResolved = 0;
    ["sku", "branch", "customer", "uom"].forEach((type) => {
      const list = visibleGroups(type);
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

  // True when BC has more than one Customer record sharing this EXACT name — e.g. a
  // retail chain set up as a separate Customer master per store/branch, all under the
  // identical Name field. Best-effort: fails open (false) on any error, since the
  // fallback is simply today's blanket-link behavior, not a blocked save.
  async function isCustomerNameAmbiguous(name) {
    if (!name || !state) return false;
    try {
      const res = await fetch(`/api/customer-name-matches?name=${encodeURIComponent(name)}&company=${encodeURIComponent(state.company)}`);
      if (!res.ok) return false;
      const data = await res.json();
      return (data.data || []).length > 1;
    } catch (e) {
      return false;
    }
  }

  // Saves `candidate` as the resolved link for `group` and mutates `group` in place
  // (resolved/resolved_by/resolved_at/override_id) — no DOM access, so this is usable
  // both from a row's own "pick a candidate" handler and from a background pass with
  // no row rendered yet. A ship-to address belongs to a specific BC customer —
  // selecting one tells us that customer too, so the matching "customer" group is
  // auto-linked alongside it instead of making the user resolve the same information
  // twice.
  //
  // That auto-link used to always write the FLAT customer-name override — correct only
  // when every branch sharing that raw customer name really does belong to the same BC
  // Customer record. When BC has split the name across multiple Customer records (one
  // per branch/ship-to), writing the flat override from just ONE branch's resolution
  // would silently misroute every OTHER branch sharing that name on its next
  // reprocess, with no error. isCustomerNameAmbiguous checks for that live; when true,
  // the customer link is saved scoped to this one branch ("{name}::{branch}", see
  // so_buffer_service._customer_key_parts) instead of touching the shared flat link —
  // the Overrides tab flags a scoped link distinctly (overrideKeyDisplay) so a human
  // auditing it later can tell at a glance which overrides are blanket vs. per-branch.
  //
  // Returns { customerLinkError, customerScopedToBranch } rather than throwing on the
  // secondary save, since the branch link itself still succeeded either way.
  async function applyLinkToGroup(type, group, candidate, resolvedBy) {
    const resolved = resolvedPayload(type, candidate, group.key);
    const data = await saveOverride(type, group.key, resolved, resolvedBy, group.buffer_ids);
    applyResolvedFields(group, resolved, data);

    let customerLinkError = null;
    let customerScopedToBranch = false;
    if (type === "branch" && group.customer_name && candidate.extra) {
      const custResolved = {
        customerNo: candidate.extra,
        displayName: candidate.customerName || candidate.extra,
        // Reference metadata — the exact ship-to this customer link came from, shown
        // on the Overrides tab (resolvedDisplay) and used by rgmc-worker-pool as a
        // fallback ship-to when this override is used without a parallel branch link.
        shipToCode: candidate.code || "",
        shipToName: candidate.name || "",
      };
      try {
        const ambiguous = await isCustomerNameAmbiguous(group.customer_name);
        customerScopedToBranch = ambiguous;
        if (ambiguous) {
          const custKey = `${group.customer_name}::${group.key}`;
          await saveOverride("customer", custKey, custResolved, resolvedBy, group.buffer_ids);
          // Deliberately NOT applied to the flat "customer" group's state here — that
          // group stays whatever it was (likely unresolved), since a blanket link for
          // this ambiguous name still isn't correct. The scoped link just saved covers
          // this branch's own buffered orders directly via group.buffer_ids.
        } else {
          const custGroup = findGroup("customer", group.customer_name);
          const custData = await saveOverride(
            "customer", group.customer_name, custResolved, resolvedBy, custGroup ? custGroup.buffer_ids : []
          );
          if (custGroup) applyResolvedFields(custGroup, custResolved, custData);
        }
      } catch (e) {
        customerLinkError = e.message;
      }
    }
    return { customerLinkError, customerScopedToBranch };
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
      setBufferStatus(
        `Loaded ${state.order_count} buffered order(s) for ${state.company}. ` +
        `${flaggedCount} group(s) have a high-confidence match (≥90%, highlighted below) — ` +
        `link them yourself, then trigger Reprocess Buffer when you're ready.`
      );
    }
  }

  // Background pass — flags every Customers-tab group whose raw name matches more
  // than one BC customer record, so direct linking is disabled (see
  // applyAmbiguousState in buildGroupRow) before a human can accidentally save a
  // blanket link for a name that's actually ambiguous. Checks every group, not just
  // unresolved ones (unlike highlightHighConfidenceMatches) — an ALREADY-resolved
  // group can still be wrong today if it was blanket-linked before this check
  // existed, and flagging it lets a human notice and re-resolve it via Split instead.
  async function flagAmbiguousCustomerGroups() {
    if (!state) return;
    const groups = state.groups.customer || [];
    if (!groups.length) return;

    let flaggedCount = 0;
    // Sequential, not parallel — same reasoning as highlightHighConfidenceMatches:
    // avoid hitting BC with one request per group simultaneously.
    for (const group of groups) {
      const ambiguous = await isCustomerNameAmbiguous(group.key);
      if (ambiguous) {
        group.ambiguousCustomerName = true;
        flaggedCount++;
      }
    }
    if (flaggedCount) renderGroupsPanel("customer");
  }

  // ── Group label / flags ───────────────────────────────────────────────────
  function groupDisplayLabel(type, group) {
    if (type === "branch") {
      const customer = group.customer_name || "—";
      const company = group.company_name || "—";
      return `${group.key} (${customer} - ${company})`;
    }
    if (type === "uom") {
      return `${group.item_no} — raw unit "${group.raw_uom}" (BC rejected "${group.bad_code}")`;
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

    const labelFor = { branch: "Branch", customer: "Customer", sku: "Items", uom: "Unit of Measure" };
    const needs = { branch: 0, customer: 0, sku: 0, uom: 0 };
    const blocked = []; // { poRef, labels } — which specific PO(s), and what each still needs
    orders.forEach((o) => {
      const r = orderReadiness(o);
      if (r.ready) return;
      const missing = [];
      if (!r.branchOk) { needs.branch++; missing.push(labelFor.branch); }
      if (!r.customerOk) { needs.customer++; missing.push(labelFor.customer); }
      if (!r.skuOk) { needs.sku++; missing.push(labelFor.sku); }
      if (!r.uomOk) { needs.uom++; missing.push(labelFor.uom); }
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

  // A group with many POs (seen live: 37 for one UOM group) otherwise renders as one
  // unbroken wall of comma-separated numbers that just duplicates the count already
  // shown in the badge next to it — collapse past a threshold, same disclosure
  // pattern as .btn-history-toggle elsewhere on the page.
  const PO_REFS_PREVIEW_COUNT = 8;
  function renderPoRefsList(el, poRefs) {
    let expanded = poRefs.length <= PO_REFS_PREVIEW_COUNT;
    function draw() {
      const shown = expanded ? poRefs : poRefs.slice(0, PO_REFS_PREVIEW_COUNT);
      const toggleLabel = expanded ? "Show less" : `+${poRefs.length - PO_REFS_PREVIEW_COUNT} more`;
      el.innerHTML = `POs: ${escapeHtml(shown.join(", "))}${expanded ? "" : ", …"} ` +
        (poRefs.length > PO_REFS_PREVIEW_COUNT
          ? `<button class="btn-history-toggle btn-po-refs-toggle" type="button">${toggleLabel}</button>`
          : "");
      const btn = el.querySelector(".btn-po-refs-toggle");
      if (btn) btn.addEventListener("click", () => { expanded = !expanded; draw(); });
    }
    draw();
  }

  // ── Customer group "Split…" — resolve a Customers-tab group's buffered POs to
  // DIFFERENT BC customers instead of one blanket link for the whole raw customer
  // name. Each sub-group gets its own "customer" override, scoped by branch
  // ("{name}::{branch}") or by individual PO ("{name}::PO:{ref}") — same composite-key
  // scheme applyLinkToGroup already uses for the automatic branch-link case, just
  // picked explicitly here instead of only when ambiguity is auto-detected. ─────────

  function customerGroupOrders(group) {
    const ids = new Set(group.buffer_ids);
    return (state.orders || []).filter((o) => ids.has(o.id));
  }

  // The resolved ship-to for a raw branch name, if the Customer Branches (Ship-To) tab
  // already has a link for it — {customerNo, shipToCode, name}, or null if that branch
  // isn't linked yet. Shown on each split sub-row so a human picking which BC customer
  // this branch/PO belongs to can see what the branch is already tied to, instead of
  // guessing from the name alone.
  function branchShipToInfo(branchName) {
    if (!branchName) return null;
    const upper = branchName.trim().toUpperCase();
    const g = (state.groups.branch || []).find((x) => x.key.trim().toUpperCase() === upper);
    return (g && g.resolved) ? g.resolved : null;
  }

  // One bucket per distinct branch (mode "branch", the default — POs that really do
  // share a branch almost always belong together) or one bucket per individual PO
  // (mode "po" — the rarer escape hatch for when even two POs sharing the same raw
  // branch text turn out to need different customers). Sorted by PO count descending
  // so the highest-impact buckets lead, same convention as sortGroupsForDisplay.
  function customerSplitBuckets(group, mode) {
    const buckets = new Map();
    customerGroupOrders(group).forEach((o) => {
      const header = o.header || {};
      const poRef = header.poRefNumber || o.id;
      const branchName = (header.customerBranchName || "").trim();
      const bucketKey = mode === "po" ? o.id : (branchName.toUpperCase() || "(NO BRANCH)");
      if (!buckets.has(bucketKey)) {
        const label = mode === "po" ? poRef : (branchName || "(no branch on this PO)");
        const scopeKey = mode === "po" ? `${group.key}::PO:${poRef}` : `${group.key}::${label}`;
        buckets.set(bucketKey, { label, scopeKey, poRefs: [], bufferIds: [], shipTo: branchShipToInfo(branchName) });
      }
      const b = buckets.get(bucketKey);
      b.poRefs.push(poRef);
      b.bufferIds.push(o.id);
    });
    return Array.from(buckets.values()).sort((a, b) => b.poRefs.length - a.poRefs.length);
  }

  function buildSplitSubRow(sub) {
    const row = document.createElement("div");
    row.className = "split-sub-row";
    const shipToLine = sub.shipTo
      ? `<div class="split-sub-shipto">Ship-to on file: <strong>${escapeHtml(sub.shipTo.shipToCode || "")}</strong>` +
        `${sub.shipTo.name ? " — " + escapeHtml(sub.shipTo.name) : ""} (customer ${escapeHtml(sub.shipTo.customerNo || "")})</div>`
      : "";
    row.innerHTML = `
      <div class="split-sub-head">
        <span class="split-sub-label">${escapeHtml(sub.label)}</span>
        <span class="group-po-count">${sub.poRefs.length} PO${sub.poRefs.length === 1 ? "" : "s"}</span>
        <button class="btn-link-toggle btn-split-link-toggle" type="button">Link…</button>
      </div>
      ${shipToLine}
      <div class="split-sub-po-refs">POs: ${escapeHtml(sub.poRefs.join(", "))}</div>
      <div class="group-resolved hidden">
        Linked to <strong class="resolved-text"></strong>
        <button class="btn-unlink" type="button">Undo</button>
      </div>
      <div class="split-sub-link-panel hidden">
        <div class="search-row"><input type="text" class="search-input" placeholder="Search existing BC customers…" /></div>
        <div class="search-results"></div>
      </div>
    `;

    const toggleBtn = row.querySelector(".btn-split-link-toggle");
    const linkPanel = row.querySelector(".split-sub-link-panel");
    const resolvedBox = row.querySelector(".group-resolved");
    const unlinkBtn = row.querySelector(".btn-unlink");
    const searchInput = row.querySelector(".search-input");
    const searchResults = row.querySelector(".search-results");
    let currentOverrideId = null;

    toggleBtn.addEventListener("click", () => linkPanel.classList.toggle("hidden"));

    function markOrdersResolved(value) {
      const ids = new Set(sub.bufferIds);
      (state.orders || []).forEach((o) => { if (ids.has(o.id)) o._splitCustomerResolved = value; });
    }

    async function saveSplitLink(candidate) {
      toggleBtn.disabled = true;
      try {
        const resolvedBy = getEmployeeDetails().employee_name;
        const resolved = resolvedPayload("customer", candidate);
        const data = await saveOverride("customer", sub.scopeKey, resolved, resolvedBy, sub.bufferIds);
        currentOverrideId = data.id;
        resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(resolved);
        resolvedBox.classList.remove("hidden");
        linkPanel.classList.add("hidden");
        toggleBtn.textContent = "Change…";
        markOrdersResolved(true);
        setBufferStatus(`Linked "${sub.label}" (${sub.poRefs.length} PO${sub.poRefs.length === 1 ? "" : "s"}) to ${resolved.customerNo}.`);
        refreshOtherGroupPanels("customer"); // branch/sku/uom blocked-notes may change; customer panel itself is skipped so this split panel survives
        renderOrdersPanel();
        renderSummary();
      } catch (e) {
        setBufferStatus("Could not save split link: " + e.message, true);
      } finally {
        toggleBtn.disabled = false;
      }
    }

    unlinkBtn.addEventListener("click", async () => {
      if (!currentOverrideId) return;
      unlinkBtn.disabled = true;
      try {
        await fetch(`/api/overrides/${encodeURIComponent(currentOverrideId)}`, { method: "DELETE" });
        currentOverrideId = null;
        resolvedBox.classList.add("hidden");
        toggleBtn.textContent = "Link…";
        markOrdersResolved(false);
        setBufferStatus(`Removed the split link for "${sub.label}".`);
        refreshOtherGroupPanels("customer");
        renderOrdersPanel();
        renderSummary();
      } catch (e) {
        setBufferStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      const term = searchInput.value.trim();
      if (term.length < 2) { searchResults.innerHTML = ""; return; }
      searchTimer = setTimeout(async () => {
        searchResults.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/customers?search=${encodeURIComponent(term)}&company=${encodeURIComponent(state.company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Search failed");
          if (searchInput.value.trim() !== term) return; // stale response guard
          const candidates = normalizeCandidates("customer", data.data);
          renderCandidateList(searchResults, "customer", candidates, saveSplitLink);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    return row;
  }

  function buildCustomerSplitPanel(container, group) {
    container.innerHTML = "";
    const hint = document.createElement("p");
    hint.className = "search-hint";
    hint.textContent =
      "Link each branch (or, if needed, each individual PO) to its own BC customer — doesn't touch the blanket " +
      "link above, and each save applies immediately. A row showing \"Ship-to on file\" already has a link saved " +
      "on the Customer Branches (Ship-To) tab — that's the customer its orders actually belong to. Switching " +
      "mode rebuilds this list from scratch, so an already-linked row won't show as linked here afterward (it " +
      "still is — check the Overrides tab for every link saved so far).";
    container.appendChild(hint);

    const toolbar = document.createElement("div");
    toolbar.className = "split-mode-toolbar";
    toolbar.innerHTML =
      `<span class="split-mode-label">Split by</span>` +
      `<button class="split-mode-btn active" data-mode="branch" type="button">Ship-to / branch</button>` +
      `<button class="split-mode-btn" data-mode="po" type="button">Individual PO</button>`;
    container.appendChild(toolbar);

    const list = document.createElement("div");
    list.className = "split-sub-list";
    container.appendChild(list);

    let mode = "branch";
    function renderSubGroups() {
      list.innerHTML = "";
      customerSplitBuckets(group, mode).forEach((sub) => list.appendChild(buildSplitSubRow(sub)));
    }

    toolbar.querySelectorAll(".split-mode-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (btn.dataset.mode === mode) return;
        mode = btn.dataset.mode;
        toolbar.querySelectorAll(".split-mode-btn").forEach((b) => b.classList.toggle("active", b === btn));
        renderSubGroups();
      });
    });

    renderSubGroups();
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
    renderPoRefsList(row.querySelector(".group-po-refs"), group.po_refs);

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
          renderGroupsPanel("branch"); // branch/customer/uom blocked-notes may change — inactive SKUs no longer count against order readiness
          renderGroupsPanel("customer");
          renderGroupsPanel("uom");
          renderOrdersPanel(); // readiness may have changed
          renderSummary();
          setBufferStatus(`Marked "${group.key}" inactive.`);
        } catch (e) {
          setBufferStatus("Could not mark inactive: " + e.message, true);
        } finally {
          markInactiveBtn.disabled = false;
        }
      });
    }

    // Split… — lets a Customer group's buffered POs be linked to DIFFERENT BC
    // customers instead of one blanket link for the whole raw customer name. Only
    // meaningful with more than one PO (see buildCustomerSplitPanel).
    const splitToggle = row.querySelector(".btn-split-toggle");
    const splitPanel = row.querySelector(".group-split-panel");
    if (type === "customer" && group.po_refs.length > 1) {
      splitToggle.classList.remove("hidden");
      splitToggle.addEventListener("click", () => {
        const opening = splitPanel.classList.toggle("hidden") === false;
        if (opening && !splitPanel.dataset.built) {
          buildCustomerSplitPanel(splitPanel, group);
          splitPanel.dataset.built = "1";
        }
      });
    }

    const blockedNote = row.querySelector(".group-blocked-note");
    const confidenceBadge = row.querySelector(".group-confidence-badge");
    const ambiguousNote = row.querySelector(".group-ambiguous-note");

    // BC has more than one Customer record sharing this exact name (detected in the
    // background — see flagAmbiguousCustomerGroups) — direct linking/relinking here
    // always applies to every branch/PO sharing the name, which is exactly the wrong
    // thing to do once that's ambiguous, so it's disabled outright rather than just
    // warned about. Split… (when there's more than one PO) or the Customer Branches
    // (Ship-To) tab are the ways to resolve it correctly instead.
    function applyAmbiguousState(g) {
      const ambiguous = type === "customer" && g.ambiguousCustomerName;
      toggleBtn.disabled = ambiguous;
      toggleBtn.title = ambiguous
        ? "BC has multiple customers with this exact name — linking here would apply to every branch/PO sharing it. Use Split below, or the Customer Branches (Ship-To) tab, instead."
        : "";
      if (ambiguous) {
        // No leading "⚠ " here — .group-ambiguous-note::before already renders one.
        ambiguousNote.textContent = g.po_refs.length > 1
          ? "BC has multiple customers sharing this exact name. Direct linking is disabled — use Split below to resolve per branch or PO instead."
          : "BC has multiple customers sharing this exact name. Direct linking is disabled — link its one PO from the Customer Branches (Ship-To) tab instead.";
        ambiguousNote.classList.remove("hidden");
      } else {
        ambiguousNote.classList.add("hidden");
      }
    }
    applyAmbiguousState(group);

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
        setBufferStatus("Could not remove link: " + e.message, true);
      } finally {
        unlinkBtn.disabled = false;
      }
    });

    async function saveLink(candidate) {
      linkPanel.classList.add("is-saving");
      try {
        const resolvedBy = getEmployeeDetails().employee_name;
        const { customerLinkError, customerScopedToBranch } = await applyLinkToGroup(type, group, candidate, resolvedBy);
        applyResolvedState(group);
        resolvedBox.classList.add("pop-in");
        resolvedBox.addEventListener("animationend", () => resolvedBox.classList.remove("pop-in"), { once: true });
        linkPanel.classList.remove("is-open");
        if (customerLinkError) {
          setBufferStatus("Branch linked, but auto-linking the customer failed: " + customerLinkError, true);
        } else if (customerScopedToBranch) {
          setBufferStatus(
            `Branch linked. BC has multiple customers named "${group.customer_name}", so the customer link was ` +
            `scoped to this branch only (see the Overrides tab) — other branches sharing that name still need their own link.`
          );
        }

        refreshOtherGroupPanels(type); // other groups' blocked-notes may change now that this one resolved (also covers the auto-linked customer group, if any)
        renderOrdersPanel(); // readiness may have changed
        renderSummary();
      } catch (e) {
        setBufferStatus("Could not save link: " + e.message, true);
      } finally {
        linkPanel.classList.remove("is-saving");
      }
    }

    // Suggestions (BC fuzzy match via rgmc-gcp-api) — sku/branch only. "customer" has
    // no suggest endpoint (inferred via its branch link instead); "uom" has no fuzzy
    // match either — its "search" below already loads the item's full valid-UOM list.
    const suggestBtn = row.querySelector(".btn-suggest");
    const suggestStatus = row.querySelector(".suggest-status");
    const suggestResults = row.querySelector(".suggest-results");
    if (type === "customer" || type === "uom") {
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
          setBufferStatus("Suggestion lookup failed: " + e.message, true);
        } finally {
          suggestBtn.disabled = false;
        }
      });
    }

    // Manual search (BC list/search via rgmc-bc-api).
    const searchInput = row.querySelector(".search-input");
    const searchResults = row.querySelector(".search-results");
    if (type === "uom") {
      // Candidates stay scoped to THIS item's own BC-valid units (rgmc-bc-api's Item
      // Unit of Measure lookup) — never a free-text search across every unit code in
      // BC, since a code not already attached to this item in BC would just fail the
      // exact same rejection again (confirmed live for MU01CH02: BC's company-wide
      // Units of Measure list has 13 codes incl. "CS", but MU01CH02's OWN Item Units
      // of Measure table has exactly one, "PC/S" — those are different BC tables).
      // The full (short) list loads once per panel-open; the search box just filters
      // it client-side, for an item with enough valid units that scrolling is a pain.
      searchInput.placeholder = "Filter this item's valid units…";
      let uomCandidates = null;
      let uomLoaded = false;
      function renderFiltered() {
        const term = searchInput.value.trim().toLowerCase();
        const filtered = !term
          ? uomCandidates
          : uomCandidates.filter((c) =>
              (c.code || "").toLowerCase().includes(term) || (c.name || "").toLowerCase().includes(term)
            );
        renderCandidateList(searchResults, type, filtered, saveLink);
      }
      toggleBtn.addEventListener("click", async () => {
        if (!linkPanel.classList.contains("is-open") || uomLoaded) return;
        uomLoaded = true;
        searchResults.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/uom?item_no=${encodeURIComponent(group.item_no)}&company=${encodeURIComponent(state.company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Lookup failed");
          uomCandidates = normalizeCandidates(type, data.data);
          renderFiltered();
        } catch (e) {
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
          uomLoaded = false;
        }
      });
      searchInput.addEventListener("input", () => {
        if (uomCandidates) renderFiltered();
      });
    } else {
      if (type === "branch") {
        searchInput.placeholder = "Search by ship-to name, code, or lookup code…";
      }
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
            const candidates = normalizeCandidates(type, data.data);
            renderCandidateList(searchResults, type, candidates, saveLink);
            if (type === "customer") warnIfAmbiguousCandidates(searchResults, candidates);
          } catch (e) {
            if (searchInput.value.trim() !== term) return;
            searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
          }
        }, 350);
      });
    }

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
    ["sku", "branch", "customer", "uom"].forEach((t) => {
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

  // Unresolved first, then by PO count descending — so the entries that actually need
  // checking lead the list instead of being scattered among already-resolved ones. A
  // copy, never state.groups[type] itself: several handlers (e.g. Mark Inactive, the
  // SKU/UOM reactivate flow) look a group up in that array by identity via indexOf,
  // which only works if its order/contents stay untouched by rendering.
  function sortGroupsForDisplay(groups) {
    return [...groups].sort((a, b) => {
      if (!!a.resolved !== !!b.resolved) return a.resolved ? 1 : -1;
      return (b.po_count || 0) - (a.po_count || 0);
    });
  }

  function renderGroupsPanel(type) {
    const panel = document.getElementById(`panel-${type}`);
    panel.innerHTML = "";
    if (type === "sku") panel.appendChild(buildSkuResolveHint());
    const groups = visibleGroups(type);
    if (!groups.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = fileScope
        ? "No buffered orders from this file reference this."
        : "Nothing to reconcile — no buffered orders reference this.";
      panel.appendChild(empty);
      return;
    }
    sortGroupsForDisplay(groups).forEach((g) => panel.appendChild(buildGroupRow(type, g)));
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
      <div class="group-po-refs"></div>
    `;
    renderPoRefsList(row.querySelector(".group-po-refs"), group.po_refs);
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
        renderGroupsPanel("branch"); // branch/customer/uom blocked-notes may change — this SKU counts against order readiness again
        renderGroupsPanel("customer");
        renderGroupsPanel("uom");
        renderOrdersPanel(); // readiness may have changed
        renderSummary();
        setBufferStatus(`Reactivated "${group.key}".`);
      } catch (err) {
        setBufferStatus("Could not reactivate: " + err.message, true);
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
    // A per-branch/per-PO Split link (see buildCustomerSplitPanel) resolves this
    // order's customer without ever touching the flat "customer" group's own
    // resolved state (deliberately — see applyLinkToGroup) — order._splitCustomerResolved
    // is set directly on the affected order(s) when that save succeeds, since
    // groupResolvedFor has no way to see a scoped override by itself.
    const customerOk = !!order._splitCustomerResolved || groupResolvedFor("customer", header.customerName);
    const skuKeys = [...new Set(lines.map((l) => {
      const sku = (l.customerSKUCode || "").trim();
      if (sku) return sku;
      return (l.customerSKUDesc || "").trim() || "(no SKU code, no description)";
    }))].filter((s) => !isSkuInactive(s));
    const skuResolved = skuKeys.filter((s) => groupResolvedFor("sku", s)).length;
    const skuOk = skuKeys.length === 0 || skuResolved === skuKeys.length;
    // uomIssue is only tagged by rgmc-worker-pool at the moment BC actually rejects a
    // line's Unit of Measure Code for its item — a line without it isn't a UOM problem.
    const uomKeys = [...new Set(
      lines.filter((l) => l.uomIssue && l.uomIssue.itemNo).map((l) => `${l.uomIssue.itemNo}::${l.uomIssue.rawUom || ""}`)
    )];
    const uomResolved = uomKeys.filter((k) => groupResolvedFor("uom", k)).length;
    const uomOk = uomKeys.length === 0 || uomResolved === uomKeys.length;
    return {
      branchOk, customerOk, skuOk, uomOk,
      skuResolved, skuTotal: skuKeys.length,
      uomResolved, uomTotal: uomKeys.length,
      ready: branchOk && customerOk && skuOk && uomOk,
    };
  }

  function renderOrdersPanel() {
    const panel = document.getElementById("panel-orders");
    panel.innerHTML = "";
    const orders = visibleOrders();
    if (!orders.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = fileScope ? "No buffered orders match this file filter." : "No buffered orders for this company.";
      panel.appendChild(empty);
      return;
    }
    // Not-ready first, then by attempt_count descending — the orders that have been
    // stuck the longest lead the list instead of being buried among resolved ones.
    const sortedOrders = [...orders].sort((a, b) => {
      const aReady = orderReadiness(a).ready, bReady = orderReadiness(b).ready;
      if (aReady !== bReady) return aReady ? 1 : -1;
      return (b.attempt_count || 0) - (a.attempt_count || 0);
    });
    sortedOrders.forEach((order) => {
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
          ${r.uomTotal ? `<span class="link-chip ${r.uomOk ? "ok" : "pending"}">Unit of Measure ${r.uomResolved}/${r.uomTotal}</span>` : ""}
        </div>
        <div class="order-error">${escapeHtml(order.last_error || "")}</div>
      `;
      panel.appendChild(row);
    });
  }

  // ── Load buffer ───────────────────────────────────────────────────────────
  function renderAll() {
    renderSummary();
    renderGroupsPanel("sku");
    renderInactivePanel();
    renderGroupsPanel("branch");
    renderGroupsPanel("customer");
    renderGroupsPanel("uom");
    renderOrdersPanel();
    revealOnce(tabsCard);
    reprocessBtn.disabled = state.order_count === 0;
  }

  async function loadBuffer() {
    if (!refreshGate()) {
      setBufferStatus("Fill in your details above first.", true);
      return;
    }
    const company = companySelect.value;
    if (!company) {
      setBufferStatus("Select a company first.", true);
      return;
    }
    stopWatching(); // a fresh load already gives the current truth — no need to keep polling
    loadBtn.disabled = true;
    setBtnLoading("load-spinner", "load-btn-label", true, "Loading…");
    setBufferStatus("Loading buffer…");
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
      setBufferStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.`);

      // Background pass — recovers any missing lines from Cloud SQL/BigQuery (can
      // take a while per order under load) and re-renders once it's back, so the
      // SKU tab doesn't stay empty for orders buffered before the lines bug was fixed.
      const anyMissingLines = state.orders.some((o) => !o.lines || !o.lines.length);
      if (anyMissingLines) {
        setBufferStatus(`Loaded ${state.order_count} order(s) for ${company} — recovering missing lines…`);
        fetch(`/api/buffer/lines?company=${encodeURIComponent(company)}`)
          .then((r) => r.json().then((d) => [r, d]))
          .then(([r, d]) => {
            if (!r.ok || company !== companySelect.value) return; // stale response, ignore
            state = d;
            renderAll();
            refreshPulse(tabsCard);
            const n = state.orders.filter((o) => o._lines_recovered_from).length;
            setBufferStatus(`Loaded ${state.order_count} buffered order(s) for ${company}.` +
              (n ? ` Recovered lines for ${n} order(s).` : ""));
          })
          .catch(() => { /* best-effort — page already works with what it has */ });
      }

      // Background pass — flags any SKU/branch group whose top BC fuzzy match is ≥90%
      // confident, so a human can spot near-identical spelling variants at a glance
      // instead of clicking "Suggest" on every row. Not awaited, same as the
      // lines-recovery pass above.
      highlightHighConfidenceMatches();
      // Background pass — flags any Customers-tab group whose raw name matches more
      // than one BC customer, disabling direct linking for it (see buildGroupRow).
      flagAmbiguousCustomerGroups();
    } catch (e) {
      setBufferStatus("Error: " + e.message, true);
    } finally {
      skeletonBlock.classList.add("hidden");
      loadBtn.disabled = false;
      setBtnLoading("load-spinner", "load-btn-label", false);
    }
  }

  loadBtn.addEventListener("click", loadBuffer);

  // ── Buffer tab: filter to one file (by fileName or a sample PO ref) ────────
  async function applyFileScope() {
    const query = fileScopeInput.value.trim();
    if (!query) {
      fileScopeStatus.textContent = "Enter a filename or PO ref number first.";
      fileScopeStatus.classList.add("error");
      return;
    }
    fileScopeApplyBtn.disabled = true;
    fileScopeStatus.classList.remove("error");
    fileScopeStatus.textContent = "Resolving…";
    try {
      const res = await fetch(`/api/buffer/resolve-file-scope?query=${encodeURIComponent(query)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Could not resolve this file/PO ref");
      fileScope = { fileName: data.file_name, poRefs: new Set(data.po_ref_numbers.map((r) => r.toUpperCase())) };
      fileScopeStatus.textContent = `Filtered to "${data.file_name}" — ${data.count} PO(s).`;
      fileScopeClearBtn.classList.remove("hidden");
      if (state) renderAll();
    } catch (e) {
      fileScopeStatus.textContent = "Error: " + e.message;
      fileScopeStatus.classList.add("error");
    } finally {
      fileScopeApplyBtn.disabled = false;
    }
  }

  function clearFileScope() {
    fileScope = null;
    fileScopeInput.value = "";
    fileScopeStatus.textContent = "";
    fileScopeStatus.classList.remove("error");
    fileScopeClearBtn.classList.add("hidden");
    if (state) renderAll();
  }

  fileScopeApplyBtn.addEventListener("click", applyFileScope);
  fileScopeClearBtn.addEventListener("click", clearFileScope);
  fileScopeInput.addEventListener("keydown", (e) => { if (e.key === "Enter") applyFileScope(); });

  // Deep link from the BC Alignment Report's "Open Buffer tab — {company}" links
  // (?tab=buffer&company=SBIC) — jumps straight to that tab with the company
  // pre-selected and the buffer loaded, instead of making someone click through
  // manually. loadBuffer() itself still requires Your Details to be filled in first.
  (function applyDeepLinkFromQuery() {
    const params = new URLSearchParams(window.location.search);
    const tab = params.get("tab");
    const company = params.get("company");
    if (tab && document.querySelector(`.action-tab-btn[data-action-tab="${tab}"]`)) {
      activateActionTab(tab);
    }
    if (company) {
      [companySelect, cloudsqlCompanySelect, overridesCompanySelect].forEach((sel) => {
        if (sel && [...sel.options].some((o) => o.value === company)) sel.value = company;
      });
      if (tab === "buffer" || !tab) loadBuffer();
    }
  })();

  // ── Reprocess: trigger + watch ───────────────────────────────────────────
  // The trigger only kicks off an async worker-pool job. Primary signal: poll
  // rgmc-worker-pool's actual run status (ongoing/done/error), written to
  // Firestore as it processes the job and read back via rgmc-bc-api — real
  // state, not a guess. Falls back to inferring progress from the buffer count
  // only if the backend didn't hand back a run_id (e.g. not yet deployed).
  const BUFFER_POLL_MS = 20000;          // fallback mode: check the live buffer every 20s
  let watcher = null; // { mode: "run"|"buffer-count", startedAt, runId?, baselineCount?, pollTimer, tickTimer }


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
  export async function refreshBufferSilently() {
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

  export function startWatching(runId, baselineCount) {
    stopWatching();
    const mode = runId ? "run" : "buffer-count";
    watcher = { mode, startedAt: Date.now(), runId, baselineCount, pollTimer: null, tickTimer: null };
    reprocessStatusEl.classList.remove("hidden", "is-success", "is-error", "is-stopped");
    reprocessTitleEl.textContent = "Reprocessing triggered — watching for results…";
    reprocessDetailEl.textContent = `You'll also get an email at ${getEmployeeDetails().email}.`;
    stopWatchingBtn.classList.remove("hidden");
    watcher.tickTimer = startElapsedTicker(reprocessElapsedEl, watcher.startedAt);
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
      setBufferStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Trigger the reprocess-buffer retry for ${state.company}? Any links saved above are applied first, ahead of automatic matching. You'll get an email at ${getEmployeeDetails().email} with the result.`)) {
      return;
    }
    reprocessBtn.disabled = true;
    setBtnLoading("reprocess-spinner", "reprocess-btn-label", true, "Reprocessing…");
    setBufferStatus("Triggering reprocess…");
    try {
      const res = await fetch("/api/reprocess", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: state.company, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Reprocess trigger failed");
      setBufferStatus(`Reprocess triggered for ${state.company}.`);
      startWatching(data.run_id || null, state.order_count);
    } catch (e) {
      setBufferStatus("Error: " + e.message, true);
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
      setBufferStatus("Select a company first.", true);
      return;
    }
    if (!refreshGate()) {
      setBufferStatus("Fill in your details above first.", true);
      return;
    }
    if (!confirm(`Sync inserted orders for ${company} from Cloud SQL? This backfills missing lines onto BC orders already created for CustomerPOUL rows (createBy='trigger') — any line that still can't be matched gets buffered for reconciliation. You'll get an email at ${getEmployeeDetails().email} with the result.`)) {
      return;
    }
    syncBtn.disabled = true;
    setBtnLoading("sync-spinner", "sync-btn-label", true, "Syncing…");
    setBufferStatus("Triggering Cloud SQL sync…");
    try {
      const res = await fetch("/api/sync-inserted-orders", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Sync trigger failed");
      setBufferStatus(`Cloud SQL sync triggered for ${company}.`);
      startWatching(data.run_id || null, state && state.company === company ? state.order_count : 0);
    } catch (e) {
      setBufferStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("sync-spinner", "sync-btn-label", false);
      syncBtn.disabled = false;
    }
  });

  backfillBtn.addEventListener("click", async () => {
    const company = cloudsqlCompanySelect.value;
    if (!company) {
      setBufferStatus("Select a company first.", true);
      return;
    }
    if (!refreshGate()) {
      setBufferStatus("Fill in your details above first.", true);
      return;
    }
    const dateFrom = backfillDateFrom.value;
    const dateTo = backfillDateTo.value;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      setBufferStatus("The From date must be on or before the To date.", true);
      return;
    }
    const rangeText = dateFrom || dateTo
      ? ` (${dateFrom || "earliest"} through ${dateTo || "latest"})`
      : " (no date range — every createBy='trigger' row)";
    if (!confirm(`Backfill BC sales orders for ${company} from Cloud SQL${rangeText}? This creates a BC order for any CustomerPOUL row not already in BC — anything unresolved gets buffered for reconciliation. You'll get an email at ${getEmployeeDetails().email} with the result.`)) {
      return;
    }
    backfillBtn.disabled = true;
    setBtnLoading("backfill-spinner", "backfill-btn-label", true, "Backfilling…");
    setBufferStatus("Triggering Cloud SQL backfill…");
    try {
      const res = await fetch("/api/backfill-from-cloudsql", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company, date_from: dateFrom, date_to: dateTo, ...getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Backfill trigger failed");
      setBufferStatus(`Cloud SQL backfill triggered for ${company}.`);
      startWatching(data.run_id || null, state && state.company === company ? state.order_count : 0);
    } catch (e) {
      setBufferStatus("Error: " + e.message, true);
    } finally {
      setBtnLoading("backfill-spinner", "backfill-btn-label", false);
      backfillBtn.disabled = false;
    }
  });

