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
- Best bid (YES and NO)
- Spread (YES and NO inside widths)
- Cross prices (computed from opposite side)
- **Structural metrics:**
  - Bid sum (best YES bid + best NO bid)
  - Imbalance gap (100 - bid sum)
- Fee estimate
- Net edge (YES and NO)
- Liquidity at top of book
- Decision reason code
- All gating stage outcomes (structure, liquidity, spread, edge)

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

# Signal Scoring Mechanics

## Structural Metrics

The signal engine uses **structural metrics** to distinguish real opportunities from equilibrium noise (e.g., tight 1-tick symmetric markets).

### Bid Sum

**Definition:**
```
bid_sum = best_yes_bid + best_no_bid
```

**Interpretation:**
At perfect market equilibrium, if YES and NO contracts each represent 50¢ of value, their sum should equal ~100¢ (the total payout space). 

**Example:**
- If YES bid = 65¢ and NO bid = 30¢, then `bid_sum = 95¢`
- The market is slightly YES-biased (imbalanced)

### Imbalance Gap

**Definition:**
```
imbalance_gap = 100 - bid_sum
```

**Interpretation:**
Measures structural inefficiency. A larger imbalance gap suggests the market is pricing YES or NO unfairly relative to the other, often creating opportunities for resting orders to fill at better prices.

**Example:**
- If `bid_sum = 95¢`, then `imbalance_gap = 5¢`
- If `bid_sum = 98¢`, then `imbalance_gap = 2¢` 
- If `bid_sum = 100¢`, then `imbalance_gap = 0¢` (perfectly balanced—high equilibrium, low opportunity)

**Why it matters:**
A tight 1-tick symmetric market (YES bid=50, cross=51 on both sides) has:
- `inside_width_yes = 1.0`, `inside_width_no = 1.0`
- `bid_sum = 100`, `imbalance_gap = 0`
- Old logic would accept this as SIGNAL_VALID (mistake)
- New logic rejects because `imbalance_gap < 1.5¢` threshold (correct)

---

## Reason Code Decision Tree

The signal engine uses a **5-stage eligibility cascade** to emit reason codes:

### Stage A: Structural Validity
```
IF best_yes_bid is missing
   OR best_no_bid is missing
   OR bid_sum > 100
   → MARKET_INELIGIBLE
```

**Why:** Missing bids mean we cannot compute cross prices. `bid_sum > 100` indicates crossed/distorted market structure.

### Stage B: Liquidity Gate
```
IF top_liquidity < min_liquidity (default: 10 contracts)
   → LOW_LIQUIDITY
```

**Why:** Small book size may not allow for meaningful order fills.

### Stage C/D: Spread & Imbalance Gate
```
IF (both inside_width_yes <= 1.0 tick AND inside_width_no <= 1.0 tick)
   OR (imbalance_gap < 1.5¢)
   → SPREAD_TOO_SMALL
```

**Why:** 
- Symmetric 1-tick spreads are equilibrium noise (reject)
- Large structural imbalance (gap >= 1.5¢) indicates a real opportunity
- If either condition fails, no trading opportunity

### Stage E: Edge Gate
```
IF at_least_one_side has (net_edge >= min_edge, default 0.1¢)
   → SIGNAL_VALID
ELSE
   → SPREAD_TOO_SMALL
```

**Why:** After costs and safety buffer, only truly profitable edges are marked valid.

---

## Mathematical Walkthrough: Why Imbalance Gating Works

### Scenario 1: Artificially Tight Market (False Positive Without Gate)

```
Market State:
  YES bid: 50.0, cross: 51.0 (inside width = 1.0)
  NO bid:  50.0, cross: 51.0 (inside width = 1.0)
  bid_sum: 100.0, imbalance_gap: 0.0

Stage A: ✓ Both bids present, bid_sum = 100 (not crossed)
Stage B: ✓ Liquidity sufficient
Stage C: ✗ FAIL
  Condition 1: Both widths = 1.0 tick (meets tight criteria) ✓
  Condition 2: imbalance_gap (0.0) < 1.5¢ ✓ Both failed
  Result: SPREAD_TOO_SMALL ← CORRECT REJECTION

Interpretation:
This is pure equilibrium. There is no real inefficiency—just natural
market-making spread at a fair price. The 1-tick reflects normal
trading friction on a balanced position.
```

