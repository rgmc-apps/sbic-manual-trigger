# PRINCE RETAIL (SUNCOAST BRANDS) — PO Investigation Findings

**Date:** 2026-11-09
**Scope:** 36 POs from the BigQuery query — `customer_name = 'PRINCE RETAIL GROUP OF COMPANIES'`, `company_name = 'SUNCOAST BRANDS'`, `created_at > '2026-11-09T00:20:02.342883'`.

## 1. Current BC status (re-checked live, 2026-11-09)

**31 of 36 POs are confirmed in Business Central** as real Sales Orders. **5 are not**, split across two distinct, unrelated root causes.

### 1a. Successfully inserted (31)

| PO Ref | BC Order | Ship-to |
|---|---|---|
| 4559556 | SBSO269178 | PR006-B005 |
| 4559562 | SBSO269177 | PR006-B005 |
| 4559566 | SBSO269176 | PR006-B005 |
| 4559567 | SBSO269175 | PR006-B005 |
| 4559569 | SBSO269174 | PR006-J001 |
| 4559570 | SBSO269173 | PR006-S004 |
| 4559571 | SBSO269172 | PR006-J001 |
| 4559572 | SBSO269171 | PR006-B005 |
| 4560025 | SBSO269170 | PR006-R002 |
| 4560030 | SBSO269169 | PR006-B007 |
| 4560035 | SBSO269105 | PR006-P010 |
| 4560039 | SBSO269106 | PR006-P010 |
| 4560045 | SBSO269168 | PR006-T004 |
| 4560052 | SBSO269050 | PR006-P018 |
| 4560054 | SBSO269107 | PR006-P010 |
| 4560056 | SBSO269108 | PR006-P010 |
| 4560303 | SBSO269167 | PR006-B007 |
| 4560317 | SBSO269166 | PR006-B007 |
| 4560324 | SBSO269154 | PR006-P010 |
| 4560325 | SBSO269165 | PR006-B007 |
| 4560329 | SBSO269164 | PR006-B007 |
| 4560331 | SBSO269153 | PR006-P010 |
| 4560336 | SBSO269155 | PR006-P018 |
| 4560340 | SBSO269049 | PR006-P018 |
| 4560344 | SBSO269157 | PR006-P018 |
| 4560459 | SBSO269163 | PR006-S002 |
| 4560463 | SBSO269162 | PR006-B005 |
| 4561872 | SBSO269161 | PR006-T001 |
| 4561957 | SBSO269160 | PR006-T001 |
| 4561959 | SBSO269159 | PR006-T001 |
| 4563456 | SBSO269158 | PR006-B006 |

### 1b. Not yet in BC (5) — two different causes

**Category A — blocked by an ambiguous/duplicate branch lookup code (3 POs)**

| PO Ref | Branch | MSSQL state |
|---|---|---|
| 4561798 | RDC BACOLOD, 426 HOLDINGS, J2 LUIS ONG BLDG, BANAGO, 6100 | Not promoted — vanishes from both staging and promoted tables after insert attempt |
| 4561836 | same | same |
| 4561912 | same | same |

