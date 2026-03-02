# Kalshi Edge Engine

A real-time Kalshi NBA signal scanner that continuously monitors markets to detect structural inefficiencies and spread opportunities — without placing any trades.

This is a **signals-only research system** using only Kalshi API data.

No external sportsbooks.  
No prediction models.  
No live trading (yet).

Instead, we:

- Monitor live NBA market orderbooks continuously
- Detect spread inefficiencies and structural imbalances
- Score signals using quantitative edge calculations
- Expose live data via FastAPI backend
- Feed a dark local dashboard that updates in real time

The goal is to build a stable, observable quant daemon that proves whether detectable edge exists through measurement and data.

---

# Current Status

**What's Built:**
- ✅ Kalshi API client (RSA-PSS signing, async operations)
- ✅ NBA market universe discovery with caching
- ✅ Background NBA scanner with rotating slices
- ✅ Signal scoring with structural imbalance detection
- ✅ FastAPI backend with 10 endpoints
- ✅ React dashboard with auto-scan + manual modes
- ✅ Scanner lifecycle controls (start/stop)
- ✅ Full CI pipeline (ruff + mypy + pytest)

**What's NOT Built (Yet):**
- ❌ Database persistence
- ❌ Paper trading simulator
- ❌ Live order placement
- ❌ Auto-trading mode
- ❌ PnL tracking
- ❌ Inventory management

This is a signals + observability system first. Trading comes later.

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
- We measure structural imbalances.
- We log everything.
- We run continuously in the background.
- We make the system observable.

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

# Signal Strategy — Spread Capture Detection

The signal scoring logic is designed to detect spread capture opportunities.

**Current Implementation:** Detection only (no trading).

**Strategy Concept:**

If a market has:
- Best YES bid = 48¢
- Best NO bid = 52¢ (implies YES cross = 48¢)
- Inside width = 0¢ (no room)

→ **SPREAD_TOO_SMALL**

But if:
- Best YES bid = 48¢  
- Best NO bid = 49¢ (implies YES cross = 51¢)
- Inside width = 3¢
- After fees + safety = 2.4¢ net edge

→ **SIGNAL_VALID**

**Target opportunities:**
- Structural imbalances (`bid_sum < 100`, `imbalance_gap >= 1.5¢`)
- Wide inside spreads (both sides > 1 tick)
- Measurable net edge after costs

**Future implementation (paper/live mode):**
- Place resting orders inside the spread
- Buy slightly cheaper, sell slightly higher
- Capture small edges repeatedly

**Current focus:**
- Prove detectable opportunities exist
- Measure frequency and edge magnitude
- Build observability infrastructure

No outcome prediction required.

---

# What We Are Detecting

We look for:

1. **Structural Imbalances**
   Market pricing where YES + NO bids don't sum to 100¢, indicating inefficiency.

2. **Wide Spreads**
   Enough room between bid and cross to place profitable resting orders.

3. **Thin Liquidity**
   Small books where our orders could get filled without moving the market significantly.

4. **Impatient Flow**
   (Future) Traders who cross the spread, potentially filling our resting orders.

We do not exploit illegal or unethical behavior.

---

# Architecture Overview

```
Kalshi API (GET only)
↓
NBA Universe Discovery (cached 60s)
↓
Background Scanner Loop
├─ Rotating slice (100 tickers/cycle)
├─ Concurrent fetch (semaphore limit)
├─ Per-ticker cooldown on failure
└─ Top-20 snapshot (updated each cycle)
↓
In-Memory Snapshot Store
↓
FastAPI Backend (10 endpoints)
├─ GET /signals/top
├─ GET /scanner/status
├─ POST /scanner/start
├─ POST /scanner/stop
└─ Manual scan endpoints
↓
React Dashboard (dark minimal UI)
├─ Auto-Scan NBA (default mode)
├─ Manual Watchlist mode
└─ Orderbook drawer with charts
```

**Key Design Choices:**
- No database yet (pure in-memory state)
- No trading calls (GET-only Kalshi API usage)
- Rotating slice scan for predictable cadence (~2s target)
- Adaptive sizing to maintain slice size despite cooldowns
- Structured logging throughout
- Strict reason code taxonomy

---

# NBA Scanner Architecture

The background NBA scanner is the heart of the system.

## How It Works

