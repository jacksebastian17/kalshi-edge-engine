# Research Log

This file preserves the outcome of paper-trading experiments so that research progress is not lost across chats, config changes, or refactors.

It is intentionally lightweight:
- what config family was tested
- what actually happened
- what conclusion was justified
- what next test should be run

---

## 2026-03-12 — Moved-through-enabled NBA research runs

### Goal
Validate whether the paper stack works end-to-end under realistic multi-hour runtime conditions and observe which fill paths actually drive paper profitability.

### Config family
Relaxed NBA research profile with:
- `price_improves=false`
- `moved_through=true`
- moderate NBA thresholds
- headline reporting configured to exclude moved-through from confidence headlines

### What was observed
Across multi-hour NBA runs, the system demonstrated that it can:
- generate entry intents
- simulate fills
- open and close paper positions
- persist fills, exits, intents, and PnL
- produce entry-only metrics and multi-horizon outcome telemetry

Observed runtime/accounting behavior looked increasingly credible.

However, the most profitable paper behavior was heavily driven by `moved_through` fills.

### Why that matters
The `moved_through` path is useful for research, but it is currently a realism-risk path because:
- it may over-credit stale or gap-through transitions
- short-horizon post-fill outcomes looked unusually strong
- paper profitability may therefore overstate realizable live edge

### High-level conclusion
What was learned:
- end-to-end paper runtime plumbing works
- accounting and telemetry are functioning
- signal generation is producing actionable candidates
- positive paper PnL under this config is **not** strong evidence of live tradability
- current paper profitability was too dependent on `moved_through` to be trusted as proof of edge

### Result classification
- runtime validity: **positive**
- accounting validity: **positive**
- telemetry validity: **positive**
- execution realism confidence: **still limited**
- live-edge evidence: **not established**

### Next step that followed
Run a stricter NBA profile with:
- `price_improves=false`
- `moved_through=false`
- queue-cleared-only reporting

---

## 2026-03-12 — Queue-cleared-only NBA validation run

### Goal
Test the higher-trust execution baseline by disabling the sketchier fill paths and seeing whether the strategy still produces fills and positive outcomes.

### Config family
Strict NBA validation profile with:
- `price_improves=false`
- `moved_through=false`
- queue-cleared-only reporting
- higher-trust interpretation of fills

### What was observed
In a roughly 4-hour evening run:
- total intents were generated across many tickers
- only one entry fill occurred
- that fill was `queue_cleared`
- the trade was profitable
- accounting remained consistent
- open positions were cleanly closed by the end
- sample size was extremely small

### Why that matters
This run showed:
- the system can still fill under stricter path gating
- queue-cleared fills are possible
- higher-trust profitability evidence exists in principle

But it also showed:
- fill throughput collapses when `moved_through` is disabled
- the current trusted evidence base is far too small to claim strategy quality

### High-level conclusion
What was learned:
- `queue_cleared` is the current higher-trust baseline
- strict realism dramatically reduces sample count
- the project’s current bottleneck is no longer “does the system run?” but rather:
  - can it produce enough high-trust fills
  - across enough distinct tickers
  - without relying on low-trust paths

### Result classification
- runtime validity: **positive**
- accounting validity: **positive**
- queue-cleared execution evidence: **present but tiny**
- statistical confidence: **very low**
- live-edge evidence: **not established**

### Next open research question
Find a middle ground that:
- remains much more trustworthy than moved-through-heavy runs
- but generates enough fills to evaluate whether the strategy survives on higher-trust paths

---

## Current overall research state

### What has been demonstrated
- paper mode works end-to-end
- entry/exit lifecycle works
- accounting is increasingly trustworthy
- telemetry is capable of separating entry evidence from exit artifacts
- fill-path analysis is surfacing genuinely useful realism questions

### What has not been demonstrated
- durable positive edge under high-trust execution assumptions
- sufficient breadth across distinct tickers
- sufficient sample size for confidence
- evidence strong enough to justify live trading

### Current best interpretation
The project has made real progress, but current evidence supports:

