# Phase B3A — Canonical orderbooks and execution consistency

Toronto's raw entry book contained a 40¢ YES bid throughout the apparent 35¢ to 40¢ paper gain. Canonical parsing puts the historical 35¢ order below that bid. It removes Toronto's queue-cleared entry from the corrected reconstruction. The original paper ledger still records the trade. On this capture, the queue correction alone changed no seed-42 outcomes.

## One interpretation of displayed depth

`engine/orderbook.py` normalizes the preferred `orderbook_fp.yes_dollars/no_dollars` ladders, legacy nested `orderbook.yes/no`, and legacy flat `yes/no`. A fixed-point container takes precedence over legacy fields, including when the fixed-point container is malformed. Each side is an immutable, descending sequence of distinct price levels. A level carries a Decimal cent price and a Decimal reported quantity. The opposite side's best displayed bid determines the implied ask by exact subtraction from 100.

- Fixed-point dollar prices are converted to cents. Supported prices are strictly between 0¢ and 100¢, on a 0.01¢ grid (four decimal dollar places). Excess nonzero price precision is rejected. Supported quantities are nonnegative, fit within the signed SQLite integer range, and can have up to eight nonzero fractional places. Unsupported precision is rejected without rounding. The transport preserves the decimal spelling of unquoted orderbook numbers through parsing. Historical decoder behavior remains available offline.
- Malformed pairs, invalid numbers, negative quantities, nonfinite values and invalid prices are excluded and flagged. Missing or malformed sides remain identifiable. Zero quantity never sets the bid. For duplicate prices the smallest reported size wins, including zero, to prevent double counting. Duplicate evidence is flagged even when both copies agree.
- Any positive size sets a displayed price. Whole capacity is the floor **at each price**, so 0.75 at two different prices supplies zero whole contracts. A 0.75 top still sets the displayed bid and implied ask. A sale uses the first available whole capacity down the same ladder, requires the full requested integer size, rejects crossed books and applies the existing fee profile per consumed level. The displayed top and the sale VWAP can therefore differ legitimately.
- Signal and paper APIs retain cent-number price fields and integer capacity fields. `Book.at` exposes exact quantity. Reported-size JSON fields preserve the decimal value using an integer, a round-tripping number, or a decimal string for quantities that exceed float precision. Replay traces include exact quantity strings and the modeled and observed queue values. The deprecated polling simulator also checks full whole capacity at or above the modeled price before booking its sampled outcome.

The scanner's signal calculation, submission diagnostics, paper-entry simulation, inventory liquidation and corrected replay all use this normalization. Signal thresholds, ranking rules, fee schedules, fill probabilities, path settings, paper contract sizes, risk limits and polling budgets retain their prior settings.

## Reconstructing the displayed queue

B1 initializes its queue when a resting order first becomes best, applies consecutive displayed-size reductions, and clears the initialization when the quote stops being best or a handled read error resets the simulator. Previously, a fresh B2 queue could initialize its numerical estimate while leaving its evidence flag false. For displayed sizes **2 → 8 → 1**, the first poll initializes ahead to two, the second adds no inferred volume, and the third observes a seven-contract reduction with five beyond the estimated queue. B2 suppressed the opportunity when the queue began with its default flag. B2 already marked queues known for witnessed submissions, which limits this defect's historical impact.

Live and corrected replay now use the same displayed-queue transition. A witnessed first observation initializes without drawing for a queue fill. For fractional liquidity, the initial estimate rounds **up** to a whole contract. Each consecutive positive decrease rounds **down** once after subtracting the exact reported quantities. Fractional decreases are not pooled across polls. Thus 1.01 → 0.99 contributes zero inferred contracts. Only a strictly positive whole residual beyond the prior estimated queue can generate a queue-cleared candidate, still subject to the original draw and validation.

