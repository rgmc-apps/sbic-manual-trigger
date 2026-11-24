// Buffer tab's "View History" sub-panel — read-only log of every PO a Reprocess
// Buffer run has touched — split out of the original monolithic reconcile.js.
import { escapeHtml, refreshGate, setBtnLoading, setStatus } from "./core.js";

  const historyCompanySelect = document.getElementById("history-company-select");
  const historyOutcomeSelect = document.getElementById("history-outcome-select");
  const historyPoRefInput    = document.getElementById("history-po-ref-input");
  const historyLoadBtn       = document.getElementById("history-load-btn");
  const historyStatusLine    = document.getElementById("history-status-line");
  const historyListEl        = document.getElementById("history-list");

  // ── History tab (buffer-reconciliation log) ──────────────────────────────
  // Read-only log of every PO a Reprocess Buffer run has touched (so_buffer_history_{env},
  // written by rgmc-worker-pool) — distinct from the sku/branch/customer "Past
  // resolutions" history above, which is about override links, not PO attempts.
  const OUTCOME_LABEL = { resolved: "Resolved", still_buffered: "Still buffered", failed: "Failed", blocked_in_bc: "Blocked in BC" };
  const OUTCOME_BADGE_CLASS = { resolved: "ready", still_buffered: "pending", failed: "failed", blocked_in_bc: "failed" };

  export function setHistoryStatus(msg, isError) {
    setStatus(historyStatusLine, msg, isError);
  }

  export function formatHistoryDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  export function renderHistoryLines(lines) {
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

  export function renderHistory(records) {
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

  export async function loadHistory() {
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