### Scenario 2: Real Opportunity (True Positive)

```
Market State:
  YES bid: 48.0, cross: 51.0 (inside width = 3.0)
  NO bid:  49.0, cross: 52.0 (inside width = 3.0)
  bid_sum: 97.0, imbalance_gap: 3.0¢
  top_liquidity: 200 contracts

Stage A: ✓ Both bids present, bid_sum < 100 (not crossed)
Stage B: ✓ Liquidity (200) >= min (10)
Stage C: ✓ PASS
  Condition 1: Both widths > 1.0 tick ✓
  Condition 2: imbalance_gap (3.0) >= 1.5 ✓ Both passed
Stage E: ✓ PASS
  net_edge_yes = 3.0 - 0.5 (fees) - 0.1 (safety) = 2.4¢ >= 0.1¢ ✓
  net_edge_no = 3.0 - 0.5 - 0.1 = 2.4¢ >= 0.1¢ ✓
  Result: SIGNAL_VALID ← CORRECT ACCEPTANCE

Interpretation:
The market is imbalanced (YES side favored) with real room between
bids and crosses. This is an exploitable inefficiency. Both sides
have measurable edge after costs.
```

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

## Run Dashboard Locally
Fetches live orderbook data from Kalshi. Useful for debugging and development.

**Example:**
```bash
cd web
npm install
npm run dev
```

Dashboard runs on `http://localhost:5173`
```bash
Backend automatically allows CORS requests from localhost:5173 in development.
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
  "bid_sum": 95.0,
  "imbalance_gap": 5.0,
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
  "bid_sum": 100.0,
  "imbalance_gap": 0.0,
  "top_liquidity": 100,
  "timestamp_ms": 1735689123000
}
```

**Reason Codes:**

Emitted by the 5-stage decision cascade:

- `SIGNAL_VALID`: Market passed all gates (Stage A–E)
  - Best bids present, `bid_sum <= 100`, liquidity sufficient
  - Both sides have `inside_width > 1.0` tick AND `imbalance_gap >= 1.5¢` 
  - At least one side has `net_edge >= min_edge` (default: 0.1¢)
  - **Interpretation:** Real exploitable inefficiency with edge after costs

- `SPREAD_TOO_SMALL`: Market failed the spread/imbalance gate (Stage C)
  - Either both sides have `inside_width <= 1.0` tick
  - OR `imbalance_gap < 1.5¢` (insufficient structural imbalance)
  - OR passed spread gate but both sides have `net_edge < min_edge`
  - **Interpretation:** No measurable opportunity after costs; likely equilibrium noise

- `LOW_LIQUIDITY`: Market failed the liquidity gate (Stage B)
  - `top_liquidity < min_liquidity` (default: 10 contracts)
  - **Interpretation:** Book too thin; order fill uncertain

- `MARKET_INELIGIBLE`: Market failed structural validity (Stage A)
  - Either best YES bid or best NO bid is missing
  - OR `bid_sum > 100` (crossed/distorted structure)
  - **Interpretation:** Cannot compute cross prices; market data invalid

- `DATA_STALE`: Orderbook data exceeds age threshold (future use)
  - **Interpretation:** Signal too old to act on; data freshness issue

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

## Scan Multiple Tickers

**Endpoint:** `GET /signals/scan?tickers=...`

Scans multiple markets in parallel and returns sorted signal opportunities. Useful for screening portfolios or finding best opportunities across a watchlist.

**Key Features:**
- Scans up to 20 tickers per request
- Automatic deduplication and normalization
- Per-ticker best-effort error handling (skips failed tickers, continues with others)
- Results sorted by strongest edge descending (best opportunity first)

**Example:**

```bash
curl "http://localhost:8000/signals/scan?tickers=TICKER-A,TICKER-B,TICKER-C"
```

Query string supports:
- Comma-separated tickers
- Whitespace around commas (stripped)
- Duplicate tickers (deduplicated)
- Mixed case (normalized to uppercase)

