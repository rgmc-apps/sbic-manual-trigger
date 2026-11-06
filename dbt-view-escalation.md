# Escalation: `int_document_ai` / `int_document_ai_detail` need to be dbt tables, not views

**Project:** `durable-woods-465907-n1`, dataset `sbic_int`
**Owner needed:** whoever maintains the `dbt-github` CI project (not accessible from the
RGMC SO-import app repos)
**Reported by:** Erwin Arellano
**Date:** 2026-11-06

## Problem

`int_document_ai` and `int_document_ai_detail` are dbt **views**, not materialized
tables (confirmed via `bq show --format=prettyjson`). `int_document_ai_detail`'s
definition includes:

- A `UNION ALL` of two staging tables
- `FIRST_VALUE(...) OVER (PARTITION BY ... ROWS BETWEEN UNBOUNDED PRECEDING AND
  UNBOUNDED FOLLOWING)` window functions (used to gap-fill SKU code/description)
- A final `QUALIFY ROW_NUMBER() OVER (...) = 1` dedup

Because of the window functions, BigQuery **cannot push a `WHERE created_at > X`
filter into the view**. Every single read re-executes the entire transformation over
**all historical staging data** before filtering the result. Cost scales with total
accumulated document volume, not with what's actually new — it gets slower every day
regardless of how much new data there is.

## Impact

This caused a real production incident: the SO-import bridge's scheduled BigQuery
reads of these views started timing out, silently stopping all new PO handoffs into
MSSQL/Business Central for hours (first seen 2026-10-29, `BigQuery read timed out
after 60s`, then recurring at a raised `120s` ceiling shortly after). Observed read
latency for the same query ranged from ~12s to 150s+ depending on BigQuery slot
contention — layered on top of the structural cost above.

## Current stopgap (already deployed, not a fix)

The app-side read timeout has been raised to `300s` (headroom exists up to just under
the consuming Pub/Sub subscription's 600s ack deadline). This buys time against
contention variance but does **not** address the structural cost, which will keep
growing as more documents accumulate and will eventually time out again regardless of
how high the timeout is set.

## Ask

Materialize `int_document_ai` and `int_document_ai_detail` as actual dbt tables —
ideally `materialized: incremental`, keyed on a stable ID with `created_at` as the
incremental filter — so the window-function/dedup logic runs once per new row instead
of being recomputed from scratch on every external read.

Without this, expect this incident to recur as document volume grows, each time
requiring another timeout bump that only delays the problem.
