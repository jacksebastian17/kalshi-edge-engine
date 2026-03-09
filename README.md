# Kalshi Edge Engine

Real-time Kalshi sports-market scanner and paper-trading research engine.

This project is intentionally **paper-only** right now:

- Uses Kalshi market data endpoints (`GET`)
- Generates structured signal decisions
- Runs a continuous background scanner
- Simulates fills with a conservative queue/volume-aware model
- Persists paper state and PnL in SQLite
- Exposes a local FastAPI backend + React dashboard
- Produces telemetry to compare predicted edge vs realized post-fill outcomes

It does **not** place live orders.

---

## What this repository is

Kalshi Edge Engine is an exchange-behavior research daemon focused primarily on **NBA** markets, with early support for **CBB** and a code structure that can extend into additional sports and universes over time.

The strategy layer is currently focused on structural market conditions:

- spread / width,
- liquidity,
- imbalance,
- fee-aware entry economics,
- conservative paper execution assumptions,
- post-fill outcome telemetry.

The system is built to answer:

- Are measurable edge opportunities showing up in live books?
- Do those opportunities survive realistic fill assumptions?
- Is PnL internally consistent when fees + marks + exits are modeled correctly?
- Are outcomes dependent on fill path, ticker concentration, or universe-specific configuration?
- Is the strategy still promising after conservative telemetry, not just headline paper PnL?

---

## Current product stance

This repo is a **research engine**, not a production trading engine.

Important stance:

- **NBA is the primary research universe**
- **CBB is early / structural support**
- Universe-specific thresholds and simulator assumptions are expected to diverge over time
- Good paper results are **not** proof of live tradability
- The purpose of telemetry is to **validate or falsify** strategy assumptions before live trading is considered

---

## Current capabilities

### Backend (FastAPI)

- Health/version/debug endpoints
- Single-ticker and multi-ticker signal scoring
- Cached universe discovery
- Background rotating scanner with lifecycle controls
- In-memory top-signals snapshot for UI
- Paper trading runtime controls + inspection endpoints
- Intent/fill/position/PnL persistence in SQLite
- Outcome metrics, concentration metrics, and entry-summary endpoints

### Scanner

- Universe refresh on a recurring cadence
- Rotating slice scan of active markets
- Concurrency-limited orderbook fetches
- Per-ticker cooldown after failures
- Snapshot sorted by strongest computed edge
- Snapshot exposure through `/signals/top`

### Paper trading

- Quote intent generation from qualifying signals
- Universe-aware entry gating
- Conservative fill simulation via `RealisticOrderSimulator`
- Queue-ahead + traded-volume-aware fill eligibility
- Path-aware entry policy:
  - `price_improves`
  - `moved_through`
  - at-best / queue-cleared behavior
- Fill telemetry:
  - best bid at submit/fill
  - cross at submit/fill
  - fill-path tagging
  - multi-horizon post-fill outcomes
- Position exit lifecycle with fee-aware realized PnL
- Periodic PnL snapshots
- Entry-only metrics separated from synthetic exits

### Outcome telemetry

- Multi-horizon follow-up outcome tracking:
  - 5s
  - 15s
  - 30s
  - 60s
- Entry-only grouped summaries by:
  - universe
  - side
  - fill path
  - edge bucket
- Concentration metrics:
  - unique tickers
  - unique ticker-side pairs
  - top ticker concentration share
  - top ticker-side concentration share
- Confidence-threshold metadata for distinct ticker breadth

### Safety and gating (paper mode)

- Max open orders
- Max concurrent simulation intents
- Max open positions
- Max exposure per ticker
- Intents-per-minute rate limit
- Per-ticker cooldown
- Daily loss kill-switch gate for **new** intents
- Optional entry-sample concentration gates by ticker / ticker-side

---

## Explicit non-goals

- **No live order placement**
- No Kalshi POST trading endpoints unless explicitly requested later
- No “make it look profitable” changes that weaken realism
- No confusing synthetic exits with true entry-strategy observations
- No treating paper profitability as sufficient evidence of real edge

---

## Architecture (high level)

```text
Kalshi API (GET only)
  -> Universe discovery + caching
  -> Background scanner loop
       -> Fetch orderbooks (bounded concurrency)
       -> score_signal()
       -> top-of-book snapshot + top signals cache
       -> enqueue paper work item when paper mode is enabled

Paper worker flow
  -> build_quote_intents()
  -> apply universe-aware entry gating
  -> persist intent
  -> RealisticOrderSimulator.simulate_order_fills()
  -> path-aware fill acceptance/rejection
  -> persist fills
  -> update positions
  -> compute multi-horizon outcome telemetry
  -> close positions via exit helpers
  -> snapshot realized / unrealized / total PnL

FastAPI
  -> scanner lifecycle
  -> signal endpoints
  -> paper trading inspection + metrics endpoints

React dashboard
  -> polls scanner status / top signals
  -> basic watchlist / observability UI
```

