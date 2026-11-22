# Phase B2 offline historical replay

The commands below reproduce the B2 historical analysis and its retrospective alignment. For fresh experiments, follow the [B3B1 Windows runbook](phase-b3b1.md#overnight-run-on-windows).

The November 20 archive explains Toronto's rapid paper gain. The raw book already showed a 40¢ bid when the entry simulator treated 35¢ as best. Atlanta's raw books had been evicted before the snapshot. Across the retained capture, all 7,709 uniquely aligned polls reproduced B1's price classification. That agreement measures fidelity to the simulator. Toronto shows why it cannot establish execution quality.

## Run from Windows PowerShell

The existing project virtual environment contains the dependencies. Run in the repository root. The CLI opens the archived files with SQLite `mode=ro&immutable=1` and query-only connections. It hashes both inputs and runs `quick_check` before analysis. It requires a standalone recorder snapshot, accepts an accompanying paper ledger, and refuses an archive with SQLite sidecars. A recorder-only archive supports observation replay. The backend, exchange, credentials and frontend are unnecessary.

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$python = '.\.venv\Scripts\python.exe'
$archive = 'C:\Users\jacks\kalshi-B1-capture-20261120-192225'
$out = '.\research\phase-b2'
$session = '30c366720c9046589ad7b6e4633c5eb5'
$toronto = 'KXNBAGAME-26NOV20TORDET-TOR'

# Inventory, integrity hashes, sequence coverage and missing intent evidence
& $python -m kalshi_edge_engine.replay inspect --archive $archive --out "$out\inventory"
& $python -m kalshi_edge_engine.replay validate --archive $archive --session $session --out "$out\coverage"

# Queue-cleared baseline across the retained session
& $python -m kalshi_edge_engine.replay replay --archive $archive --session $session --seed 42 --out "$out\session"

# One ticker and receipt-time window, including raw receipts and reconstructed poll traces
& $python -m kalshi_edge_engine.replay replay --archive $archive --ticker $toronto --start-ns 1795223092463595700 --end-ns 1795223970000000000 --seed 42 --events --out "$out\toronto-window"

# Three controlled experiments, first with B1 parsing and then with a raw top-of-book check
& $python -m kalshi_edge_engine.replay compare --archive $archive --seed 42 --out "$out\comparison"
& $python -m kalshi_edge_engine.replay compare --archive $archive --seed 42 --require-raw-consistency --out "$out\raw-consistency"

# Complete archived lifecycle of either intent
& $python -m kalshi_edge_engine.replay intent --archive $archive --intent-id 5877fe52-337d-4c78-bc9b-bd9540a1727e --out "$out\atlanta"
& $python -m kalshi_edge_engine.replay intent --archive $archive --intent-id b094c81f-445b-4b80-9c4f-89ac3dd64441 --out "$out\toronto"

# Full research report, including discrepancy rows and both case studies
& $python -m kalshi_edge_engine.replay report --archive $archive --seed 42 --out "$out\seed-42"
& $python -m kalshi_edge_engine.replay report --archive $archive --seed 42 --out "$out\repeat"
Get-FileHash "$out\seed-42\report.json","$out\repeat\report.json" -Algorithm SHA256
```

Each command writes `<command>.json` to the explicit directory, or prints JSON to stdout when `--out` is omitted. Rerunning a command in the same directory replaces its generated report. Use a directory per experiment. `--events` also writes `observations.jsonl` in receipt order and `reconstructed.jsonl` with each intent book, its validation receipt when available, reconstructed queue, guard and hypothetical uniform draw. The two streams have different timestamps and meanings. The JSONL observation body is the decoded archived payload. The database hashes cover the original bytes.

Repeat `--ticker` to select multiple markets. Time bounds are local receipt Unix nanoseconds, start inclusive and end exclusive. Session filtering admits ledger intents with selected recorder provenance. The ledger has millisecond decision times and no recorder session IDs. A window starting after submission censors hypothetical fills that require that missing history. The full `report` command labels the two forensic appendices as full-archive investigations even when the experiment is filtered.

## How to read an experiment

| Evidence | Location | Interpretation |
|---|---|---|
| Observed HTTP responses | `coverage`, optional `observations.jsonl` | Successful sampled metadata and book GETs. SQLite insertion ID preserves receipt order across equal timestamps and exposes wall-clock reversals. Exchange-supplied time is a separate field and can be absent. |
| Persisted B1 decisions | `reconciliation.recorded_*`, `intent.polls`, linked paper fills | The paper ledger records what B1 decided, including candidates rejected by guards. |
| Reconstructed state | `submission_agreements`, `matched_metric_agreements`, optional `reconstructed.jsonl` | Uses the original pure book, simulator, fee and liquidation functions. Compare queue values only where the trace marks the relevant queue state known. |
| Hypothetical execution | `models` | Each model conditions on archived admitted intents, order sizes, an observed polling schedule and the archived per-intent configuration. A hash of seed, session, sequence and intent supplies a common draw across policies. B1's original draws were not recorded. |
| Missing or conflicting evidence | `coverage.issues`, `intent_evidence_gaps`, `reconciliation.discrepancies` | Each comparison gives intent, ticker, expected and replayed values, both available clocks, numeric difference where applicable and the limiting evidence. A missing observation stays missing. |

The validator checks the full recorder sequence before filtering. A segment labeled `complete_relative_to_capture_contract` has contiguous retained responses and both selected response types for each represented ticker. `partially_observed` flags gaps or missing supporting evidence. `insufficient_evidence` applies when there is no usable selected book. A missing prefix of stored IDs demonstrates prior stored rows are absent. A sequence jump alone cannot distinguish an unrecorded response from pruning. The archive cannot establish the final session suffix, trades between polls, queue ownership, or whether any specific displayed reduction was a cancellation.

The replay processes a book before its subsequent intent-linked market response. It applies freshness, tradability, known close, horizon and path checks. Discontinuities invalidate pending validations, queue history and future marks for that segment. Retrospective alignment compares only same-intent books received within 25 ms at or before the journal poll, bounded by paired validation where available. Both directions must be unique. The simulator sees the selected history up to each decision. Guard invocation times, request starts, scheduler delays and concurrent ledger transactions are only partly recoverable.

The conservative policy admits only `queue_cleared`. It requires consecutive displayed observations with an estimated decrease exceeding queue ahead and a successful seeded draw. Each research alternative additionally admits one B1 heuristic. The inside-quote model assumes seller arrivals at a hypothetical bid. The moved-through model assumes an order could fill at its limit when a higher bid appears. Neither passive fill mechanism is established by these GETs. Each policy stops filling an order at its original size, so opportunities differ by model. This is a conditional execution comparison on an observed schedule. A portfolio rerun would also need resimulated signals, admission, inventory, risk and exits.

The mark is the first selected same-ticker book with sufficient displayed exit depth between five and seven seconds after hypothetical entry validation. It uses the B1 liquidation algorithm, archived fee profile and the full fill size. Missing marks are excluded from the mean and counted. Gross negative marks measure adverse movement. Exposure is cumulative hypothetical contracts by ticker. Marks are distinct from booked paper exits. `--probability-scale` scales entry draw probabilities (default 1, capped at probability 1). `--require-raw-consistency` rejects a hypothetical entry if the positive-size raw ladder disagrees with B1 on either the best bid or implied ask. Agreement alone does not establish access to that quantity.

## Verified archive

| Item | November 20 standalone snapshot |
|---|---|
| Dataset SHA-256 identifier | `d85774061bb71d39c1704bf701001956b4876dd68a75e036a22d91d97e5cb3ab` |
| Recorder SHA-256 | `416dbf89df6a060e56aed052189550e5cf50869ce6918ba28fb4d10fbe286372` |
| Paper ledger SHA-256 | `ace0eabc0b57881bd461cc71dd848bfe38e76b4e3d5b5fd3528bafb9049c0bed` |
| Retained interval | November 20, 17:04:52–19:22:25 Pacific. Session and sequence IDs are in the PowerShell workflow and reports. |
| Retained observations | 80,308 across 78 tickers, comprising 61,225 books and 19,083 metadata responses |
| Coverage | Sequences 201,063–281,370 are contiguous. Stored rows 1–201,062 are absent, or 71.46% through the final retained ID. No internal sequence gaps, receipt clock reversals or invalid bodies detected. Overall classification is `partially_observed`. |

This snapshot is later than the supplied B1 dashboard checkpoint. The ledger contains 724 instrumented intents, 27,211 polls and 1,021 legacy intents without submission evidence. Of the instrumented intents, 515 have no retained simulator book and 518 have at least one missing category of intent evidence. The archive records 1,210 candidates, two accepted candidates and two persisted entry fills. Polls classify as 25,063 price-improving, 1,233 moved-through, 715 displayed and 200 ineligible.

Of 7,724 retained simulator books, 7,709 uniquely align. Bid, implied ask, displayed size and price class agree on all aligned polls. Fifteen books lack a unique paper poll and 19,502 paper polls lack a unique book. All 206 recoverable submissions reproduce the intended quote, submission bid and implied ask. All 450 aligned recorded candidates reproduce the conditional acceptance decision, including Toronto. Atlanta has no retained entry book. The validation or policy reason agrees on 7,654 aligned polls. The remaining 55 discrepancies are listed individually. Historical journal reason counts include 767 disabled inside-quote paths, 438 disabled moved-through paths, 24 elapsed horizons, 18 rejected market observations and seven stale or incomplete validations. Guards also run on polls without candidates, so these counts use a different denominator from candidate rejections.

The reconstructed before/after queue estimates agree on 7,688 aligned polls. On 21, initialization or prior queue state was unavailable. Volume estimates and candidate eligibility agree on 7,690, with 19 unknown. The full report contains 19,671 discrepancy records, including missing evidence and separate fields for the same poll. Two independent full-archive seed-42 runs produced byte-identical JSON with SHA-256 `603af7df6e286b3d617d9518c9aad4d718a369030cfe7eb7cb8deefed9d2ba2b`. Both source files retained the hashes shown above after replay.

## Seed 42 model comparison

The archived probabilities were 0.10 for a cleared queue, 0.03 for price improvement and 0.35 for moved-through. The fill horizon was 120 seconds. Opportunities and candidates below count all eligible paths before policy gates and before an order finishes filling. Mean net mark is cents per marked hypothetical fill, including entry and modeled exit fees.

| Policy | Opportunities | Candidates | Fills | Mean time to fill | Marks | Gross adverse | Mean net mark | Largest ticker share |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Queue-cleared baseline | 7,258 | 437 | 1 | 112.0 s | 1 | 0 | +2.92¢ | 100.0% |
| Add price-improving | 4,787 | 346 | 118 | 48.0 s | 114 | 32 | +1.59¢ | 7.6% |
| Add moved-through | 6,348 | 223 | 41 | 38.3 s | 39 | 3 | +10.68¢ | 9.8% |

Raw top-of-book disagreement affected the baseline fill, 86 of the 118 inside-quote model fills, and 32 of the 41 moved-through model fills. Repeating the experiment with the raw-consistency gate gives the following results. A rejected earlier candidate leaves the intent eligible at later polls.

| Policy with raw-consistency gate | Fills | Marks | Gross adverse | Mean net mark | Largest ticker share |
|---|---:|---:|---:|---:|---:|
| Queue-cleared baseline | 0 | 0 | 0 | unavailable | unavailable |
| Add price-improving | 32 | 30 | 27 | −2.79¢ | 28.1% |
| Add moved-through | 9 | 9 | 3 | −0.98¢ | 33.3% |

The apparent positive marks are sensitive to inconsistent book parsing. Even the stricter results condition on heavily pruned history, a single seed, sampled depth and unobserved counterparties. They do not establish profitable execution.

## The two rapid simulated trades

**Atlanta YES, 62¢ to 73¢ in 297 ms.** The final three ledger polls report a 62¢ best bid with 13,240, 12,007 and seven contracts. The last poll had zero estimated queue ahead, a 12,000-contract decrease and no observed cross. B1 accepted a one-contract queue-cleared candidate after active-market validation. The exit ledger records a one-contract sweep at 73¢. All raw receipts for this interval precede the retained archive. The exit record attests to the simulator's depth calculation, but the actual displayed size, timing and simultaneous book cannot be checked independently. A cancellation, an asynchronous refresh or inconsistent parsing remains possible. Reproducing the booked outcome would require treating the decrease as executable volume, obtaining a successful original draw and accessing the 73¢ bid. Gross 11¢ less 0.42¢ entry and 1.38¢ exit fees equals **+9.20¢** modeled.

**Toronto YES, 35¢ to 40¢ in 773 ms.** B1 recorded 4,200 contracts at 35¢, followed by 3,810. The estimated 390-contract decrease exceeded the remaining queue of 330 by 60. Raw entry book **209395** already showed **112,263.52 YES contracts at 40¢**. The B1 entry parser skipped the fractional-size string, so a lower whole-size level appeared best. Active validation **209398** also reported 40¢, scanner book **209402** saw the same top, and inventory book **209416** still showed 112,263.52 at 40¢ in the exit's logged millisecond. The liquidation parser retained the whole-contract portion of that fractional level and had sufficient displayed depth for one contract. B1 did not persist the exact inventory receipt ID. The different entry and exit parsers can explain the entire apparent 5¢ improvement with no bid move. Reproducing the paper result requires those parser rules, executable queue depletion, a successful draw and access to the exit bid. Gross 5¢ less 0.40¢ entry and 1.68¢ exit fees equals **+2.92¢** modeled.

The ledger's combined modeled result is **+12.12¢** across two one-contract trades. The execution source in each case was simulated displayed-size decrease followed by simulated displayed-bid sweep. There is no exchange fill confirmation, public trade stream or historical order priority for either case.

## Phase B3 research plan

1. Shadow-run a shared, fixed-point interpretation of book prices and fractional quantities across signal, entry and exit. Regression-test the Toronto receipt and compare both parsers on held-out sessions.
2. Retain sealed capture segments with session manifests, eviction/drop counters and enough capacity for the whole experiment. Persist receipt IDs at signal, submission, validation and exit, together with decision times, effective configuration and per-candidate random draws.
3. Capture sequenced depth changes and public trades with exchange and receipt times. Measure how often displayed reductions coincide with traded volume, and how sensitive results are to cancellations, latency and assumed priority.
4. Compare prespecified models on independent sessions and tickers. Report fee-aware markouts, missingness, concentration and adverse movement across seeds and liquidity regimes before reconsidering paper execution settings.
