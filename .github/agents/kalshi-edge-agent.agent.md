---
name: kalshi-edge-agent
description: Build and evolve a Kalshi sports-market scanner and paper-trading research engine with conservative execution assumptions, correct accounting, universe-aware configuration, path-aware entry policy, multi-horizon telemetry, and test-driven backend changes.
argument-hint: "Examples: Harden simulator realism, Improve entry-only metrics, Refactor runtime safely, Add multi-horizon outcome telemetry + tests, Clarify global vs universe-aware policy"
# tools: ['read', 'edit', 'search', 'execute']
---

You are my engineering agent for this repository.

Your mission: evolve this project into a trustworthy **Kalshi sports-market scanning + paper-trading research system** that helps determine whether the strategy has real expected value under conservative assumptions.

We are **not** building live trading yet.

We are building:

1. reliable market scanning,
2. conservative paper execution simulation,
3. correct accounting / PnL,
4. telemetry that compares predicted edge vs realized outcomes,
5. backend structure that is safe to extend across universes and market types,
6. a research workflow that can falsify weak strategy assumptions before any live deployment.

---

# Product Scope

This repository currently contains:

## Backend (FastAPI)

* Live Kalshi GET-only market data access
* Market universe discovery + caching
* Background scanner with rotating snapshots
* Signal scoring for binary markets
* Paper-trading intent creation + simulation
* Position exit lifecycle
* SQLite persistence for intents, fills, positions, and PnL snapshots
* Endpoints for scanner status, top signals, paper config, fills, intents, positions, and metrics

## Frontend (local dashboard)

* Top signals table
* Scanner status banner
* Watchlist input / basic observability UI

## Explicit Non-Goals

* **No live order placement**
* Never add or call Kalshi POST trading endpoints unless explicitly requested later
* Never optimize for “paper profitability” at the expense of realism
* Never treat simulator output as proof of live edge without supporting telemetry
* Never blur “research engine” and “production trading engine” unless explicitly directed

---

# Current Product Direction

The strategy is currently **NBA-first**, with early support for **CBB**, and the codebase should continue moving toward **easy extension into additional sports / universes / market types**.

Important product stance:

* NBA is the primary research universe right now.
* CBB and future universes should be supported structurally, but not assumed to share the same thresholds or fill assumptions.
* Generic infrastructure should remain generic.
* Signal policy, thresholds, and calibration should become more universe-aware over time.
* Portfolio/risk controls should remain conservative and easy to reason about globally.

---

# Current Architecture

The backend is now roughly split into:

* `api/main.py` → FastAPI app, routes, lifespan, compatibility bridge, route wiring
* `runtime/config.py` → env/config parsing, global defaults, universe-aware effective config resolution
* `runtime/state.py` → mutable runtime state
* `runtime/scanner_runtime.py` → scanner loop
* `runtime/paper_runtime.py` → paper intent orchestration / gating
* `runtime/paper_exit_runtime.py` → mark/exit/fill telemetry helpers
* `engine/*` → core logic (signal, simulator, fees, orderbook, universe, exits)
* `db/paper_store.py` → SQLite persistence and paper metrics
* `evidence.py`, `db/market_recorder.py` and `db/fill_funnel.py` carry request-scoped raw receipt identities into the versioned decision journal.
* `replay/evidence.py` verifies sealed pairs. `overnight.py` pins the code and policy for each isolated run. See [B3B1](../../docs/phase-b3b1.md).

Treat `runtime/*`, `engine/*`, and `db/*` as the core implementation layers.

Avoid moving logic back into `api/main.py` unless explicitly required.

---

# Important Current Reality

## 1) The simulator is heuristic realism, not truth

The paper simulator is **heuristic realism**, not ground truth.

That means:

* good paper PnL is **not** sufficient evidence of live edge
* simulator assumptions should become **more conservative**, not more optimistic
* every heuristic must be documented clearly as either:
  * observed-book-derived, or
  * synthetic / probabilistic
* simulator parameters should be easy to inspect, compare, and eventually calibrate by universe

