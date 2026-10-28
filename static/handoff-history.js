(function () {
  const listEl = document.getElementById("handoff-history-list");
  const statusMsgEl = document.getElementById("handoff-history-status-msg");
  const refreshBtn = document.getElementById("handoff-history-refresh");
  const groupCodeSelect = document.getElementById("handoff-history-group-code");
  const mssqlStatusSelect = document.getElementById("handoff-history-mssql-status");
  const bcStatusSelect = document.getElementById("handoff-history-bc-status");
  if (!listEl) return; // not on this page

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function formatTimestamp(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString("en-PH", {
        month: "short", day: "numeric", year: "numeric",
        hour: "2-digit", minute: "2-digit", second: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  // MSSQL: "inserted" is the only non-error outcome. BC: "created" is the only fully
  // resolved outcome — "pending"/"not_attempted" are neutral (nothing has gone wrong
  // yet), "buffered"/"error" are the ones worth flagging.
  function mssqlBadgeClass(status) {
    return status === "inserted" ? "ready" : "failed";
  }
  function bcBadgeClass(status) {
    if (status === "created") return "ready";
    if (status === "buffered" || status === "error") return "failed";
    return "pending";
  }
  // The AFTER INSERT trigger's OWN result (did the row actually reach CustomerPOUL/
  // CustomerRA, not just the _BQ staging table) — "unknown" is neutral (never
  // checked, or the check itself failed), distinct from a confirmed "not_promoted".
  function triggerBadgeClass(status) {
    if (status === "promoted") return "ready";
    if (status === "not_promoted") return "failed";
    return "pending";
  }

  // Only offered for "not_promoted" — the row is confirmed stuck in CustomerPOULBQ/
  // CustomerPOULDetailBQ with no matching CustomerPOUL row, so a clean-up-then-
  // re-insert can actually help. Any other trigger_status either doesn't need it
  // (promoted) or wouldn't be fixed by it (unknown — e.g. the MSSQL insert itself
  // already failed, so there's nothing stale in the staging tables to clean up).
  function renderRetriggerAction(container, poRef) {
    container.classList.remove("hidden");
    container.innerHTML = `
      <button class="btn-reset handoff-history-retrigger-btn" type="button">Retrigger (clean up &amp; re-insert)</button>
      <span class="handoff-history-retrigger-result"></span>
    `;
    const btn = container.querySelector(".handoff-history-retrigger-btn");
    const resultEl = container.querySelector(".handoff-history-retrigger-result");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      resultEl.textContent = "Cleaning up and re-inserting…";
      resultEl.className = "handoff-history-retrigger-result";
      try {
        const res = await fetch("/api/handoff-history/retrigger-mssql", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ po_ref_number: poRef }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || data.message || "Retrigger failed");
        resultEl.textContent = "✓ Re-inserted — refreshing…";
        resultEl.className = "handoff-history-retrigger-result ok";
        load();
      } catch (e) {
        resultEl.textContent = "Error: " + e.message;
        resultEl.className = "handoff-history-retrigger-result warn";
        btn.disabled = false;
      }
    });
  }

  function renderRecord(rec) {
    const row = document.createElement("div");
    row.className = "order-row history-row";
    row.innerHTML = `
      <div class="order-row-head">
        <span class="order-ref">${escapeHtml(rec.po_ref_number || "—")}</span>
        <span class="order-badge ${mssqlBadgeClass(rec.mssql_status)}">MSSQL: ${escapeHtml(rec.mssql_status || "unknown")}</span>
        <span class="order-badge ${triggerBadgeClass(rec.trigger_status)}">Trigger: ${escapeHtml(rec.trigger_status || "unknown")}</span>
        <span class="order-badge ${bcBadgeClass(rec.bc_status)}">BC: ${escapeHtml(rec.bc_status || "unknown")}${rec.bc_order_no ? " (" + escapeHtml(rec.bc_order_no) + ")" : ""}</span>
      </div>
      <div class="order-detail">
        ${escapeHtml(rec.group_code || "—")} &middot; ${escapeHtml(rec.method || "—")} &middot;
        ${escapeHtml(rec.file_name || "—")} &middot; ${formatTimestamp(rec.timestamp)}
      </div>
      ${rec.mssql_error ? `<div class="order-error">MSSQL: ${escapeHtml(rec.mssql_error)}</div>` : ""}
      ${rec.bc_error ? `<div class="order-error">BC: ${escapeHtml(rec.bc_error)}</div>` : ""}
      <div class="handoff-history-retrigger-action hidden"></div>
    `;
    if (rec.trigger_status === "not_promoted" && rec.po_ref_number) {
      renderRetriggerAction(row.querySelector(".handoff-history-retrigger-action"), rec.po_ref_number);
    }
    return row;
  }

  // Groups the flat per-PO list by source file so related POs still read together,
  // largest files first — same convention the previous by-table/by-file view used.
  function renderGroupedByFile(records) {
    const byFile = new Map();
    records.forEach((rec) => {
      const key = rec.file_name || "(unknown file)";
      if (!byFile.has(key)) byFile.set(key, []);
      byFile.get(key).push(rec);
    });
    const groups = [...byFile.entries()].sort((a, b) => b[1].length - a[1].length);
    groups.forEach(([fileName, recs]) => {
      const head = document.createElement("div");
      head.className = "handoff-history-file-head";
      head.innerHTML = `<span class="handoff-history-filename">${escapeHtml(fileName)}</span><span class="handoff-history-file-count">${recs.length} PO(s)</span>`;
      listEl.appendChild(head);
      recs.forEach((rec) => listEl.appendChild(renderRecord(rec)));
    });
  }

  function render(records) {
    listEl.innerHTML = "";
    if (!records.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No handoff history found for these filters.";
      listEl.appendChild(empty);
      return;
    }
    renderGroupedByFile(records);
  }

  async function load() {
    const params = new URLSearchParams();
    if (groupCodeSelect.value) params.set("group_code", groupCodeSelect.value);
    if (mssqlStatusSelect.value) params.set("mssql_status", mssqlStatusSelect.value);
    if (bcStatusSelect.value) params.set("bc_status", bcStatusSelect.value);

    refreshBtn.disabled = true;
    statusMsgEl.textContent = "Loading…";
    try {
      const res = await fetch(`/api/handoff-history?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to load handoff history");
      render(data.data || []);
      statusMsgEl.textContent = `Loaded ${data.total != null ? data.total : (data.data || []).length} record(s).`;
    } catch (e) {
      statusMsgEl.textContent = `Error: ${e.message}`;
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.addEventListener("click", load);
  groupCodeSelect.addEventListener("change", load);
  mssqlStatusSelect.addEventListener("change", load);
  bcStatusSelect.addEventListener("change", load);
  load();
})();