## Current code layout

```text
src/kalshi_edge_engine/
  api/main.py
    FastAPI app, route wiring, lifespan, compatibility bridge

  runtime/config.py
    global env parsing, universe-aware config resolution

  runtime/state.py
    mutable scanner/paper runtime state

  runtime/scanner_runtime.py
    background scanner loop

  runtime/paper_runtime.py
    paper intent orchestration, gating, simulator launch

  runtime/paper_exit_runtime.py
    mark-price fetches, exit helpers, outcome telemetry updates

  engine/signal.py
    signal math and reason codes

  engine/paper.py
    quote intent construction + paper dataclasses

  engine/paper_sim.py
    RealisticOrderSimulator + diagnostics

  engine/orderbook.py
    orderbook parsing helpers

  engine/position_exit.py
    take-profit / exit decision helpers

  engine/universe.py
    universe discovery + caching

  db/paper_store.py
    SQLite persistence + metrics + grouped telemetry

tests/
  Pytest suite covering API, runtime behavior, simulator logic,
  PnL consistency, universe-aware config, path policy, and metrics

web/
  Vite + React dashboard
```

## Current architecture note: compatibility bridge

`api/main.py` still contains a compatibility bridge that syncs some legacy module globals with runtime modules.

This is **intentional for now** because parts of the test suite still patch helpers and globals through `api/main.py`.

That means:

- this bridge should not be removed casually
- compatibility reduction should happen only in a focused cleanup pass
- behavior preservation matters more than file neatness

The goal is to reduce that bridge later, but only after tests are migrated to patch runtime/config and runtime/state more directly.

---

## API surface

### Core

- `GET /health`
- `GET /version`
- `GET /debug/orderbook/{ticker}`
- `GET /signals/live?ticker=...`
- `GET /signals/scan?tickers=A,B,C`
- `GET /signals/top?n=20`
- `GET /universe/nba`

### Scanner lifecycle

- `GET /scanner/status`
- `POST /scanner/start`
- `POST /scanner/stop`

### Paper lifecycle + observability

- `POST /paper/start`
- `POST /paper/stop`
- `GET /paper/config`
- `GET /paper/intents?status=OPEN&limit=50`
- `GET /paper/intents?status=FILLED&limit=50`
- `GET /paper/fills?limit=50`
- `GET /paper/positions`
- `GET /paper/pnl-snapshot`
- `GET /paper/summary`
- `GET /paper/metrics`

### Outcome / research endpoints

- `GET /paper/metrics/outcomes?horizon_seconds=5`
- `GET /paper/metrics/outcomes?horizon_seconds=15`
- `GET /paper/metrics/outcomes?horizon_seconds=30`
- `GET /paper/metrics/outcomes?horizon_seconds=60`
- `GET /paper/metrics/outcomes/entry-summary`
- `GET /paper/metrics/outcomes/concentration`
- `GET /paper/metrics/outcomes/audit`

### Paper debug

- `GET /paper/debug/intent/{intent_id}`
- `POST /paper/debug/force-fill`

---

## Configuration

Create `.env` from `.env.example` and set at least Kalshi credentials.

### Required

- `KALSHI_API_KEY_ID`
- `KALSHI_API_KEY_FILE` (path to private key PEM)

### Optional core

- `KALSHI_BASE_URL`
- `ENABLE_NBA_SCANNER`
- `ENABLE_PAPER_TRADING`
- `ENABLE_UNIVERSE_NBA`
- `ENABLE_UNIVERSE_CBB`

---

## Paper configuration model

The project now supports **global defaults** plus **optional universe-specific overrides**.

### Global defaults

Global `PAPER_*` keys define the baseline behavior.

Examples:

- `PAPER_MIN_EDGE_CENTS`
- `PAPER_MIN_TOP_LIQUIDITY`
- `PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS`
- `PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS`
- `PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE`
- `PAPER_FILL_PROBABILITY`
- `PAPER_FILL_PROBABILITY_AT_BEST`
- `PAPER_FILL_PROBABILITY_PRICE_IMPROVES`
- `PAPER_ALLOW_PRICE_IMPROVES`
- `PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES`
- `PAPER_ALLOW_MOVED_THROUGH`
- `PAPER_MIN_EDGE_CENTS_MOVED_THROUGH`

### Universe-specific overrides

You can override many paper controls using:

```text
PAPER_<UNIVERSE>_<KEY>
```

Examples:

