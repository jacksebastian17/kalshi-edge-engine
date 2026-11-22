# B3B2 preparation — forensic review of the November 21 B3B1 run

## Executive summary

The ledger closed one Sacramento YES contract for **+10.44¢ net paper P&L**. Its two fills, fees, four markouts, final inventory and portfolio snapshots reconcile. The entry rested entirely on an inference about two GET snapshots 3.187 seconds apart. They showed 20,862.60 fewer contracts at 67¢. They supplied neither the cause of that reduction nor a queue position for the hypothetical order.

Across 137 admitted intents, only four polls qualified for the enabled queue-cleared probability test. Two belonged to the same Sacramento intent. The simulator generated 422 candidates, including 421 on disabled heuristic paths. A positive single-trade ledger and a complete recorder establish that the experiment can be audited. **Proceed to prospective public-trade and depth validation. Executability and economic edge remain inconclusive.**

## Identity and reproduction

| Item | Verified value |
|---|---|
| Cohort / source commit | `decision_linked_b3b1_v1` / `6b444ea23f070d97650eceea6d8bdf8bbe1897b8` |
| Runtime source SHA-256 | `bbce4f315ed239e286625b6afad9bbff6c39fa6cf36d9a477fd96d2c4667c598` |
| Backup | `b3b1-night-2026-11-21/backup-a17e7e7d225c42f3aead041e5a0bc176` |
| Manifest SHA-256 | `29acbf1dd6ee3193b44326717d99504385d533db6f1398408cc45378fe176a6d` |
| Recorder SHA-256 | `49b46952a265573dcbd9e7808212c7558f80e4607eb695bc22d05c78bf7a4a9d` |
| Paper ledger SHA-256 | `60e073434d14556c05427e1afdf27911e092c9cb611bcd096cbd3593fbe07dd5` |
| Data session | `a08516246a574d7f8a61c7039a57f048`, receipts 1–97,113 |
| Empty session | `c4b7cf9ae3e84059a1e460a739772d50`, zero receipts, clean seal |
| Observation window | November 21, 20:09:07.602–23:04:52.405 Pacific, about 2 h 56 min |
| Raw response kinds | 73,391 orderbooks, 17,832 markets, 5,715 events, 175 series |

The independent rerun of the existing evidence audit returned `verified_links`, 5,165 complete polls, zero incomplete polls and zero missing, unresolved, duplicate, inconsistent, dropped or future references. Both sessions closed with zero drops, recording errors and sequence breaks. All receipt numbers below refer to the data session. They order local HTTP arrivals. The archive contains no exchange depth-update stream, individual public trade messages, or exchange acknowledgments for paper orders.

Run from the checkout using the installed project environment. The tool opens both databases immutable and query-only, verifies the evidence links first and prints deterministic JSON to stdout. Its lifecycle calculation covers one entry and one full displayed-depth exit. Missing horizons and calculation discrepancies appear explicitly. The existing replay CLI continues to reproduce historical experiments. [Forensic command](../tools/b3b1_forensics.py) · [B3B1 evidence audit](phase-b3b1.md)

```powershell
Set-Location 'C:\Users\jacks\kalshi-edge-engine'
$archive = 'C:\Users\jacks\kalshi-paper-runs\b3b1-night-2026-11-21\backup-a17e7e7d225c42f3aead041e5a0bc176'
$intent = 'dc278e8c-8f80-4432-baf1-514b19464540'
.\.venv\Scripts\python.exe tools\b3b1_forensics.py --archive $archive --intent $intent
```

Two executions produced identical bytes. SHA-256 of stdout with LF line endings was `add1cc1c0a78d3b028d3ab9a2f1ed658702e77e8f98eb2baa4715b16451820e0`. All three archive files retained the hashes above after both executions.

## The completed lifecycle

**Market.** NBA, Boston vs Sacramento, event `KXNBAGAME-26NOV21BOSSAC`, series `KXNBAGAME`. The contract `KXNBAGAME-26NOV21BOSSAC-SAC` resolves YES if Sacramento wins. The paper order was to buy **one YES at 67¢**. Times in the next tables use UTC on November 22, eight hours ahead of local Pacific time.

