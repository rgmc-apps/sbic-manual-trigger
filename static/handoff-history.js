(function () {
  const listEl = document.getElementById("handoff-history-list");
  const statusMsgEl = document.getElementById("handoff-history-status-msg");
  const refreshBtn = document.getElementById("handoff-history-refresh");
  const groupCodeSelect = document.getElementById("handoff-history-group-code");
  const statusSelect = document.getElementById("handoff-history-status");
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

  function renderFileGroups(byFile) {
    const fileEntries = Object.entries(byFile || {});
    if (!fileEntries.length) return "";
    // Largest files first so the most significant uploads surface without scrolling.
    fileEntries.sort((a, b) => (b[1] || []).length - (a[1] || []).length);
    return fileEntries.map(([fileName, refs]) => {
      refs = refs || [];
      const id = `handoff-refs-${Math.random().toString(36).slice(2, 10)}`;
      return `
        <div class="handoff-history-file">
          <div class="handoff-history-file-head">
            <span class="handoff-history-filename">${escapeHtml(fileName)}</span>
            <span class="handoff-history-file-count">${refs.length} PO(s)</span>
            ${refs.length ? `<button class="btn-history-toggle" type="button" data-target="${id}">Show PO refs</button>` : ""}
          </div>
          ${refs.length ? `<div class="handoff-history-refs hidden" id="${id}">${refs.map(escapeHtml).join(", ")}</div>` : ""}
        </div>
      `;
    }).join("");
  }

  function renderTables(tables) {
    const entries = Object.entries(tables || {});
    if (!entries.length) return '<p class="tab-empty">No tables recorded.</p>';
    return entries.map(([tableName, info]) => {
      const byFile = (info && info.by_file) || {};
      const count = (info && info.count) ?? 0;
      return `
        <div class="handoff-history-table">
          <div class="handoff-history-table-head">
            <strong>${escapeHtml(tableName)}</strong> — ${count} PO ref(s) across ${Object.keys(byFile).length} file(s)
          </div>
          ${renderFileGroups(byFile) || '<p class="tab-empty">No PO refs recorded.</p>'}
        </div>
      `;
    }).join("");
  }

  function renderRecord(rec) {
    const isError = rec.status === "error";
    const row = document.createElement("div");
    row.className = "order-row history-row";
    row.innerHTML = `
      <div class="order-row-head">
        <span class="order-ref">${escapeHtml(rec.group_code || "—")} &middot; ${escapeHtml(rec.method || "—")}</span>
        <span class="order-badge ${isError ? "failed" : "ready"}">${escapeHtml(rec.status || "unknown")}</span>
      </div>
      <div class="order-detail">
        ${escapeHtml(rec.date || "—")} ${escapeHtml(rec.time || "")} &middot; ${formatTimestamp(rec.timestamp)}
      </div>
      ${renderTables(rec.tables)}
      ${rec.error ? `<div class="order-error">${escapeHtml(rec.error)}</div>` : ""}
    `;
    row.querySelectorAll(".btn-history-toggle").forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = document.getElementById(btn.dataset.target);
        if (!target) return;
        const show = target.classList.contains("hidden");
        target.classList.toggle("hidden", !show);
        btn.textContent = show ? "Hide PO refs" : "Show PO refs";
      });
    });
    return row;
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
    records.forEach((rec) => listEl.appendChild(renderRecord(rec)));
  }

  async function load() {
    const params = new URLSearchParams();
    if (groupCodeSelect.value) params.set("group_code", groupCodeSelect.value);
    if (statusSelect.value) params.set("status", statusSelect.value);

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
  statusSelect.addEventListener("change", load);
  load();
})();