- `PAPER_NBA_MIN_EDGE_CENTS`
- `PAPER_NBA_MIN_TOP_LIQUIDITY`
- `PAPER_NBA_FILL_PROBABILITY`
- `PAPER_NBA_ALLOW_PRICE_IMPROVES`
- `PAPER_NBA_MIN_EDGE_CENTS_PRICE_IMPROVES`
- `PAPER_CBB_MIN_EDGE_CENTS`

If a universe-specific key is missing, runtime falls back to the global `PAPER_*` value.

---

## Global vs universe-aware control boundary

This is important.

### Universe-aware controls

These are resolved through `get_effective_paper_config(universe)` and are intended to vary across NBA / CBB / future universes.

Examples include:

- `MIN_EDGE_CENTS`
- `ALLOW_YES_ENTRIES`
- `ALLOW_NO_ENTRIES`
- `MIN_TOP_LIQUIDITY`
- `REQUIRE_SIGNAL_VALID_ONLY`
- `MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS`
- `MIN_ABS_DISTANCE_FROM_50_CENTS`
- `MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE`
- `TAKE_PROFIT_CENTS_PER_CONTRACT`
- `MAX_HOLD_SECONDS`
- `FILL_HORIZON_SECONDS`
- `POLL_INTERVAL_SECONDS`
- `FILL_PROBABILITY`
- `FILL_PROBABILITY_AT_BEST`
- `FILL_PROBABILITY_PRICE_IMPROVES`
- `ALLOW_PRICE_IMPROVES`
- `ALLOW_MOVED_THROUGH`
- `MIN_EDGE_CENTS_PRICE_IMPROVES`
- `MIN_EDGE_CENTS_MOVED_THROUGH`
- `MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR`
- `MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR`
- `MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE`
- `SIMULATED_VOLUME_PER_POLL`
- `TICKER_COOLDOWN_SECONDS`

### Global portfolio-level controls

These remain intentionally global and conservative:

- `ENABLE_PAPER_TRADING`
- `PAPER_MAX_CONCURRENT_INTENTS`
- `PAPER_MAX_OPEN_ORDERS`
- `PAPER_MAX_OPEN_POSITIONS`
- `PAPER_MAX_INTENTS_PER_MINUTE`
- `PAPER_MAX_DAILY_LOSS_CENTS`
- `PAPER_MAX_POS_PER_TICKER`

`PAPER_MAX_POS_PER_TICKER` is treated as a **global portfolio exposure control**, not a universe-aware policy knob.

---

## Important paper controls

### Entry gating

- `PAPER_ALLOW_YES_ENTRIES`
- `PAPER_ALLOW_NO_ENTRIES`
- `PAPER_MIN_TOP_LIQUIDITY`
- `PAPER_REQUIRE_SIGNAL_VALID_ONLY`
- `PAPER_MIN_EDGE_CENTS`
- `PAPER_MIN_CAPTURABLE_EDGE_TO_CROSS_CENTS`
- `PAPER_MIN_ABS_DISTANCE_FROM_50_CENTS`
- `PAPER_MIN_REENTRY_DELAY_SECONDS_SAME_TICKER_SIDE`

### Path-aware policy

- `PAPER_ALLOW_PRICE_IMPROVES`
- `PAPER_MIN_EDGE_CENTS_PRICE_IMPROVES`
- `PAPER_ALLOW_MOVED_THROUGH`
- `PAPER_MIN_EDGE_CENTS_MOVED_THROUGH`

These let you be more selective by fill path.

### Fill simulator

- `PAPER_FILL_PROBABILITY`
- `PAPER_FILL_PROBABILITY_AT_BEST`
- `PAPER_FILL_PROBABILITY_PRICE_IMPROVES`
- `PAPER_SIMULATED_VOLUME_PER_POLL`
- `PAPER_POLL_INTERVAL_SECONDS`
- `PAPER_FILL_HORIZON_SECONDS`

### Lifecycle / exit

- `PAPER_TAKE_PROFIT_CENTS`
- `PAPER_TAKE_PROFIT_CENTS_PER_CONTRACT` (preferred explicit name)
- `PAPER_MAX_HOLD_SECONDS`

### Capacity / rate / risk

- `PAPER_MAX_CONCURRENT_INTENTS`
- `PAPER_MAX_OPEN_ORDERS`
- `PAPER_MAX_OPEN_POSITIONS`
- `PAPER_MAX_POS_PER_TICKER`
- `PAPER_MAX_INTENTS_PER_MINUTE`
- `PAPER_TICKER_COOLDOWN_SECONDS`
- `PAPER_MAX_DAILY_LOSS_CENTS`

### Concentration / confidence controls

