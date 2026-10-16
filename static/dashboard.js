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
  const detailsModalOverlay = document.getElementById("dash-details-modal-overlay");
  const detailsModalBody    = document.getElementById("dash-details-modal-body");
  const detailsModalTitle   = document.getElementById("dash-details-modal-title");
  const detailsModalClose   = document.getElementById("dash-details-modal-close");

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

  // A plain <input type="date"> carries no time component at all, so "Date to"
  // naturally meant "midnight at the start of that day" once it reached BigQuery's
  // DATETIME comparison — excluding every record from later that same day. Pins the
  // boundary times explicitly instead of leaving it to implicit date->datetime casting.
  function dateFromBoundary(value) {
    return value ? `${value}T00:00:00` : "";
  }
  function dateToBoundary(value) {
    return value ? `${value}T23:59:59` : "";
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
      if (dateFromInput.value) params.set("date_from", dateFromBoundary(dateFromInput.value));
      if (dateToInput.value) params.set("date_to", dateToBoundary(dateToInput.value));
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
        } else if (!data.header_in_mssql) {
          resultEl.textContent = "✗ Not found in MSSQL staging tables";
          resultEl.className = "dash-check-result missing";
        } else {
          const mssqlDetailRows = data.detail_data || [];
          const comparisonRows = buildComparisonRows(detailRows, (bq) => {
            if (bq.line_index == null) return undefined;
            const m = mssqlDetailRows.find((r) => r.lineIndex === bq.line_index);
            return m ? { qty: m.poQty, uom: m.unitOfMeasurement, price: m.unitPrice } : undefined;
          });
          const countMismatch = detailRows.length !== mssqlDetailRows.length;
          const varianceCount = comparisonRows.filter((r) => r.priceVariance || r.uomVariance).length;

          let msg = `✓ In MSSQL (${mssqlDetailRows.length} detail line(s))`;
          let cls = "ok";
          if (countMismatch) {
            msg += ` — ⚠ count mismatch (BQ: ${detailRows.length})`;
            cls = "warn";
          }
          if (varianceCount) {
            msg += ` — ⚠ ${varianceCount} line(s) with price/UOM variance`;
            cls = "warn";
          }
          resultEl.textContent = msg + " ";
          resultEl.className = "dash-check-result " + cls;
          const viewBtn = document.createElement("button");
          viewBtn.type = "button";
          viewBtn.className = "btn-link-toggle dash-view-mssql-btn";
          viewBtn.textContent = "View MSSQL Details";
          viewBtn.addEventListener("click", () => showMssqlDetailsModal(detailRows, mssqlDetailRows));
          resultEl.appendChild(viewBtn);
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
            const skuOverrides = await fetchSkuOverrides();
            const bcLineCount = data.orders.reduce((sum, o) => sum + (o.salesOrderLines || []).length, 0);
            let countMismatch = false;
            let varianceCount = 0;
            data.orders.forEach((order) => {
              const bcByItem = {};
              (order.salesOrderLines || []).forEach((l) => {
                const key = (l.number || "").trim().toUpperCase();
                if (key && !bcByItem[key]) bcByItem[key] = { qty: l.quantity, uom: l.unitOfMeasureCode, price: l.unitPrice };
              });
              const rows = buildComparisonRows(detailRows, (bq) => {
                const itemNo = resolvedItemNoForBqLine(bq, skuOverrides);
                return itemNo ? bcByItem[itemNo] : undefined;
              });
              if (detailRows.length !== (order.salesOrderLines || []).length) countMismatch = true;
              varianceCount += rows.filter((r) => r.priceVariance || r.uomVariance).length;
            });

            let msg = `✓ Found in Business Central (${data.orders.length} order(s), ${bcLineCount} line(s))`;
            let cls = "ok";
            if (countMismatch) {
              msg += ` — ⚠ count mismatch (BQ: ${detailRows.length})`;
              cls = "warn";
            }
            if (varianceCount) {
              msg += ` — ⚠ ${varianceCount} line(s) with price/UOM variance`;
              cls = "warn";
            }
            resultEl.textContent = msg + " ";
            resultEl.className = "dash-check-result " + cls;
            const viewBtn = document.createElement("button");
            viewBtn.type = "button";
            viewBtn.className = "btn-link-toggle dash-view-bc-btn";
            viewBtn.textContent = "View BC Details";
            viewBtn.addEventListener("click", () => showBcDetailsModal(ref, data.orders, detailRows, skuOverrides));
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

  // ── Shared details/variance modal — avoids cramming order headers, line items,
  // and BQ-vs-other comparisons inline into the already 3-level-deep tree. ────────
  function closeDetailsModal() {
    detailsModalOverlay.classList.add("hidden");
    detailsModalBody.innerHTML = "";
  }
  detailsModalClose.addEventListener("click", closeDetailsModal);
  detailsModalOverlay.addEventListener("click", (e) => { if (e.target === detailsModalOverlay) closeDetailsModal(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !detailsModalOverlay.classList.contains("hidden")) closeDetailsModal();
  });

  function openDetailsModal(title, bodyHtml) {
    detailsModalTitle.textContent = title;
    detailsModalBody.innerHTML = bodyHtml;
    detailsModalOverlay.classList.remove("hidden");
  }

  // ── BQ-vs-other-system line comparison — shared by Check MSSQL and Check
  // Business Central, since both ultimately compare the same BQ detail rows
  // (customer_sku_code/po_qty/unit_of_measurement/unit_price) against another
  // system's equivalent lines. Returns a common {sku, found, bqQty, otherQty,
  // bqUom, otherUom, bqPrice, otherPrice, priceVariance, uomVariance} shape
  // regardless of which "other" system it came from. ──────────────────────────
  function valuesVary(a, b) {
    if (a == null || a === "") return b != null && b !== "";
    if (b == null || b === "") return true;
    const na = Number(a), nb = Number(b);
    if (!Number.isNaN(na) && !Number.isNaN(nb)) return Math.abs(na - nb) >= 0.005;
    return String(a).trim().toUpperCase() !== String(b).trim().toUpperCase();
  }

  function buildComparisonRows(bqLines, matchFn) {
    const rows = bqLines.map((bq) => {
      const other = matchFn(bq);
      const bqUom = bq.unit_of_measurement || "";
      const otherUom = other ? other.uom || "" : "";
      const bqPrice = bq.unit_price;
      const otherPrice = other ? other.price : null;
      return {
        sku: bq.customer_sku_code || bq.customer_sku_desc || "(no SKU)",
        found: !!other,
        bqQty: bq.po_qty, otherQty: other ? other.qty : null,
        bqUom, otherUom,
        bqPrice, otherPrice,
        priceVariance: !!other && valuesVary(bqPrice, otherPrice),
        uomVariance: !!other && valuesVary(bqUom, otherUom),
      };
    });
    return rows;
  }

  function renderComparisonTable(rows, countMismatch, bqCount, otherCount, otherLabel) {
    const bodyRows = rows.map((r) => `
      <tr${r.priceVariance || r.uomVariance ? ' class="dash-variance-row"' : ""}>
        <td>${escapeHtml(r.sku)}</td>
        <td>${r.found ? "✓" : "✗ not found"}</td>
        <td>${escapeHtml(r.bqQty ?? "—")}</td>
        <td>${escapeHtml(r.otherQty ?? "—")}</td>
        <td${r.uomVariance ? ' class="dash-variance-cell"' : ""}>${escapeHtml(r.bqUom || "—")}</td>
        <td${r.uomVariance ? ' class="dash-variance-cell"' : ""}>${escapeHtml(r.otherUom || "—")}</td>
        <td${r.priceVariance ? ' class="dash-variance-cell"' : ""}>${escapeHtml(r.bqPrice ?? "—")}</td>
        <td${r.priceVariance ? ' class="dash-variance-cell"' : ""}>${escapeHtml(r.otherPrice ?? "—")}</td>
      </tr>
    `).join("");
    return `
      ${countMismatch ? `<p class="dash-variance-banner">⚠ Line count mismatch — BigQuery has ${bqCount} line(s), ${escapeHtml(otherLabel)} has ${otherCount}.</p>` : ""}
      <div class="modal-table-wrap">
        <table class="modal-so-table">
          <thead><tr>
            <th>SKU/Item</th><th>Found in ${escapeHtml(otherLabel)}</th>
            <th>BQ Qty</th><th>${escapeHtml(otherLabel)} Qty</th>
            <th>BQ UOM</th><th>${escapeHtml(otherLabel)} UOM</th>
            <th>BQ Price</th><th>${escapeHtml(otherLabel)} Price</th>
          </tr></thead>
          <tbody>${bodyRows || `<tr><td colspan="8">No lines to compare.</td></tr>`}</tbody>
        </table>
      </div>
    `;
  }

  async function fetchSkuOverrides() {
    try {
      const res = await fetch("/api/overrides?type=sku");
      const data = await res.json();
      if (!res.ok) return {};
      const map = {};
      (data.data || []).forEach((ov) => { map[(ov.key || "").trim().toUpperCase()] = ov.resolved || {}; });
      return map;
    } catch (e) {
      return {};
    }
  }

  // Resolves a BQ line's BC item number the same way Missing Items does: a saved SKU
  // override first, falling back to the raw customer_sku_code itself (the common case
  // where the customer's SKU code literally is the BC item number).
  function resolvedItemNoForBqLine(bq, skuOverrides) {
    const sku = (bq.customer_sku_code || "").trim();
    const key = (sku || `__NOSKU__::${(bq.customer_sku_desc || "(no SKU code, no description)").toUpperCase()}`).toUpperCase();
    const override = skuOverrides[key];
    return ((override && override.itemNo) || sku || "").trim().toUpperCase();
  }

  function showBcDetailsModal(searchedRef, orders, bqDetailRows, skuOverrides) {
    const html = (orders || []).map((order, i) => {
      const bcLines = order.salesOrderLines || [];
      const bcByItem = {};
      bcLines.forEach((l) => {
        const key = (l.number || "").trim().toUpperCase();
        if (key && !bcByItem[key]) {
          bcByItem[key] = { qty: l.quantity, uom: l.unitOfMeasureCode, price: l.unitPrice };
        }
      });
      const rows = buildComparisonRows(bqDetailRows, (bq) => {
        const itemNo = resolvedItemNoForBqLine(bq, skuOverrides);
        return itemNo ? bcByItem[itemNo] : undefined;
      });
      const countMismatch = bqDetailRows.length !== bcLines.length;
      return `
        <div class="dash-bc-order-block"${i > 0 ? ' style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);"' : ""}>
          <p class="modal-subtitle">
            Order <strong>${escapeHtml(order.number || "—")}</strong> &middot;
            Status: ${escapeHtml(order.status || "—")} &middot;
            Sell-to: ${escapeHtml(order.sellToCustomerName || order.sellToCustomerNo || "—")} &middot;
            Order date: ${escapeHtml(order.orderDate || "—")} &middot;
            PO ref: ${escapeHtml(order.externalDocumentNo || searchedRef || "—")}
          </p>
          ${renderComparisonTable(rows, countMismatch, bqDetailRows.length, bcLines.length, "BC")}
        </div>
      `;
    }).join("");
    openDetailsModal("Business Central Details — BQ vs BC Comparison", html || "<p>No orders to show.</p>");
  }

  function showMssqlDetailsModal(bqDetailRows, mssqlDetailRows) {
    const mssqlRows = (mssqlDetailRows || []).map((m) => ({
      qty: m.poQty, uom: m.unitOfMeasurement, price: m.unitPrice, lineIndex: m.lineIndex,
    }));
    const byIndex = {};
    mssqlRows.forEach((m) => { if (m.lineIndex != null) byIndex[m.lineIndex] = m; });
    const rows = buildComparisonRows(bqDetailRows, (bq) => (bq.line_index != null ? byIndex[bq.line_index] : undefined));
    const countMismatch = bqDetailRows.length !== mssqlRows.length;
    const html = renderComparisonTable(rows, countMismatch, bqDetailRows.length, mssqlRows.length, "MSSQL");
    openDetailsModal("MSSQL Staging Details — BQ vs MSSQL Comparison", html);
  }
})();