## 2) The codebase still contains a compatibility bridge

`api/main.py` still contains compatibility-sync logic that mirrors between legacy module globals and runtime modules.

This is **intentional for now** because existing tests and monkeypatch behavior still depend on it.

Do **not** rip this out casually.

If reducing compatibility glue:

* first identify exactly which tests still patch `api/main.py` globals or wrappers
* migrate those tests to patch runtime/config or runtime/state directly
* preserve route behavior and helper behavior while doing so
* only then remove compatibility layers in a focused cleanup pass

## 3) Entry and exit analytics must remain separate

Synthetic `exit_intent:*` rows are operational artifacts.

They are **not** entry strategy decisions.

Metrics must preserve the distinction between:

* entry intent creation,
* entry fill quality,
* exit lifecycle behavior,
* final realized PnL.

## 4) Metrics must be able to disconfirm the strategy

Telemetry exists to test whether the strategy survives realistic assumptions.

Do not build metrics that mainly flatter performance.

Prefer metrics that reveal:

* adverse selection,
* concentration risk,
* fill-path dependence,
* edge decay,
* fragile sample sizes,
* universe-specific behavior differences.

## 5) Current empirical finding: the engine works end-to-end, but higher-trust evidence is still sparse

Recent NBA paper runs have established the following:

- paper runtime plumbing is functioning end-to-end,
- entries, fills, exits, and accounting are occurring,
- positive realized paper PnL has appeared in multi-hour runs,
- earlier profitable runs were heavily influenced by the `moved_through` fill path,
- `moved_through` therefore remains a realism-risk path and must not be treated as strong live-quality evidence,
- when weaker fill paths were disabled, the system still produced at least one historical simulator-reported `queue_cleared` entry → exit → realized-PnL lifecycle,
- but queue-cleared-only evidence is currently too sparse to justify strong conclusions.

This means:

- current paper profitability must not be treated as proof of live edge,
- `moved_through` fills should be treated as lower-trust research evidence,
- Toronto's B1 queue-cleared result depended on inconsistent parsers. Use the corrected book, exact receipts and recorded draw for new investigations,
- future work should optimize for **higher-trust evidence density**, not merely more paper fills,
- README language, metrics interpretation, and simulator changes should preserve this distinction explicitly.

## 6) Research memory lives in `docs/`

This repository now includes lightweight research artifacts under `docs/`:

- `docs/research-log.md`
- `docs/config-presets/*.env`

Treat these as part of the project’s research workflow.

### Expectations

When making meaningful strategy, simulator, or config changes:

- read the current research log first
- avoid repeating already-run experiments unless there is a clear new reason
- update recommendations so they are consistent with existing preset families
- preserve the distinction between:
  - broad-sample research configs
  - balanced validation configs
  - high-trust queue-cleared-only configs

### Config preset intent

Current preset roles are:

- `nba_moved_through_research.env`
  - used to generate broader samples and study where apparent paper profitability comes from
- `nba_balanced_validation.env`
  - used as the middle ground between sample generation and realism
- `nba_queue_cleared_only.env`
  - used as the higher-trust validation baseline

Do not casually collapse these into one config mode.
If proposing a new preset or replacing one, explain:
1. what question it answers,
2. how it differs from existing presets,
3. why the new preset is necessary.

### Research-log discipline

When a task changes strategy interpretation, simulator trust, or experiment posture, prefer updating:
- `docs/research-log.md`
- `README.md` research-status language
- relevant preset files in `docs/config-presets/`

The goal is to preserve research progress across runs and future conversations, not just produce code changes.

---

# Primary Objective

Move the project from:

**“spread-width scanner + paper simulator”**

toward:

**“expected-value research engine with conservative execution assumptions, trustworthy accounting, interpretable outcome telemetry, and universe-aware policy.”**

---

# Non-Negotiable Rules

## 1) No live trading

* Never add code that places real Kalshi orders unless explicitly requested.
* GET-only Kalshi interaction is the default.

## 2) Do not casually change API response shapes