A window beginning mid-intent has unknown original priority. Seeing the first retained displayed level cannot establish that it was the first level B1 saw. A witnessed off-quote transition resets the queue, and a recorded simulator read error permits fresh initialization. An unresolved interruption, unpaired validation, invalid book or capture discontinuity breaks continuity. The replay keeps separate state per intent and ticker and clears it across session/sequence boundaries. The historical submission guard remains independent of queue initialization.

## Reproduce the three interpretations

| CLI mode | Entry/exit parser and queue | Intended use |
|---|---|---|
| `b2` (default) | B1's original parsers and B2's original queue-evidence behavior | Reproduce the Phase B2 audit, including the old price-only gate |
| `b1-queue` | B1 parsing with witnessed queue initialization and reset handling | Isolate the queue correction and compare B1 liquidity with the raw canonical ladder |
| `canonical` | Shared fixed-point interpretation and corrected queue | Counterfactual on the archived B1 intents and polling schedule |

`--require-raw-consistency` retains the B2 bid/ask comparison. `--require-execution-consistency` in either new mode gates **entry candidates** on a complete, unambiguous, two-sided, uncrossed book. It checks bid and implied ask, displayed size at the top and order price, reported quantity and whole capacity at every price on both sides, and at least one whole displayed contract at each top. At a displayed-quote candidate, it also requires known consecutive liquidity and matching queue-before, queue-after and inferred depletion against the canonical reference. For heuristic paths that do not consume a modeled displayed queue, the gate checks the book evidence. Seller arrival and priority remain assumptions. Comparable, disagreeing and unknown measurements are reported separately. The canonical and modeled views use the same receipt, so agreement measures internal consistency.

From PowerShell in the repository root, the following writes new reports under the ignored B3A output directory. The CLI refuses a B3A-mode output directory under the B2 baseline tree.

```powershell
$py = '.\.venv\Scripts\python.exe'
$archive = 'C:\Users\jacks\kalshi-B1-capture-20261120-192225'
$out = '.\research\phase-b3a'
foreach ($mode in 'b2','b1-queue','canonical') {
    & $py -m kalshi_edge_engine.replay report --archive $archive --seed 42 --mode $mode --out "$out\$mode"
}
& $py -m kalshi_edge_engine.replay report --archive $archive --seed 42 --mode b1-queue --require-execution-consistency --out "$out\queue-strict"
& $py -m kalshi_edge_engine.replay report --archive $archive --seed 42 --mode canonical --require-execution-consistency --out "$out\canonical-strict"
& $py -m kalshi_edge_engine.replay report --archive $archive --seed 42 --mode canonical --require-execution-consistency --out "$out\repeat"
Get-FileHash "$out\canonical-strict\report.json","$out\repeat\report.json"
& $py -m kalshi_edge_engine.replay intent --archive $archive --mode canonical --intent-id b094c81f-445b-4b80-9c4f-89ac3dd64441 --out "$out\toronto"
```

The report distinguishes the historical journal, alignment discrepancies, parser and queue versions, per-dimension consistency counts, per-policy hypothetical fills and subsequent five-second marks. Use `--events` on a selected ticker/window to inspect the per-receipt diagnostics. The B3A observation export preserves decimal tokens as text, including legacy unquoted fractional numbers. The original bytes remain in the immutable archive. Canonical replay excludes malformed book receipts from the causal stream and marks a boundary. Individual parsed levels are still available to the normalization API for inspection. The reports contain the archive hashes, selected scope, effective settings and common seed. A fresh seed-42 draw cannot recover B1's original unrecorded draw.

## Archive and alignment

The standalone recorder and paper ledger passed SQLite integrity checks in immutable read-only mode. Dataset ID is `d85774061bb71d39c1704bf701001956b4876dd68a75e036a22d91d97e5cb3ab`. The archive retained 80,308 responses for 78 tickers, including 7,724 intent-linked simulator books. Stored rows 1–201,062 were missing, or 71.46% through the last retained ID. There were 724 instrumented intents, 27,211 journal polls, 1,210 recorded candidates, and two booked paper entries. Of 7,709 uniquely aligned polls, B2 matched all historical price classifications. Canonical interpretation matched 2,596 and changed 5,113. Fifteen raw simulator books and 19,502 journal polls lack unique counterparts.

