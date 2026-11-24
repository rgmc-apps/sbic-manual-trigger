// Page-shell tab switching for the Reconcile page — split out of the original
// monolithic reconcile.js. Imports `state`/`refreshBufferSilently` from buffer.js,
// which in turn imports `activateActionTab` from here (both only ever referenced
// inside a function body, never at module-evaluation time, so this circular import
// resolves correctly in both directions).
import { state, refreshBufferSilently } from "./buffer.js";

  // ── Tabs ──────────────────────────────────────────────────────────────────
  // Crossfade instead of an instant hide/show — the panels hold genuinely
  // different content (SKU vs branch vs customer vs orders), so this is a
  // state transition, not decoration.
  const TAB_EXIT_MS = 150;
  export function activateTab(tabKey) {
    const targetBtn = document.querySelector(`.tab-btn[data-tab="${tabKey}"]`);
    const next = document.getElementById(`panel-${tabKey}`);
    if (!targetBtn || !next || targetBtn.classList.contains("active")) return;

    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    targetBtn.classList.add("active");

    const current = document.querySelector(".tab-panel:not(.hidden)");
    if (current && current !== next) {
      current.classList.add("tab-panel-exit");
      setTimeout(() => {
        current.classList.add("hidden");
        current.classList.remove("tab-panel-exit");
      }, TAB_EXIT_MS);
    }

    next.classList.remove("hidden");
    next.classList.add("tab-panel-enter");
    void next.offsetWidth; // force reflow so the enter state is registered before transitioning out of it
    requestAnimationFrame(() => next.classList.remove("tab-panel-enter"));
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  // ── Action tabs (Buffer vs Cloud SQL process controls) ───────────────────
  // Separate class names/selectors from the .tab-btn/.tab-panel system above —
  // these switch which *controls* are shown (Load Buffer/Reprocess vs Sync/Backfill),
  // not buffer content, and must not be managed by activateTab's generic
  // ".tab-panel:not(.hidden)" lookup, which would otherwise treat both tab groups as
  // one set. A plain instant swap is enough here — no crossfade needed for controls.
  export function activateActionTab(tabKey) {
    const targetBtn = document.querySelector(`.action-tab-btn[data-action-tab="${tabKey}"]`);
    const next = document.getElementById(`action-panel-${tabKey}`);
    if (!targetBtn || !next || targetBtn.classList.contains("active")) return;

    document.querySelectorAll(".action-tab-btn").forEach((b) => b.classList.remove("active"));
    targetBtn.classList.add("active");
    document.querySelectorAll(".action-tab-panel").forEach((p) => p.classList.toggle("hidden", p !== next));

    // Reprocess/Sync/Backfill/Quick Align can all run from elsewhere (another tab, or
    // the BigQuery Lookup tab's background-triggered Reprocess/Sync) with no live
    // watcher tied to whatever's currently on screen — coming back to the Buffer tab
    // is the one moment we know for sure the user wants the current truth, not
    // whatever was last fetched.
    if (tabKey === "buffer" && state) refreshBufferSilently();
  }

  document.querySelectorAll(".action-tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => activateActionTab(btn.dataset.actionTab));
  });