**Response:**
```json
[
  {
    "ticker": "TICKER-B",
    "reason_code": "SIGNAL_VALID",
    "net_edge_yes": 2.3,
    "net_edge_no": null,
    "inside_width_yes": 2.8,
    "inside_width_no": null,
    "bid_yes": 48.0,
    "cross_yes": 50.8,
    "bid_no": null,
    "cross_no": null,
    "bid_sum": null,
    "imbalance_gap": null,
    "top_liquidity": 150,
    "timestamp_ms": 1735689200000
  },
  {
    "ticker": "TICKER-A",
    "reason_code": "SIGNAL_VALID",
    "net_edge_yes": 1.1,
    "net_edge_no": 1.1,
    "inside_width_yes": 1.6,
    "inside_width_no": 1.6,
    "bid_yes": 52.0,
    "cross_yes": 53.6,
    "bid_no": 48.0,
    "cross_no": 48.0,
    "bid_sum": 100.0,
    "imbalance_gap": 0.0,
    "top_liquidity": 125,
    "timestamp_ms": 1735689200000
  },
  {
    "ticker": "TICKER-C",
    "reason_code": "SPREAD_TOO_SMALL",
    "net_edge_yes": -0.1,
    "net_edge_no": -0.1,
    "inside_width_yes": 0.4,
    "inside_width_no": 0.4,
    "bid_yes": 50.0,
    "cross_yes": 50.4,
    "bid_no": 50.0,
    "cross_no": 50.4,
    "bid_sum": 100.0,
    "imbalance_gap": 0.0,
    "top_liquidity": 100,
    "timestamp_ms": 1735689200000
  }
]
```

**Sorting Behavior:**
- Computes `max_edge = max(net_edge_yes, net_edge_no)` for each signal
- Ranks by `max_edge` descending (highest edge first)
- Treats `null` edges as negative infinity in comparison
- SIGNAL_VALID candidates naturally rank higher than SPREAD_TOO_SMALL

**Query Parameters (optional):**

All same parameters as `/signals/live`:
- `fee_estimate`: Estimated round-trip fees in cents (default: 0.5)
- `min_edge`: Minimum net edge in cents (default: 0.1)
- `min_liquidity`: Minimum contracts at best bid (default: 10)
- `safety_buffer`: Safety margin in cents (default: 0.1)
- `imbalance_threshold`: Minimum structural imbalance gap in cents (default: 1.5)

**Error Handling:**

- `400`: No valid tickers provided OR more than 20 tickers requested
- `400`: Ticker string is empty or contains only whitespace
- `502`: Authentication error (hard fail—entire request fails)
- `502`: All tickers' API calls failed (no results available)
- **Partial failures:** If 1–N of M tickers fail, those failures are logged and skipped; request still succeeds with results from successful tickers

**Example with partial failure:**
```bash
curl "http://localhost:8000/signals/scan?tickers=TICKER-A,INVALID-TICKER,TICKER-B"
```

If `INVALID-TICKER` fails API call, response includes only `[TICKER-A result, TICKER-B result]` (INVALID-TICKER skipped, logged as "scan_ticker_failed"). Request returns 200, not 502.

**Requirements:**
- Valid Kalshi API credentials in `.env`
- At least one valid market ticker
- Maximum 20 distinct tickers per request

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
Project foundation + minimal API + CI ✅

## PR #2
Structural imbalance metrics + tightened signal validity ✅
- Added `bid_sum` and `imbalance_gap` fields to Signal
- Implemented 1.5¢ imbalance threshold gate
- Updated reason code cascade to 5-stage eligibility pipeline
- Rejected artificial 1-tick equilibrium as SPREAD_TOO_SMALL

## PR #3
Multi-ticker scan endpoint + safeguards ✅
- `/signals/scan` with up to 20 tickers per request
- Automatic deduplication and normalization
- Per-ticker best-effort error handling
- Sorted by strongest edge descending
- Comprehensive test coverage

## PR #4
Database models + snapshot persistence

## PR #5
Orderbook poller + daemon loop

## PR #6
Paper simulator + PnL tracking

## PR #7
Optional live quoting with strict guardrails

---

# Disclaimer

Trading involves risk.

Auto mode should only be enabled with tiny size and strict safeguards.

This project is for research and educational purposes.