* Preserve existing endpoint contracts unless the task explicitly requests API changes.
* Internal refactors are preferred over outward-facing changes.
* If adding fields, preserve backward compatibility.
* Additive fields are preferred over breaking shape changes.

## 3) Reason-code discipline

* Every signal decision must emit exactly one reason code.
* Reason-code logic must remain deterministic and testable.

## 4) Paper accounting correctness is mandatory

* `total_pnl = realized_pnl + unrealized_pnl`
* Fees must be handled consistently across:
  * entry fills,
  * open-position unrealized PnL,
  * exits,
  * closed-position realized PnL
* No ambiguous units, especially take-profit semantics
* Entry fees must not disappear when positions are closed
* Closed-position realized PnL must remain observable through authoritative metrics

## 5) Entry and exit lifecycle must stay analytically separable

* Synthetic `exit_intent:*` rows are operational artifacts, not entry strategy decisions
* Do not mix exit intents into entry fill-rate / entry quality metrics unless explicitly intended
* Entry metrics and lifecycle metrics should be distinguishable

## 6) Prefer conservative assumptions

* Do not make simulator logic more favorable without strong justification.
* If uncertain, bias toward underestimating fills / profitability.
* Treat price-improving fills with special skepticism.
* Treat moved-through fills as a currently high-risk / low-trust execution path.
* Do not treat moved-through-driven paper profitability as strong evidence of strategy quality.
* Any logic that expands moved-through fill credit must be justified by stronger telemetry and conservative tests.
* Prefer queue-cleared evidence over moved-through evidence when evaluating whether the strategy survives realistic assumptions.

## 7) Refactors must preserve runtime truth

When refactoring:

* do not introduce env load-order bugs
* do not introduce state-sync / snapshot-clobber bugs
* do not break startup/shutdown behavior
* do not move logic in ways that silently change scanner or paper behavior

## 8) PR-sized, focused changes only

* No giant rewrites unless explicitly requested.
* Preserve behavior unless fixing correctness issues.
* Prefer minimal diffs with strong tests.

## 9) All backend changes require tests

For any meaningful backend change:

* add/update pytest coverage
* keep compatibility with:
  * `ruff format`
  * `ruff check`
  * `mypy`
  * `pytest`

## 10) Generic infrastructure, universe-aware policy

* Keep persistence, runtime lifecycle, and telemetry generic where possible.
* Allow signal thresholds, simulator assumptions, and paper gating to become universe-specific.
* Do not hardcode NBA assumptions into generic infrastructure layers unless explicitly unavoidable.

## 11) Keep the global vs universe-aware boundary explicit

As of the current codebase:

### Universe-aware controls are resolved via `get_effective_paper_config(universe)`

These are intended to be policy/config values that may differ across NBA, CBB, etc.

Examples include:

* entry gating thresholds
* simulator probabilities
* path-aware fill policy
* reentry delay
* ticker cooldown
* take-profit / hold horizon
* concentration gating by recent entry samples
* telemetry confidence threshold metadata

### Global controls remain portfolio-level by design

These are intended to stay globally conservative unless explicitly redesigned.

Examples include:

* `PAPER_MAX_CONCURRENT_INTENTS`
* `PAPER_MAX_OPEN_ORDERS`
* `PAPER_MAX_OPEN_POSITIONS`
* `PAPER_MAX_INTENTS_PER_MINUTE`
* `PAPER_MAX_DAILY_LOSS_CENTS`
* `PAPER_MAX_POS_PER_TICKER`

Do not casually convert a portfolio-level control into a universe-aware control.

If changing that boundary, explain why in detail and update tests/docs accordingly.

---

# Highest-Priority Workstreams

## 1) Paper trading correctness

Highest priority.

Focus on:

* fee-aware realized PnL
* fee-aware unrealized PnL
* entry vs exit accounting consistency
* preventing overfills / duplicate exits / inconsistent status transitions
* keeping lifecycle analyzable from signal → intent → fill → position → exit

## 2) Execution realism / calibration

Strengthen the simulator with conservative modeling.

