// Entry point for the Reconcile page (/reconcile) — imports every feature module for
// its side effects (DOM-ref lookups + event-listener wiring), in the same order the
// original monolithic reconcile.js ran top-to-bottom, so initial page behavior is
// unchanged. Each module runs its own wiring immediately on import; nothing here
// needs to call an init() function itself.
import "./tabs.js";
import "./buffer.js";
import "./buffer-history.js";
import "./overrides.js";
import "./bigquery-lookup.js";
import "./missing-shipto.js";
import "./missing-items.js";
import "./customer-shipto.js";
