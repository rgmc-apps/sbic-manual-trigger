// Overrides tab (full link registry, independent of any one loaded buffer), plus the
// "Find previous orders" / bulk Sales Order customer-correction modal — split out of
// the original monolithic reconcile.js.
import { getEmployeeDetails, refreshGate, employeeDetailsValid, setBtnLoading, escapeHtml, setStatus } from "./core.js";
import {
  LOOKUP_PATH, normalizeCandidates, resolvedPayload, saveOverride, resolvedDisplay,
  customerKeyParts, overrideKeyDisplay, renderCandidateList, warnIfAmbiguousCandidates,
} from "./override-utils.js";

  const overridesTypeSelect    = document.getElementById("overrides-type-select");
  const overridesKeyInput      = document.getElementById("overrides-key-input");
  const overridesCompanySelect = document.getElementById("overrides-company-select");
  const overridesLoadBtn       = document.getElementById("overrides-load-btn");
  const overridesStatusLine    = document.getElementById("overrides-status-line");
  const overridesListEl        = document.getElementById("overrides-list");
  const overrideRowTemplate    = document.getElementById("override-row-template");
  const soUpdateModalOverlay   = document.getElementById("so-update-modal-overlay");
  const soUpdateModalSubtitle  = document.getElementById("so-update-modal-subtitle");
  const soUpdateModalTbody     = document.getElementById("so-update-modal-tbody");
  const soUpdateSelectAll      = document.getElementById("so-update-select-all");
  const soUpdateModalClose     = document.getElementById("so-update-modal-close");
  const soUpdateModalCancel    = document.getElementById("so-update-modal-cancel");
  const soUpdateModalConfirm   = document.getElementById("so-update-modal-confirm");

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
  const OVERRIDE_TYPE_LABEL = { sku: "SKU", branch: "Branch", customer: "Customer", uom: "Unit of Measure" };

  export function setOverridesStatus(msg, isError) {
    setStatus(overridesStatusLine, msg, isError);
  }

  // Restricted server-side to submittedBy=="SBIC AI Uploading" (rgmc-worker-pool's own
  // automated SO-import orders) — see _find_orders_by_customer in app.py. A human-created
  // BC order sharing the same (wrong) customer number is never matched or touched.
  export async function findPreviousOrders(customerNo, statusEl, resultsEl, ov) {
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

  export function openSalesOrderUpdateModal({ orders, company, oldCustomerNo, ov, resultsEl }) {
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

  export function buildOverrideRow(ov) {
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
    row.querySelector(".group-key").textContent = overrideKeyDisplay(ov);
    // Flags a branch-scoped customer override distinctly from a blanket one — BC has
    // more than one Customer record sharing this exact name, so this link only applies
    // to buffered orders from that one branch, never the others sharing the name. See
    // isCustomerNameAmbiguous/applyLinkToGroup, which is what creates these.
    const descEl = row.querySelector(".group-desc");
    const scopedToBranch = ov.type === "customer" && customerKeyParts(ov.key).branch;
    descEl.textContent = scopedToBranch
      ? "⚠ Scoped to this branch only — BC has multiple customers named this; other branches need their own link."
      : "";
    descEl.classList.toggle("override-scope-note", !!scopedToBranch);
    resolvedBox.querySelector(".resolved-text").textContent = resolvedDisplay(ov.resolved);
    resolvedBox.querySelector(".resolved-by").textContent =
      ov.resolved_by ? `(by ${ov.resolved_by}, ${ov.resolved_at || ""})` : "";

    // Mutable, not a const snapshot — an edit below updates this so the button and the
    // auto-triggered post-edit search always act on the CURRENT vs. the just-replaced
    // customer number, not whatever was true when the row was first built.
    let currentCustomerNo = ov.type === "sku" || ov.type === "uom" ? null : (ov.resolved || {}).customerNo;
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

    async function pickCandidate(candidate) {
      const oldCustomerNo = currentCustomerNo;
      linkPanel.classList.add("is-saving");
      try {
        const resolvedBy = getEmployeeDetails().employee_name;
        const resolved = resolvedPayload(ov.type, candidate, ov.key);
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

        currentCustomerNo = ov.type === "sku" || ov.type === "uom" ? null : resolved.customerNo;
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
    }

    const searchInput = row.querySelector(".search-input");
    const searchResults = row.querySelector(".search-results");
    if (ov.type === "branch") searchInput.placeholder = "Search by ship-to name, code, or lookup code…";

    if (ov.type === "uom") {
      // A uom override's key is "{itemNo}::{rawUom}" — candidates stay scoped to this
      // item's own BC-valid units (same reasoning as buildGroupRow's uom handling: a
      // code not already attached to the item in BC would just fail again), loaded
      // once the first time the panel opens; the search box filters that list
      // client-side rather than re-querying BC per keystroke.
      searchInput.placeholder = "Filter this item's valid units…";
      const itemNo = ov.key.split("::")[0];
      let uomCandidates = null;
      let uomLoaded = false;
      function renderFiltered() {
        const term = searchInput.value.trim().toLowerCase();
        const filtered = !term
          ? uomCandidates
          : uomCandidates.filter((c) =>
              (c.code || "").toLowerCase().includes(term) || (c.name || "").toLowerCase().includes(term)
            );
        renderCandidateList(searchResults, ov.type, filtered, pickCandidate);
      }
      toggleBtn.addEventListener("click", async () => {
        if (!linkPanel.classList.contains("is-open") || uomLoaded) return;
        uomLoaded = true;
        const company = overridesCompanySelect.value;
        if (!company) {
          searchResults.innerHTML = `<div class="search-hint">Select a company above first.</div>`;
          uomLoaded = false;
          return;
        }
        searchResults.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Looking up BC…</div>`;
        try {
          const url = `/api/lookup/uom?item_no=${encodeURIComponent(itemNo)}&company=${encodeURIComponent(company)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.detail || data.error || "Lookup failed");
          uomCandidates = normalizeCandidates(ov.type, data.data);
          renderFiltered();
        } catch (e) {
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
          uomLoaded = false;
        }
      });
      searchInput.addEventListener("input", () => {
        if (uomCandidates) renderFiltered();
      });
      return row;
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
          const candidates = normalizeCandidates(ov.type, data.data);
          renderCandidateList(searchResults, ov.type, candidates, pickCandidate);
          if (ov.type === "customer") warnIfAmbiguousCandidates(searchResults, candidates);
        } catch (e) {
          if (searchInput.value.trim() !== term) return;
          searchResults.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }, 350);
    });

    return row;
  }

  export function renderOverrides(overrides) {
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

  export async function loadOverrides() {
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
