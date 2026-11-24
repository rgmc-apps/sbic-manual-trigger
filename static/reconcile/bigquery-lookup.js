// BigQuery (int_document_ai) Lookup tab — finds POs Document AI parsed into BigQuery
// that may never have reached MSSQL/BC, Quick Align, and report generation — split
// out of the original monolithic reconcile.js. The "Generate Report" button handler
// was physically at the very end of the original file (after the Customer/Ship-to
// section) despite belonging to this tab — kept here, where it logically belongs.
import { getEmployeeDetails, refreshGate, setBtnLoading, escapeHtml, setStatus } from "./core.js";
import { state, startWatching } from "./buffer.js";

  const bqPoRefInput      = document.getElementById("bq-po-ref-input");
  const bqCustomerInput   = document.getElementById("bq-customer-input");
  const bqCompanySelect   = document.getElementById("bq-company-select");
  const bqDateFrom        = document.getElementById("bq-date-from");
  const bqDateTo          = document.getElementById("bq-date-to");
  const bqSearchBtn       = document.getElementById("bq-search-btn");
  const bqStatusLine      = document.getElementById("bq-status-line");
  const bqResultsList     = document.getElementById("bq-results-list");
  const bqInsertAction    = document.getElementById("bq-insert-action");
  const bqReportBtn       = document.getElementById("bq-report-btn");
  const bqReportResult    = document.getElementById("bq-report-result");
  const bqQuickAlignAllBtn = document.getElementById("bq-quick-align-all-btn");
  const bqGroupToolbar    = document.getElementById("bq-group-toolbar");
  const bqGroupSelect     = document.getElementById("bq-group-select");
  const documentAiRowTemplate = document.getElementById("document-ai-row-template");

  // ── BigQuery (int_document_ai) lookup tab ────────────────────────────────────
  // Finds POs Document AI parsed into BigQuery that may never have reached MSSQL at
  // all — independent of the normal automated bridge. Only a header not already in
  // MSSQL can be selected for insertion; one already there is shown (so the user can
  // see its BC/MSSQL status) but its checkbox stays disabled — the normal sync/
  // backfill/reprocess flows already cover it.
  let bqDetails = []; // the current search's full detail_data, filtered per-row by po_ref_number when expanded
  let bqHeaders = []; // the current search's full header list, for re-matching a selection back to its exact BigQuery-shaped dict
  let bqGroupMode = "flat"; // "flat" | "presence" — see bqPresenceBucket/renderBqResultsGrouped

  export function setBqStatus(msg, isError) {
    setStatus(bqStatusLine, msg, isError);
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

  // Same raw-SKU-or-description identity app.py's _buffer_line_key/so_buffer_service's
  // _line_key use server-side, so a detail line here matches the bufferedSkus keys the
  // search response already computed against the actual buffer doc's lines.
  function bqLineBufferKey(l) {
    const sku = (l.customer_sku_code || "").trim();
    return (sku || (l.customer_sku_desc || "").trim() || "(no SKU code, no description)").toUpperCase();
  }

  function renderBqDetailLines(poRef, bufferedSkus) {
    const lines = bqDetails.filter((d) => d.po_ref_number === poRef);
    if (!lines.length) return '<p class="tab-empty">No detail lines found for this PO in BigQuery.</p>';
    const bufferedSet = bufferedSkus ? new Set(bufferedSkus) : null;
    const rows = lines.map((l) => `
      <tr>
        <td>${escapeHtml(l.customer_sku_code || "—")}</td>
        <td>${escapeHtml(l.customer_sku_desc || "—")}</td>
        <td>${escapeHtml(l.po_qty ?? l.po_qty_pcs ?? "—")}</td>
        <td>${escapeHtml(l.unit_price ?? "—")}</td>
        <td>${bufferedSet ? (bufferedSet.has(bqLineBufferKey(l)) ? "✓" : "—") : "—"}</td>
      </tr>
    `).join("");
    return `
      <table>
        <thead><tr><th>SKU</th><th>Description</th><th>Qty</th><th>Unit Price</th><th>In Buffer?</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function buildDocumentAiRow(header) {
    const node = documentAiRowTemplate.content.cloneNode(true);
    const row = node.querySelector(".bq-row");
    const poRef = header.po_ref_number || "";
    row.dataset.poRef = poRef;

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

    // Unlike MSSQL/BC, being in the buffer isn't inherently bad (a brand-new PO sits
    // there briefly before its first retry) — "missing"/warn styling just flags it as
    // worth a look, "unknown"/neutral styling for the common not-buffered case.
    row.querySelector(".bq-status-buffer").className =
      `bq-status-badge bq-status-buffer ${header.in_buffer ? "missing" : "unknown"}`;
    row.querySelector(".bq-status-buffer").textContent = header.in_buffer ? "In Buffer" : "Not buffered";

    const toggleBtn = row.querySelector(".bq-detail-toggle");
    const detailEl = row.querySelector(".bq-detail-lines");
    toggleBtn.addEventListener("click", () => {
      const show = detailEl.classList.contains("hidden");
      if (show && !detailEl.dataset.rendered) {
        detailEl.innerHTML = renderBqDetailLines(poRef, header.bufferedSkus);
        detailEl.dataset.rendered = "1";
      }
      detailEl.classList.toggle("hidden", !show);
      toggleBtn.textContent = show ? "Hide detail lines" : "Show detail lines";
    });

    return row;
  }

  const QUICK_ALIGN_ICON = {
    inserted: "✅", merged: "✅", triggered: "✅", aligned: "✅",
    blocked: "⛔", unknown: "❓", not_found: "❓", failed: "❌",
  };
  const QUICK_ALIGN_ASYNC_NOTE =
    "⏳ Reprocess Buffer / Sync from Cloud SQL run in the background — re-run this search " +
    "later, or check the Buffer/History tabs, to confirm each PO fully resolved.";

  // Checks every currently-searched PO against all three data sources server-side
  // (BigQuery is always the source of truth) and runs whichever existing tool brings
  // MSSQL/BC up to match it — see /api/bigquery/quick-align. One batch call for the
  // whole result set (not one call per row), and server-side that one call fans out
  // into at most one Reprocess Buffer trigger and one Sync from Cloud SQL trigger
  // TOTAL — every company touched is folded into a single companies=A,B,C call for
  // each, processed as one batch on the worker pool, rather than one HTTP call per
  // company, since /customerpoul/reprocess-buffer and /customerpoul/sync-inserted-
  // orders are rate-limited per endpoint path (not per company) and would 429 on the
  // second company otherwise.
  export async function quickAlignAll() {
    if (!refreshGate()) {
      setBqStatus("Fill in your details above first.", true);
      return;
    }
    if (!bqHeaders.length) {
      setBqStatus("Nothing to align — run a search first.", true);
      return;
    }
    const spinner = document.getElementById("bq-quick-align-all-spinner");
    const label = document.getElementById("bq-quick-align-all-btn-label");
    bqQuickAlignAllBtn.disabled = true;
    spinner.classList.remove("hidden");
    label.textContent = `Aligning ${bqHeaders.length} PO(s)…`;
    setBqStatus(`Running Quick Align for ${bqHeaders.length} PO(s)…`);
    try {
      const res = await fetch("/api/bigquery/quick-align", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ headers: bqHeaders, details: bqDetails, employee: getEmployeeDetails() }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || data.detail || "Quick Align failed");
      const results = data.data || [];

      // Group per-PO messages, plus company-level ones (po_ref === null — the single
      // Reprocess Buffer trigger covering every "not yet in BC" PO for that company
      // at once) onto every row for that company.
      const byPoRef = {};
      const byCompany = {};
      results.forEach((r) => {
        if (r.po_ref) (byPoRef[r.po_ref] = byPoRef[r.po_ref] || []).push(r);
        else if (r.company) (byCompany[r.company] = byCompany[r.company] || []).push(r);
      });

      bqHeaders.forEach((h) => {
        const poRef = h.po_ref_number || "";
        const row = bqResultsList.querySelector(`.bq-row[data-po-ref="${CSS.escape(poRef)}"]`);
        if (!row) return;
        const resultEl = row.querySelector(".bq-align-result");
        const ownMsgs = byPoRef[poRef] || [];
        const company = ownMsgs.length ? ownMsgs[0].company : null;
        const msgs = ownMsgs.concat(company && byCompany[company] ? byCompany[company] : []);
        if (!msgs.length) return; // "none" rows with nothing worth showing inline
        resultEl.innerHTML = msgs.map((m) => `
          <div>${QUICK_ALIGN_ICON[m.status] || "❓"} ${escapeHtml(m.detail)}</div>
        `).join("") + `<div class="bq-align-async-note">${QUICK_ALIGN_ASYNC_NOTE}</div>`;
        resultEl.classList.remove("hidden");
      });

      const okCount = results.filter((r) => ["inserted", "merged", "triggered"].includes(r.status)).length;
      setBqStatus(`${okCount} of ${results.length} step(s) completed. ${QUICK_ALIGN_ASYNC_NOTE}`);

      // If the Buffer tab is already loaded for one of the companies this just
      // triggered Reprocess/Sync for, watch that run through to completion the exact
      // same way the Buffer tab's own buttons do — so switching over (or just leaving
      // it open) shows the real end state instead of whatever was last fetched.
      const runIds = data.run_ids || [];
      const matching = state && runIds.find((r) => r.company === state.company);
      if (matching) startWatching(matching.run_id, state.order_count);
    } catch (e) {
      setBqStatus("Could not run Quick Align: " + e.message, true);
    } finally {
      bqQuickAlignAllBtn.disabled = false;
      spinner.classList.add("hidden");
      label.textContent = "⚡ Quick Align All";
    }
  }

  bqQuickAlignAllBtn.addEventListener("click", quickAlignAll);

  // Buckets a header by MSSQL/BC presence so results can be grouped for easier
  // checking (see bq-group-select) instead of scanning per-row badges on a flat
  // list — mutually exclusive, ordered most- to least-actionable. in_bc is always
  // boolean from _enrich_document_ai_status (never null), in_mssql can be null when
  // the live MSSQL check itself failed (see bqBadgeClass).
  function bqPresenceBucket(header) {
    const mssql = header.in_mssql;
    const bc = header.in_bc;
    if (mssql === false && bc === false) return "missing_both";
    if (mssql === false) return "missing_mssql";
    if (bc === false) return "missing_bc";
    if (mssql == null || bc == null) return "unknown";
    return "aligned";
  }

  const BQ_PRESENCE_GROUPS = [
    { key: "missing_both", label: "❌ Missing from MSSQL and Business Central" },
    { key: "missing_mssql", label: "⚠️ Missing from MSSQL only (already in BC)" },
    { key: "missing_bc", label: "⚠️ Missing from Business Central only (already in MSSQL)" },
    { key: "unknown", label: "❓ Status unknown — a live check failed, re-run the search" },
    { key: "aligned", label: "✅ Present in both" },
  ];

  function renderBqResultsGrouped(headers) {
    const buckets = {};
    headers.forEach((h) => {
      const key = bqPresenceBucket(h);
      (buckets[key] = buckets[key] || []).push(h);
    });
    BQ_PRESENCE_GROUPS.forEach(({ key, label }) => {
      const rows = buckets[key];
      if (!rows || !rows.length) return;
      const heading = document.createElement("div");
      heading.className = "bq-group-heading";
      heading.innerHTML = `<span>${label}</span><span class="bq-group-count">${rows.length}</span>`;
      bqResultsList.appendChild(heading);
      rows.forEach((h) => bqResultsList.appendChild(buildDocumentAiRow(h)));
    });
  }

  function renderBqResults(headers) {
    bqResultsList.innerHTML = "";
    bqReportResult.classList.add("hidden");
    if (!headers.length) {
      const empty = document.createElement("div");
      empty.className = "tab-empty";
      empty.textContent = "No matching records found in BigQuery.";
      bqResultsList.appendChild(empty);
      bqInsertAction.classList.add("hidden");
      bqGroupToolbar.classList.add("hidden");
      return;
    }
    if (bqGroupMode === "presence") {
      renderBqResultsGrouped(headers);
    } else {
      headers.forEach((h) => bqResultsList.appendChild(buildDocumentAiRow(h)));
    }
    bqInsertAction.classList.remove("hidden");
    bqGroupToolbar.classList.remove("hidden");
  }

  bqGroupSelect.addEventListener("change", () => {
    bqGroupMode = bqGroupSelect.value;
    if (bqHeaders.length) renderBqResults(bqHeaders);
  });

  export async function searchDocumentAi() {
    const params = new URLSearchParams();
    const poRef = bqPoRefInput.value.trim();
    const customer = bqCustomerInput.value.trim();
    const company = bqCompanySelect.value;
    if (poRef) params.set("po_ref_number", poRef);
    if (customer) params.set("customer_name", customer);
    if (company) params.set("company", company);
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
      setBqStatus(`Found ${bqHeaders.length} header(s) in BigQuery${company ? ` for ${company}` : ""}.`);
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

  // Generate Report covers every result the current search found — it's a status
  // snapshot for a person-in-charge, not an action on BC/MSSQL.
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
      company: bqCompanySelect.value,
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
