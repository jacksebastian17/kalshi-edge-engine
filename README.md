# Kalshi Edge Engine

Real-time NBA market scanner and paper-trading validator for Kalshi.

This project is intentionally **paper-only** right now:

- Uses Kalshi market data endpoints (`GET`)
- Generates structured signal decisions
- Runs a continuous background scanner
- Simulates fills with a queue/volume-aware model
- Persists paper state and PnL in SQLite
- Exposes a local FastAPI backend + dark React dashboard

It does **not** place live orders.

---

## What this repository is

Kalshi Edge Engine is an exchange-behavior research daemon for NBA markets. The strategy layer is currently focused on structural market conditions (spread/imbalance/liquidity + fees), then validated through realistic paper execution and PnL tracking.

The system is built to answer:

- Are measurable edge opportunities showing up in live books?
- Do those opportunities survive realistic fill assumptions?
- Is PnL internally consistent when fees + marks + exits are modeled correctly?

---

## Current capabilities

### Backend (FastAPI)

- Health/version/debug endpoints
- Single-ticker and multi-ticker signal scoring
- Cached NBA universe discovery
- Background rotating scanner with lifecycle controls
- In-memory top-signals snapshot for UI
- Paper trading runtime controls + inspection endpoints
- Paper diagnostics endpoints for intent-level debug

### Scanner

- Universe refresh every ~300s
- Rotating slice scan (target 100 tickers/cycle)
- Concurrency-limited orderbook fetches
- Per-ticker cooldown after failures
- Target cadence near 2s per cycle
- Snapshot sorted by best computed edge

### Paper trading

- Quote intent generation from `SIGNAL_VALID` signals
- Realistic fill simulator (`RealisticOrderSimulator`) is the active path
- Queue-ahead + traded-volume-aware fill eligibility
- Fill telemetry (submit/fill best bid, cross, and post-fill metrics)
- SQLite persistence for intents/fills/positions/PnL snapshots
- Take-profit close logic and periodic mark-to-market snapshots
- Daily loss kill-switch gate for **new** intents

### Safety and gating (paper mode)

- Max open orders
- Max concurrent simulation intents
- Max open positions
- Max exposure per ticker (`max_pos_per_ticker`)
- Intents-per-minute rate limit
- Per-ticker cooldown
- Max daily loss gate (`PAPER_MAX_DAILY_LOSS_CENTS`)

---

## Architecture (high level)

```text
Kalshi API (GET)
  -> NBA Universe Discovery (cached)
  -> Background Scanner Loop
       -> Fetch orderbooks (bounded concurrency)
       -> score_signal()
       -> top-of-book snapshot + top signals cache
       -> enqueue paper work item (if paper enabled)

Paper Worker Loop
  -> build_quote_intents()
  -> persist intent
  -> RealisticOrderSimulator.simulate_order_fills()
  -> persist fills + update positions
  -> periodic PnL snapshots (realized + unrealized + total)

FastAPI
  -> scanner and paper endpoints

React dashboard
  -> polls scanner status/top signals
  -> supports manual watchlist scan mode
```

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
- `GET /paper/fills?limit=50`
- `GET /paper/positions`
- `GET /paper/pnl-snapshot`
- `GET /paper/summary`

### Paper debug

- `GET /paper/debug/intent/{intent_id}`
- `POST /paper/debug/force-fill?intent_id=...&fill_size=1`

---

## Project structure

```text
src/kalshi_edge_engine/
  api/main.py            FastAPI app, scanner loop, paper orchestration
  kalshi/client.py       Kalshi client + auth signing
  engine/signal.py       Signal math and reason codes
  engine/paper.py        Quote intent construction (legacy fill helpers remain)
  engine/paper_sim.py    RealisticOrderSimulator + diagnostics
  engine/universe.py     NBA universe discovery + cache
  db/paper_store.py      SQLite persistence + paper metrics

tests/                   Pytest suite (API, scanner, simulator, PnL invariants)
web/                     Vite + React dashboard
```

---

## Configuration

Create `.env` from `.env.example` and set at least Kalshi credentials.

### Required

- `KALSHI_API_KEY_ID`
- `KALSHI_API_KEY_FILE` (path to private key PEM)

### Optional core

- `KALSHI_BASE_URL` (defaults to Kalshi trade API v2 base)
- `ENABLE_NBA_SCANNER` (`true/false` auto-start scanner on app startup)
- `ENABLE_PAPER_TRADING` (`true/false` enables paper subsystem)

### Paper controls

- `PAPER_MIN_EDGE_CENTS`
- `PAPER_MAX_CONCURRENT_INTENTS`
- `PAPER_MAX_OPEN_ORDERS`
- `PAPER_MAX_POS_PER_TICKER`
- `PAPER_MAX_OPEN_POSITIONS`
- `PAPER_TAKE_PROFIT_CENTS`
- `PAPER_MAX_HOLD_SECONDS`
- `PAPER_FILL_HORIZON_SECONDS`
- `PAPER_POLL_INTERVAL_SECONDS`
- `PAPER_FILL_PROBABILITY`
- `PAPER_FILL_PROBABILITY_AT_BEST`
- `PAPER_FILL_PROBABILITY_PRICE_IMPROVES`
- `PAPER_SIMULATED_VOLUME_PER_POLL`
- `PAPER_MAX_INTENTS_PER_MINUTE`
- `PAPER_TICKER_COOLDOWN_SECONDS`
- `PAPER_MAX_DAILY_LOSS_CENTS` (`0` disables)

### Persistence

- Paper data persists to SQLite file `paper_trading.db` by default.

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

Open:

- API docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/health`

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

```powershell
ruff format .
ruff check .
mypy src
pytest
```

Manual API sanity checks:

```powershell
curl http://localhost:8000/health
curl http://localhost:8000/scanner/status
curl -X POST http://localhost:8000/scanner/start
curl http://localhost:8000/signals/top
curl http://localhost:8000/paper/config
```

If paper is configured:

```powershell
curl -X POST http://localhost:8000/paper/start
curl http://localhost:8000/paper/intents
curl http://localhost:8000/paper/pnl-snapshot
curl -X POST http://localhost:8000/paper/stop
```

---

## Notes and guardrails

- Signal decisions emit a single reason code.
- Paper PnL uses fee-aware accounting and reports realized/unrealized/total.
- Scanner + paper are independently toggleable at runtime.
- No live order placement endpoints are invoked by this service.

---

## Roadmap focus

Near-term engineering priorities remain:

1. Paper accounting correctness and telemetry depth
2. Safety gates hardening (intent/exposure/loss controls)
3. Scanner stability and observability improvements
4. Strategy validation via paper outcomes before any live execution path