1. **Universe Discovery** (every 300s)
   - Fetches all open KXNBAGAME markets
   - Caches market ticker list
   - Tracks discovered market count

2. **Rotating Slice Scan** (every ~2s)
   - Selects next 100 tickers from universe
   - Skips tickers currently in cooldown
   - Fetches orderbooks concurrently (max 5 parallel)
   - Scores each market using signal math
   - Updates top-20 snapshot by max edge

3. **Failure Handling**
   - Failed tickers enter 60s cooldown
   - Cooldown prevents retry spam
   - Expired cooldowns auto-cleaned each cycle

4. **Observability**
   - `/scanner/status` exposes all metrics
   - Snapshot age tracked in milliseconds
   - Cycle duration measured
   - Failed/skipped tickers logged

## Scanner Lifecycle

**Auto-start (optional):**
```bash
ENABLE_NBA_SCANNER=true
```

**Manual control:**
```bash
POST /scanner/start   # Idempotent, returns already_running if active
POST /scanner/stop    # Cancels task, clears snapshot
```

**Status check:**
```bash
GET /scanner/status   # Returns enabled, metrics, snapshot age
```

---

# Project Structure

```
/src/kalshi_edge_engine
  /kalshi          # Kalshi API client (RSA-PSS signing, async operations)
  /engine          # Signal scoring, orderbook parsing, NBA universe discovery
  /api             # FastAPI application (10 endpoints, background scanner)
  /db              # Database models (not implemented yet)
  /execution       # Order placement (not implemented yet)
  /risk            # Risk limits (not implemented yet)
  /utils           # Shared utilities

/tests             # Unit and integration tests (pytest)
/web               # React dashboard (TypeScript + Vite)
/.github/workflows # CI configuration (ruff + mypy + pytest)
```

**Core modules currently implemented:**
- `kalshi/client.py`: KalshiClient with httpx.AsyncClient, RSA-PSS auth
- `engine/signal.py`: Signal scoring with 5-stage cascade
- `engine/orderbook.py`: Orderbook parsing (nested + flat handling)
- `engine/universe.py`: NBA market discovery with caching
- `api/main.py`: FastAPI app + background scanner loop

---

# Signal Scoring Mechanics

Every market is scored using a 5-stage decision cascade that emits exactly one reason code.

## Data Captured Per Signal

For every signal generated:

- Timestamp (milliseconds)
- Market ticker
- Best bid (YES and NO)
- Cross prices (computed from opposite-side best bids)
- Inside width (YES and NO)
- **Structural metrics:**
  - `bid_sum` = best_yes_bid + best_no_bid
  - `imbalance_gap` = 100 - bid_sum
- Fee estimate
- Net edge (YES and NO) after costs
- Liquidity at top of book (YES and NO)
- Decision reason code

**Current state:** All data captured in structured logs. No database persistence yet.

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