| Stage | UTC time | Receipt or ledger evidence |
|---|---|---|
| Signal | 05:33:26.655 | Book 48556 and fee inputs 48089 (market), 48092 (event), 48191 (series). The ledger records 3.51¢ modeled net edge. |
| Submission | 05:33:26.767 | Book 48557 showed 96.22 YES at 67¢, whole capacity 96, implied ask 70¢. Market 48558 was active. |
| Candidate, poll 3431 | 05:34:07.951 | Book 49009, previous book 48982, at-best probability 0.10. Draw **0.0977825960996972** passed. |
| Validation and booking | 05:34:07.981 / 05:34:08.000 | Market 49010 was active. Ledger fill `sim:dc278e8c-8f80-4432-baf1-514b19464540:1795325647951:2`, one at 67¢, zero remaining. Submission to booking took 41.233 s. |
| Exit | 05:34:58.008 | Market-state receipt 49335, fee inputs 49396/49375/49376, inventory book 49455. Simulated bid sweep sold one at 79¢ against 34,037.84 displayed, implied ask 80¢. Fill `exit:4f66b10e-1f53-4ca8-87cd-140540da3fa0` shares the opening lifecycle ID. |

The take-profit rule used a 1¢ net per-contract target and a 600 s maximum hold. The modeled 10.44¢ gain cleared the target after **50.008 s**. The exit has a recorded contemporaneous price and sufficient displayed whole capacity. A real marketable order would also face transmission delay, intervening consumption, possible price movement and slippage.

**Market-state caveat.** Book 48557 showed a 67¢ bid. Market detail 48558, received about 25 ms later, reported a 32¢ YES bid and a 36¢ last trade. A market-detail GET and a book GET can describe different effective instants. The entry guard consumed the market status, and the pricing logic consumed the book. The underlying venue times needed to explain that discrepancy are unavailable. At the successful poll, both endpoints displayed a 67¢ bid and 68¢ ask.

### How the queue produced the entry

At each first observation with the quote at the displayed best, the simulator initializes queue ahead to the ceiling of the displayed quantity. On consecutive at-quote polls it uses `V = floor(max(previous_size − current_size, 0))`, reduces queue ahead by `V` down to zero, and permits a draw only when `V > ahead_before`. An increase contributes zero modeled volume and adds nothing to the existing estimate of queue ahead. Leaving the best resets the carried state. The 10% probability came from the archived NBA intent configuration. No trade-flow calibration appears in the model or archive. [Simulator](../src/kalshi_edge_engine/engine/paper_sim.py) · [Canonical book](../src/kalshi_edge_engine/engine/orderbook.py)

| Book receipt(s) | Canonical observation and queue result |
|---|---|
| 48559, 48577 | First poll saw 189.51 at 67¢ and initialized 190 ahead. A rise to 222.00 left 190 ahead. The submission size was not used as queue initialization. |
| 48607, 48624, 48661 | Best bid moved to 70¢, 69¢, then 68¢. The model lost its prior queue estimate. Poll 48624 produced this intent's other candidate, rejected by moved-through policy. |
| 48695, 48718 | Best returned to 67¢ with 85,120.27 displayed, initializing 85,121 ahead. Size rose to 174,768.52. |
| 48753, 48787, 48857, 48898 | Modeled decreases of 19,691, 31,192, 26,579 and 2,826 reduced modeled queue ahead to 65,430, 34,238, 7,659 and 4,833. |
| 48958 | Raw size fell from 94,477.83 to 79,225.78. The model counted 15,252, with 10,419 beyond queue. Draw 0.8790986317492565 failed. |
| 48982 → 49009 | At 67¢, displayed size grew to 137,307.83, then fell to 116,445.23. Modeled ahead stayed zero. The subsequent decrease of 20,862.60 produced 20,862 whole contracts of inferred volume beyond queue. The draw passed and the one-contract remainder limited the proposed fill. |