Root cause: this branch text's `lookUpCode` is shared by **two different, legitimately active** `CustomerBranch` records (3276 "SILAY" and 4204 "PRINCE BINALBAGAN") — see Section 2. The MSSQL promotion trigger's branch-matching join becomes ambiguous for this lookup code, and two independent retrigger attempts both failed identically (insert reports success, but the row disappears from MSSQL entirely afterward — consistent with the trigger's `SET XACT_ABORT ON` transaction rolling back on the ambiguous match). **Will not self-heal; needs the lookup-code fix in Section 2 before these can be retried successfully.**

**Category B — branch not registered in BC/MSSQL at all (2 POs)**

| PO Ref | Branch | MSSQL state |
|---|---|---|
| 4563702 | RDC DUMAGUETE, LOWER TALAY, DUMAGUETE, 6200 | Promoted into `CustomerPOUL`, `customerBranchId = -1`, `poStatus = "NEW"` — never reached BC |
| 4563680 | same | same |

Root cause: **"RDC DUMAGUETE, LOWER TALAY..." has zero matches in `CustomerBranch`** (checked directly — not a duplicate, a genuine absence). This is a different problem from Category A: there is no existing branch record to disambiguate between — none exists yet. Either a new `CustomerBranch`/BC ship-to needs to be registered for this location, or a human needs to confirm it corresponds to an existing branch under a different name/address and link it via `/reconcile`. These two POs are sitting correctly in MSSQL (not lost) but will not reach BC until that's resolved.

## 2. Duplicate/ambiguous `CustomerBranch.lookUpCode` findings

Diagnostic query (`find_ambiguous_branch_lookup_codes.sql`) run against `sbic_prod`. **All 33 affected branch rows belong to exactly one customer: `PRINCE RETAIL MDSG INC` (customerId 553).** No other customer in the system has this problem.

Ten distinct `lookUpCode` values are each shared by 2–6 different, active, legitimately-distinct branches:

| Shared `lookUpCode` | # branches | Branches |
|---|---|---|
| `90004` | 3 | BAYBAY, MAASIN, ISABEL |
| `90005` | 3 | TALISAY, CANLAON, SAGAY |
| `90010` | 3 | BAYAWAN, TANJAY, LARENA |
| `90011` | 2 | BUENAVISTA, PRINCE COMPOSTELA |
| `90012` | 5 | CABATUAN, POTOTAN, CALINOG, ESTANCIA, JORDAN |
| `90017` | 5 | PRINCE ARGAO, PRINCE MEDELLIN, SIBONGA, SAN REMEGIO, PRINCE - BADIAN |
| `RDC BACOLOD, 426 HOLDINGS, J2 LUIS ONG BLDG, BANAG...` | 2 | SILAY, PRINCE BINALBAGAN |
| `RDC CEBU, PUROK SAMBAG TUBURAN, BRGY.JUBAY,, LILOA...` | 6 | PRINCE CAMOTES, MINGLANILLA 2, DANAO, TUBURAN, DALAGUETE, NORTH RECLAMATION AREA BRANCH |
| `RDC TACLOBAN, ABUCAY STREET BARANGAY 91, TACLOBAN,...` | 4 | DULAG, STO CATBALOGAN, GUIUAN, PALOMPON |

**Why this matters:** every one of these 33 branches already has its own genuinely unique `customerBranchCode` (e.g. `PR006-S008-SU` for SILAY) — only `lookUpCode` is non-unique. The MSSQL trigger (`tr_CustomerPOBQ`) that promotes staged POs into `CustomerPOUL` resolves a branch by matching the PO's extracted branch text against `CustomerBranch.lookUpCode`; when that match is ambiguous (2+ branches share the code), the branch cannot be reliably resolved. The `90012` and `90017` groups (5 branches each) are the biggest exposure — any future PO for those branches carries the same risk as the 3 POs stuck today.

**These are not duplicate rows and must not be deleted/merged** — each is a real, active, distinct store. The fix is giving each branch its own unique `lookUpCode` (e.g. reusing its own `customerBranchCode`, since that's already unique and already tied to the correct branch everywhere else). This is a business/master-data decision pending confirmation, not yet applied.

## 3. Recommended next steps

1. **Decide and apply the `lookUpCode` fix** for the 10 ambiguous groups (33 rows) — recommend setting `lookUpCode = customerBranchCode` per branch, pending confirmation this doesn't conflict with any other convention SBIC uses. Unblocks `4561798`, `4561836`, `4561912` plus prevents the same failure for any future PO against those 33 branches.
2. **Resolve the 2 "RDC DUMAGUETE" POs** (`4563702`, `4563680`) via `/reconcile` — either register the branch/ship-to in BC + `CustomerBranch` if it's genuinely new, or link it to an existing branch if it's a text-variant of one already on file.
3. Once both are done, retrigger the 5 remaining POs by file/PO ref the same way the other 31 were resolved.