- **infrastructure credibility is improving**
- **signal generation is promising**
- **execution realism is still the core unresolved problem**
- **live-trading readiness is not yet established**

---

## How to use this log going forward

For each meaningful run, add a short entry with:
1. date
2. config family
3. run duration
4. what happened
5. what was learned
6. what should be tested next

Do not try to preserve every endpoint dump here.
Preserve the conclusion, not the raw noise.

## 2026-11-19 — Paper execution and accounting audit

This entry records engineering changes. The earlier experiments remain historical records under their original simulation and accounting assumptions.

### Trading economics

- Market discovery and quote scoring still use Kalshi's public orderbook. The best bid is the highest displayed buy price for the held contract. The implied ask is 100¢ minus the opposite outcome's best bid. The midpoint averages the two. A modeled immediate sale walks the held outcome's bids from highest to lowest, requires enough displayed depth for the full position, and records the size-weighted price and bid levels consumed. A 46¢ YES entry against a 45¢ bid and a 55¢ implied ask loses 1¢ gross.
- For either long outcome, gross P&L is `(sale price − entry price) × contracts` in that outcome's own coordinates. Paper entries pay the configured fee model per accepted fill. Liquidation estimates pay modeled taker fees rounded per consumed level. The take-profit threshold uses net cents per contract after both fees. Time stops use the same liquidation price and wait when depth is unavailable. A passive maker-exit intent has no role in completion.
- `imbalance_gap` and the positive `inside_width_yes/no` fields describe the same two-sided spread. `net_edge_yes/no` measures implied ask minus the intended entry bid, estimated costs and the configured buffer. The gross room to the ask is hypothetical spread capture. Fill probability and conditional adverse selection require separate evidence. The bid sweep estimates proceeds for immediate liquidation. Simulated net P&L includes the model's fees and assumptions. Estimating expected value requires weighting filled and unfilled outcomes by credible probabilities and testing net returns out of sample.

### Durability and measurement

- Accepted entry fills atomically update fill history, remaining intent size, inventory, average entry price and current entry fees. Closing atomically records the exit and realized P&L delta, closes inventory, and cancels same-side remainders. The first accepted fill sets the lifecycle clock. The next lifecycle starts with its own timestamp and fees and preserves the ticker-side realized balance. On single-worker startup, orphaned intents are finalized from persisted fill quantities and any unfilled remainder is canceled. Previously committed partial inventory survives.
- Position maintenance fetches books independently of universe discovery and rankings. REST timestamp freshness is measured from local receipt, with a 10-second age limit and request timeout. Insufficient depth, stale observations, and unavailable books leave positions unmarked. API totals are unavailable when coverage is incomplete. The marked subtotal, unmarked contract count, unmarked cost basis and oldest mark timestamp show the coverage.
- The daily gate uses UTC calendar days. Its risk P&L is the sum of today's persisted realized exits and current open downside, including full cost plus entry fees on unmarked positions. Open gains have zero credit. Overnight inventory stays exposed at its cost basis until exit. A threshold breach cancels open entry remainders and persists through restarts until the next UTC day. A prior-day booked loss never enters a new day's realized sum.
- The simulator records inference sources with each fill. `queue_cleared` uses consecutive displayed size declines at the same quote level. These can reflect cancellations. `moved_through` is a higher same-side bid plus a volume heuristic. `price_improves` models a seller arriving at a hypothetical price. Public depth is observed; queue position, matched size and whether a paper order would have filled are inferred. Model parameters and probability thresholds remain those configured by the operator.
- The outcome worker records actual book receipt times for 5s, 15s, 30s and 60s entry horizons. Headline samples allow up to two seconds after the target. Observations up to 15 seconds late are retained with their actual timestamps. Later observations are counted as missed. Entry outcomes use a standalone bid sweep for the fill size, with both modeled fees. Exit fills and timestamp-free historical marks are excluded from horizon statistics.
- The additive schema migration preserves historical fills, positions and research notes. Older fills and snapshots carry unverified provenance. Legacy ask-side exits, timing and overwritten realized balances cannot be recovered reliably from the available polls. The summary exposes the legacy realized balance alongside the sum of new exit ledger events. Daily realized risk uses those new timestamped exit events.