# Scanner Configuration
ENABLE_NBA_SCANNER=false  # Set to true for auto-start on app launch
```

Get your credentials from: https://dashboard.kalshi.com/settings/api-keys

Place `kalshi-key.pem` in the project root (it is gitignored).

**Note:** Trading configuration variables (`TRADING_MODE`, `AUTO_TRADING`, etc.) exist in `.env.example` for future use but are not implemented yet.

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

All endpoints are read-only operations except scanner controls.

## Endpoints Summary

**Health & Debug:**
- `GET /health` - Health check
- `GET /version` - Version info
- `GET /debug/orderbook/{ticker}` - Raw orderbook data

**Signal Endpoints:**
- `GET /signals/live?ticker=...` - Score single market
- `GET /signals/scan?tickers=...` - Scan multiple markets (max 20)
- `GET /signals/top?n=20` - Top signals from scanner snapshot

**Universe & Scanner:**
- `GET /universe/nba` - All open NBA markets (cached 60s)
- `GET /scanner/status` - Scanner health metrics
- `POST /scanner/start` - Start background scanner
- `POST /scanner/stop` - Stop background scanner

---

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

---

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

## Get Top Signals from Scanner

**Endpoint:** `GET /signals/top?n=20`

Retrieves the top signals from the background NBA scanner's latest snapshot.

**Key Features:**
- Returns signals sorted by max edge descending
- Snapshot updated every ~2s by scanner
- Includes snapshot timestamp and age
- Returns market count from universe

**Example:**

```bash
curl "http://localhost:8000/signals/top?n=10"
```

**Response:**
```json
{
  "timestamp_ms": 1735690000000,
  "signals": [
    {
      "ticker": "KXNBAGAME-01MAR26-BRK-NOP-BRK",
      "reason_code": "SIGNAL_VALID",
      "net_edge_yes": 3.2,
      "net_edge_no": null,
      "inside_width_yes": 4.0,
      "inside_width_no": null,
      "bid_yes": 62.0,
      "cross_yes": 66.0,
      "bid_no": 34.0,
      "cross_no": 38.0,
      "bid_sum": 96.0,
      "imbalance_gap": 4.0,
      "liq_yes": 150,
      "liq_no": 200,
      "top_liquidity": 200,
      "timestamp_ms": 1735690000000
    }
  ],
  "market_count": 187
}
```

**Query Parameters:**
- `n`: Maximum number of signals to return (default: 20)

**Error Responses:**
- `503`: Scanner snapshot not available yet (scanner starting up or stopped)

**Requirements:**
- NBA scanner must be running (via `ENABLE_NBA_SCANNER=true` or `POST /scanner/start`)

---

## Get NBA Universe

**Endpoint:** `GET /universe/nba`

Fetches all open NBA markets from Kalshi. Cached for 60 seconds.

**Example:**

```bash
curl "http://localhost:8000/universe/nba"
```

**Response:**
```json
{
  "series_ticker": "KXNBAGAME",
  "event_tickers": [],
  "market_tickers": [
    "KXNBAGAME-01MAR26-BRK-NOP-BRK",
    "KXNBAGAME-01MAR26-BRK-NOP-NOP",
    "KXNBAGAME-01MAR26-CHI-POR-CHI"
  ]
}
```

**Fields:**
- `series_ticker`: Always "KXNBAGAME"
- `event_tickers`: Empty (not used in current implementation)
- `market_tickers`: List of all open market tickers, sorted

**Requirements:**
- Valid Kalshi API credentials in `.env`

---

## Scanner Status

**Endpoint:** `GET /scanner/status`

Returns detailed status of the background NBA scanner.

**Example:**

```bash
curl "http://localhost:8000/scanner/status"
```

**Response:**
```json
{
  "enabled": true,
  "last_scan_ms": 1735690123456,
  "markets_discovered": 187,
  "markets_scanned_last_cycle": 100,
  "last_cycle_duration_ms": 1842,
  "tickers_failed_last_cycle": ["KXNBAGAME-01MAR26-INVALID"],
  "tickers_skipped_cooldown_last_cycle": ["KXNBAGAME-01MAR26-FAILED"],
  "snapshot_timestamp_ms": 1735690123456,
  "snapshot_age_ms": 543,
  "last_error": null
}
```

**Fields:**
- `enabled`: Boolean indicating if scanner task is running
- `last_scan_ms`: Timestamp of last completed scan cycle
- `markets_discovered`: Total markets in NBA universe
- `markets_scanned_last_cycle`: Number of tickers scanned in last cycle
- `last_cycle_duration_ms`: How long the last cycle took
- `tickers_failed_last_cycle`: Tickers that failed API calls
- `tickers_skipped_cooldown_last_cycle`: Tickers skipped due to cooldown
- `snapshot_timestamp_ms`: When current snapshot was created
- `snapshot_age_ms`: How old the snapshot is (current time - snapshot time)
- `last_error`: Last error message (if any)

**Use Cases:**
- Monitor scanner health
- Check if snapshot is stale
- Debug market discovery issues
- Track failure rates

---

## Start Scanner

**Endpoint:** `POST /scanner/start`

Starts the background NBA scanner if not already running.

**Example:**

```bash
curl -X POST "http://localhost:8000/scanner/start"
```

**Response (started):**
```json
{
  "started": true
}
```

**Response (already running):**
```json
{
  "started": false,
  "reason": "already_running"
}
```

**Behavior:**
- Idempotent: Safe to call multiple times
- Creates background asyncio task
- Scanner begins universe discovery and scanning
- Sets `enabled` flag to true

---

## Stop Scanner

**Endpoint:** `POST /scanner/stop`

Stops the background NBA scanner cleanly.

**Example:**

```bash
curl -X POST "http://localhost:8000/scanner/stop"
```

**Response (stopped):**
```json
{
  "stopped": true
}
```

**Response (not running):**
```json
{
  "stopped": false,
  "reason": "not_running"
}
```

**Behavior:**
- Cancels scanner task
- Clears top signals snapshot
- Sets `enabled` flag to false
- Safe to call even if scanner not running

---

# Run Dashboard Locally

The React dashboard provides a dark minimal UI for monitoring signals in real time.

## Setup

```bash
cd web
npm install
npm run dev
```

Dashboard runs on `http://localhost:5173`