The independently reconstructed queue matches the journal on every at-quote poll of this intent. During the final pair of market validations, 48988 and 49010, reported cumulative market volume rose from 12,212,988.01 to 12,333,061.50 and both last prices were 68¢. Those aggregate fields have no per-trade side, price allocation or precise linkage to the 67¢ level. Cancellations ahead or behind, replenishment and executions can produce the observed depth path. The paper order never joined the exchange queue. For a hypothetical posted order the archive supports the full range from **zero to one contract**. A real posted contract could have changed displayed quantity, reactions and matching.

## Fees, markouts and ledger reconciliation

All amounts below are **cents for one contract**. The archived series-aware profile is `kalshi_quadratic_direct_2026_07_v1`, `quadratic_with_maker_fees`, multiplier 1. For price fraction `p` and quantity `q`, the profile calculates dollar fee `rate × q × p × (1−p)`, rounds up to a microdollar, then floors signed cash change after the fee to $0.0001. With these whole-cent prices this yields the displayed 0.01¢ fee precision. Entry uses maker rate 0.0175 and each hypothetical sale uses taker rate 0.07 per consumed level. These are the run's modeled fees. [Versioned fee calculation](../src/kalshi_edge_engine/engine/fees.py)

- **Entry cost** was 67.00 + 0.39 = **67.39¢**. Exit proceeds were 79.00 − 1.17 = **77.83¢**.
- **Booked gross P&L** was 1 × (79 − 67) = **12.00¢**. Net was 12.00 − 0.39 − 1.17 = **10.44¢**, agreeing with the exit fill's realized delta.
- **Gross markout** equals quantity × (canonical bid-sweep price − entry price). Net equals gross − booked entry fee − modeled liquidation fee. The analysis recomputed price, size, fee and net from the linked book and archived fee profile, then compared against the entry fill's stored outcome columns.

| Horizon | Observed UTC | Elapsed | Book | Sale price | Gross | Sale fee | Net mark |
|---|---|---:|---:|---:|---:|---:|---:|
| 5 s | 05:34:13.368 | 5.368 s | 49065 | 67¢ | 0¢ | 1.55¢ | −1.94¢ |
| 15 s | 05:34:23.666 | 15.666 s | 49153 | 67¢ | 0¢ | 1.55¢ | −1.94¢ |
| 30 s | 05:34:39.944 | 31.944 s | 49283 | 66¢ | −1¢ | 1.58¢ | −2.97¢ |
| 60 s | 05:35:08.473 | 60.473 s | 49642 | 99¢ | +32¢ | 0.07¢ | +31.54¢ |

**The 60-second mark arrived 10.465 s after inventory closed.** The book showed 1,015,811.40 on the YES bid at 99¢ and no opposite bid for an implied ask. The one-contract liquidation calculation was possible. A two-sided midpoint at that instant was unavailable. Each horizon answers a separate hypothetical liquidation question. Adding the marks to realized P&L would double-count the position.

The fill ledger contains exactly one opening buy and one closing sale. The position ended flat, open entry fees were zero, and the position row and last portfolio snapshot both reported +10.44¢ realized. The last snapshot had zero unrealized P&L, open orders and open positions. Across all 345 snapshots, realized plus unrealized equaled total, and the open-position count and cumulative realized P&L agreed with the two fill timestamps. The two snapshots during the hold each marked unrealized P&L at −1.94¢. Snapshot and horizon marks run on different schedules. No inventory or monetary discrepancy surfaced. The successful poll's raw `policy_accepted` flag is zero. The runtime treats a committed matching fill as authoritative. Joining on both candidate and intent ID recovers the one acceptance. [Booking logic](../src/kalshi_edge_engine/runtime/paper_runtime.py)

## Candidate and rejection funnel

137 admitted intents across 25 tickers and 16 event IDs yielded 136 expirations and one filled intent. The denominator below is the simulator's 5,165 journaled polls. Pre-admission exclusions lack a complete lifecycle journal.

