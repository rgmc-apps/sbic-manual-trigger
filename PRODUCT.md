# Product

## Register

product

## Users

RGMC IT/MIS staff and warehouse/ops staff who reconcile buffered SO-import purchase orders against Business Central. They work at a desk during business hours, often alongside Business Central itself, resolving real mismatches (SKU codes, ship-to branch names, customer names) that blocked an automated sales-order import. The stakes are real: a bad link can misroute a real customer order, so the job demands precision, not speed at the cost of accuracy.

## Product Purpose

Let staff manually resolve the gap between raw customer PO data (from Suncoast/SBIC POUL imports) and Business Central's own records, then re-trigger the automated import so it succeeds. Success looks like: buffered orders drain to zero, links are reusable the next time the same raw SKU/branch/customer appears, and staff always know the true current state of the buffer and any in-flight reprocess run — never a guess.

## Brand Personality

Precise, fast, trustworthy. Closest reference: Linear — crisp, confident, restrained color, dense data handled gracefully without feeling cluttered.

## Anti-references

Not flashy or gamified. No consumer-app playfulness, no badges/confetti/mascots, no bouncy/elastic motion. This is a tool for correcting real financial data, not a dashboard to delight casual users.

## Design Principles

- Show real system state, never invented progress. Loading and waiting states must reflect actual backend truth (buffer counts, retry attempts, resolved-link counts) — no fake progress bars disconnected from what's really happening.
- Confidence over decoration. Motion and visual weight exist to build trust in a workflow that touches real orders, not to entertain.
- Long waits stay honest. Triggering a buffer reprocess kicks off an async job (results can take a minute or more, delivered by email) — the UI must clearly communicate "triggered, still working" without faking completion or leaving staff wondering if it hung.
- Dense data stays legible. This page routinely shows tables of SKU/branch/customer mismatches; hierarchy and restraint matter more than flourish.
- Protect focus during resolution work. Staff are actively searching and linking records — avoid motion that steals attention from search results or candidate lists mid-task.

## Accessibility & Inclusion

Respect `prefers-reduced-motion`: purposeful motion by default, falling back to minimal/no motion when the OS setting requests it. No explicit WCAG level specified beyond that — maintain the existing color-contrast-safe token palette and solid semantic HTML.
