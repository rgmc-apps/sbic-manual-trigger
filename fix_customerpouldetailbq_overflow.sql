-- Fix for: "Arithmetic overflow error converting float to data type numeric"
-- inserting into CustomerPOULDetailBQ (sbic_prod).
--
-- Root cause: unitAmount/netAmount/netPrice/netPricePcs are numeric(8,2) (max
-- 999,999.99), but a legitimate large-quantity PO line total can exceed that —
-- confirmed live 2026-11-06 with PO414226102904912 (SANFORD MARKETING CORPORATION,
-- poQty=615), whose unitAmount computed to 2,851,219.95. One oversized value in a
-- batch fails the whole insert, blocking every other row in that same batch too.
--
-- unitPrice, unitPricePcs, and discountPercent are left untouched — they're
-- per-unit/percent values, not totals, and aren't at risk of this overflow.
--
-- Run against sbic_prod.

ALTER TABLE CustomerPOULDetailBQ ALTER COLUMN unitAmount numeric(14,2);
ALTER TABLE CustomerPOULDetailBQ ALTER COLUMN netAmount numeric(14,2);
ALTER TABLE CustomerPOULDetailBQ ALTER COLUMN netPrice numeric(14,2);
ALTER TABLE CustomerPOULDetailBQ ALTER COLUMN netPricePcs numeric(14,2);
