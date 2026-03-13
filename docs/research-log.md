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