| Evaluated path | Polls | Draws | Passed / candidates | Disabled-path rejections | Other candidate rejections | Booked |
|---|---:|---:|---:|---:|---:|---:|
| Above our limit (`moved_through`, p = 0.35) | 884 | 884 | 319 | 318 | 1 | 0 |
| Inside spread (`price_improves`, p = 0.03) | 3,512 | 3,512 | 102 | 102 | 0 | 0 |
| At displayed quote (`queue_cleared`, p = 0.10 when eligible) | 765 | 4 | 1 | 0 | 0 | 1 |
| No evaluated path | 4 | 0 | 0 | 0 | 0 | 0 |
| **Total** | **5,165** | **4,400** | **422** | **420** | **1** | **1** |

The 420 policy reasons were exactly `fill_path_moved_through_disabled` (318) and `fill_path_price_improves_disabled` (102). A further moved-through candidate, Phoenix–Houston poll 2840, book 38546 and validation 38547, hit `horizon_elapsed_at_validation` before the path-policy check. Its market response was active. The horizon check uses the local clock after validation processing. Its exact check time is unavailable. Thus **422 = 318 + 102 + 1 expired + 1 persisted**. Separately, four horizon rejections and six `market_observation_rejected` polls had no candidate and contribute zero to that equation. A higher displayed bid does not imply that a resting bid below it traded; the inside-spread path assumes seller arrivals at a hypothetical bid. The archive establishes no counterfactual P&L for the rejected candidates.

**Four eligible queue draws** occurred on three intents and three games. Sacramento books 48958 and 49009 had 10,419 and 20,862 modeled contracts beyond queue. Boston–Washington book 59480 had 10 and drew 0.8839559454544738. Atlanta–Boston book 84873 had 7 and drew 0.2522104274700562. Only the second Sacramento draw was below 0.10. The remaining **761 of 765** at-quote polls had insufficient modeled volume beyond queue.

**Concentration.** The 422 proposals came from 103 intents, 24 distinct market tickers and 16 games grouped by the archived fee profile's event ID. Seventy-two intents generated multiple candidates and account for 391 proposals. One intent generated 17. Opposing team tickers can represent the same game.

| Games, grouped by event date | Candidates per game |
|---|---|
| Nov 21 | BOS–SAC 2 |
| Nov 24 | BOS–WAS 1, DET–ORL 4 |
| Nov 25 | BKN–MIL 54, CHI–CHA 15, CLE–NYK 46, MIN–OKC 16, PHX–HOU 42, SAC–GSW 1, SAS–POR 51 |
| Nov 27 | ATL–BOS 15, BKN–TOR 4, DEN–UTA 31, LAL–SAS 46, NOP–LAC 9, PHI–IND 85 |

PHI–IND alone accounts for 85/422 (20.1%). The top five games account for 282/422 (66.8%). Counting repeated polls or both team tickers as independent opportunities would inflate the sample. At the candidate stage policy excluded almost all proposals. For the enabled path the largest measured reduction came **before** the 10% probability test, which only 4/765 at-quote polls qualified to take. Raw displayed liquidity was substantial on the winning trade. Executable opposing flow and queue priority were unobserved. These data cannot rank strategy discovery quality or establish a liquidity shortage.

## Execution reality and the next experiment

The nominal three-second polling interval yielded 5,028 consecutive within-intent book intervals with median 3.181 s, p95 3.270 s and maximum 12.623 s. Clean local receipt sequences verify capture of the requested responses. Many intervening exchange actions fit inside those intervals. A displayed-depth reduction alone cannot identify executed volume, and a favorable exit print cannot validate a passive entry. The engineering evidence supports a new measurement experiment. This run supplies one conditional outcome and no defensible estimate of live fill probability or expected net return.

### Proposed B3B2 protocol