| Across all 7,724 retained simulator books | Historical parser | Canonical parser |
|---|---:|---:|
| Price-improving polls | 6,554 | 1,674 |
| At displayed quote | 306 | 1,485 |
| Moved-through polls | 702 | 4,397 |
| Ineligible polls | 162 | 168 |
| Known initializations / holds / decrements at quote | 85 / 193 / 9 | 110 / 1,332 / 17 |
| Leave displayed level / remain off quote | 75 / 7,343 | 68 / 6,171 |
| Displayed polls with unknown initialization | 19 | 26 |
| Baseline opportunities with whole residual beyond queue | 2 | 3 |

The archive had no journal polls marked unprocessed. Across 7,724 books, the historical model disagreed with the canonical reference on the chosen-side best bid in 5,565, implied ask in 5,153, displayed quantity at the top or quote in 5,631, and whole capacity somewhere in either ladder in 6,832. On the 7,709 aligned historical polls, the historical and queue-only modes both matched the journal queue-before and queue-after values in 7,688 cases. Canonical matched 6,241 before and 6,246 after. These are comparisons against the historical simulator. In canonical mode, all 7,724 books agreed on both price and depth dimensions against the same-receipt reference. Queue-relevant evidence was unknown on 136.

## Seed-42 execution comparison

Each row conditions on original B1 admissions. Opportunities are eligible polls across all paths before random draws. Candidates count successful draws across those paths **before** guard and policy rejections. In the table, Q denotes queue-cleared, P price-improving and M moved-through. Each policy includes Q. Orders stop producing opportunities once their original size is filled.

| Interpretation | Allowed paths | Opportunities | Candidates | Hypothetical fills by path | Marks / missing | Mean net mark (¢) | Largest ticker share |
|---|---|---:|---:|---|---:|---:|---:|
| B2 and queue-only | Q | 7,258 | 437 | 1 Q | 1 / 0 | +2.92 | 100% |
| B2 and queue-only | Q+P | 4,787 | 346 | 1 Q + 117 P | 114 / 4 | +1.59 | 7.6% |
| B2 and queue-only | Q+M | 6,348 | 223 | 1 Q + 40 M | 39 / 2 | +10.68 | 9.8% |
| B2 price-only gate | Q | 7,258 | 437 | 0 | 0 / 0 | — | — |
| B2 price-only gate | Q+P | 6,595 | 406 | 32 P | 30 / 2 | −2.79 | 28.1% |
| B2 price-only gate | Q+M | 7,021 | 390 | 9 M | 9 / 0 | −0.98 | 33.3% |
| Historical parser, corrected queue and strict gate | Q | 7,258 | 437 | 0 | 0 / 0 | — | — |
| Historical parser, corrected queue and strict gate | Q+P | 6,875 | 417 | 16 P | 15 / 1 | −2.59 | 31.3% |
| Historical parser, corrected queue and strict gate | Q+M | 7,021 | 390 | 9 M | 9 / 0 | −0.98 | 33.3% |
| Canonical | Q | 6,074 | 1,604 | 0 | 0 / 0 | — | — |
| Canonical | Q+P | 5,334 | 1,571 | 35 P | 33 / 2 | −2.80 | 25.7% |
| Canonical | Q+M | 1,928 | 184 | 126 M | 125 / 1 | +6.89 | 8.7% |
| Canonical, strict gate | Q | 6,074 | 1,604 | 0 | 0 / 0 | — | — |
| Canonical, strict gate | Q+P | 5,334 | 1,571 | 35 P | 33 / 2 | −2.80 | 25.7% |
| Canonical, strict gate | Q+M | 1,929 | 185 | 126 M | 125 / 1 | +6.89 | 8.7% |

