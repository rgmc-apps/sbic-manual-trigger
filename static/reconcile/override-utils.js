// Shared "resolve a candidate / save an override link" utilities used by the Buffer
// tab, Overrides tab, Missing Ship-To tab, Missing Items tab, and Customer/Ship-to
// Mismatch tab alike — split out of the original monolithic reconcile.js.
import { escapeHtml } from "./core.js";

  export const LOOKUP_PATH = { sku: "items", branch: "ship-to", customer: "customers", uom: "uom" };

  // Not-yet-applied queued fixes for `company` from one of the so-buffer/*-fixes list
  // endpoints, keyed by `keyFn` (defaults to order_id — Missing Items instead keys by
  // order_id + sku_key, since one order can have several queued item fixes). Used by
  // the Missing Ship-To / Missing Items / Customer-Ship-To Mismatch tabs to mark rows
  // that already have a fix queued. Network/HTTP failures resolve to an empty map
  // rather than throwing — a caller showing "no fix queued yet" on a failed lookup is
  // harmless, unlike surfacing a hard error for what's just a supporting annotation.
  export async function fetchPendingFixes(path, company, keyFn = (f) => f.order_id) {
    try {
      const res = await fetch(`${path}?company=${encodeURIComponent(company)}&applied=false`);
      const data = await res.json();
      if (!res.ok) return {};
      const map = {};
      (data.data || []).forEach((f) => { map[keyFn(f)] = f; });
      return map;
    } catch (e) {
      return {};
    }
  }

  // ── Candidate normalization ──────────────────────────────────────────────
  // Search results (rgmc-bc-api's RGMC custom pages) and suggestion results
  // (rgmc-gcp-api, which reads BC's *standard* items API) name fields
  // differently for the same entity, so both are checked here.
  export function normalizeCandidates(type, rawList) {
    return (rawList || []).map((c) => {
      if (type === "sku") {
        return {
          code: c.number,
          name: c.description || c.displayName || c.displayName2 || "",
          score: c.score, extra: null, raw: c,
        };
      }
      if (type === "branch") {
        // extra stays the raw customerNumber (used when saving the link, and to
        // auto-apply the matching customer link); extraLabel is what's shown.
        const custLabel = c.customerName ? `${c.customerName} (${c.customerNumber})` : c.customerNumber;
        return {
          code: c.code, name: c.name || "",
          score: c.score, extra: c.customerNumber, customerName: c.customerName || null,
          lookupCode: c.lookupCode || null,
          extraLabel: c.lookupCode ? `${custLabel} · lookup: ${c.lookupCode}` : custLabel,
          source: c.source || null,
          raw: c,
        };
      }
      if (type === "uom") {
        // rgmc-bc-api's Item Unit of Measure lookup — code/description/
        // qtyPerUnitOfMeasure, already scoped to the group's one item.
        return {
          code: c.code, name: c.description || "",
          score: null, extra: null,
          extraLabel: c.qtyPerUnitOfMeasure != null ? `qty per UOM: ${c.qtyPerUnitOfMeasure}` : null,
          raw: c,
        };
      }
      // customer — RGMC's custom customers page uses customerNo/name/address
      return { code: c.customerNo, name: c.name || "", address: c.address || "", score: c.score, extra: null, raw: c };
    });
  }

  // `key` (the raw customerSKUCode this link is for, when known) is stored as
  // referenceNo on a "sku" resolution — the same raw-text identity BC's own Item
  // Reference entity (referenceNo/itemNo) uses, so a human's manual link here reads
  // the same way a real BC item reference would if this SKU ever gets registered as
  // one. Never fetched/looked up from BC — just the override's own key, carried along.
  export function resolvedPayload(type, candidate, key) {
    if (type === "sku") return { itemNo: candidate.code, description: candidate.name, referenceNo: key || "" };
    if (type === "branch") return { customerNo: candidate.extra, shipToCode: candidate.code, name: candidate.name };
    if (type === "uom") return { uomCode: candidate.code, description: candidate.name };
    return { customerNo: candidate.code, displayName: candidate.name };
  }

  export async function saveOverride(type, key, resolved, resolvedBy, bufferIds) {
    const res = await fetch("/api/overrides", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ type, key, resolved, resolved_by: resolvedBy || "", buffer_ids: bufferIds || [] }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Save failed");
    return data;
  }

  export function resolvedDisplay(resolved) {
    if (!resolved) return "";
    if (resolved.itemNo) return `${resolved.itemNo} — ${resolved.description || ""}`;
    if (resolved.uomCode) return `${resolved.uomCode} — ${resolved.description || ""}`;
    // "customer" (displayName present, even "") vs "branch" ({customerNo, shipToCode,
    // name}, no displayName key at all) — check displayName first since a "customer"
    // override can now also carry shipToCode/shipToName (reference metadata captured
    // from the branch link it came from) and would otherwise be misrouted into the
    // "branch" formatting below.
    if (resolved.customerNo && resolved.displayName !== undefined) {
      const base = `${resolved.customerNo} — ${resolved.displayName || ""}`;
      return resolved.shipToCode
        ? `${base} (ship-to: ${resolved.shipToCode}${resolved.shipToName ? " — " + resolved.shipToName : ""})`
        : base;
    }
    if (resolved.shipToCode) return `${resolved.shipToCode} (${resolved.customerNo}) — ${resolved.name || ""}`;
    if (resolved.customerNo) return `${resolved.customerNo} — ${resolved.displayName || ""}`;
    return JSON.stringify(resolved);
  }

  // A "customer" override's key is flat ("{name}") for the common case, or composite
  // ("{name}::{branch}") when BC has more than one Customer record sharing that exact
  // name — mirrors rgmc-bc-api's so_buffer_service._customer_key_parts exactly (same
  // split, same meaning). Used to render a scoped override's key readably on the
  // Overrides tab instead of showing the raw "NAME::BRANCH" string.
  export function customerKeyParts(key) {
    const sep = (key || "").indexOf("::");
    if (sep === -1) return { name: key || "", branch: "" };
    return { name: key.slice(0, sep), branch: key.slice(sep + 2) };
  }

  // ── Candidate list rendering ─────────────────────────────────────────────
  export function renderCandidateList(container, type, candidates, onPick) {
    container.innerHTML = "";
    if (!candidates.length) {
      const empty = document.createElement("div");
      empty.className = "search-hint";
      empty.textContent = "No matches.";
      container.appendChild(empty);
      return;
    }
    const list = document.createElement("div");
    list.className = "candidate-list";
    candidates.forEach((c) => {
      const item = document.createElement("div");
      item.className = "candidate-item" + (c.source ? " candidate-item-override" : "");
      const main = document.createElement("div");
      main.className = "candidate-main";
      // Customer candidates show address inline ("name (address)") to help tell apart
      // same-named customers at different branches — everything else keeps the
      // existing "name · extraLabel" format (extraLabel/extra is branch/SKU-specific).
      const extraLabel = c.extraLabel || c.extra;
      const nameLine = c.address
        ? `${escapeHtml(c.name || "")} (${escapeHtml(c.address)})`
        : `${escapeHtml(c.name || "")}${extraLabel ? " · " + escapeHtml(extraLabel) : ""}`;
      main.innerHTML =
        (c.source ? `<div class="candidate-source">✓ ${escapeHtml(c.source)}</div>` : "") +
        `<div class="candidate-code">${escapeHtml(c.code || "")}</div>` +
        `<div class="candidate-name">${nameLine}</div>`;
      item.appendChild(main);
      if (typeof c.score === "number") {
        const score = document.createElement("span");
        score.className = "candidate-score";
        score.textContent = Math.round(c.score * 100) + "%";
        item.appendChild(score);
      }
      item.addEventListener("click", () => onPick(c));
      list.appendChild(item);
    });
    container.appendChild(list);
  }

  // Warns above the candidate list when two or more customer candidates share the
  // exact same name (not just a substring match) — resolving the Customers tab's
  // group directly saves ONE blanket link for the whole raw customer name, so if the
  // chosen candidate is one of several sharing that name, every OTHER branch sharing
  // the name would silently get routed to it too on its next reprocess, whether or not
  // that's actually correct for them. Suggests the branch-level Link instead, which
  // scopes the customer link to just that one branch automatically (see
  // applyLinkToGroup/isCustomerNameAmbiguous) — informational, never blocks the save.
  export function warnIfAmbiguousCandidates(container, candidates) {
    const counts = {};
    candidates.forEach((c) => {
      const key = (c.name || "").trim().toUpperCase();
      if (key) counts[key] = (counts[key] || 0) + 1;
    });
    if (!Object.values(counts).some((n) => n > 1)) return;
    const warn = document.createElement("div");
    warn.className = "search-hint override-scope-note";
    warn.textContent =
      "⚠ More than one BC customer shares this exact name. Linking here applies to every branch sharing the " +
      "name — if they use different ship-to addresses, resolve each branch instead (Customer Branches tab), " +
      "which scopes the customer link to just that branch automatically.";
    container.insertBefore(warn, container.firstChild);
  }

  export function overrideKeyDisplay(ov) {
    if (ov.type !== "customer") return ov.key;
    const { name, branch } = customerKeyParts(ov.key);
    return branch ? `${name} — branch: ${branch}` : name;
  }