1. **Freeze the comparison.** Register a new cohort and separate append-only dataset. Freeze the B3B1 simulator, configuration, fee profile interpretation, random draws and decision clocks. Collect alongside paper decisions, starting before admission. Include failed draws, eligible misses, non-eligible at-quote controls and adverse outcomes. Join prospective records by session, ticker and intent IDs. Keep the original sealed run as a historical baseline.
2. **Capture documented public feeds.** Kalshi documents an authenticated read-only `orderbook_delta` WebSocket with an initial aggregated snapshot and subsequent by-side, by-price changes. Request unconflated updates. Record connection, subscription `sid`, `seq`, market clock where supplied, exchange and send timestamps where present, and local wall-clock and monotonic arrival. Apply deltas in subscription sequence. Exchange clocks can skip or repeat per market. [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)
3. **Capture prints independently.** Subscribe to the public `trade` channel on an authenticated connection. Preserve trade ID, market, YES/NO prices, exact quantity, taker direction, block flag, sequence and exchange time when supplied. Reconcile deduplicated IDs with cursor-paginated recent and historical trade GETs across their cutoff. Exclude off-book/block trades from queue consumption. These feeds are documented; access, field coverage and delays on the operator's key require a read-only pilot. This archive contains none of those streams. [Public trades](https://docs.kalshi.com/websockets/public-trades) · [Recent trades](https://docs.kalshi.com/api-reference/market/get-trades) · [Historical trades](https://docs.kalshi.com/api-reference/historical/get-historical-trades)
4. **Reconstruct before attribution.** Start from a snapshot, normalize side and price to the contract being bought, and replay signed fixed-point deltas into a per-market book. Track subscription continuity across **all** subscribed markets. A sequence gap or reconnect ends the usable segment until a fresh snapshot. Deduplicate identical messages, flag conflicting duplicates and retain both exchange-time and arrival-time order. Never equate a depth sequence with a trade-channel sequence. Match only compatible opposing on-book prints by ticker, price, quantity and bounded time/order. Allow partial and many-to-many matches. Report unmatched decreases as unexplained removals and retain multiple feasible allocations. REST trade backfill can restore prints; it cannot restore missed depth order.
5. **Bound a shadow order.** For each prespecified arrival/latency scenario, replay FIFO at the original quote through cancellation or expiry. Carry fractional ahead quantities until the final whole-contract bound. With fixed external order flow and no market impact, let `T` be compatible opposing volume reaching the limit under a feasible sequence, and `A` remaining earlier priority that absorbs that flow. Update ahead at each event, place pre-arrival orders ahead, and allocate unidentified cancellations ahead or behind in separate feasible histories. Count a print at most once, respecting better prices and depth. Take the minimum and maximum filled quantity over feasible histories, capped at the paper size. A simple one-block case gives `min(size, floor(max(0, T−A)))`. Report the unconditional own-fill lower bound as zero. If order timing, direction or coverage is unknown, label the window indeterminate with bounds [0, size]. Even a tight conditional bound depends on ignoring the response to posting our order.
6. **Measure adverse selection.** At 5/15/30/60 s after both modeled and tape-compatible entries, publish directional midpoint movement when both sides exist, executable full-size bid-sweep P&L after both fees, observation delay, and missing outcomes. Compare with matched at-quote misses in the same game, spread and time stratum. Report marks and actual paper exits separately. Publish game-weighted results and game-clustered intervals; retain negative marks and zero-fill games.
7. **Preregister stopping and decision rules.** Proposed minimum for the primary comparison is **100 non-overlapping eligible episodes from 100 distinct games**, first eligible episode per game, on at least 10 distinct dates. Report every other episode as secondary, clustered by game and date. Four draws on three games cannot calibrate 10%. Require zero unresolved continuity errors in any scored interval, at least 95% usable primary windows, and at least 95% on-time 5 s marks among scored windows. Publish all exclusions against the original denominator. Below any coverage or sample minimum, return **inconclusive**. Do not replace excluded games selectively.
8. **Decide what to test next.** In the primary games, classify each one-contract episode as conditionally required, possible-only, or incompatible under the prespecified queue/latency assumptions. Compare 0.10 to the game-level lower/upper mean fill bounds with 95% sampling uncertainty, and publish sensitivity across latency and cancellation cases. An upper confidence limit below 0.10, or a lower confidence limit above 0.10 across the prespecified cases, **fails** the probability assumption under the model. A simulated fill with a fully observed zero upper bound fails that individual fill's tape-compatibility check. Passing coverage with 0.10 still inside the bounds warrants independent replication only. Report an economic result as unresolved unless conservative fee- and latency-adjusted outcomes remain positive out of sample with a positive lower confidence bound and robustness to the declared fill bounds. Public feeds alone cannot demonstrate the queue priority of an unsubmitted order.