The corrected baseline had three queue-cleared opportunities and zero successful draws on that path. Its 1,604 candidates were all generated on disabled heuristic paths. On the historical parser the stronger gate rejected 27 price-improving policy candidates specifically for liquidity disagreement. On the canonical moved-through policy the strict gate vetoed a sub-contract top at receipt 271167. The intent filled hypothetically on a later receipt, 271204, leaving the aggregate count unchanged.

Marks use the first sampled same-ticker book with sufficient whole depth five to seven seconds after hypothetical validation, less entry and modeled sale fees. Of the canonical strict samples, 30 of 33 price-improving marks and three of 125 moved-through marks had adverse gross movement. Models select different intents and times, and some outcomes lack subsequent usable books. These conditional means cannot establish strategy profitability. In particular a higher visible bid does not demonstrate that an earlier passive bid received an execution.

## Recorded trades

**Toronto.** Entry receipt 209395 shows 3,810 contracts at 35¢ and 112,263.52 at 40¢. B1's entry parser skipped a nonintegral string quantity while its exit parser floored positive quantities. B1 therefore estimated a queue at 35¢ and accepted a 390-contract displayed decrease against 330 ahead. The corrected scanner, entry, replay and liquidation all see 40¢ with 112,263 whole contracts at that level. The one-contract 40¢ inventory mark is compatible with receipt 209416 in the exit millisecond. The original +2.92¢ booked result (5¢ gross less 2.08¢ fees in 773 ms) remains a B1 ledger fact. Priority, counterparty action and execution were never observed.

**Atlanta.** The journal records a 12,000-contract reduction with zero estimated queue ahead and a modeled sale at 73¢ after a 62¢ entry. Gross 11¢ less 1.80¢ fees gives the original +9.20¢ in 297 ms. Entry and inventory books predate retention. No corrected reconstruction can independently verify the top, displayed exit size, or cause of the decrease. The combined historical result stays +12.12¢.

## Deployment and B3B

Two independent canonical strict runs produced byte-identical JSON with SHA-256 `0b64ee30a45d8ee59fba5e6a5c679ebec4eea774572c75771363535c3788e299`. The B2 default and price-only reruns matched the original baseline files byte for byte. Archive hashes were rechecked after analysis. The regression suite also runs entirely against disposable fixtures.

| Review area | Files changed |
|---|---|
| Shared orderbook and paper consumers | `engine/orderbook.py`, `engine/paper_sim.py`, `engine/paper.py`, `kalshi/client.py`, `runtime/paper_runtime.py`, `db/fill_funnel.py` under `src/kalshi_edge_engine/` |
| Versioned replay and forensics | `replay/__init__.py`, `__main__.py`, `archive.py`, `execution.py`, `research.py`, `forensics.py` and the new `b1_orderbook.py` in the same replay package |
| Regression coverage | New `tests/test_canonical_orderbook.py` and expanded `tests/test_historical_replay.py` |
| Documentation and local outputs | `README.md`, `.gitignore` and this new `docs/phase-b3a.md` |

B3A requires no database migration or configuration changes. For a later operator-controlled maintenance window, preserve a sealed backup, verify the archived replay and full test suite, and review the changed classifications and queue trace. Start the existing paper-only services only after explicit operator approval. Record the parser version for the new experiment and compare its first sample against an offline replay before joining results across versions. Keep the historical B1 cohort separate.

B3B should prioritize immutable segments with retention and drop manifests, links from decisions to exact receipt IDs, and the effective configuration and random draw at decision time. Then collect sequenced public depth changes and trades across independent sessions. Snapshot agreement alone cannot resolve queue ownership, cancellation versus trade, intervening liquidity changes, polling latency or whether the displayed bid remained accessible.