- `PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_PER_HOUR`
- `PAPER_MAX_ENTRY_SAMPLES_PER_TICKER_SIDE_PER_HOUR`
- `PAPER_MIN_DISTINCT_TICKERS_FOR_STRATEGY_CONFIDENCE`

The first two are optional entry gates.  
The distinct-ticker threshold is currently a **telemetry confidence aid**, not proof of edge.

---

## Current observability focus

The paper stack is designed to help answer:

- which entry paths are actually filling
- whether post-fill short-horizon outcomes are positive
- whether performance is concentrated in one ticker or one repeated pattern
- whether apparent paper profitability survives conservative accounting

Current telemetry includes:

- entry-only metrics separated from synthetic exits
- fill-path tagging such as `entry_price_improves` and `entry_moved_through`
- multi-horizon post-fill outcome tracking at 5s / 15s / 30s / 60s
- entry-summary breakdowns by universe / side / fill path / edge bucket
- concentration metrics by ticker and ticker-side
- realized vs unrealized PnL consistency

---

## Development setup

### 1) Python environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

### 2) Backend

```powershell
uvicorn kalshi_edge_engine.api.main:app --reload
```

### 3) Frontend

```powershell
cd web
npm install
npm run dev
```

Dashboard default: `http://localhost:5173`

Frontend API base can be set via:

```env
VITE_API_BASE_URL=http://localhost:8000
```

---

## Verification workflow

Run backend quality gates:

```powershell id="verify-01"
ruff format .
ruff check .
mypy src
pytest
```

### Basic scanner sanity checks

```powershell id="verify-02"
curl http://localhost:8000/health
curl http://localhost:8000/scanner/status
curl -X POST http://localhost:8000/scanner/start
curl http://localhost:8000/signals/top
```

### Paper sanity checks

```powershell id="verify-03"
curl http://localhost:8000/paper/config
curl -X POST http://localhost:8000/paper/start
curl http://localhost:8000/paper/summary
curl http://localhost:8000/paper/fills?limit=20
curl http://localhost:8000/paper/intents?status=OPEN&limit=20
curl http://localhost:8000/paper/intents?status=FILLED&limit=20
curl http://localhost:8000/paper/positions
curl http://localhost:8000/paper/pnl-snapshot
curl -X POST http://localhost:8000/paper/stop
```

### Outcome telemetry checks

```powershell id="verify-04"
curl "http://localhost:8000/paper/metrics/outcomes?horizon_seconds=5"
curl "http://localhost:8000/paper/metrics/outcomes?horizon_seconds=15"
curl "http://localhost:8000/paper/metrics/outcomes?horizon_seconds=30"
curl "http://localhost:8000/paper/metrics/outcomes?horizon_seconds=60"
curl http://localhost:8000/paper/metrics/outcomes/entry-summary
curl http://localhost:8000/paper/metrics/outcomes/concentration
curl http://localhost:8000/paper/metrics/outcomes/audit
```

---

## Interpreting paper results

A few important cautions:

- positive paper PnL is **not** proof of live EV
- low fill count means outcome metrics are fragile
- repeated fills in one ticker can make results look stronger than they are
- price-improving fills should be treated with extra skepticism
- moved-through fills may carry adverse-selection risk
- concentration metrics matter as much as raw win rate

Good signs are:

- fills across multiple distinct tickers
- stable positive short-horizon outcomes across more than one path
- low dependence on one ticker or one repeated setup
- consistent realized/unrealized accounting
- strategy still looking reasonable under stricter thresholds

---

## Notes and guardrails

- Signal decisions emit a single reason code.
- Paper PnL uses fee-aware accounting and reports realized / unrealized / total.
- Entry and exit lifecycle are kept analytically separable.
- Synthetic `exit_intent:*` rows are operational artifacts, not entry strategy signals.
- Scanner and paper trading are independently toggleable at runtime.
- No live order placement endpoints are invoked by this service.
- Simulator output should be treated as research telemetry, not live-trading proof.

---

## Current roadmap focus

Near-term priorities are:

1. tighten paper execution realism without making assumptions friendlier
2. improve interpretability of fill-path and post-fill outcome telemetry
3. continue reducing compatibility glue carefully without breaking tests
4. validate whether edge survives broader, less concentrated sampling
5. keep the system ready for future universes without hardcoding NBA policy into generic layers

---

## Not live trading yet

This repository is still intentionally **paper-only**.

That is not because the plumbing is worthless.  
It is because the hard problem is not just “place an order” — it is verifying that:

- signal quality is real
- fill assumptions are not flattering you
- accounting is correct
- edge is not just one-ticker luck
- observed paper behavior is strong enough to justify live-risk discussion

Only after that research burden is met should live-trading architecture even be considered.