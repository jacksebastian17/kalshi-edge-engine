# Kalshi Edge Engine

A Kalshi-only live signal engine and optional auto-quoting daemon designed to exploit exchange behavior — not predict outcomes.

This project builds a quant-style trading system using **only Kalshi API data**.

No external sportsbooks.  
No prediction models.  
No magic probability oracle.

Instead, we exploit:

- Spread inefficiencies  
- Impatient order flow  
- Thin liquidity  
- Market structure behavior  

The goal is to build a measurable, auditable, disciplined trading system that can run continuously and prove whether edge exists.

> ⚠️ Auto mode is OFF by default and guarded by strict risk controls.

---

# Core Philosophy

There are two ways to try to win on Kalshi:

1. Predict outcomes better than the market.
2. Exploit how the market behaves.

This project focuses on **#2**.

We are not trying to be sports analysts.  
We are building an exchange behavior engine.

That means:

- We measure spreads.
- We measure liquidity.
- We measure volatility.
- We measure fill behavior.
- We measure realized PnL.
- We log everything.

If edge exists, we prove it with data.

---

# Non-Goals

This project is **not**:

- An infinite-money loophole
- Based on insider information
- A sportsbook comparison tool
- A prediction model using external feeds
- An attempt to bypass platform rules

This is a legitimate exchange-behavior research engine.

---

# Operating Modes

## Signals Mode
- No trading
- Live signal detection
- Full audit logging
- Research-only

## Paper Mode
- Simulated fills
- Simulated PnL
- Inventory tracking
- Strategy validation

## Auto Mode (Optional)
- Real orders
- Very small size
- Hard risk limits
- Kill switch enforced
- Explicit enable required

Auto mode is disabled by default.

---

# Strategy v0 — Spread Capture

The first strategy implemented is spread capture.

Example:

If:
- Best bid = 0.48
- Best ask = 0.51

Spread = 0.03

If spread exceeds:
- Entry fees
- Exit fees
- Safety buffer

The engine may place resting orders inside the spread.

Goal:
- Buy slightly cheaper
- Sell slightly higher
- Capture small edges repeatedly

No outcome prediction required.

---

# What We Are Exploiting

We exploit:

1. **Impatience**
   Traders who cross the spread for immediate execution.

2. **Thin liquidity**
   Small books create temporary inefficiencies.

3. **Behavioral movement**
   Retail-heavy markets can move unevenly.

We do not exploit illegal or unethical behavior.

---

# Architecture Overview


Kalshi API
↓
Orderbook Poller
↓
Signal Engine
↓
Database (snapshots + signals + orders + fills)
↓
FastAPI Backend
↓
React Dashboard


---

# Project Structure


/src
/kalshi # API client + auth + types
/engine # Polling, signal generation, persistence
/risk # Eligibility filters, limits, kill switch
/execution # Order placement + inventory (after paper mode)
/api # FastAPI application
/db # Database models + migrations
/utils # Shared utilities

/tests # Unit and integration tests
.github/workflows # CI configuration


---

# Data Logging (Critical)

Every decision must be auditable.

For every signal:

- Timestamp
- Market ticker
- Best bid
- Best ask
- Spread
- Fee estimate
- Net edge
- Liquidity at top
- Time to expiration
- Decision reason code

If auto mode is enabled:

- Order submission
- Order cancellation
- Fills
- Inventory change
- Realized PnL
- Risk events

Nothing is hidden.  
Everything is measurable.

---

# Configuration

## Environment Variables

Create a `.env` file in the project root (see `.env.example` for reference):

```
KALSHI_API_KEY_ID=your_key_id
KALSHI_API_KEY_FILE=kalshi-key.pem
# Optional fallback if you prefer inline secret instead of key file:
# KALSHI_API_SECRET=-----BEGIN RSA PRIVATE KEY-----...-----END RSA PRIVATE KEY-----
KALSHI_BASE_URL=https://api.elections.kalshi.com/trade-api/v2  # optional
```

Get your credentials from: https://dashboard.kalshi.com/settings/api-keys

Place `kalshi-key.pem` in the project root (it is gitignored).

Optional trading configuration:

```
TRADING_MODE=signals          # signals, paper, or live
AUTO_TRADING=false            # Always start with false
MIN_SPREAD_AFTER_FEES=0.01
MIN_TOP_LIQUIDITY=0
MIN_MINUTES_TO_EXPIRY=1
```

---

# Development Setup

## Clone repo

```bash
git clone https://github.com/jacksebastian17/kalshi-edge-engine.git
cd kalshi-edge-engine
```

## Create virtual environment

**Windows:**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**macOS/Linux:**
```bash
python -m venv .venv
source .venv/bin/activate
```

## Install dependencies

```bash
pip install -e ".[dev]"
```

## Set up environment

```bash
# macOS/Linux
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Edit `.env` with your Kalshi API credentials.

## Run API server

```bash
python -m uvicorn kalshi_edge_engine.api.main:app --reload
```

Server runs on `http://localhost:8000`

## API Documentation

Visit http://localhost:8000/docs for interactive Swagger documentation

---

# Using the API

## Health Check

```bash
curl http://localhost:8000/health
```

Response:
```json
{
  "status": "ok"
}
```