Backend automatically allows CORS requests from localhost:5173 in development.

## Features

**Auto-Scan NBA Mode (Default):**
- Displays top signals from background scanner
- Start/Stop controls for scanner lifecycle
- Real-time status metrics (snapshot age, cycle duration, failures)
- Updates every 2 seconds from snapshot
- Signal table with sorting by edge/liquidity

**Manual Watchlist Mode:**
- Enter comma-separated market tickers
- Scan on-demand with configurable poll interval
- Pause/resume polling
- Same signal table and sorting

**Shared Features:**
- Click any ticker to open orderbook drawer
- Live orderbook display (YES/NO ladders)
- Edge time-series chart for selected ticker
- Structured metrics (bid sum, imbalance gap)
- Reason code badges
- Responsive dark design

## Screenshots

*(Consider adding screenshots here)*

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

# Roadmap

## Phase 1: Signals + Scanner (Completed) ✅

**PR #1: Project foundation + minimal API + CI**
- Kalshi client with RSA-PSS signing
- FastAPI backend structure
- Basic signal endpoints
- GitHub Actions CI (ruff + mypy + pytest)

**PR #2: Structural imbalance metrics**
- Added `bid_sum` and `imbalance_gap` fields
- Implemented 1.5¢ imbalance threshold gate
- 5-stage eligibility cascade with reason codes
- Rejects artificial 1-tick equilibrium

**PR #3: Multi-ticker scan endpoint**
- `/signals/scan` with up to 20 tickers per request
- Automatic deduplication and normalization
- Per-ticker best-effort error handling
- Sorted by strongest edge descending

**PR #4: NBA Scanner + Dashboard**
- Background rotating slice scanner
- Universe discovery with caching
- Cooldown on failures
- Top-20 snapshot endpoint
- Scanner lifecycle controls (start/stop/status)
- React dashboard with Auto-Scan and Manual modes
- Orderbook drawer with charts

## Phase 2: Persistence + Paper Trading (Next)

**Database persistence:**
- SQLite for audit logging
- Signal history storage
- Scanner metrics tracking
- Query interface for analysis

**Paper trading simulator:**
- Simulated order placement
- Fill simulation based on orderbook
- Inventory tracking
- Simulated PnL calculation
- Risk limits (position size, daily loss)

## Phase 3: Live Trading (Future)

**Optional live quoting (OFF by default):**
- Real order placement via Kalshi API
- Very small size
- Hard risk limits
- Kill switch
- Explicit enable required
- Full audit trail

**Note:** Live trading is a research feature, not a production money-making system. Use extreme caution.

---

# What to Build Next

Based on mode instructions, highest ROI next steps:

1. **Scanner stability improvements**
   - Rate-limit detection (handle 429 responses)
   - Adaptive backoff behavior
   - More granular metrics

2. **SQLite persistence**
   - Signal history for analysis
   - Scanner performance metrics
   - Ability to replay/analyze past signals

3. **Paper trading mode**
   - Prove strategy works with simulated fills
   - Build inventory tracking
   - Measure simulated PnL

4. **Multi-series expansion**
   - Beyond NBA to other YES/NO markets
   - Generic series scanner
   - Series selection UI

---

# Metrics That Matter (When Trading Enabled)

Future metrics to track once trading is implemented:

- Fill rate
- Captured spread
- Inventory duration
- Realized PnL
- Slippage
- Risk triggers

We will measure everything. No guessing.

---

# Disclaimer

This is a research and educational project.

**Current state:** Signals only. No live trading implemented.

**Future state:** If live trading is added, it will involve risk. Use extremely small size and strict safeguards.

This project is for research purposes to study exchange behavior and market microstructure.