// Shared cross-feature utilities for the Reconcile page (/reconcile) — split out of
// the original monolithic reconcile.js. The "Your Details" employee gate, the
// button-loading spinner helper, HTML escaping, and the run-watcher timing constants
// used by both the Buffer tab's own watcher and the Missing Ship-To tab's watcher.
"use strict";

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

  export function employeeDetailsValid() {
    return (
      employeeFields.name.value.trim() &&
      employeeFields.company.value.trim() &&
      employeeFields.department.value.trim() &&
      EMAIL_RE.test(employeeFields.email.value.trim())
    );
  }

  export function getEmployeeDetails() {
    return {
      employee_name: employeeFields.name.value.trim(),
      employee_company: employeeFields.company.value.trim(),
      employee_department: employeeFields.department.value.trim(),
      email: employeeFields.email.value.trim(),
    };
  }

  export function refreshGate() {
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
  export function setBtnLoading(spinnerId, labelId, loading, loadingText) {
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

  // textContent + error-class toggle repeated by every tab's own named status-setter
  // (setBqStatus, setMissingShiptoStatus, etc.) — kept as thin per-tab wrappers around
  // this so every call site elsewhere keeps its existing, more readable function name.
  export function setStatus(el, msg, isError) {
    el.textContent = msg || "";
    el.classList.toggle("error", !!isError);
  }

  export function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  export const RUN_POLL_MS = 4000;              // check run status every 4s — real work, not a long wait
  export const MAX_WATCH_MS = 10 * 60 * 1000;   // auto-stop after 10 minutes either way

  export function formatElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  // Ticks `el`'s text every second with the elapsed time since `startedAt` — the one
  // piece of the Buffer tab's and Missing Ship-To tab's run watchers that's genuinely
  // identical; the rest of each watcher (poll modes, DOM targets, done/error callbacks)
  // differs enough between the two that unifying it would just move the same code into
  // parameters, so only this sliver is shared. Returns the interval id to clearInterval.
  export function startElapsedTicker(el, startedAt) {
    el.textContent = "0:00";
    return setInterval(() => {
      el.textContent = formatElapsed(Date.now() - startedAt);
    }, 1000);
  }