## Fetch Orderbook (Debug Endpoint)

**Endpoint:** `GET /debug/orderbook/{ticker}`

Fetches live orderbook data from Kalshi. Useful for debugging and development.

**Example:**

```bash
curl "http://localhost:8000/debug/orderbook/KXNBAGAME-26MAR01SASNYK-SAS"
```

**Response:**
```json
{
  "ticker": "KXNBAGAME-26MAR01SASNYK-SAS",
  "orderbook": {
    "market_ticker": "KXNBAGAME-26MAR01SASNYK-SAS",
    "yes": [
      [96, 250],
      [95, 50]
    ],
    "no": [
      [5, 100],
      [4, 75]
    ]
  },
  "latency_ms": 125.5,
  "success": true
}
```

**Requirements:**
- Valid `KALSHI_API_KEY_ID` and either `KALSHI_API_KEY_FILE` or `KALSHI_API_SECRET` in `.env`
- Ticker must be a valid Kalshi market ticker (not event ticker), e.g. `KXNBAGAME-26MAR01SASNYK-SAS`

**Error Responses:**

- `400`: Invalid ticker (empty or whitespace)
- `502`: API authentication error or upstream API failure

## Get Live Signal

**Endpoint:** `GET /signals/live?ticker=...`

Fetches orderbook data and scores it using inside-width market-making opportunity detection. Returns reason code and edge estimates.

**Key Concepts:**

- **Best Bid**: Highest price in YES/NO ladder (price people willing to buy at)
- **Cross Price**: `100 - opposite_side_best_bid` (price you'd immediately pay to sell)
- **Inside Width**: `cross - bid` (room available to quote between bid and cross)
- **Net Edge**: `inside_width - fee_estimate - safety_buffer` (profit after costs)

Kalshi markets DO NOT have explicit ask prices. We compute implied "cross" prices from opposite-side best bids.

**Example:**

```bash
curl "http://localhost:8000/signals/live?ticker=KXNBAGAME-26MAR01SASNYK-SAS"
```

**Response (SIGNAL_VALID):**
```json
{
  "ticker": "KXNBAGAME-26MAR01SASNYK-SAS",
  "reason_code": "SIGNAL_VALID",
  "net_edge_yes": 4.4,
  "net_edge_no": 4.4,
  "inside_width_yes": 5.0,
  "inside_width_no": 5.0,
  "bid_yes": 80.0,
  "cross_yes": 85.0,
  "bid_no": 15.0,
  "cross_no": 20.0,
  "top_liquidity": 250,
  "timestamp_ms": 1735689123000
}
```

**Response (SPREAD_TOO_SMALL):**
```json
{
  "ticker": "KXNBAGAME-26MAR01SASNYK-SAS",
  "reason_code": "SPREAD_TOO_SMALL",
  "net_edge_yes": -0.5,
  "net_edge_no": -0.5,
  "inside_width_yes": 0.0,
  "inside_width_no": 0.0,
  "bid_yes": 50.0,
  "cross_yes": 50.0,
  "bid_no": 50.0,
  "cross_no": 50.0,
  "top_liquidity": 100,
  "timestamp_ms": 1735689123000
}
```

**Reason Codes:**

- `SIGNAL_VALID`: At least one side has `net_edge >= min_edge` (default: 0.1¢) — room to make money
- `SPREAD_TOO_SMALL`: Inside width exists but `net_edge < min_edge` — not enough room after fees
- `LOW_LIQUIDITY`: Best bid size < `min_liquidity` (default: 10 contracts) — can't fill size
- `MARKET_INELIGIBLE`: Missing best bids or one side unavailable — cannot compute cross
- `DATA_STALE`: Orderbook data older than threshold (future use)

**Query Parameters (optional):**
- `fee_estimate`: Estimated round-trip fees in cents (default: 0.5)
- `min_edge`: Minimum net edge in cents to consider signal valid (default: 0.1)
- `min_liquidity`: Minimum contracts at best bid level (default: 10)
- `safety_buffer`: Safety margin below inside width (cents) (default: 0.1)

**Requirements:**
- Valid Kalshi API credentials in `.env`
- Ticker must be a valid market ticker, e.g. `KXNBAGAME-26MAR01SASNYK-SAS`

**Error Responses:**

- `400`: Invalid ticker (empty or whitespace)
- `502`: API authentication error or upstream API failure

---

# Running Tests

## Unit Tests

```bash
pytest -v
```

All tests use mocked Kalshi API calls — no real API keys required.

## Lint & Type Check

```bash
# Format and lint
python -m ruff check src tests --fix

# Type checking
python -m mypy src tests
```

---

# Metrics That Matter

- Fill rate
- Captured spread
- Inventory duration
- Realized PnL
- Slippage
- Risk triggers

We measure. We do not guess.

---

# Roadmap

## PR #1
Project foundation + minimal API + CI

## PR #2
Database models + snapshot persistence

## PR #3
Orderbook poller + daemon loop

## PR #4
Spread-capture signal engine

## PR #5
API routes for live signals

## PR #6
Paper simulator + PnL tracking

## PR #7
Optional live quoting with strict guardrails

---

# Disclaimer

Trading involves risk.

Auto mode should only be enabled with tiny size and strict safeguards.

This project is for research and educational purposes.