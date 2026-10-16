(function () {
  "use strict";

  const uploadTypeSelect = document.getElementById("dash-upload-type");
  const refInput         = document.getElementById("dash-ref-input");
  const refLabel         = document.getElementById("dash-ref-label");
  const companySelect    = document.getElementById("dash-company");
  const companyHint      = document.getElementById("dash-company-hint");
  const dateFromInput    = document.getElementById("dash-date-from");
  const dateToInput      = document.getElementById("dash-date-to");
  const searchBtn        = document.getElementById("dash-search-btn");
  const statusLine       = document.getElementById("dash-status-line");
  const fileListEl       = document.getElementById("dash-file-list");
  const bcModalOverlay   = document.getElementById("dash-bc-modal-overlay");
  const bcModalBody      = document.getElementById("dash-bc-modal-body");
  const bcModalClose     = document.getElementById("dash-bc-modal-close");

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  function setStatus(msg, isError) {
    statusLine.textContent = msg || "";
    statusLine.classList.toggle("error", !!isError);
  }

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

  // RA / RA(xml) have no company_name column at all (confirmed via bq show) — the
  // Company filter only ever applies to PO Upload. Disabling it (rather than silently
  // ignoring a selected value) keeps that limitation visible instead of surprising.
  function updateCompanyFilterAvailability() {
    const isPo = uploadTypeSelect.value === "po";
    companySelect.disabled = !isPo;
    if (!isPo) companySelect.value = "";
    companyHint.classList.toggle("hidden", isPo);
    companyHint.textContent = isPo
      ? ""
      : `${uploadTypeSelect.options[uploadTypeSelect.selectedIndex].text} has no company column in BigQuery — company filtering isn't available for this upload type.`;
  }
  uploadTypeSelect.addEventListener("change", updateCompanyFilterAvailability);
  updateCompanyFilterAvailability();

  function updateRefFieldLabel() {
    const isPo = uploadTypeSelect.value === "po";
    refLabel.textContent = isPo ? "PO Ref Number(s)" : "RA Ref Number(s)";
    refInput.placeholder = isPo
      ? "e.g. PO414226100110212 (comma-separated for multiple)"
      : "e.g. 0197984 (comma-separated for multiple)";
  }
  uploadTypeSelect.addEventListener("change", updateRefFieldLabel);
  updateRefFieldLabel();

  function fmtDate(value) {
    if (!value) return "—";
    try {
      return new Date(value).toLocaleString();
    } catch (e) {
      return String(value);
    }
  }

  // ── File list (top level) ────────────────────────────────────────────────

  // Comma/whitespace-separated ref numbers, normalized for case-insensitive exact-match
  // highlighting — mirrors _split_po_refs' splitting convention server-side.
  function parseRefInput(raw) {
    return (raw || "").split(/[,\s]+/).map((r) => r.trim()).filter(Boolean);
  }

  async function searchFiles() {
    const uploadType = uploadTypeSelect.value;
    const refs = parseRefInput(refInput.value);
    const highlightRefs = new Set(refs.map((r) => r.toUpperCase()));
    searchBtn.disabled = true;
    setBtnLoading("dash-search-spinner", "dash-search-btn-label", true, "Searching…");
    setStatus(refs.length ? `Searching for file(s) containing ${refs.length} ref number(s)…` : "Searching BigQuery for uploaded files…");
    try {
      const params = new URLSearchParams({ upload_type: uploadType });
      if (refs.length) params.set("ref_values", refs.join(","));
      if (companySelect.value) params.set("company", companySelect.value);
      if (dateFromInput.value) params.set("date_from", dateFromInput.value);
      if (dateToInput.value) params.set("date_to", dateToInput.value);
      const res = await fetch(`/api/dashboard/files?${params.toString()}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Search failed");
      const files = data.data || [];
      renderFileList(files, uploadType, highlightRefs);
      setStatus(
        refs.length
          ? `Found ${files.length} file(s) containing the searched ref number(s).`
          : `Found ${files.length} uploaded file(s).`
      );
    } catch (e) {
      fileListEl.innerHTML = "";
      setStatus("Error: " + e.message, true);
    } finally {
      searchBtn.disabled = false;
      setBtnLoading("dash-search-spinner", "dash-search-btn-label", false);
    }
  }
  searchBtn.addEventListener("click", searchFiles);
  refInput.addEventListener("keydown", (e) => { if (e.key === "Enter") searchFiles(); });

  function renderFileList(files, uploadType, highlightRefs) {
    fileListEl.innerHTML = "";
    if (!files.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No uploaded files found for these filters.";
      fileListEl.appendChild(empty);
      return;
    }
    files.forEach((f) => fileListEl.appendChild(buildFileRow(f, uploadType, highlightRefs)));
  }

  function buildFileRow(file, uploadType, highlightRefs) {
    const row = document.createElement("div");
    row.className = "dash-file-row";
    const lastSegment = (file.file_name || "").split("/").pop() || file.file_name || "(unnamed file)";
    row.innerHTML = `
      <div class="dash-file-head">
        <div class="dash-file-name-wrap">
          <span class="dash-file-name" title="${escapeHtml(file.file_name || "")}">${escapeHtml(lastSegment)}</span>
          <span class="dash-file-meta">
            ${file.record_count ?? 0} record(s) &middot;
            ${escapeHtml(fmtDate(file.first_created_at))} – ${escapeHtml(fmtDate(file.last_created_at))}
          </span>
        </div>
        <div class="dash-file-actions">
          <button class="btn-link-toggle dash-view-file-btn" type="button">View file</button>
          <button class="btn-history-toggle dash-expand-btn" type="button">Expand ▾</button>
        </div>
      </div>
      <div class="dash-refs-list hidden"></div>
    `;

    const viewFileBtn = row.querySelector(".dash-view-file-btn");
    const expandBtn = row.querySelector(".dash-expand-btn");
    const refsListEl = row.querySelector(".dash-refs-list");

    viewFileBtn.addEventListener("click", async () => {
      viewFileBtn.disabled = true;
      const orig = viewFileBtn.textContent;
      viewFileBtn.textContent = "Looking up…";
      try {
        const url = `/api/dashboard/source-file?upload_type=${encodeURIComponent(uploadType)}&file_name=${encodeURIComponent(file.file_name)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Lookup failed");
        if (data.status === "found") {
          window.open(data.url, "_blank", "noopener");
        } else if (data.status === "not_available") {
          setStatus(data.message || "The original file isn't available for this upload type yet.", true);
        } else {
          setStatus("The original uploaded file could not be found in the input bucket (it may have been processed through a non-standard path).", true);
        }
      } catch (e) {
        setStatus("Could not look up the uploaded file: " + e.message, true);
      } finally {
        viewFileBtn.disabled = false;
        viewFileBtn.textContent = orig;
      }
    });

    async function loadRefs() {
      if (refsListEl.dataset.loaded) return;
      refsListEl.dataset.loaded = "1";
      refsListEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Loading…</div>`;
      try {
        const url = `/api/dashboard/file-records?upload_type=${encodeURIComponent(uploadType)}&file_name=${encodeURIComponent(file.file_name)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Lookup failed");
        renderRefsList(refsListEl, data.data || [], uploadType, highlightRefs);
      } catch (e) {
        refsListEl.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
      }
    }

    expandBtn.addEventListener("click", () => {
      const opening = refsListEl.classList.toggle("hidden") === false;
      expandBtn.textContent = opening ? "Collapse ▴" : "Expand ▾";
      if (opening) loadRefs();
    });

    // A ref-number search is really asking "show me what's in the file(s) containing
    // this ref" — auto-expanding every result (rather than requiring an extra click
    // per file) gets straight to the highlighted row(s) the search was for.
    if (highlightRefs && highlightRefs.size) {
      refsListEl.classList.remove("hidden");
      expandBtn.textContent = "Collapse ▴";
      loadRefs();
    }

    return row;
  }

  // ── Refs list (second level — one per PO/RA ref sharing this file) ──────────
  function refNumber(row, uploadType) {
    return uploadType === "po" ? row.po_ref_number : row.ra_ref_number;
  }

  function renderRefsList(container, rows, uploadType, highlightRefs) {
    container.innerHTML = "";
    if (!rows.length) {
      container.innerHTML = `<div class="search-hint">No records found for this file.</div>`;
      return;
    }
    // Refs within one already-expanded file all share the same file_name, so there's
    // nothing meaningful left to sort BY file name here — sorted by ref number instead,
    // for a stable, predictable order.
    const sorted = [...rows].sort((a, b) => (refNumber(a, uploadType) || "").localeCompare(refNumber(b, uploadType) || ""));
    sorted.forEach((row) => container.appendChild(buildRefRow(row, uploadType, highlightRefs)));
  }

  function buildRefRow(row, uploadType, highlightRefs) {
    const ref = refNumber(row, uploadType);
    const isHighlighted = !!(highlightRefs && ref && highlightRefs.has(String(ref).trim().toUpperCase()));
    const el = document.createElement("div");
    el.className = "dash-ref-row" + (isHighlighted ? " is-highlighted" : "");
    const isPo = uploadType === "po";
    el.innerHTML = `
      <div class="dash-ref-head">
        <span class="order-ref">${escapeHtml(ref || "(no ref)")}</span>
        <button class="btn-history-toggle dash-more-details-btn" type="button">More details ▾</button>
      </div>
      <div class="order-detail">
        ${isPo
          ? `Company: ${escapeHtml(row.company_name || "—")} &middot; Branch: ${escapeHtml(row.customer_branch_name || "—")} &middot; Created: ${escapeHtml(fmtDate(row.created_at))}`
          : `Customer: ${escapeHtml(row.customer_name || "—")} &middot; Created: ${escapeHtml(fmtDate(row.created_at))}`
        }
      </div>
      <div class="dash-detail-panel hidden"></div>
    `;

    const moreBtn = el.querySelector(".dash-more-details-btn");
    const panel = el.querySelector(".dash-detail-panel");

    moreBtn.addEventListener("click", async () => {
      const opening = panel.classList.toggle("hidden") === false;
      moreBtn.textContent = opening ? "Hide details ▴" : "More details ▾";
      if (opening && !panel.dataset.loaded) {
        panel.dataset.loaded = "1";
        panel.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Loading…</div>`;
        try {
          const url = `/api/dashboard/detail-records?upload_type=${encodeURIComponent(uploadType)}&ref_value=${encodeURIComponent(ref)}`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || data.detail || "Lookup failed");
          renderDetailPanel(panel, row, data.data || [], uploadType);
        } catch (e) {
          panel.innerHTML = `<div class="search-hint">${escapeHtml(e.message)}</div>`;
        }
      }
    });

    return el;
  }

  // ── Detail panel (third level — detail lines + MSSQL/BC/buffer actions) ─────
  function renderDetailPanel(panel, headerRow, detailRows, uploadType) {
    const ref = refNumber(headerRow, uploadType);
    const isPo = uploadType === "po";
    const company = (headerRow.company_name || "").trim().toUpperCase();
    const isSbicPo = isPo && (company.includes("SBIC") || company.includes("SUNCOAST"));
    const columns = isPo
      ? ["customer_sku_code", "customer_sku_desc", "po_qty", "unit_of_measurement", "unit_price"]
      : ["customer_sku_code", "customer_sku_desc", "po_qty", "unit_price"];

    const tableRows = detailRows.map((d) => `
      <tr>${columns.map((c) => `<td>${escapeHtml(d[c] ?? "—")}</td>`).join("")}</tr>
    `).join("");

    panel.innerHTML = `
      <div class="dash-detail-table-wrap">
        <table class="dash-detail-table">
          <thead><tr>${columns.map((c) => `<th>${escapeHtml(c.replace(/_/g, " "))}</th>`).join("")}</tr></thead>
          <tbody>${tableRows || `<tr><td colspan="${columns.length}">No detail lines found.</td></tr>`}</tbody>
        </table>
      </div>
      <div class="dash-action-row">
        <button class="btn-history-toggle dash-check-mssql-btn" type="button">Check MSSQL</button>
        ${isSbicPo ? `<button class="btn-history-toggle dash-check-bc-btn" type="button">Check Business Central</button>` : ""}
        <span class="dash-check-result"></span>
      </div>
      <div class="dash-buffer-action hidden"></div>
    `;

    const resultEl = panel.querySelector(".dash-check-result");
    const mssqlBtn = panel.querySelector(".dash-check-mssql-btn");
    const bcBtn = panel.querySelector(".dash-check-bc-btn");
    const bufferActionEl = panel.querySelector(".dash-buffer-action");

    mssqlBtn.addEventListener("click", async () => {
      mssqlBtn.disabled = true;
      resultEl.textContent = "Checking MSSQL…";
      resultEl.className = "dash-check-result";
      try {
        const url = `/api/dashboard/mssql-status?upload_type=${encodeURIComponent(uploadType)}&ref_value=${encodeURIComponent(ref)}`;
        const res = await fetch(url);
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Check failed");
        if (data.check_failed) {
          resultEl.textContent = `⚠ Could not confirm MSSQL status${data.error ? ": " + data.error : ""} — try again.`;
          resultEl.className = "dash-check-result warn";
        } else {
          resultEl.textContent = data.header_in_mssql
            ? `✓ In MSSQL (${data.detail_count_in_mssql ?? 0} detail line(s))`
            : "✗ Not found in MSSQL staging tables";
          resultEl.className = "dash-check-result " + (data.header_in_mssql ? "ok" : "missing");
        }
      } catch (e) {
        resultEl.textContent = "Error: " + e.message;
        resultEl.className = "dash-check-result warn";
      } finally {
        mssqlBtn.disabled = false;
      }
    });

    if (bcBtn) {
      bcBtn.addEventListener("click", async () => {
        bcBtn.disabled = true;
        resultEl.textContent = "Checking Business Central…";
        resultEl.className = "dash-check-result";
        bufferActionEl.classList.add("hidden");
        bufferActionEl.innerHTML = "";
        try {
          // Always "SBIC" here, never headerRow.company_name's raw free text (e.g.
          // "SUNCOAST BRANDS INTERNATIONAL CORP") -- this button only ever renders when
          // isSbicPo already matched that text against SBIC/SUNCOAST, and the backend
          // only supports the real BC company code "SBIC" anyway (see RECONCILE_COMPANIES
          // in app.py — BC Production has no company matching the raw text verbatim).
          const url = `/api/dashboard/bc-status?upload_type=${encodeURIComponent(uploadType)}&ref_value=${encodeURIComponent(ref)}&company=SBIC`;
          const res = await fetch(url);
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || data.detail || "Check failed");
          if (data.in_bc) {
            resultEl.innerHTML = `✓ Found in Business Central (${data.orders.length} order(s)) `;
            resultEl.className = "dash-check-result ok";
            const viewBtn = document.createElement("button");
            viewBtn.type = "button";
            viewBtn.className = "btn-link-toggle dash-view-bc-btn";
            viewBtn.textContent = "View BC Details";
            viewBtn.addEventListener("click", () => openBcDetailsModal(ref, data.orders));
            resultEl.appendChild(viewBtn);
          } else {
            resultEl.textContent = "✗ Not found in Business Central";
            resultEl.className = "dash-check-result missing";
            renderAddToBufferAction(bufferActionEl, headerRow, detailRows);
          }
        } catch (e) {
          resultEl.textContent = "Error: " + e.message;
          resultEl.className = "dash-check-result warn";
        } finally {
          bcBtn.disabled = false;
        }
      });
    }
  }

  function renderAddToBufferAction(container, headerRow, detailRows) {
    container.classList.remove("hidden");
    container.innerHTML = `
      <button class="btn-reprocess dash-add-to-buffer-btn" type="button">Add to Buffer</button>
      <span class="dash-buffer-result"></span>
    `;
    const btn = container.querySelector(".dash-add-to-buffer-btn");
    const resultEl = container.querySelector(".dash-buffer-result");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      resultEl.textContent = "Adding to buffer…";
      resultEl.className = "dash-buffer-result";
      try {
        const res = await fetch("/api/dashboard/add-to-buffer", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ header: headerRow, details: detailRows, employee: {} }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || data.detail || "Add to buffer failed");
        resultEl.textContent = "✓ Added to the SO-import buffer — resolve any flagged links on the Buffer Reconciliation page, then Reprocess Buffer.";
        resultEl.className = "dash-buffer-result ok";
        btn.remove();
      } catch (e) {
        resultEl.textContent = "Error: " + e.message;
        resultEl.className = "dash-buffer-result warn";
        btn.disabled = false;
      }
    });
  }

  // ── Business Central details modal — shown instead of cramming order headers +
  // line items inline into the already 3-level-deep tree. ──────────────────────
  function closeBcDetailsModal() {
    bcModalOverlay.classList.add("hidden");
    bcModalBody.innerHTML = "";
  }
  bcModalClose.addEventListener("click", closeBcDetailsModal);
  bcModalOverlay.addEventListener("click", (e) => { if (e.target === bcModalOverlay) closeBcDetailsModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !bcModalOverlay.classList.contains("hidden")) closeBcDetailsModal();
  });

  function openBcDetailsModal(searchedRef, orders) {
    bcModalBody.innerHTML = (orders || []).map((order, i) => {
      const lines = order.salesOrderLines || [];
      const lineRows = lines.map((l) => `
        <tr>
          <td>${escapeHtml(l.number || "—")}</td>
          <td>${escapeHtml(l.description || "—")}</td>
          <td>${escapeHtml(l.quantity ?? "—")}</td>
          <td>${escapeHtml(l.unitOfMeasureCode || "—")}</td>
          <td>${escapeHtml(l.unitPrice ?? "—")}</td>
          <td>${escapeHtml(l.lineAmount ?? "—")}</td>
        </tr>
      `).join("");
      return `
        <div class="dash-bc-order-block"${i > 0 ? ' style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);"' : ""}>
          <p class="modal-subtitle">
            Order <strong>${escapeHtml(order.number || "—")}</strong> &middot;
            Status: ${escapeHtml(order.status || "—")} &middot;
            Sell-to: ${escapeHtml(order.sellToCustomerName || order.sellToCustomerNo || "—")} &middot;
            Order date: ${escapeHtml(order.orderDate || "—")} &middot;
            PO ref: ${escapeHtml(order.externalDocumentNo || searchedRef || "—")}
          </p>
          <div class="modal-table-wrap">
            <table class="modal-so-table">
              <thead><tr><th>Item</th><th>Description</th><th>Qty</th><th>UOM</th><th>Unit Price</th><th>Amount</th></tr></thead>
              <tbody>${lineRows || `<tr><td colspan="6">No lines found.</td></tr>`}</tbody>
            </table>
          </div>
        </div>
      `;
    }).join("");
    bcModalOverlay.classList.remove("hidden");
  }
})();