### Next milestone — Capture and replay public events

1. Build a read-only recorder for `orderbook_delta` and `trade` on a controlled set of markets. Store raw messages with connection ID, subscription ID, sequence, event timestamp, monotonic receipt time and configuration version. Keep data segments replayable across reconnects. [Kalshi orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates) · [Kalshi public trades](https://docs.kalshi.com/websockets/public-trades)
2. Reconstruct each ladder from snapshots and deltas in subscription order, normalize fixed-point prices and sizes, and deduplicate public trades by trade ID. Detect gaps and invalidate affected intervals until a fresh snapshot. Compare reconstructed depth with occasional REST snapshots. [Kalshi market orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
3. Replay from a deterministic clock with configurable quote latency and queue-ahead bounds. Match trades by ticker, outcome, aggressor side, price and time, with explicit cross-feed ordering uncertainty. Use matched trade volume once. Treat unmatched size reductions as ambiguous. Persist conservative and optimistic fill bounds and carry the same liquidation, fees and horizon semantics into replay.
4. Test gaps, reconnects, duplicates, cancellations and partial fills with captured fixtures. Evaluate modeled fill rates, adverse selection and net P&L on held-out markets with uncertainty intervals and coverage counts. Include fees, latency, possible price impact and missed observations in the interpretation.

## 2026-11-19 — Settlement, fee profiles, and experiment cutover

- Paper maintenance runs in the application lifespan with or without discovery. An exclusive database lock protects recovery and worker ownership. Scanner stops leave marks, exits, settlement, outcome measurement and daily risk active. The lifespan supervises failures, cancels tasks before closing the client, and recovers outstanding intents after acquiring the lock on startup.
- Reconciliation checks tracked tickers through the read-only market endpoint, with a historical lookup on 404. It requires final status, a recognized result, and a valid settlement timestamp. Scalar resolutions also require an explicit $1-contract payout. A transaction records the authoritative result, books YES and complementary NO payouts with zero settlement fee, closes open inventory, and cancels pending remainders. The records include exchange settlement time and local reconciliation time. The operational UTC risk session books the result on reconciliation. [Market lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle) · [Historical market endpoint](https://docs.kalshi.com/api-reference/historical/get-historical-market) · [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- The `kalshi_quadratic_direct_2026_07_v1` profile uses fee type and multiplier from the series and event-level overrides. The live series records for `KXNBAGAME` and `KXNCAAMBGAME` both returned `quadratic_with_maker_fees` with multiplier 1. At 50¢, the modeled single-contract direct-member fees are 0.44¢ maker and 1.75¢ taker. Intents, fills, bid exits and horizon estimates retain fee metadata and amounts. The legacy whole-cent model has its own version and can be used to value legacy inventory when current metadata is missing. Unsupported fee types, active fee waivers and unavailable metadata block new entries. [Series metadata](https://docs.kalshi.com/api-reference/market/get-series) · [Event metadata](https://docs.kalshi.com/api-reference/events/get-event) · [Fee rounding](https://docs.kalshi.com/getting_started/fee_rounding)
- The database stores a fixed cutover for `series_fees_and_settlement_v1`. The primary API and dashboard P&L include only complete post-cutover lifecycles backed by fee-profiled simulated entries and a fee-profiled bid exit or authoritative settlement. Pre-cutover inventory, mixed lifecycles, synthetic debug activity and legacy balances stay outside that ledger. Historical fills and snapshots remain available. All-inventory risk still covers historical inventory. Timestamped horizon aggregates cover multiple vintages and fill records carry experiment identifiers for research filtering.
- This was an engineering follow-up. Fee estimates use direct-member precision per modeled fill or consumed level. Per-order rounding rebates, third-party clearing costs, changes between fee metadata reads, and simulated execution probabilities remain unvalidated.
