(function () {
  "use strict";

  // ── Tabs (Search vs Handoff History) — plain instant swap, same convention as
  // reconcile.js's activateActionTab, no crossfade needed for one-off page sections.
  // Handoff History's own content/data loading lives entirely in handoff-history.js
  // (included separately below); this only toggles which panel is visible.
  document.querySelectorAll(".action-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const tabKey = btn.dataset.actionTab;
      const next = document.getElementById(`action-panel-${tabKey}`);
      if (!next || btn.classList.contains("active")) return;
      document.querySelectorAll(".action-tab-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.querySelectorAll(".action-tab-panel").forEach((p) => p.classList.toggle("hidden", p !== next));
    });
  });

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

  // Polls /api/reprocess-status/<run_id> (generic — shared with Reprocess Buffer/Sync
  // on the reconcile page) until rgmc-worker-pool reports "done" or "error". Used by
  // the file-scoped retrigger below, which now runs in the worker pool instead of this
  // app's own request path (confirmed live, 2026-10-29: a large file's BigQuery lookups
  // can take several minutes — no HTTP request deadline should have to survive that).
  async function pollRunStatus(runId, onTick) {
    const POLL_INTERVAL_MS = 3000;
    const MAX_WAIT_MS = 20 * 60 * 1000; // generous — this worker has no deadline of its own
    const deadline = Date.now() + MAX_WAIT_MS;
    while (Date.now() < deadline) {
      const res = await fetch(`/api/reprocess-status/${encodeURIComponent(runId)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Failed to check retrigger status");
      if (onTick) onTick(data);
      if (data.status === "done" || data.status === "error") return data;
      await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
    }
    throw new Error("Still running after 20 minutes — check the Handoff History tab later for results.");
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
      let msg = refs.length
        ? `Found ${files.length} file(s) containing the searched ref number(s).`
        : `Found ${files.length} uploaded file(s).`;
      // Set server-side (api_dashboard_files) whenever no date range and no ref search
      // was given — an unfiltered search is an unbounded scan over the entire table, so
      // it's silently narrowed to the last 30 days instead. Said explicitly here so the
      // result count doesn't read as "that's everything" when it isn't.
      if (data.defaulted_date_window) msg += " Showing uploads from the last 30 days — set Date from to look further back.";
      setStatus(msg);
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
          ${uploadType === "po" ? `<button class="btn-reprocess dash-queue-file-btn" type="button">Queue Missing Items</button>` : ""}
          ${uploadType === "po" ? `<button class="btn-reset dash-retrigger-file-btn" type="button">Retrigger Handoff (cleanup &amp; re-insert all)</button>` : ""}
          <button class="btn-history-toggle dash-expand-btn" type="button">Expand ▾</button>
        </div>
      </div>
      <div class="dash-refs-list hidden"></div>
      <div class="handoff-history-list dash-retrigger-results hidden"></div>
    `;

    const viewFileBtn = row.querySelector(".dash-view-file-btn");
    const queueFileBtn = row.querySelector(".dash-queue-file-btn");
    const retriggerFileBtn = row.querySelector(".dash-retrigger-file-btn");
    const expandBtn = row.querySelector(".dash-expand-btn");
    const refsListEl = row.querySelector(".dash-refs-list");
    const retriggerResultsEl = row.querySelector(".dash-retrigger-results");

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

    if (queueFileBtn) {
      queueFileBtn.addEventListener("click", async () => {
        queueFileBtn.disabled = true;
        const orig = queueFileBtn.textContent;
        queueFileBtn.textContent = "Checking refs…";
        try {
          await loadRefs();
          const sbicRefs = Array.from(refsListEl.querySelectorAll(".dash-ref-row"))
            .filter((r) => {
              const c = (r.dataset.company || "").toUpperCase();
              return c.includes("SBIC") || c.includes("SUNCOAST");
            })
            .map((r) => r.dataset.ref)
            .filter(Boolean);
          if (!sbicRefs.length) {
            setStatus("No SBIC PO ref(s) found under this file — Missing Items queuing only applies to SBIC.", true);
            return;
          }
          queueFileBtn.textContent = `Queuing ${sbicRefs.length} PO(s)…`;
          const res = await fetch("/api/dashboard/queue-missing-items", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ company: "SBIC", po_ref_number: sbicRefs.join(",") }),
          });
          const data = await res.json();
          if (!res.ok) throw new Error(data.error || data.detail || "Queue failed");
          let msg = `✓ Checked ${sbicRefs.length} SBIC PO(s) — ${data.orders_with_missing_items} had missing items, queued ${data.queued_count} item(s) to the buffer`;
          if (data.skipped_count) msg += `; ${data.skipped_count} need linking/UOM resolution on the Missing Items tab`;
          if (data.errors_count) msg += `; ${data.errors_count} failed`;
          setStatus(msg, !!data.errors_count);
        } catch (e) {
          setStatus("Could not queue missing items for this file: " + e.message, true);
        } finally {
          queueFileBtn.disabled = false;
          queueFileBtn.textContent = orig;
        }
      });
    }

    if (retriggerFileBtn) {
      retriggerFileBtn.addEventListener("click", async () => {
        retriggerFileBtn.disabled = true;
        const orig = retriggerFileBtn.textContent;
        retriggerFileBtn.textContent = "Retriggering…";
        retriggerResultsEl.classList.remove("hidden");
        retriggerResultsEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> Starting retrigger for every PO in this file…</div>`;
        try {
          // Runs in rgmc-worker-pool, not this app's own request — a large file's
          // BigQuery lookups can take several minutes (confirmed live, 2026-10-29).
          // Start the job, then poll for its result instead of waiting on one HTTP call.
          const startRes = await fetch("/api/handoff-history/retrigger-file-async", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ file_name: file.file_name }),
          });
          const startData = await startRes.json();
          if (!startRes.ok) throw new Error(startData.error || startData.detail || "Could not start retrigger");

          const finalData = await pollRunStatus(startData.run_id, (d) => {
            retriggerResultsEl.innerHTML = `<div class="search-hint search-loading"><span class="btn-spinner"></span> ${
              d.status === "queued" ? "Queued — waiting for the worker pool…" : "Cleaning up and re-inserting in the background…"
            }</div>`;
          });
          if (finalData.status === "error") throw new Error(finalData.error || "Retrigger failed");

          const summary = finalData.summary || {};
          const results = summary.results || [];
          const summaryMsg = `Retriggered ${summary.total ?? results.length} PO(s) — ${summary.ok_count ?? 0} succeeded, ${summary.error_count ?? 0} failed.`;
          // r.status is "success" / "not_promoted" (inserted, but the MSSQL trigger
          // never promoted it) / "error" (the insert itself failed) — three distinct
          // outcomes, not just a pass/fail pair, so each gets its own badge color.
          retriggerResultsEl.innerHTML = `<div class="search-hint">${escapeHtml(summaryMsg)}</div>` + results.map((r) => {
            const badgeClass = r.status === "success" ? "ready" : r.status === "not_promoted" ? "pending" : "failed";
            const label = r.status === "not_promoted" ? "not promoted" : r.status;
            return `
            <div class="order-row history-row">
              <div class="order-row-head">
                <span class="order-ref">${escapeHtml(r.po_ref || "—")}</span>
                <span class="order-badge ${badgeClass}">${escapeHtml(label)}</span>
              </div>
              <div class="order-detail">${escapeHtml(r.detail || "")}</div>
            </div>
          `;
          }).join("");
        } catch (e) {
          retriggerResultsEl.innerHTML = `<div class="search-hint">Error: ${escapeHtml(e.message)}</div>`;
        } finally {
          retriggerFileBtn.disabled = false;
          retriggerFileBtn.textContent = orig;
        }
      });
    }

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
    el.dataset.ref = ref || "";
    el.dataset.company = isPo ? (row.company_name || "") : "";
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

  // Same composite key the BigQuery bridge itself already uses to decide a detail row
  // is a duplicate (mappings.required_columns['customerpouldetailbq'] = po_ref_number +
  // customer_name + customer_sku_code, kept='first') -- within one already-single-PO
  // detailRows list, po_ref_number/customer_name are constant, so customer_sku_code (or
  // the description, for blank-SKU lines) alone is the key. Without this, a PO whose
  // Document AI extraction genuinely has every line duplicated inflates the dashboard's
  // BQ count and "price/UOM variance" tally to look like a huge mismatch when the real
  // line count already matches BC. Confirmed live: PO 14356975 showed "BQ: 14" / "14
  // line(s) with variance" against BC's real 7 lines, because each line was extracted
  // twice -- deduped, it's "BQ: 7" with no variance.
  function dedupeBqLines(detailRows) {
    const seen = new Set();
    const deduped = [];
    (detailRows || []).forEach((row) => {
      const sku = (row.customer_sku_code || "").trim().toUpperCase();
      const key = sku || `__NOSKU__::${(row.customer_sku_desc || "").trim().toUpperCase()}`;
      if (seen.has(key)) return;
      seen.add(key);
      deduped.push(row);
    });
    return deduped;
  }

  // ── Detail panel (third level — detail lines + MSSQL/BC/buffer actions) ─────
  function renderDetailPanel(panel, headerRow, detailRows, uploadType) {
    const ref = refNumber(headerRow, uploadType);
    // The raw table below still shows every extracted row, duplicates included --
    // useful for spotting a genuine extraction problem. Only the MSSQL/BC comparison
    // counts below use the deduped set, so "BQ: N" reflects distinct lines.
    const dedupedDetailRows = dedupeBqLines(detailRows);
    const duplicateCount = detailRows.length - dedupedDetailRows.length;
    const duplicateNote = duplicateCount
      ? ` (ignoring ${duplicateCount} duplicate BigQuery line(s))`
      : "";
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
      bufferActionEl.classList.add("hidden");
      bufferActionEl.innerHTML = "";
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
          if (data.retrigger_supported) renderRetriggerAction(bufferActionEl, ref);
        } else {
          const mssqlDetailRows = data.detail_data || [];
          const comparisonRows = buildComparisonRows(dedupedDetailRows, (bq) => {
            if (bq.line_index == null) return undefined;
            const m = mssqlDetailRows.find((r) => r.lineIndex === bq.line_index);
            return m ? { qty: m.poQty, uom: m.unitOfMeasurement, price: m.unitPrice } : undefined;
          });
          const countMismatch = dedupedDetailRows.length !== mssqlDetailRows.length;
          const varianceCount = comparisonRows.filter((r) => r.priceVariance || r.uomVariance).length;

          let msg = `✓ In MSSQL (${mssqlDetailRows.length} detail line(s))`;
          let cls = "ok";
          if (countMismatch) {
            msg += ` — ⚠ count mismatch (BQ: ${dedupedDetailRows.length}${duplicateNote})`;
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
          viewBtn.addEventListener("click", () => showMssqlDetailsModal(dedupedDetailRows, mssqlDetailRows));
          resultEl.appendChild(viewBtn);

          // In CustomerPOULBQ (checked above) but its own AFTER INSERT trigger never
          // created the matching CustomerPOUL row — a silent, server-side promotion
          // failure the staging-table comparison above can't show on its own.
          if (data.retrigger_supported && data.promoted_in_mssql === false) {
            const warn = document.createElement("div");
            warn.className = "dash-check-result warn";
            warn.style.marginTop = "6px";
            warn.textContent = "⚠ In staging but never promoted to CustomerPOUL — the insert trigger didn't fire.";
            resultEl.after(warn);
            renderRetriggerAction(bufferActionEl, ref);
          }
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
              const rows = buildComparisonRows(dedupedDetailRows, (bq) => {
                const itemNo = resolvedItemNoForBqLine(bq, skuOverrides);
                return itemNo ? bcByItem[itemNo] : undefined;
              });
              if (dedupedDetailRows.length !== (order.salesOrderLines || []).length) countMismatch = true;
              varianceCount += rows.filter((r) => r.priceVariance || r.uomVariance).length;
            });

            let msg = `✓ Found in Business Central (${data.orders.length} order(s), ${bcLineCount} line(s))`;
            let cls = "ok";
            if (countMismatch) {
              msg += ` — ⚠ count mismatch (BQ: ${dedupedDetailRows.length}${duplicateNote})`;
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
            viewBtn.addEventListener("click", () => showBcDetailsModal(ref, data.orders, dedupedDetailRows, skuOverrides));
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

  // Same endpoint the Handoff History tab's own retrigger button uses
  // (/api/handoff-history/retrigger-mssql) — re-fetches this PO's current header/
  // detail straight from BigQuery by po_ref_number, deletes any stale CustomerPOULBQ/
  // CustomerPOULDetailBQ rows first, then re-inserts fresh so the AFTER INSERT trigger
  // gets a genuinely new row to react to. Safe whether nothing was ever inserted yet
  // (cleanup is a harmless no-op) or a previous insert got stuck un-promoted.
  function renderRetriggerAction(container, poRef) {
    container.classList.remove("hidden");
    container.innerHTML = `
      <button class="btn-reprocess dash-retrigger-mssql-btn" type="button">Retrigger Handoff (MSSQL)</button>
      <span class="dash-buffer-result"></span>
    `;
    const btn = container.querySelector(".dash-retrigger-mssql-btn");
    const resultEl = container.querySelector(".dash-buffer-result");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      resultEl.textContent = "Cleaning up and re-inserting…";
      resultEl.className = "dash-buffer-result";
      try {
        const res = await fetch("/api/handoff-history/retrigger-mssql", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ po_ref_number: poRef }),
        });
        const data = await res.json();
        if (!res.ok || data.status !== "success") throw new Error(data.message || data.error || data.detail || "Retrigger failed");
        resultEl.textContent = "✓ Re-inserted — click Check MSSQL again to confirm.";
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

  // Items counted "missing" here use the same SKU-override-then-raw-SKU resolution as
  // the Missing Items tab's own matching (resolvedItemNoForBqLine), so what's shown
  // here lines up with what Queue would actually find — but the real readiness check
  // (resolved item AND a BC-valid Unit of Measure) only happens server-side in
  // _find_missing_items when the queue button is clicked; a row shown as "missing"
  // here may still come back "skipped" if it needs linking or UOM resolution first.
  function renderMissingItemsSection(missingRows, bqTotal) {
    if (!missingRows.length) {
      return `<p class="dash-missing-items-summary ok">✓ All ${bqTotal} BigQuery item(s) are already in this BC order.</p>`;
    }
    const bodyRows = missingRows.map((r) => `
      <tr>
        <td>${escapeHtml(r.sku)}</td>
        <td>${escapeHtml(r.bqQty ?? "—")}</td>
        <td>${escapeHtml(r.bqUom || "—")}</td>
        <td>${escapeHtml(r.bqPrice ?? "—")}</td>
      </tr>
    `).join("");
    return `
      <div class="dash-missing-items">
        <p class="dash-missing-items-summary warn">⚠ ${missingRows.length} of ${bqTotal} BigQuery item(s) are missing from this BC order.</p>
        <div class="modal-table-wrap">
          <table class="modal-so-table">
            <thead><tr><th>SKU/Item</th><th>BQ Qty</th><th>BQ UOM</th><th>BQ Price</th></tr></thead>
            <tbody>${bodyRows}</tbody>
          </table>
        </div>
        <div class="dash-queue-missing-action">
          <button class="btn-reprocess dash-queue-missing-btn" type="button">Add Missing Items to Buffer</button>
          <span class="dash-queue-missing-result"></span>
        </div>
      </div>
    `;
  }

  // Only ever queues to the so_missing_item_fixes Firestore buffer — see
  // /api/dashboard/queue-missing-items. The real BC write stays behind Apply Missing
  // Item Fixes on the Buffer Reconciliation page, same queue-then-apply shape as every
  // other link in this app.
  async function queueMissingItemsToBuffer(btn, resultEl, poRefNumber) {
    btn.disabled = true;
    resultEl.textContent = "Queuing…";
    resultEl.className = "dash-queue-missing-result";
    try {
      const res = await fetch("/api/dashboard/queue-missing-items", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ company: "SBIC", po_ref_number: poRefNumber }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Queue failed");
      let msg = `✓ Queued ${data.queued_count} item(s) to the buffer`;
      if (data.skipped_count) msg += ` — ${data.skipped_count} need linking/UOM resolution on the Missing Items tab`;
      if (data.errors_count) msg += ` — ${data.errors_count} failed`;
      resultEl.textContent = msg;
      resultEl.className = "dash-queue-missing-result " + (data.errors_count ? "warn" : "ok");
      if (data.queued_count && !data.errors_count) {
        btn.remove();
      } else {
        btn.disabled = false;
      }
    } catch (e) {
      resultEl.textContent = "Error: " + e.message;
      resultEl.className = "dash-queue-missing-result warn";
      btn.disabled = false;
    }
  }

  function showBcDetailsModal(searchedRef, orders, bqDetailRows, skuOverrides) {
    const orderData = (orders || []).map((order) => {
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
      return {
        order, bcLines, rows,
        countMismatch: bqDetailRows.length !== bcLines.length,
        missingRows: rows.filter((r) => !r.found),
        poRef: order.externalDocumentNo || searchedRef || "",
      };
    });

    const html = orderData.map((od, i) => `
      <div class="dash-bc-order-block" data-order-index="${i}"${i > 0 ? ' style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border);"' : ""}>
        <p class="modal-subtitle">
          Order <strong>${escapeHtml(od.order.number || "—")}</strong> &middot;
          Status: ${escapeHtml(od.order.status || "—")} &middot;
          Sell-to: ${escapeHtml(od.order.sellToCustomerName || od.order.sellToCustomerNo || "—")} &middot;
          Order date: ${escapeHtml(od.order.orderDate || "—")} &middot;
          PO ref: ${escapeHtml(od.poRef || "—")}
        </p>
        ${renderComparisonTable(od.rows, od.countMismatch, bqDetailRows.length, od.bcLines.length, "BC")}
        <div class="dash-missing-items-block">${renderMissingItemsSection(od.missingRows, bqDetailRows.length)}</div>
      </div>
    `).join("");

    openDetailsModal("Business Central Details — BQ vs BC Comparison", html || "<p>No orders to show.</p>");

    orderData.forEach((od, i) => {
      if (!od.missingRows.length) return;
      const block = detailsModalBody.querySelector(`.dash-bc-order-block[data-order-index="${i}"]`);
      const btn = block && block.querySelector(".dash-queue-missing-btn");
      const resultEl = block && block.querySelector(".dash-queue-missing-result");
      if (!btn || !resultEl) return;
      btn.addEventListener("click", () => queueMissingItemsToBuffer(btn, resultEl, od.poRef));
    });
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