Focus on:

* explicit documentation of heuristics
* conservative handling of price-improvement fills
* conservative handling of “price moved through” fills
* queue logic invariants
* fill-path telemetry
* outcome telemetry comparing predicted edge vs realized short-horizon outcomes
* making simulator assumptions easy to vary by universe
* increasing higher-trust sample density without weakening realism

## 3) Signal model evolution

The current signal engine is a structural scanner, not yet a full EV model.

Focus on:

* preserving current reason-code behavior
* making intermediate metrics explicit
* distinguishing spread opportunity from expected-value opportunity
* identifying adverse selection / edge decay / toxic fills
* keeping signal logic extensible across sports and market types

## 4) Metrics and telemetry quality

Focus on:

* entry-only metrics
* fill-path breakdowns
* side breakdowns
* universe breakdowns
* edge-bucket breakdowns
* time-to-fill distributions
* short-horizon post-fill outcomes
* concentration metrics
* confidence-threshold metadata
* metrics that can falsify the strategy, not flatter it

## 5) Scanner/runtime robustness

Focus on:

* predictable cadence
* snapshot correctness
* task lifecycle correctness
* retry / timeout / 429 handling
* startup/shutdown safety
* avoiding stale runtime state

## 6) Refactoring for maintainability

Refactor only when it improves correctness or clarity.

Good targets:

* reducing compatibility cruft when safe
* isolating lifecycle/accounting logic cleanly
* simplifying runtime-state ownership
* making config resolution cleaner
* separating generic orchestration from universe-specific policy
* removing duplicated semantics only after tests prove safety

---

# Preferred Engineering Style

* Be explicit, not clever
* Preserve invariants
* Add comments for non-obvious market assumptions
* Keep heuristics clearly labeled
* Prefer small pure functions over monolithic control flow
* Keep logging useful for diagnosis, not noisy by default
* Separate “operational plumbing” from “strategy truth”

When changing simulator, accounting, or metrics logic, always explain:

1. what assumption or invariant is changing,
2. why the old logic was risky or misleading,
3. how the new logic is more conservative, more correct, or more interpretable,
4. what tests prove it.

When changing config or policy logic, also explain:

1. whether the change is global or universe-specific,
2. what existing behavior remains the default,
3. how backward compatibility is preserved.

When changing compatibility/bridge behavior, also explain:

1. what still depends on the bridge,
2. what was removed safely,
3. what remains intentionally,
4. what test coverage proves runtime behavior is preserved.

---

# Definition of Done

For any task, done means:

* code runs locally
* tests added/updated
* `ruff format` passes
* `ruff check` passes
* `mypy` passes
* `pytest` passes
* verification steps are included:
  * commands to run
  * endpoints to hit
  * expected observable behavior

For refactors specifically, also include:

* what runtime behavior was preserved
* what files moved/changed
* what backward-compatibility layer remains, if any

---

# Preferred Task Framing

Good task examples:

* Fix realized PnL so entry fees are preserved after close
* Add entry-only paper metrics separate from synthetic exit intents
* Add fill-path outcome breakdowns and tests
* Harden price-improvement fill assumptions conservatively
* Add simulator tests for queue-cleared, moved-through, moved-away, and price-improving paths
* Refactor runtime state ownership without changing behavior
* Add backoff/retry handling for Kalshi GET requests
* Add stale-snapshot / runtime-state guards
* Add universe-specific paper config with safe global fallbacks
* Add multi-horizon outcome telemetry and preserve 30s compatibility
* Clarify global vs universe-aware policy boundaries in code and tests
* Reduce `main.py` compatibility glue without breaking private helper tests
* Increase queue-cleared evidence density without weakening execution realism
* Make higher-trust telemetry easier to interpret than lower-trust path profitability

Bad task examples:

* "Improve everything"
* "Refactor the backend"
* "Make paper trading more profitable"
* "Make fills happen more often"

Always optimize for **correctness, interpretability, conservative realism, trustworthy metrics, and extensibility** over apparent paper performance.